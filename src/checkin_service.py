"""
AnyRouter 自动签到服务
集成到 WAF Proxy 中，支持定时自动签到
支持多站点故障转移

更新: 使用共享的浏览器管理器，避免重复启动浏览器
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

# 使用共享的浏览器管理器
from browser_manager import browser_manager

# 配置
import os
ANYROUTER_BASE_URL = os.getenv("ANYROUTER_BASE_URL", "https://anyrouter.top")
# 支持 Windows 本地测试和 Docker 容器运行
_default_accounts_file = "/app/data/accounts.json"
_default_balances_file = "/app/data/balances.json"
if os.name == 'nt' and not os.path.exists(_default_accounts_file):
    _default_accounts_file = str(Path(__file__).parent.parent / "data" / "keeper" / "accounts.json")
    _default_balances_file = str(Path(__file__).parent.parent / "data" / "keeper" / "balances.json")
ACCOUNTS_FILE = Path(os.getenv("ACCOUNTS_FILE", _default_accounts_file))
BALANCES_FILE = Path(os.getenv("BALANCES_FILE", _default_balances_file))
HTTP_PROXY = os.getenv("HTTP_PROXY", "http://127.0.0.1:7890")

# 签到相关配置
SIGN_IN_PATH = "/api/user/sign_in"
USER_INFO_PATH = "/api/user/self"
LOGIN_PATH = "/login"
API_USER_HEADER = "new-api-user"
WAF_COOKIE_NAMES = ["acw_tc", "cdn_sec_tc", "acw_sc__v2"]

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

# 签到状态存储
checkin_status = {
    "last_run": None,
    "next_run": None,
    "results": [],
    "total_success": 0,
    "total_failed": 0
}


def load_accounts():
    """加载账号配置"""
    if ACCOUNTS_FILE.exists():
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # 加载所有启用的账号（签到需要 session cookie，不需要 api_key）
            accounts = [acc for acc in data if acc.get("enabled", True)]
            return accounts
    else:
        logger.error(f"Accounts file not found: {ACCOUNTS_FILE}")
        return []


async def get_waf_cookies_for_checkin():
    """使用共享浏览器获取 WAF cookies 用于签到"""
    logger.info("Getting WAF cookies for check-in using shared browser...")

    try:
        # 使用共享的浏览器管理器
        login_url = f"{ANYROUTER_BASE_URL}{LOGIN_PATH}"
        cookies = await browser_manager.get_page_cookies(
            url=login_url,
            wait_time=5000
        )

        # 过滤出 WAF 相关的 cookies
        waf_cookies = {
            name: value
            for name, value in cookies.items()
            if name in WAF_COOKIE_NAMES
        }

        logger.info(f"Got WAF cookies for check-in: {list(waf_cookies.keys())}")
        return waf_cookies

    except Exception as e:
        logger.error(f"Failed to get WAF cookies for check-in: {e}")
        return {}


async def checkin_single_account(account: dict, waf_cookies: dict) -> dict:
    """为单个账号执行签到，支持多站点故障转移"""
    account_name = account.get("name", "Unknown")
    api_user = account.get("api_user", "")
    session_cookie = account.get("cookies", {}).get("session", "")

    if not session_cookie:
        return {
            "account": account_name,
            "success": False,
            "message": "Missing session cookie"
        }

    if not api_user:
        return {
            "account": account_name,
            "success": False,
            "message": "Missing api_user"
        }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        API_USER_HEADER: api_user,
    }

    result = {
        "account": account_name,
        "api_user": api_user,
        "success": False,
        "message": "",
        "quota": None,
        "used_quota": None,
        "timestamp": datetime.now().isoformat(),
        "site_used": None
    }

    # 尝试所有站点
    for site in SITES:
        site_name = site["name"]
        site_url = site["url"]
        use_proxy = site.get("use_proxy", False)
        need_waf = site.get("need_waf", False)

        # 根据站点配置设置 cookies
        if need_waf:
            all_cookies = {**waf_cookies, "session": session_cookie}
        else:
            all_cookies = {"session": session_cookie}

        proxy_config = HTTP_PROXY if use_proxy else None

        # 设置请求头中的 Referer 和 Origin
        site_headers = {
            **headers,
            "Referer": site_url,
            "Origin": site_url,
        }

        logger.info(f"[{account_name}] Trying {site_name} ({site_url})...")

        try:
            async with httpx.AsyncClient(
                http2=True,
                timeout=30.0,
                proxy=proxy_config,
                cookies=all_cookies
            ) as client:
                # 1. 先获取用户信息（也会触发签到）
                user_info_url = f"{site_url}{USER_INFO_PATH}"
                logger.info(f"[{account_name}] Getting user info from {user_info_url}")

                user_response = await client.get(user_info_url, headers=site_headers)

                # 检查是否被 WAF 拦截
                content_type = user_response.headers.get("content-type", "")
                if "text/html" in content_type:
                    logger.warning(f"[{account_name}] [{site_name}] WAF challenge or error page detected")
                    continue  # 尝试下一个站点

                if user_response.status_code == 200:
                    try:
                        user_data = user_response.json()
                        if user_data.get("success"):
                            data = user_data.get("data", {})
                            # quota 单位是 1/500000 美元
                            result["quota"] = round(data.get("quota", 0) / 500000, 2)
                            result["used_quota"] = round(data.get("used_quota", 0) / 500000, 2)
                            logger.info(f"[{account_name}] [{site_name}] Balance: ${result['quota']}, Used: ${result['used_quota']}")
                    except Exception as e:
                        logger.warning(f"[{account_name}] [{site_name}] Failed to parse user info: {e}")

                # 2. 执行签到
                sign_in_url = f"{site_url}{SIGN_IN_PATH}"
                logger.info(f"[{account_name}] [{site_name}] Executing check-in at {sign_in_url}")

                checkin_response = await client.post(sign_in_url, headers=site_headers)

                logger.info(f"[{account_name}] [{site_name}] Check-in response status: {checkin_response.status_code}")

                # 检查是否被 WAF 拦截
                content_type = checkin_response.headers.get("content-type", "")
                if "text/html" in content_type:
                    logger.warning(f"[{account_name}] [{site_name}] WAF challenge on check-in")
                    continue  # 尝试下一个站点

                if checkin_response.status_code == 200:
                    try:
                        checkin_data = checkin_response.json()
                        # 判断签到成功的条件
                        if checkin_data.get("ret") == 1 or checkin_data.get("code") == 0 or checkin_data.get("success"):
                            result["success"] = True
                            result["message"] = checkin_data.get("msg", checkin_data.get("message", "Check-in successful"))
                            result["site_used"] = site_name
                            logger.info(f"[{account_name}] [{site_name}] Check-in successful: {result['message']}")
                            return result  # 成功，返回结果
                        else:
                            result["message"] = checkin_data.get("msg", checkin_data.get("message", "Check-in failed"))
                            result["site_used"] = site_name
                            # 如果是"已签到"等消息，也算成功
                            if "已签到" in result["message"] or "already" in result["message"].lower():
                                result["success"] = True
                                logger.info(f"[{account_name}] [{site_name}] Already checked in: {result['message']}")
                                return result
                            logger.warning(f"[{account_name}] [{site_name}] Check-in failed: {result['message']}")
                    except json.JSONDecodeError:
                        # 非 JSON 响应
                        if "success" in checkin_response.text.lower():
                            result["success"] = True
                            result["message"] = "Check-in successful (non-JSON response)"
                            result["site_used"] = site_name
                            return result
                        else:
                            result["message"] = f"Invalid response format: {checkin_response.text[:100]}"
                else:
                    logger.warning(f"[{account_name}] [{site_name}] HTTP {checkin_response.status_code}")
                    continue  # 尝试下一个站点

        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ReadError) as e:
            logger.warning(f"[{account_name}] [{site_name}] Connection error: {e}")
            continue  # 尝试下一个站点

        except Exception as e:
            logger.error(f"[{account_name}] [{site_name}] Error: {e}")
            continue  # 尝试下一个站点

    # 所有站点都失败
    if not result["message"]:
        result["message"] = "All sites failed"
    logger.error(f"[{account_name}] All sites failed for check-in")
    return result


async def run_checkin_for_all_accounts() -> dict:
    """为所有账号执行签到"""
    global checkin_status

    start_time = datetime.now()
    logger.info(f"Starting check-in for all accounts at {start_time.isoformat()}")

    # 加载账号
    accounts = load_accounts()
    if not accounts:
        logger.error("No accounts found for check-in")
        return {
            "success": False,
            "message": "No accounts found",
            "results": []
        }

    logger.info(f"Found {len(accounts)} accounts for check-in")

    # 获取 WAF cookies（只需要获取一次，所有账号共用）
    waf_cookies = await get_waf_cookies_for_checkin()
    if not waf_cookies:
        logger.error("Failed to get WAF cookies, check-in aborted")
        return {
            "success": False,
            "message": "Failed to get WAF cookies",
            "results": []
        }

    # 为每个账号执行签到
    results = []
    success_count = 0
    failed_count = 0

    for account in accounts:
        result = await checkin_single_account(account, waf_cookies)
        results.append(result)

        if result["success"]:
            success_count += 1
        else:
            failed_count += 1

        # 账号之间间隔 1 秒，避免请求过快
        await asyncio.sleep(1)

    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    # 更新状态
    checkin_status["last_run"] = start_time.isoformat()
    checkin_status["results"] = results
    checkin_status["total_success"] = success_count
    checkin_status["total_failed"] = failed_count

    summary = {
        "success": failed_count == 0,
        "message": f"Check-in completed: {success_count}/{len(accounts)} successful",
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "duration_seconds": round(duration, 2),
        "total_accounts": len(accounts),
        "success_count": success_count,
        "failed_count": failed_count,
        "results": results
    }

    logger.info(f"Check-in completed: {success_count}/{len(accounts)} successful, took {duration:.2f}s")

    # 保存余额数据到文件
    save_balances_to_file(results)

    return summary


def save_balances_to_file(results: list):
    """保存余额数据到 balances.json 文件"""
    try:
        # 构建余额数据
        # 注意：AnyRouter API 返回的 quota 是"剩余额度"，不是"总额度"
        # 总额度需要在读取时计算：total = quota (剩余) + used_quota (已用)
        balances_data = {
            "accounts": [],
            "last_updated": datetime.now().isoformat(),
            "total_quota_usd": 0,  # 这里存的是剩余额度总和
            "total_used_usd": 0
        }

        for result in results:
            if result.get("quota") is not None:
                # quota = 剩余额度 (AnyRouter 返回的原始值)
                # used_quota = 已使用额度
                account_balance = {
                    "name": result.get("account", "Unknown"),
                    "username": result.get("api_user", result.get("account", "")),
                    "quota": int(result.get("quota", 0) * 500000),  # 剩余额度（原始单位）
                    "quota_usd": result.get("quota", 0),  # 剩余额度（美元）
                    "used_quota": int(result.get("used_quota", 0) * 500000),  # 已用（原始单位）
                    "used_usd": result.get("used_quota", 0),  # 已用（美元）
                    "request_count": 0,
                    "status": "ok" if result.get("success") else "error",
                    "last_checkin": result.get("timestamp"),
                    "checkin_message": result.get("message", "")
                }
                balances_data["accounts"].append(account_balance)
                balances_data["total_quota_usd"] += result.get("quota", 0)  # 剩余额度总和
                balances_data["total_used_usd"] += result.get("used_quota", 0)

        # 写入文件
        with open(BALANCES_FILE, "w", encoding="utf-8") as f:
            json.dump(balances_data, f, indent=2, ensure_ascii=False)

        logger.info(f"Balances saved to {BALANCES_FILE}: {len(balances_data['accounts'])} accounts, total ${balances_data['total_quota_usd']:.2f}")

    except Exception as e:
        logger.error(f"Failed to save balances to file: {e}")


def get_checkin_status() -> dict:
    """获取签到状态"""
    return checkin_status


# 需要导入 asyncio
import asyncio
