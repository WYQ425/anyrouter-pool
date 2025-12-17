"""
AnyRouter Proxy - 多账号负载均衡代理
使用原始 anyrouter.top，需要 WAF 绕过 + HTTP 代理
"""

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
from playwright.async_api import async_playwright
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

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

# WAF cookies 缓存
waf_cookies = {}
waf_cookies_expire = 0
WAF_COOKIE_TTL = 300  # 5分钟

# 账号列表
accounts = []


def load_accounts():
    """加载账号配置"""
    global accounts
    if ACCOUNTS_FILE.exists():
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # 只加载有 api_key 的账号
            accounts = [acc for acc in data if acc.get("api_key") and acc.get("enabled", True)]
            logger.info(f"Loaded {len(accounts)} accounts with API keys")
    else:
        logger.error(f"Accounts file not found: {ACCOUNTS_FILE}")


def get_next_account():
    """获取下一个账号 (随机负载均衡)"""
    if not accounts:
        return None
    return random.choice(accounts)


async def refresh_waf_cookies():
    """使用 Playwright 获取 WAF cookies"""
    global waf_cookies, waf_cookies_expire

    logger.info("Refreshing WAF cookies using Playwright...")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[f"--proxy-server={HTTP_PROXY}"]
            )
            context = await browser.new_context()
            page = await context.new_page()

            # 访问登录页触发 WAF (使用 domcontentloaded 更快)
            await page.goto("https://anyrouter.top/login", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)  # 等待 JS 执行生成 WAF cookies

            # 获取 cookies
            cookies = await context.cookies()
            waf_cookies = {c["name"]: c["value"] for c in cookies}
            waf_cookies_expire = time.time() + WAF_COOKIE_TTL

            await browser.close()

            logger.info(f"WAF cookies refreshed: {list(waf_cookies.keys())}")
            return waf_cookies

    except Exception as e:
        logger.error(f"Failed to refresh WAF cookies: {e}")
        return {}


