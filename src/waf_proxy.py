"""
AnyRouter Proxy - 多账号负载均衡代理

企业级架构特性:
- 常驻浏览器: 单例 Playwright 浏览器，避免重复启动
- 智能缓存: WAF Cookie 30分钟缓存 + 预刷新机制
- 并发安全: 防止多个请求同时刷新 Cookie
- 自动恢复: 浏览器崩溃自动重连
- 站点故障转移: 主站优先 + 自动切换备用站
"""

import asyncio
import json
import random
import time
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

# 导入新的浏览器和 Cookie 管理器
from browser_manager import browser_manager
from waf_cookie_manager import (
    waf_cookie_manager,
    get_waf_cookies,
    refresh_waf_cookies,
    WAF_COOKIE_TTL,
)

# 导入余额查询路由
from balance_api import router as balance_router
# 导入签到路由
from checkin_api import router as checkin_router
from checkin_service import run_checkin_for_all_accounts, checkin_status
# 导入账号管理路由
from accounts_api import router as accounts_router
# 导入认证路由和中间件
from auth_api import router as auth_router, auth_middleware, is_dashboard_auth_enabled
# 导入 API Key 验证
from api_key_validation import (
    is_validation_enabled,
    extract_api_key,
    validate_api_key,
    get_validation_stats,
    clear_validation_cache
)

# 配置 (支持环境变量)
import os
ANYROUTER_BASE_URL = os.getenv("ANYROUTER_BASE_URL", "https://anyrouter.top")
# 支持 Windows 本地测试和 Docker 容器运行
_default_accounts_file = "/app/data/accounts.json"
if os.name == 'nt' and not os.path.exists(_default_accounts_file):
    _default_accounts_file = str(Path(__file__).parent.parent / "data" / "keeper" / "accounts.json")
ACCOUNTS_FILE = Path(os.getenv("ACCOUNTS_FILE", _default_accounts_file))
PROXY_PORT = int(os.getenv("WAF_PROXY_PORT", "18081"))
HTTP_PROXY = os.getenv("HTTP_PROXY", "http://127.0.0.1:7890")

# 站点配置：主站需要代理+WAF，备用站直接访问
SITES = [
    {
        "url": "https://anyrouter.top",
        "name": "主站",
        "use_proxy": True,
        "need_waf": True
    },
    {
        "url": "https://c.cspok.cn",
        "name": "备用站1",
        "use_proxy": False,
        "need_waf": False
    },
    {
        "url": "https://pmpjfbhq.cn-nb1.rainapp.top",
        "name": "备用站2",
        "use_proxy": False,
        "need_waf": False
    },
    {
        "url": "https://a-ocnfniawgw.cn-shanghai.fcapp.run",
        "name": "备用站3",
        "use_proxy": False,
        "need_waf": False
    }
]

# 当前活跃站点索引
current_site_index = 0
site_fail_count = 0
MAX_SITE_FAILS = 3  # 连续失败次数达到此值后切换站点


def get_current_site():
    """获取当前活跃站点配置"""
    return SITES[current_site_index]


def switch_to_next_site():
    """切换到下一个站点"""
    global current_site_index, site_fail_count
    old_site = SITES[current_site_index]
    current_site_index = (current_site_index + 1) % len(SITES)
    site_fail_count = 0
    new_site = SITES[current_site_index]
    logger.warning(f"Switching from {old_site['name']} ({old_site['url']}) to {new_site['name']} ({new_site['url']})")
    return new_site


def record_site_failure():
    """记录站点失败，达到阈值后切换站点"""
    global site_fail_count
    site_fail_count += 1
    if site_fail_count >= MAX_SITE_FAILS:
        return switch_to_next_site()
    return None


def record_site_success():
    """记录站点成功，重置失败计数"""
    global site_fail_count
    site_fail_count = 0

# 签到定时任务配置
CHECKIN_INTERVAL_HOURS = int(os.getenv("CHECKIN_INTERVAL_HOURS", "6"))  # 每 6 小时签到一次
CHECKIN_ENABLED = os.getenv("CHECKIN_ENABLED", "true").lower() == "true"
# Cron 表达式配置：8:30 开始，每 6 小时执行一次 (2:30, 8:30, 14:30, 20:30)
CHECKIN_CRON_HOUR = os.getenv("CHECKIN_CRON_HOUR", "2,8,14,20")  # 执行的小时
CHECKIN_CRON_MINUTE = os.getenv("CHECKIN_CRON_MINUTE", "30")  # 执行的分钟

