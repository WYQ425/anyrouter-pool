"""
余额监控服务
定时从 AnyRouter 获取各账号余额
自动处理 WAF cookie 获取
"""

import asyncio
import tempfile
from typing import Any

import httpx
from loguru import logger
from playwright.async_api import async_playwright

from config import AccountConfig, AppConfig, ProviderConfig


class WafCookieManager:
    """WAF Cookie 管理器 - 使用 Playwright 获取 WAF cookies"""

    WAF_COOKIE_NAMES = ["acw_tc", "cdn_sec_tc", "acw_sc__v2"]

    def __init__(self):
        self._cookie_cache: dict[str, dict[str, str]] = {}  # domain -> cookies
        self._cache_time: dict[str, float] = {}
        self._cache_ttl = 300  # 5 分钟缓存

    async def get_waf_cookies(self, domain: str, login_path: str = "/login") -> dict[str, str]:
        """获取 WAF cookies"""
        import time

        # 检查缓存
        cache_key = domain
        if cache_key in self._cookie_cache:
            if time.time() - self._cache_time.get(cache_key, 0) < self._cache_ttl:
                logger.debug(f"使用缓存的 WAF cookies: {domain}")
                return self._cookie_cache[cache_key]

        logger.info(f"获取 WAF cookies: {domain}")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
            )

            page = await context.new_page()

            try:
                login_url = f"{domain}{login_path}"
                await page.goto(login_url, wait_until="networkidle", timeout=30000)

                # 等待 JavaScript 执行完成
                await page.wait_for_timeout(3000)

                # 获取 cookies
                cookies = await context.cookies()

                waf_cookies = {}
                for cookie in cookies:
                    if cookie["name"] in self.WAF_COOKIE_NAMES:
                        waf_cookies[cookie["name"]] = cookie["value"]

                if waf_cookies:
                    logger.info(f"成功获取 {len(waf_cookies)} 个 WAF cookies")
                    self._cookie_cache[cache_key] = waf_cookies
                    self._cache_time[cache_key] = time.time()
                else:
                    logger.warning("未获取到 WAF cookies")

                return waf_cookies

            except Exception as e:
                logger.error(f"获取 WAF cookies 失败: {e}")
                return {}
            finally:
                await browser.close()


