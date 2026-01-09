"""
AnyRouter Proxy - 多账号负载均衡代理 v2.1

架构特性:
- 多站点故障转移: 主站优先 + 自动切换备用站
- 站点优先降级: 先换站点再换模型（用户请求的模型尽量满足）
- 快速失败: 所有资源不可用时立即返回 503
- 半开状态: 冷却期后渐进式恢复（10%放行）
- 冷却抖动: 防止惊群效应
- 并发控制: 信号量限制并发请求
- 定期 GC: 防止内存泄漏
- 邮件告警: 全站不可用时通知

v2.1 变更:
- 新增: 站点优先降级策略 (DEGRADATION_STRATEGY=site_first)
- 新增: 快速失败机制 (FAST_FAIL_ENABLED)
- 新增: 熔断器半开状态 (HALF_OPEN_ENABLED)
- 新增: 冷却时间抖动 (COOLDOWN_JITTER_ENABLED)
- 改进: 健康检测需要连续成功才切回主站

v2 变更（简化）:
- 移除: 常驻浏览器（改为按需创建/销毁）
- 移除: WAF Cookie 缓存（API 请求不需要）
- 移除: 复杂熔断器状态机（简化为 site_manager）
- 新增: 模型降级功能
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

# 导入新的站点管理器（替代 circuit_breaker）
from site_manager import (
    init_site_manager,
    get_site_manager,
    SITE_MANAGER_ENABLED,
    DEGRADATION_STRATEGY,
    FAST_FAIL_ENABLED,
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

# 站点配置：主站优先（API 请求不需要 WAF），备用站作为后备
# 注意：API 请求带 Authorization header 不会被 WAF 拦截
# WAF cookies 仅在遇到 HTML 响应时动态获取
SITES = [
    {
        "url": "https://anyrouter.top",
        "name": "主站",
        "use_proxy": True,
        "need_waf": False  # API 请求默认不需要 WAF，遇到拦截时动态获取
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
    # 备用站1 (c.cspok.cn) 已移除 - SSL 连接错误，不可用
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
PRIMARY_SITE_CHECK_INTERVAL_URGENT = int(os.getenv("PRIMARY_SITE_CHECK_INTERVAL_URGENT", "1"))  # 紧急间隔
PRIMARY_SITE_CHECK_REQUIRED_SUCCESSES = int(os.getenv("PRIMARY_SITE_CHECK_REQUIRED_SUCCESSES", "2"))  # 需要连续成功次数

# 主站健康检查状态
primary_site_status = {
    "last_check": None,
    "last_check_result": None,
    "last_recovery": None,
    "check_count": 0,
    "recovery_count": 0,
    "consecutive_successes": 0,  # v2.1 新增：连续成功计数
}

# 账号列表
accounts = []

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


def get_total_accounts_count():
    """获取所有账号的总数（包括无 api_key 和禁用的账号）"""
    if ACCOUNTS_FILE.exists():
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return len(data)
        except Exception as e:
            logger.error(f"Failed to count total accounts: {e}")
            return len(accounts)
    return len(accounts)


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

    # 立即更新下次运行时间（避免签到过程中显示错误的时间）
    checkin_job = scheduler.get_job("scheduled_checkin")
    if checkin_job and checkin_job.next_run_time:
        checkin_status["next_run"] = checkin_job.next_run_time.isoformat()
        logger.info(f"Next scheduled check-in: {checkin_status['next_run']}")

    try:
        result = await run_checkin_for_all_accounts()
        logger.info(f"Scheduled check-in completed: {result.get('message', 'Unknown')}")
    except Exception as e:
        logger.error(f"Scheduled check-in failed: {e}")


async def check_primary_site_health():
    """
    检查主站是否可用 - 轻量级检查优化版

    优化点：
    1. 使用 HEAD 请求代替 GET，减少数据传输
    2. 更短的超时时间
    3. 携带 Authorization header 避免 WAF 拦截
    """
    from datetime import datetime

    primary_site = SITES[0]  # 主站始终是第一个
    primary_site_status["last_check"] = datetime.now().isoformat()
    primary_site_status["check_count"] += 1

    # 获取一个账号的 API key 用于健康检查
    check_account = get_next_account()
    if not check_account:
        logger.warning("[Primary Check] No account available for health check")
        primary_site_status["last_check_result"] = "no_account"
        return False

    api_key = check_account.get("api_key", "")

    try:
        # 使用 HEAD 请求进行轻量级检查
        # 携带 Authorization header 避免 WAF 拦截
        async with httpx.AsyncClient(
            http2=False,
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            proxy=HTTP_PROXY if primary_site.get("use_proxy") else None,
        ) as client:
            response = await client.head(
                f"{primary_site['url']}/v1/models",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Authorization": f"Bearer {api_key}",
                    "x-api-key": api_key,
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
    """
    定时主站健康检查任务 v2.1

    改进：
    1. 需要连续成功 N 次才切换回主站
    2. 不在主站时使用更短的检查间隔
    """
    global current_site_index, site_fail_count
    from datetime import datetime

    # 如果当前已经在主站，不需要检查
    if current_site_index == 0:
        logger.debug("[Primary Check] Already using primary site, skip check")
        # 重置连续成功计数
        primary_site_status["consecutive_successes"] = 0
        return

    logger.info(f"[Primary Check] Checking primary site health (current: {SITES[current_site_index]['name']})")

    is_healthy = await check_primary_site_health()

    if is_healthy:
        primary_site_status["consecutive_successes"] += 1
        logger.info(
            f"[Primary Check] Primary site healthy "
            f"({primary_site_status['consecutive_successes']}/{PRIMARY_SITE_CHECK_REQUIRED_SUCCESSES} consecutive successes)"
        )

        # 检查是否达到连续成功要求
        if primary_site_status["consecutive_successes"] >= PRIMARY_SITE_CHECK_REQUIRED_SUCCESSES:
            old_site = SITES[current_site_index]
            current_site_index = 0
            site_fail_count = 0
            primary_site_status["last_recovery"] = datetime.now().isoformat()
            primary_site_status["recovery_count"] += 1
            primary_site_status["consecutive_successes"] = 0
            logger.info(
                f"[Primary Check] Primary site recovered! "
                f"Switching from {old_site['name']} back to {SITES[0]['name']}"
            )
    else:
        # 健康检查失败，重置连续成功计数
        primary_site_status["consecutive_successes"] = 0
        logger.info(f"[Primary Check] Primary site still unavailable, staying on {SITES[current_site_index]['name']}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理 v2 - 简化版"""

    # ============== 启动阶段 ==============
    logger.info("=" * 60)
    logger.info("AnyRouter Proxy v2 starting...")
    logger.info("=" * 60)

    # 1. 加载账号
    load_accounts()
    logger.info(f"[Startup] Loaded {len(accounts)} accounts")

    # 2. 初始化站点管理器
    if SITE_MANAGER_ENABLED:
        logger.info("[Startup] Initializing site manager...")
        site_manager = init_site_manager(SITES)
        logger.info("[Startup] Site manager initialized")
    else:
        logger.info("[Startup] Site manager disabled")

    # 3. 配置定时任务
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
    logger.info(f"[Config] Primary site preferred: Yes")
    logger.info(f"[Config] Model degradation: {'enabled' if SITE_MANAGER_ENABLED else 'disabled'}")
    logger.info("-" * 60)
    logger.info("AnyRouter Proxy v2 started successfully!")
    logger.info("=" * 60)

    yield  # ============== 应用运行中 ==============

    # ============== 关闭阶段 ==============
    logger.info("=" * 60)
    logger.info("AnyRouter Proxy shutting down...")

    # 停止调度器
    if scheduler.running:
        scheduler.shutdown()
        logger.info("[Shutdown] Scheduler stopped")

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
    """健康检查 - 返回详细的系统状态 v2"""
    from datetime import datetime
    current_site = get_current_site()
    site_manager = get_site_manager()

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
        "version": "v2",  # 标记版本
        "accounts": get_total_accounts_count(),
        "active_accounts": len(accounts),

        # 账号健康状态
        "account_health": {
            "total": get_total_accounts_count(),
            "active": len(accounts),
            "healthy": len(healthy_accounts),
            "unhealthy": len(unhealthy_accounts),
            "unhealthy_accounts": unhealthy_accounts,
            "max_fails_before_disable": ACCOUNT_MAX_FAILS,
            "disable_duration_seconds": ACCOUNT_DISABLE_DURATION
        },

        # 站点状态（简化版）
        "sites": {
            "current": current_site["name"],
            "current_url": current_site["url"],
            "use_proxy": current_site["use_proxy"],
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

        # 代理配置
        "proxy": HTTP_PROXY,

        # Dashboard 认证
        "dashboard_auth": {
            "enabled": is_dashboard_auth_enabled(),
            "description": "Dashboard 登录认证" if is_dashboard_auth_enabled() else "Dashboard 无需登录"
        },

        # API Key 验证
        "api_key_validation": get_validation_stats(),

        # 站点管理器状态（替代旧的 circuit_breaker）
        "site_manager": site_manager.get_status() if site_manager else {"enabled": False},

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


@app.post("/force-gc")
async def force_gc():
    """手动触发垃圾回收"""
    site_manager = get_site_manager()
    if site_manager:
        site_manager.force_gc()
        return {"status": "ok", "message": "GC triggered"}
    return {"status": "error", "message": "Site manager not initialized"}


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


@app.post("/reset-site-manager")
async def reset_site_manager():
    """重置站点管理器（重置所有站点状态和模型降级）"""
    site_manager = get_site_manager()
    if not site_manager:
        return {"status": "error", "message": "Site manager not initialized"}

    site_manager.reset_all()
    return {
        "status": "ok",
        "message": "Site manager reset",
        "site_manager": site_manager.get_status()
    }


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def proxy(request: Request, path: str):
    """
    代理 /v1/* 请求到 AnyRouter v2.1

    特性:
    - 多站点故障转移
    - 多账号负载均衡
    - 模型降级（负载过高时自动切换）
    - 快速失败（所有资源不可用时立即返回）
    - 站点优先降级（先换站点再换模型）
    """
    global current_site_index

    site_manager = get_site_manager()

    # 并发控制检查
    if site_manager and SITE_MANAGER_ENABLED:
        can_proceed = await site_manager.acquire()
        if not can_proceed:
            raise HTTPException(
                status_code=503,
                detail="Service temporarily unavailable - concurrent request limit reached"
            )

    # v2.1: 快速失败检查 - 所有站点不可用
    if site_manager and SITE_MANAGER_ENABLED and FAST_FAIL_ENABLED:
        has_site, site_retry = site_manager.has_any_available_site()
        if not has_site:
            logger.warning(f"[FastFail] All sites unavailable, retry after {site_retry}s")
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "type": "service_unavailable",
                        "message": f"所有上游服务当前不可用，请 {site_retry} 秒后重试",
                        "retry_after": site_retry,
                    }
                },
                headers={"Retry-After": str(site_retry)}
            )

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

    # 解析请求体
    try:
        body_json = json.loads(body) if body else {}
        is_stream = body_json.get("stream", False)
        requested_model = body_json.get("model", "unknown")
    except:
        is_stream = False
        body_json = {}
        requested_model = "unknown"

    # v2.1: 快速失败检查 - 所有模型不可用（针对特定请求模型）
    if site_manager and SITE_MANAGER_ENABLED and FAST_FAIL_ENABLED:
        has_model, model_retry = site_manager.has_any_available_model()
        if not has_model:
            logger.warning(f"[FastFail] All models unavailable, retry after {model_retry}s")
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "type": "service_unavailable",
                        "message": f"所有模型当前负载过高，请 {model_retry} 秒后重试",
                        "retry_after": model_retry,
                    }
                },
                headers={"Retry-After": str(model_retry)}
            )

    # v2.1: 根据降级策略选择处理方式
    if site_manager and SITE_MANAGER_ENABLED and DEGRADATION_STRATEGY == "site_first":
        # 站点优先降级：获取模型降级列表，但不预先替换
        model_candidates = site_manager.get_model_fallback_list(requested_model)
        effective_model = requested_model  # 先使用原模型
        logger.info(f"[SiteFirst] Model candidates: {model_candidates}")
    else:
        # 原有逻辑：模型优先降级
        effective_model = requested_model
        if site_manager and SITE_MANAGER_ENABLED:
            effective_model = site_manager.get_effective_model(requested_model)
            if effective_model != requested_model:
                body_json["model"] = effective_model
                body = json.dumps(body_json).encode("utf-8")
                logger.info(f"[ModelFirst] Using fallback model: {requested_model} -> {effective_model}")
        model_candidates = [effective_model]

    logger.info(f"Request: model={effective_model}, stream={is_stream}, strategy={DEGRADATION_STRATEGY}")

    # 账号故障转移：最多尝试 3 个不同的账号
    MAX_ACCOUNT_RETRIES = 3
    tried_account_names = []
    last_error = None

    for account_attempt in range(MAX_ACCOUNT_RETRIES):
        # 获取账号（排除已尝试过的）
        account = get_next_account(exclude_names=tried_account_names)
        if not account:
            if account_attempt == 0:
                raise HTTPException(status_code=503, detail="No available accounts")
            break

        account_name = account.get("name", account.get("email", "unknown"))
        tried_account_names.append(account_name)

        if account_attempt > 0:
            logger.info(f"Account failover: trying {account_name} (attempt {account_attempt + 1}/{MAX_ACCOUNT_RETRIES})")

        # 构建请求头
        api_key = account.get("api_key", "")
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

        # v2.1: 站点优先降级 - 外层循环模型，内层循环站点
        tried_combinations = set()
        model_overload_on_all_sites = False

        for current_model in model_candidates:
            if model_overload_on_all_sites and current_model == model_candidates[0]:
                # 原模型在所有站点都过载，跳过（已经尝试过了）
                continue

            # 更新请求体中的模型
            if current_model != body_json.get("model"):
                body_json["model"] = current_model
                body = json.dumps(body_json).encode("utf-8")
                if current_model != requested_model:
                    logger.info(f"[SiteFirst] Switching to fallback model: {requested_model} -> {current_model}")

            # 尝试所有可用站点
            available_sites = site_manager.get_available_sites() if site_manager else SITES
            sites_tried_for_model = 0
            model_overload_count = 0

            # v2.1: 初始化错误状态（必须在 site 循环外部，防止 available_sites 为空时 UnboundLocalError）
            account_error = False
            model_overload = False

            for site in available_sites:
                combination = (site["name"], current_model)
                if combination in tried_combinations:
                    continue
                tried_combinations.add(combination)
                sites_tried_for_model += 1

                # 构建目标 URL
                target_url = f"{site['url']}/v1/{path}"
                if request.query_params:
                    target_url += f"?{request.query_params}"

                logger.info(f"[{site['name']}] Trying account={account_name}, model={current_model}")

                # 代理配置
                proxy_config = HTTP_PROXY if site.get("use_proxy", False) else None
                max_retries = 2

                # 重置每个站点的错误状态
                account_error = False
                model_overload = False

                for attempt in range(max_retries):
                    try:
                        if is_stream:
                            # 流式请求
                            client = httpx.AsyncClient(
                                http2=False,
                                timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0),
                                proxy=proxy_config
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

                                # 检查响应状态
                                if response.status_code == 401 or response.status_code == 403:
                                    error_body = await response.aread()
                                    await response.aclose()
                                    await client.aclose()
                                    account_error = True
                                    logger.warning(f"[{site['name']}] Account auth error: {response.status_code}")
                                    raise httpx.HTTPStatusError(f"Auth error: {response.status_code}", request=None, response=response)

                                if response.status_code >= 500:
                                    error_body = await response.aread()
                                    error_str = error_body.decode('utf-8', errors='ignore')[:500]
                                    await response.aclose()
                                    await client.aclose()

                                    logger.warning(f"[{site['name']}] Server error {response.status_code}: {error_str[:100]}")

                                    # v2.1: 检查是否是模型负载限制
                                    if "负载已经达到上限" in error_str or "rate limit" in error_str.lower():
                                        model_overload = True
                                        model_overload_count += 1
                                        logger.info(f"[SiteFirst] {current_model} overloaded at {site['name']} ({model_overload_count}/{len(available_sites)} sites)")

                                        # v2.1 站点优先：记录模型在此站点过载，但不立即切换模型
                                        # 继续尝试下一个站点的同一模型
                                        if site_manager:
                                            site_manager.record_model_load_limit(current_model)

                                        raise httpx.HTTPStatusError(f"Model overload: {response.status_code}", request=None, response=response)

                                    raise httpx.HTTPStatusError(f"Server error: {response.status_code}", request=None, response=response)

                                # 成功！
                                record_site_success()
                                record_account_success(account_name)
                                if site_manager:
                                    site_manager.record_site_success(site["url"])
                                    site_manager.record_model_success(current_model)

                                if current_model != requested_model:
                                    logger.info(f"[SiteFirst] Success with fallback model: {requested_model} -> {current_model} @ {site['name']}")
                                else:
                                    logger.info(f"[{site['name']}] Success with original model: {current_model}")

                                async def stream_response():
                                    chunk_count = 0
                                    total_bytes = 0
                                    try:
                                        async for chunk in response.aiter_bytes():
                                            chunk_count += 1
                                            total_bytes += len(chunk)
                                            yield chunk
                                        logger.info(f"[{site['name']}] Stream completed: {chunk_count} chunks, {total_bytes} bytes")
                                    except Exception as e:
                                        logger.error(f"[{site['name']}] Stream error: {e}")
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
                            # 普通请求
                            async with httpx.AsyncClient(
                                http2=False,
                                timeout=httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0),
                                proxy=proxy_config
                            ) as client:
                                response = await client.request(
                                    method=request.method,
                                    url=target_url,
                                    headers=headers,
                                    content=body
                                )

                                content_type = response.headers.get("content-type", "")

                                # 检查响应状态
                                if response.status_code == 401 or response.status_code == 403:
                                    account_error = True
                                    logger.warning(f"[{site['name']}] Account auth error: {response.status_code}")
                                    raise httpx.HTTPStatusError(f"Auth error: {response.status_code}", request=None, response=response)

                                if response.status_code >= 500:
                                    error_body = response.text[:500] if response.text else ""
                                    logger.warning(f"[{site['name']}] Server error {response.status_code}: {error_body[:100]}")

                                    # v2.1: 检查是否是模型负载限制
                                    if "负载已经达到上限" in error_body or "rate limit" in error_body.lower():
                                        model_overload = True
                                        model_overload_count += 1
                                        logger.info(f"[SiteFirst] {current_model} overloaded at {site['name']} ({model_overload_count}/{len(available_sites)} sites)")

                                        # v2.1 站点优先：记录模型在此站点过载，但不立即切换模型
                                        if site_manager:
                                            site_manager.record_model_load_limit(current_model)

                                        raise httpx.HTTPStatusError(f"Model overload: {response.status_code}", request=None, response=response)

                                    raise httpx.HTTPStatusError(f"Server error: {response.status_code}", request=None, response=response)

                                # 成功！
                                record_site_success()
                                record_account_success(account_name)
                                if site_manager:
                                    site_manager.record_site_success(site["url"])
                                    site_manager.record_model_success(current_model)

                                if current_model != requested_model:
                                    logger.info(f"[SiteFirst] Success with fallback model: {requested_model} -> {current_model} @ {site['name']}")
                                else:
                                    logger.info(f"[{site['name']}] Success with original model: {current_model}")

                                logger.info(f"[{site['name']}] Response: status={response.status_code}, size={len(response.content)} bytes")

                                if "json" in content_type:
                                    return JSONResponse(content=response.json(), status_code=response.status_code)
                                else:
                                    return JSONResponse(content={"raw": response.text}, status_code=response.status_code)

                    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ReadError, httpx.HTTPStatusError) as e:
                        last_error = e
                        error_type = "model_overload" if model_overload else "site_error"
                        logger.warning(f"[{site['name']}] Request failed ({error_type}, attempt {attempt + 1}/{max_retries}): {e}")

                        if account_error:
                            # 账号错误，立即停止当前账号的尝试
                            break

                        if model_overload:
                            # v2.1 站点优先：模型过载不重试，直接尝试下一个站点
                            break

                        if attempt < max_retries - 1:
                            continue
                        else:
                            break

                    except Exception as e:
                        last_error = e
                        logger.error(f"[{site['name']}] Proxy error: {e}")
                        break

                # 当前站点所有重试都失败
                if not model_overload:
                    # 非模型过载的站点失败才记录
                    logger.warning(f"[{site['name']}] All retries failed for account {account_name}")
                    record_site_failure()
                    if site_manager:
                        site_manager.record_site_failure(site["url"], str(last_error)[:100] if last_error else "")

                if account_error:
                    # 账号认证错误，标记账号失败并停止
                    record_account_failure(account_name)
                    break

            # v2.1: 检查当前模型是否在所有站点都过载
            if model_overload_count >= sites_tried_for_model and sites_tried_for_model > 0:
                model_overload_on_all_sites = True
                logger.info(f"[SiteFirst] {current_model} overloaded on all {sites_tried_for_model} sites, trying next model...")

        # 所有模型+站点组合都失败
        if not account_error:
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