# 主站优先恢复配置
PRIMARY_SITE_CHECK_ENABLED = os.getenv("PRIMARY_SITE_CHECK_ENABLED", "true").lower() == "true"
PRIMARY_SITE_CHECK_INTERVAL = int(os.getenv("PRIMARY_SITE_CHECK_INTERVAL", "5"))  # 检查间隔（分钟）

# 主站健康检查状态
primary_site_status = {
    "last_check": None,
    "last_check_result": None,
    "last_recovery": None,
    "check_count": 0,
    "recovery_count": 0
}

# 账号列表
accounts = []

# 后台刷新任务
_background_refresh_task = None

# 账号健康状态追踪
account_health = {}  # {account_name: {"fail_count": int, "last_fail": timestamp, "disabled_until": timestamp}}
ACCOUNT_MAX_FAILS = 3  # 连续失败次数达到此值后临时禁用账号
ACCOUNT_DISABLE_DURATION = 300  # 账号禁用时长（秒）


def load_accounts():
    """加载账号配置"""
    global accounts
    if ACCOUNTS_FILE.exists():
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # 只加载有 api_key 的账号（确保 api_key 不为空字符串）
            valid_accounts = []
            for acc in data:
                api_key = acc.get("api_key", "")
                if api_key and isinstance(api_key, str) and len(api_key.strip()) > 0 and acc.get("enabled", True):
                    valid_accounts.append(acc)
                    name = acc.get("name", acc.get("email", "unknown"))
                    # 只显示 key 的前8个字符用于调试
                    key_preview = api_key[:8] + "..." if len(api_key) > 8 else api_key
                    logger.debug(f"Loaded account {name} with key {key_preview}")
            accounts = valid_accounts
            logger.info(f"Loaded {len(accounts)} accounts with valid API keys")
    else:
        logger.error(f"Accounts file not found: {ACCOUNTS_FILE}")


def is_account_healthy(account_name: str) -> bool:
    """检查账号是否健康（未被临时禁用）"""
    if account_name not in account_health:
        return True
    health = account_health[account_name]
    disabled_until = health.get("disabled_until", 0)
    if disabled_until > 0 and time.time() < disabled_until:
        return False  # 账号仍在禁用期
    return True


def record_account_failure(account_name: str):
    """记录账号失败"""
    if account_name not in account_health:
        account_health[account_name] = {"fail_count": 0, "last_fail": 0, "disabled_until": 0}

    health = account_health[account_name]
    health["fail_count"] += 1
    health["last_fail"] = time.time()

    if health["fail_count"] >= ACCOUNT_MAX_FAILS:
        health["disabled_until"] = time.time() + ACCOUNT_DISABLE_DURATION
        logger.warning(f"Account {account_name} disabled for {ACCOUNT_DISABLE_DURATION}s after {health['fail_count']} failures")


def record_account_success(account_name: str):
    """记录账号成功，重置失败计数"""
    if account_name in account_health:
        account_health[account_name] = {"fail_count": 0, "last_fail": 0, "disabled_until": 0}


def get_healthy_accounts():
    """获取所有健康的账号列表"""
    return [acc for acc in accounts if is_account_healthy(acc.get("name", acc.get("email", "unknown")))]


def get_next_account(exclude_names: list = None):
    """获取下一个账号 (随机负载均衡，排除不健康账号和指定账号)"""
    if not accounts:
        return None

    exclude_names = exclude_names or []
    healthy = [
        acc for acc in accounts
        if is_account_healthy(acc.get("name", acc.get("email", "unknown")))
        and acc.get("name", acc.get("email", "unknown")) not in exclude_names
    ]

    if not healthy:
        # 如果没有健康账号，返回任意一个（降级策略）
        available = [acc for acc in accounts if acc.get("name", acc.get("email", "unknown")) not in exclude_names]
        if available:
            logger.warning("No healthy accounts available, using degraded selection")
            return random.choice(available)
        return None

    return random.choice(healthy)


# 定时签到任务
scheduler = AsyncIOScheduler()


async def scheduled_checkin():
    """定时签到任务"""
    from datetime import datetime
    logger.info(f"Scheduled check-in started at {datetime.now().isoformat()}")
    try:
        result = await run_checkin_for_all_accounts()
        logger.info(f"Scheduled check-in completed: {result.get('message', 'Unknown')}")
        # 更新下次运行时间
        next_run = scheduler.get_jobs()[0].next_run_time if scheduler.get_jobs() else None
        checkin_status["next_run"] = next_run.isoformat() if next_run else None
    except Exception as e:
        logger.error(f"Scheduled check-in failed: {e}")