class BalanceService:
    """余额监控服务"""

    def __init__(self, config: AppConfig):
        self.config = config
        self._balance_cache: dict[str, dict[str, Any]] = {}
        self._waf_manager = WafCookieManager()

    def _get_provider(self, provider_name: str) -> ProviderConfig | None:
        """获取 provider 配置"""
        return self.config.providers.get(provider_name)

    def _build_headers(self, account: AccountConfig, provider: ProviderConfig) -> dict[str, str]:
        """构建请求头"""
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": provider.domain,
            "Origin": provider.domain,
            provider.api_user_key: account.api_user,
        }

    async def fetch_account_balance(self, account: AccountConfig, waf_cookies: dict[str, str] | None = None) -> dict[str, Any]:
        """获取单个账号余额"""
        provider = self._get_provider(account.provider)
        if not provider:
            logger.error(f"[{account.name}] Provider '{account.provider}' 不存在")
            return {"success": False, "error": "Provider not found", "account_name": account.name}

        headers = self._build_headers(account, provider)

        # 合并 cookies: WAF cookies + 用户 session cookie
        cookies = {}
        if waf_cookies:
            cookies.update(waf_cookies)
        cookies.update(account.get_cookies_dict())

        user_info_url = f"{provider.domain}{provider.user_info_path}"

        try:
            async with httpx.AsyncClient(http2=True, timeout=30.0, cookies=cookies) as client:
                response = await client.get(user_info_url, headers=headers)

                # 检查是否被 WAF 拦截
                content_type = response.headers.get("content-type", "")
                if "text/html" in content_type and "<script>" in response.text[:200]:
                    logger.warning(f"[{account.name}] 被 WAF 拦截，需要刷新 WAF cookies")
                    return {
                        "success": False,
                        "error": "WAF blocked",
                        "waf_blocked": True,
                        "account_name": account.name,
                    }

                if response.status_code == 200:
                    try:
                        data = response.json()
                    except Exception:
                        logger.error(f"[{account.name}] 响应不是有效的 JSON")
                        return {"success": False, "error": "Invalid JSON response", "account_name": account.name}

                    if data.get("success"):
                        user_data = data.get("data", {})
                        quota = round(user_data.get("quota", 0) / 500000, 2)
                        used_quota = round(user_data.get("used_quota", 0) / 500000, 2)

                        result = {
                            "success": True,
                            "balance": quota,
                            "used": used_quota,
                            "account_name": account.name,
                        }

                        self._balance_cache[account.name] = result
                        logger.info(f"[{account.name}] 余额: ${quota}, 已使用: ${used_quota}")
                        return result
                    else:
                        error_msg = data.get("message", "API 返回失败")
                        logger.warning(f"[{account.name}] 获取余额失败: {error_msg}")
                        return {"success": False, "error": error_msg, "account_name": account.name}

                if response.status_code in [401, 403]:
                    logger.error(f"[{account.name}] Cookie 可能已失效 (HTTP {response.status_code})")
                    return {
                        "success": False,
                        "error": "Cookie expired",
                        "cookie_expired": True,
                        "account_name": account.name,
                    }

                logger.warning(f"[{account.name}] HTTP {response.status_code}")
                return {"success": False, "error": f"HTTP {response.status_code}", "account_name": account.name}

        except httpx.TimeoutException:
            logger.error(f"[{account.name}] 请求超时")
            return {"success": False, "error": "Timeout", "account_name": account.name}
        except Exception as e:
            logger.error(f"[{account.name}] 获取余额异常: {e}")
            return {"success": False, "error": str(e), "account_name": account.name}

    async def fetch_all_balances(self) -> list[dict[str, Any]]:
        """获取所有账号余额"""
        enabled_accounts = [acc for acc in self.config.accounts if acc.enabled]
        logger.info(f"开始获取 {len(enabled_accounts)} 个账号的余额...")

        # 按 provider 分组
        provider_accounts: dict[str, list[AccountConfig]] = {}
        for account in enabled_accounts:
            provider_name = account.provider
            if provider_name not in provider_accounts:
                provider_accounts[provider_name] = []
            provider_accounts[provider_name].append(account)

        all_results = []

        # 每个 provider 获取一次 WAF cookies，然后批量查询账号
        for provider_name, accounts in provider_accounts.items():
            provider = self._get_provider(provider_name)
            if not provider:
                logger.error(f"Provider '{provider_name}' 不存在")
                continue

            # 获取 WAF cookies
            waf_cookies = await self._waf_manager.get_waf_cookies(
                provider.domain, provider.login_path
            )

            # 并发获取该 provider 下所有账号余额
            tasks = [
                self.fetch_account_balance(account, waf_cookies)
                for account in accounts
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"获取余额异常: {result}")
                else:
                    all_results.append(result)

        # 统计
        success_count = sum(1 for r in all_results if r.get("success"))
        total_balance = sum(r.get("balance", 0) for r in all_results if r.get("success"))

        logger.info(f"余额获取完成: {success_count}/{len(all_results)} 成功, 总余额: ${total_balance:.2f}")

        return all_results

    def get_cached_balance(self, account_name: str) -> dict[str, Any] | None:
        """获取缓存的余额信息"""
        return self._balance_cache.get(account_name)

    def get_all_cached_balances(self) -> dict[str, dict[str, Any]]:
        """获取所有缓存的余额信息"""
        return self._balance_cache.copy()

    def get_total_balance(self) -> float:
        """获取总余额"""
        return sum(
            info.get("balance", 0)
            for info in self._balance_cache.values()
            if info.get("success")
        )

    def get_low_balance_accounts(self, threshold: float) -> list[dict[str, Any]]:
        """获取余额低于阈值的账号"""
        return [
            info
            for info in self._balance_cache.values()
            if info.get("success") and info.get("balance", 0) < threshold
        ]

    def get_expired_cookie_accounts(self) -> list[str]:
        """获取 Cookie 过期的账号"""
        return [
            info.get("account_name")
            for info in self._balance_cache.values()
            if info.get("cookie_expired")
        ]