async def get_waf_cookies():
    """获取 WAF cookies（带缓存）"""
    global waf_cookies, waf_cookies_expire

    if time.time() < waf_cookies_expire and waf_cookies:
        return waf_cookies

    return await refresh_waf_cookies()


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
    """检查主站是否可用"""
    from datetime import datetime

    primary_site = SITES[0]  # 主站始终是第一个
    primary_site_status["last_check"] = datetime.now().isoformat()
    primary_site_status["check_count"] += 1

    try:
        # 获取 WAF cookies（主站需要）
        cookies = await get_waf_cookies()

        # 发送测试请求到主站
        async with httpx.AsyncClient(
            http2=False,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
            proxy=HTTP_PROXY if primary_site.get("use_proxy") else None,
            cookies=cookies
        ) as client:
            # 使用一个简单的 API 请求测试（获取模型列表）
            response = await client.get(
                f"{primary_site['url']}/v1/models",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                }
            )

            # 检查响应
            content_type = response.headers.get("content-type", "")

            # 如果返回 HTML，说明被 WAF 拦截
            if "text/html" in content_type:
                logger.debug(f"[Primary Check] WAF challenge detected, trying to refresh cookies...")
                # 尝试刷新 WAF cookies
                await refresh_waf_cookies()
                primary_site_status["last_check_result"] = "waf_challenge"
                return False

            # 检查状态码
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
    # 启动时
    load_accounts()
    await get_waf_cookies()
    logger.info(f"Using base URL: {ANYROUTER_BASE_URL}")
    logger.info(f"HTTP Proxy: {HTTP_PROXY}")

    # 启动定时签到任务
    if CHECKIN_ENABLED:
        # 使用 CronTrigger 在固定时间点执行（如 2:10, 8:10, 14:10, 20:10）
        scheduler.add_job(
            scheduled_checkin,
            trigger=CronTrigger(hour=CHECKIN_CRON_HOUR, minute=CHECKIN_CRON_MINUTE),
            id="scheduled_checkin",
            name="AnyRouter Check-in",
            replace_existing=True
        )
        logger.info(f"Check-in scheduler started, cron: hour={CHECKIN_CRON_HOUR}, minute={CHECKIN_CRON_MINUTE}")

        # 记录下次运行时间
        checkin_status["next_run"] = None  # 将在 scheduler.start() 后更新

    # 启动主站健康检查任务
    if PRIMARY_SITE_CHECK_ENABLED:
        scheduler.add_job(
            scheduled_primary_site_check,
            trigger=IntervalTrigger(minutes=PRIMARY_SITE_CHECK_INTERVAL),
            id="primary_site_check",
            name="Primary Site Health Check",
            replace_existing=True
        )
        logger.info(f"Primary site health check enabled, interval: {PRIMARY_SITE_CHECK_INTERVAL} minutes")
    else:
        logger.info("Primary site health check is disabled")

    # 启动调度器
    if CHECKIN_ENABLED or PRIMARY_SITE_CHECK_ENABLED:
        scheduler.start()

        # 更新签到下次运行时间
        if CHECKIN_ENABLED:
            checkin_job = scheduler.get_job("scheduled_checkin")
            if checkin_job:
                checkin_status["next_run"] = checkin_job.next_run_time.isoformat() if checkin_job.next_run_time else None
    else:
        logger.info("All schedulers are disabled")

    yield  # 应用运行

    # 关闭时
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler stopped")


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
    """健康检查"""
    from datetime import datetime
    current_site = get_current_site()
    return {
        "status": "ok",
        "accounts": len(accounts),
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
        "primary_site_check": {
            "enabled": PRIMARY_SITE_CHECK_ENABLED,
            "interval_minutes": PRIMARY_SITE_CHECK_INTERVAL,
            "last_check": primary_site_status.get("last_check"),
            "last_check_result": primary_site_status.get("last_check_result"),
            "last_recovery": primary_site_status.get("last_recovery"),
            "check_count": primary_site_status.get("check_count", 0),
            "recovery_count": primary_site_status.get("recovery_count", 0)
        },
        "waf_cookies_valid": time.time() < waf_cookies_expire,
        "proxy": HTTP_PROXY,
        "dashboard_auth": {
            "enabled": is_dashboard_auth_enabled(),
            "description": "Dashboard 登录认证" if is_dashboard_auth_enabled() else "Dashboard 无需登录"
        },
        "api_key_validation": get_validation_stats(),
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
    cookies = await refresh_waf_cookies()
    return {"status": "ok", "cookies": list(cookies.keys())}


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
    """代理 /v1/* 请求到 AnyRouter，支持多站点故障转移"""
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

    # 获取账号
    account = get_next_account()
    if not account:
        raise HTTPException(status_code=503, detail="No available accounts")

    # 获取请求体
    body = await request.body()

    # 检查是否是流式请求
    try:
        body_json = json.loads(body) if body else {}
        is_stream = body_json.get("stream", False)
    except:
        is_stream = False
        body_json = {}

    # 构建请求头
    headers = {
        "Content-Type": request.headers.get("content-type", "application/json"),
        "x-api-key": account["api_key"],
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
    last_error = None

    while tried_sites < len(SITES):
        site_index = (start_index + tried_sites) % len(SITES)
        site = SITES[site_index]

        # 构建目标 URL
        target_url = f"{site['url']}/v1/{path}"
        if request.query_params:
            target_url += f"?{request.query_params}"

        logger.info(f"Trying {site['name']} ({site['url']}) for account {account['name']}")
        logger.info(f"Request type: {'stream' if is_stream else 'normal'}, model: {body_json.get('model', 'unknown')}")

        # 根据站点配置决定是否使用代理和 WAF cookies
        use_proxy = site.get("use_proxy", False)
        need_waf = site.get("need_waf", False)

        cookies = {}
        if need_waf:
            cookies = await get_waf_cookies()

        proxy_config = HTTP_PROXY if use_proxy else None

        # 最多重试 2 次（仅对需要 WAF 的站点刷新 cookies）
        max_retries = 2 if need_waf else 1

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

                        # 检查响应状态
                        if response.status_code >= 500:
                            await response.aclose()
                            await client.aclose()
                            raise httpx.HTTPStatusError(
                                f"Server error: {response.status_code}",
                                request=None,
                                response=response
                            )

                        # 成功 - 更新当前站点索引
                        if site_index != current_site_index:
                            current_site_index = site_index
                            logger.info(f"Switched to {site['name']} as current site")
                        record_site_success()

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

                        # 检查是否需要重试（5xx 错误）
                        if response.status_code >= 500:
                            raise httpx.HTTPStatusError(
                                f"Server error: {response.status_code}",
                                request=None,
                                response=response
                            )

                        # 成功 - 更新当前站点索引
                        if site_index != current_site_index:
                            current_site_index = site_index
                            logger.info(f"Switched to {site['name']} as current site")
                        record_site_success()

                        # 正常返回
                        logger.info(f"[{site['name']}] Normal response: status={response.status_code}, content-type={content_type}, size={len(response.content)} bytes")
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

                if attempt < max_retries - 1 and need_waf:
                    # 刷新 WAF cookies 并重试
                    cookies = await refresh_waf_cookies()
                    continue
                else:
                    break  # 退出重试循环，尝试下一个站点

            except Exception as e:
                last_error = e
                logger.error(f"[{site['name']}] Proxy error: {e}")
                break  # 退出重试循环，尝试下一个站点

        # 当前站点所有重试都失败，记录失败并尝试下一个站点
        logger.warning(f"[{site['name']}] All retries failed, trying next site...")
        record_site_failure()
        tried_sites += 1

    # 所有站点都失败
    logger.error(f"All sites failed, last error: {last_error}")
    raise HTTPException(status_code=502, detail=f"All upstream sites failed: {last_error}")


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting AnyRouter Proxy on port {PROXY_PORT}")
    logger.info(f"Accounts file: {ACCOUNTS_FILE}")
    logger.info(f"Base URL: {ANYROUTER_BASE_URL}")
    logger.info(f"HTTP Proxy: {HTTP_PROXY}")

    uvicorn.run(app, host="0.0.0.0", port=PROXY_PORT)