async def check_primary_site_health():
    """
    检查主站是否可用 - 轻量级检查优化版

    优化点：
    1. 使用 HEAD 请求代替 GET，减少数据传输
    2. 更短的超时时间
    3. 复用现有 WAF cookies，不触发新的浏览器操作
    """
    from datetime import datetime

    primary_site = SITES[0]  # 主站始终是第一个
    primary_site_status["last_check"] = datetime.now().isoformat()
    primary_site_status["check_count"] += 1

    try:
        # 复用现有 WAF cookies（不强制刷新，避免浏览器操作）
        cookies = waf_cookie_manager.cookies if waf_cookie_manager.is_valid else {}

        # 使用 HEAD 请求进行轻量级检查
        async with httpx.AsyncClient(
            http2=False,
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            proxy=HTTP_PROXY if primary_site.get("use_proxy") else None,
            cookies=cookies
        ) as client:
            # HEAD 请求比 GET 更轻量
            response = await client.head(
                f"{primary_site['url']}/v1/models",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                },
                follow_redirects=True
            )

            # 检查响应
            content_type = response.headers.get("content-type", "")

            # 如果返回 HTML（Content-Type 包含 text/html），说明被 WAF 拦截
            if "text/html" in content_type:
                logger.debug(f"[Primary Check] WAF challenge detected")
                primary_site_status["last_check_result"] = "waf_challenge"
                return False

            # 检查状态码：2xx/3xx/4xx 都说明服务可达（4xx 是业务错误，不是不可用）
            if response.status_code < 500:
                primary_site_status["last_check_result"] = "healthy"
                return True
            else:
                primary_site_status["last_check_result"] = f"error_{response.status_code}"
                return False

    except Exception as e:
        logger.debug(f"[Primary Check] Health check failed: {e}")
        primary_site_status["last_check_result"] = f"error: {str(e)[:50]}"
        return False


async def scheduled_primary_site_check():
    """定时主站健康检查任务"""
    global current_site_index, site_fail_count
    from datetime import datetime

    # 如果当前已经在主站，不需要检查
    if current_site_index == 0:
        logger.debug("[Primary Check] Already using primary site, skip check")
        return

    logger.info(f"[Primary Check] Checking primary site health (current: {SITES[current_site_index]['name']})")

    is_healthy = await check_primary_site_health()

    if is_healthy:
        old_site = SITES[current_site_index]
        current_site_index = 0
        site_fail_count = 0
        primary_site_status["last_recovery"] = datetime.now().isoformat()
        primary_site_status["recovery_count"] += 1
        logger.info(f"[Primary Check] Primary site is healthy! Switching from {old_site['name']} back to {SITES[0]['name']}")
    else:
        logger.info(f"[Primary Check] Primary site still unavailable, staying on {SITES[current_site_index]['name']}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global _background_refresh_task

    # ============== 启动阶段 ==============
    logger.info("=" * 60)
    logger.info("AnyRouter Proxy starting...")
    logger.info("=" * 60)

    # 1. 启动常驻浏览器
    logger.info("[Startup] Starting persistent browser...")
    browser_started = await browser_manager.start()
    if not browser_started:
        logger.error("[Startup] Failed to start browser! WAF bypass may not work.")
    else:
        logger.info("[Startup] Browser started successfully")

    # 2. 加载账号
    load_accounts()
    logger.info(f"[Startup] Loaded {len(accounts)} accounts")

    # 3. 预热 WAF Cookie（首次获取）
    logger.info("[Startup] Pre-warming WAF cookies...")
    try:
        cookies = await get_waf_cookies()
        logger.info(f"[Startup] WAF cookies ready: {list(cookies.keys())}")
    except Exception as e:
        logger.error(f"[Startup] Failed to get initial WAF cookies: {e}")

    # 4. 启动后台 Cookie 刷新任务（预刷新机制）
    logger.info("[Startup] Starting background cookie refresh task...")
    _background_refresh_task = asyncio.create_task(
        waf_cookie_manager.start_background_refresh()
    )

    # 5. 配置定时任务
    # 签到任务
    if CHECKIN_ENABLED:
        scheduler.add_job(
            scheduled_checkin,
            trigger=CronTrigger(hour=CHECKIN_CRON_HOUR, minute=CHECKIN_CRON_MINUTE),
            id="scheduled_checkin",
            name="AnyRouter Check-in",
            replace_existing=True
        )
        logger.info(f"[Startup] Check-in scheduled: hour={CHECKIN_CRON_HOUR}, minute={CHECKIN_CRON_MINUTE}")

    # 主站健康检查任务
    if PRIMARY_SITE_CHECK_ENABLED:
        scheduler.add_job(
            scheduled_primary_site_check,
            trigger=IntervalTrigger(minutes=PRIMARY_SITE_CHECK_INTERVAL),
            id="primary_site_check",
            name="Primary Site Health Check",
            replace_existing=True
        )
        logger.info(f"[Startup] Primary site check enabled: interval={PRIMARY_SITE_CHECK_INTERVAL}min")

    # 启动调度器
    if CHECKIN_ENABLED or PRIMARY_SITE_CHECK_ENABLED:
        scheduler.start()
        if CHECKIN_ENABLED:
            checkin_job = scheduler.get_job("scheduled_checkin")
            if checkin_job:
                checkin_status["next_run"] = checkin_job.next_run_time.isoformat() if checkin_job.next_run_time else None

    # 打印配置摘要
    logger.info("-" * 60)
    logger.info(f"[Config] Base URL: {ANYROUTER_BASE_URL}")
    logger.info(f"[Config] HTTP Proxy: {HTTP_PROXY}")
    logger.info(f"[Config] WAF Cookie TTL: {WAF_COOKIE_TTL}s")
    logger.info(f"[Config] Primary site preferred: Yes")
    logger.info("-" * 60)
    logger.info("AnyRouter Proxy started successfully!")
    logger.info("=" * 60)

    yield  # ============== 应用运行中 ==============

    # ============== 关闭阶段 ==============
    logger.info("=" * 60)
    logger.info("AnyRouter Proxy shutting down...")

    # 1. 停止后台刷新任务
    if _background_refresh_task:
        _background_refresh_task.cancel()
        try:
            await _background_refresh_task
        except asyncio.CancelledError:
            pass
        logger.info("[Shutdown] Background refresh task stopped")

    # 2. 停止调度器
    if scheduler.running:
        scheduler.shutdown()
        logger.info("[Shutdown] Scheduler stopped")

    # 3. 关闭浏览器
    await browser_manager.stop()
    logger.info("[Shutdown] Browser stopped")

    logger.info("AnyRouter Proxy stopped")
    logger.info("=" * 60)


# FastAPI 应用
app = FastAPI(title="AnyRouter Proxy", lifespan=lifespan)

# 添加认证中间件
app.middleware("http")(auth_middleware)

# 注册认证路由（必须在其他路由之前）
app.include_router(auth_router)
# 注册余额查询路由（必须在 catch-all 路由之前注册）
app.include_router(balance_router)
# 注册签到路由
app.include_router(checkin_router)
# 注册账号管理路由
app.include_router(accounts_router)

# 静态文件目录配置
STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def root():
    """返回管理界面首页"""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return JSONResponse({
        "message": "AnyRouter Keeper API",
        "docs": "/docs",
        "health": "/health"
    })


@app.get("/health")
async def health():
    """健康检查 - 返回详细的系统状态"""
    from datetime import datetime
    current_site = get_current_site()

    # 计算账号健康统计
    healthy_accounts = get_healthy_accounts()
    unhealthy_accounts = [
        {
            "name": name,
            "fail_count": info.get("fail_count", 0),
            "disabled_until": datetime.fromtimestamp(info.get("disabled_until", 0)).isoformat() if info.get("disabled_until", 0) > 0 else None
        }
        for name, info in account_health.items()
        if info.get("disabled_until", 0) > time.time()
    ]

    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "accounts": len(accounts),

        # 账号健康状态（新增）
        "account_health": {
            "total": len(accounts),
            "healthy": len(healthy_accounts),
            "unhealthy": len(unhealthy_accounts),
            "unhealthy_accounts": unhealthy_accounts,
            "max_fails_before_disable": ACCOUNT_MAX_FAILS,
            "disable_duration_seconds": ACCOUNT_DISABLE_DURATION
        },

        # 站点状态
        "sites": {
            "current": current_site["name"],
            "current_url": current_site["url"],
            "use_proxy": current_site["use_proxy"],
            "need_waf": current_site["need_waf"],
            "fail_count": site_fail_count,
            "total_sites": len(SITES),
            "is_primary": current_site_index == 0,
            "all_sites": [{"name": s["name"], "url": s["url"]} for s in SITES]
        },

        # 主站健康检查状态
        "primary_site_check": {
            "enabled": PRIMARY_SITE_CHECK_ENABLED,
            "interval_minutes": PRIMARY_SITE_CHECK_INTERVAL,
            "last_check": primary_site_status.get("last_check"),
            "last_check_result": primary_site_status.get("last_check_result"),
            "last_recovery": primary_site_status.get("last_recovery"),
            "check_count": primary_site_status.get("check_count", 0),
            "recovery_count": primary_site_status.get("recovery_count", 0)
        },

        # 浏览器状态（新增）
        "browser": browser_manager.stats,

        # WAF Cookie 状态（新增，更详细）
        "waf_cookies": waf_cookie_manager.stats,

        # 代理配置
        "proxy": HTTP_PROXY,

        # Dashboard 认证
        "dashboard_auth": {
            "enabled": is_dashboard_auth_enabled(),
            "description": "Dashboard 登录认证" if is_dashboard_auth_enabled() else "Dashboard 无需登录"
        },

        # API Key 验证
        "api_key_validation": get_validation_stats(),

        # 签到状态
        "checkin": {
            "enabled": CHECKIN_ENABLED,
            "cron_hour": CHECKIN_CRON_HOUR,
            "cron_minute": CHECKIN_CRON_MINUTE,
            "schedule": f"每天 {CHECKIN_CRON_HOUR} 点 {CHECKIN_CRON_MINUTE} 分",
            "last_run": checkin_status.get("last_run"),
            "next_run": checkin_status.get("next_run"),
            "scheduler_running": scheduler.running if CHECKIN_ENABLED else False
        }
    }


@app.post("/reload")
async def reload_accounts():
    """重新加载账号配置"""
    load_accounts()
    return {"status": "ok", "accounts": len(accounts)}


@app.post("/clear-api-key-cache")
async def clear_api_key_cache():
    """清除 API Key 验证缓存"""
    clear_validation_cache()
    return {"status": "ok", "message": "API key validation cache cleared"}


@app.post("/refresh-waf")
async def refresh_waf():
    """强制刷新 WAF cookies"""
    try:
        cookies = await waf_cookie_manager.force_refresh()
        return {
            "status": "ok",
            "cookies": list(cookies.keys()),
            "ttl_seconds": waf_cookie_manager.ttl,
            "state": waf_cookie_manager.state.value
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "state": waf_cookie_manager.state.value
        }


@app.post("/restart-browser")
async def restart_browser():
    """重启常驻浏览器（用于故障恢复或内存清理）"""
    try:
        await browser_manager.restart()
        return {
            "status": "ok",
            "message": "Browser restarted successfully",
            "browser": browser_manager.stats
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "browser": browser_manager.stats
        }


@app.post("/switch-to-primary")
async def switch_to_primary():
    """手动切换回主站"""
    global current_site_index, site_fail_count
    from datetime import datetime

    if current_site_index == 0:
        return {
            "status": "ok",
            "message": "Already using primary site",
            "current_site": SITES[0]["name"]
        }

    # 先检查主站是否可用
    is_healthy = await check_primary_site_health()

    if is_healthy:
        old_site = SITES[current_site_index]
        current_site_index = 0
        site_fail_count = 0
        primary_site_status["last_recovery"] = datetime.now().isoformat()
        primary_site_status["recovery_count"] += 1
        return {
            "status": "ok",
            "message": f"Switched from {old_site['name']} to primary site",
            "current_site": SITES[0]["name"]
        }
    else:
        return {
            "status": "error",
            "message": f"Primary site health check failed: {primary_site_status.get('last_check_result')}",
            "current_site": SITES[current_site_index]["name"]
        }


@app.post("/force-switch-to-primary")
async def force_switch_to_primary():
    """强制切换回主站（不检查健康状态）"""
    global current_site_index, site_fail_count
    from datetime import datetime

    old_site = SITES[current_site_index]
    current_site_index = 0
    site_fail_count = 0

    if old_site["name"] != SITES[0]["name"]:
        primary_site_status["last_recovery"] = datetime.now().isoformat()
        primary_site_status["recovery_count"] += 1

    return {
        "status": "ok",
        "message": f"Force switched to primary site (from {old_site['name']})",
        "current_site": SITES[0]["name"],
        "warning": "Primary site health was not verified"
    }


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def proxy(request: Request, path: str):
    """代理 /v1/* 请求到 AnyRouter，支持多站点和多账号故障转移"""
    global current_site_index

    # API Key 验证（如果启用）
    if is_validation_enabled():
        api_key = extract_api_key(request)
        if not api_key:
            raise HTTPException(
                status_code=401,
                detail="API key is required. Please provide x-api-key header or Authorization: Bearer <key>"
            )

        is_valid, error_msg = await validate_api_key(api_key)
        if not is_valid:
            raise HTTPException(
                status_code=401,
                detail=error_msg or "Invalid API key"
            )

    # 获取请求体（只读取一次）
    body = await request.body()

    # 检查是否是流式请求
    try:
        body_json = json.loads(body) if body else {}
        is_stream = body_json.get("stream", False)
    except:
        is_stream = False
        body_json = {}

    # 详细记录请求信息（用于调试）
    request_keys = list(body_json.keys()) if body_json else []
    has_thinking = "thinking" in body_json
    logger.info(f"Request body keys: {request_keys}, has_thinking: {has_thinking}, model: {body_json.get('model', 'unknown')}")

    # 账号故障转移：最多尝试 3 个不同的账号
    MAX_ACCOUNT_RETRIES = 3
    tried_account_names = []
    last_error = None
    account_error = False  # 标记是否是账号相关的错误

    for account_attempt in range(MAX_ACCOUNT_RETRIES):
        # 获取账号（排除已尝试过的）
        account = get_next_account(exclude_names=tried_account_names)
        if not account:
            if account_attempt == 0:
                raise HTTPException(status_code=503, detail="No available accounts")
            break  # 没有更多账号可尝试

        account_name = account.get("name", account.get("email", "unknown"))
        tried_account_names.append(account_name)

        if account_attempt > 0:
            logger.info(f"Account failover: trying account {account_name} (attempt {account_attempt + 1}/{MAX_ACCOUNT_RETRIES})")

        # 记录接收到的请求头（用于调试）
        incoming_headers = {k: v for k, v in request.headers.items() if k.lower().startswith("anthropic")}
        logger.info(f"Incoming anthropic headers: {incoming_headers}")

        # 构建请求头（每个账号需要重新构建）
        api_key = account.get("api_key", "")
        key_preview = api_key[:8] + "..." if len(api_key) > 8 else api_key
        logger.info(f"Using account {account_name} with key {key_preview}")

        headers = {
            "Content-Type": request.headers.get("content-type", "application/json"),
            "Authorization": f"Bearer {api_key}",
            "x-api-key": api_key,
            "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }

        # 添加其他 anthropic 相关头
        for key, value in request.headers.items():
            if key.lower().startswith("anthropic-") and key.lower() not in headers:
                headers[key] = value

        # 尝试所有站点（从当前活跃站点开始）
        tried_sites = 0
        start_index = current_site_index
        account_error = False

        while tried_sites < len(SITES):
            site_index = (start_index + tried_sites) % len(SITES)
            site = SITES[site_index]

            # 构建目标 URL
            target_url = f"{site['url']}/v1/{path}"
            if request.query_params:
                target_url += f"?{request.query_params}"

            logger.info(f"Trying {site['name']} ({site['url']}) for account {account_name}")
            logger.info(f"Request type: {'stream' if is_stream else 'normal'}, model: {body_json.get('model', 'unknown')}")

            # 根据站点配置决定是否使用代理和 WAF cookies
            use_proxy = site.get("use_proxy", False)
            need_waf = site.get("need_waf", False)

            cookies = {}
            if need_waf:
                cookies = await get_waf_cookies()

            proxy_config = HTTP_PROXY if use_proxy else None

            # 重试次数：需要 WAF 的站点多重试几次（因为可能遇到负载限制需要排队）
            max_retries = 4 if need_waf else 2

            for attempt in range(max_retries):
                try:
                    # 流式请求需要保持 client 活跃直到流完成
                    if is_stream:
                        # 创建不使用 async with 的 client，手动管理生命周期
                        client = httpx.AsyncClient(
                            http2=False,
                            timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0),
                            proxy=proxy_config,
                            cookies=cookies
                        )

                        try:
                            response = await client.send(
                                client.build_request(
                                    method=request.method,
                                    url=target_url,
                                    headers=headers,
                                    content=body
                                ),
                                stream=True
                            )

                            # 检查是否被 WAF 拦截（返回 HTML）
                            content_type = response.headers.get("content-type", "")
                            if "text/html" in content_type:
                                await response.aclose()
                                await client.aclose()
                                if need_waf:
                                    logger.warning(f"[{site['name']}] WAF challenge detected, refreshing cookies...")
                                    cookies = await refresh_waf_cookies()
                                    continue
                                else:
                                    raise httpx.HTTPStatusError(
                                        "Unexpected HTML response",
                                        request=None,
                                        response=response
                                    )

                            # 检查响应状态 - 区分账号错误和服务器错误
                            if response.status_code == 401 or response.status_code == 403:
                                error_body = await response.aread()
                                await response.aclose()
                                await client.aclose()
                                account_error = True
                                logger.warning(f"[{site['name']}] Account auth error: {response.status_code}, body: {error_body[:500]}")
                                raise httpx.HTTPStatusError(
                                    f"Account auth error: {response.status_code}",
                                    request=None,
                                    response=response
                                )

                            if response.status_code >= 500:
                                error_body = await response.aread()
                                error_body_str = error_body.decode('utf-8', errors='ignore')[:500]
                                await response.aclose()
                                await client.aclose()
                                # 记录详细错误信息
                                logger.warning(f"[{site['name']}] Server error {response.status_code} for account {account_name}, body: {error_body_str}")

                                # 检查是否是 500 空 body（可能是 WAF 拦截导致的）
                                if need_waf and len(error_body_str.strip()) == 0:
                                    logger.warning(f"[{site['name']}] Empty 500 response, might be WAF issue, refreshing cookies...")
                                    cookies = await refresh_waf_cookies()
                                    if attempt < max_retries - 1:
                                        continue

                                # 检查是否是模型负载限制
                                if "负载已经达到上限" in error_body_str or "rate limit" in error_body_str.lower():
                                    # 先尝试等待重试一次
                                    if attempt == 0:
                                        wait_time = 2
                                        logger.info(f"[{site['name']}] Model at capacity, waiting {wait_time}s before retry...")
                                        await asyncio.sleep(wait_time)
                                        continue
                                    # 如果第一次重试还是失败，尝试切换账号
                                    logger.warning(f"[{site['name']}] Account {account_name} at capacity, trying another account...")
                                    account_error = True
                                    raise httpx.HTTPStatusError(
                                        f"Account at capacity: {response.status_code}",
                                        request=None,
                                        response=response
                                    )

                                account_error = True
                                raise httpx.HTTPStatusError(
                                    f"Server error: {response.status_code}",
                                    request=None,
                                    response=response
                                )

                            # 成功 - 更新状态
                            if site_index != current_site_index:
                                current_site_index = site_index
                                logger.info(f"Switched to {site['name']} as current site")
                            record_site_success()
                            record_account_success(account_name)

                            async def stream_response():
                                chunk_count = 0
                                total_bytes = 0
                                try:
                                    async for chunk in response.aiter_bytes():
                                        chunk_count += 1
                                        total_bytes += len(chunk)
                                        yield chunk
                                    logger.info(f"[{site['name']}] Stream completed: {chunk_count} chunks, {total_bytes} bytes, account={account_name}")
                                except Exception as e:
                                    logger.error(f"[{site['name']}] Stream error after {chunk_count} chunks, {total_bytes} bytes: {e}")
                                    raise
                                finally:
                                    await response.aclose()
                                    await client.aclose()

                            return StreamingResponse(
                                stream_response(),
                                status_code=response.status_code,
                                media_type="text/event-stream",
                                headers={
                                    k: v for k, v in response.headers.items()
                                    if k.lower() not in ["content-length", "transfer-encoding", "content-encoding"]
                                }
                            )
                        except Exception as e:
                            await client.aclose()
                            raise
                    else:
                        # 普通响应 - 使用 async with 管理 client 生命周期
                        async with httpx.AsyncClient(
                            http2=False,
                            timeout=httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0),
                            proxy=proxy_config,
                            cookies=cookies
                        ) as client:
                            response = await client.request(
                                method=request.method,
                                url=target_url,
                                headers=headers,
                                content=body
                            )

                            # 检查是否被 WAF 拦截
                            content_type = response.headers.get("content-type", "")
                            if "text/html" in content_type and response.status_code == 200:
                                if need_waf:
                                    logger.warning(f"[{site['name']}] WAF challenge detected, refreshing cookies...")
                                    cookies = await refresh_waf_cookies()
                                    continue
                                else:
                                    raise httpx.HTTPStatusError(
                                        "Unexpected HTML response",
                                        request=None,
                                        response=response
                                    )

                            # 检查响应状态 - 区分账号错误和服务器错误
                            if response.status_code == 401 or response.status_code == 403:
                                account_error = True
                                logger.warning(f"[{site['name']}] Account auth error: {response.status_code}, body: {response.text[:500]}")
                                raise httpx.HTTPStatusError(
                                    f"Account auth error: {response.status_code}",
                                    request=None,
                                    response=response
                                )

                            if response.status_code >= 500:
                                # 记录详细错误信息以便诊断
                                error_body = response.text[:500] if response.text else ""
                                logger.warning(f"[{site['name']}] Server error {response.status_code} for account {account_name}, body: {error_body}")

                                # 检查是否是 500 空 body（可能是 WAF 拦截导致的）
                                if need_waf and len(error_body.strip()) == 0:
                                    logger.warning(f"[{site['name']}] Empty 500 response, might be WAF issue, refreshing cookies...")
                                    cookies = await refresh_waf_cookies()
                                    if attempt < max_retries - 1:
                                        continue

                                # 检查是否是模型负载限制
                                if "负载已经达到上限" in error_body or "rate limit" in error_body.lower():
                                    # 先尝试等待重试一次
                                    if attempt == 0:
                                        wait_time = 2
                                        logger.info(f"[{site['name']}] Model at capacity, waiting {wait_time}s before retry...")
                                        await asyncio.sleep(wait_time)
                                        continue
                                    # 如果第一次重试还是失败，尝试切换账号
                                    logger.warning(f"[{site['name']}] Account {account_name} at capacity, trying another account...")
                                    account_error = True
                                    raise httpx.HTTPStatusError(
                                        f"Account at capacity: {response.status_code}",
                                        request=None,
                                        response=response
                                    )

                                # 其他 500 错误可能是账号问题，尝试切换账号
                                account_error = True
                                raise httpx.HTTPStatusError(
                                    f"Server error: {response.status_code}",
                                    request=None,
                                    response=response
                                )

                            # 成功 - 更新状态
                            if site_index != current_site_index:
                                current_site_index = site_index
                                logger.info(f"Switched to {site['name']} as current site")
                            record_site_success()
                            record_account_success(account_name)

                            # 正常返回
                            logger.info(f"[{site['name']}] Normal response: status={response.status_code}, content-type={content_type}, size={len(response.content)} bytes, account={account_name}")
                            if "json" in content_type:
                                return JSONResponse(
                                    content=response.json(),
                                    status_code=response.status_code
                                )
                            else:
                                return JSONResponse(
                                    content={"raw": response.text},
                                    status_code=response.status_code
                                )

                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ReadError, httpx.HTTPStatusError) as e:
                    last_error = e
                    logger.warning(f"[{site['name']}] Request failed (attempt {attempt + 1}/{max_retries}): {e}")

                    # 如果是账号错误，立即停止重试，尝试其他账号
                    if account_error:
                        logger.info(f"[{site['name']}] Account error detected, stopping retries for {account_name}")
                        break  # 退出重试循环，然后会跳出站点循环尝试其他账号

                    # 只有连接错误才刷新 cookie（可能是 WAF 拦截导致的）
                    # 500 错误不刷新 cookie，直接重试
                    is_connection_error = isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout))
                    if attempt < max_retries - 1:
                        if is_connection_error and need_waf:
                            # 连接错误可能是 WAF 问题，刷新 cookie
                            cookies = await refresh_waf_cookies()
                        # 其他错误直接重试，不刷新 cookie
                        continue
                    else:
                        break  # 退出重试循环，尝试下一个站点

                except Exception as e:
                    last_error = e
                    logger.error(f"[{site['name']}] Proxy error: {e}")
                    break  # 退出重试循环，尝试下一个站点

            # 当前站点所有重试都失败
            logger.warning(f"[{site['name']}] All retries failed for account {account_name}")

            # 如果是账号相关错误，立即尝试其他账号而不是其他站点
            if account_error:
                logger.warning(f"Account {account_name} seems to have issues, trying another account...")
                record_account_failure(account_name)
                break  # 跳出站点循环，尝试下一个账号

            record_site_failure()
            tried_sites += 1

        # 如果不是账号错误且所有站点都失败，也记录账号失败
        if not account_error and tried_sites >= len(SITES):
            record_account_failure(account_name)

    # 所有账号都失败
    logger.error(f"All accounts and sites failed, last error: {last_error}")
    raise HTTPException(status_code=502, detail=f"All upstream sites and accounts failed: {last_error}")


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting AnyRouter Proxy on port {PROXY_PORT}")
    logger.info(f"Accounts file: {ACCOUNTS_FILE}")
    logger.info(f"Base URL: {ANYROUTER_BASE_URL}")
    logger.info(f"HTTP Proxy: {HTTP_PROXY}")

    uvicorn.run(app, host="0.0.0.0", port=PROXY_PORT)
