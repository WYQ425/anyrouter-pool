"""
AnyRouter API 客户端
用于与 AnyRouter 服务交互，获取余额等信息
注意：签到功能已由外部实现，本模块只负责余额查询
"""

from typing import Any

import httpx
from loguru import logger

from config import AccountConfig, ProviderConfig


class AnyRouterClient:
    """AnyRouter API 客户端"""

    def __init__(self, provider: ProviderConfig):
        self.provider = provider
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.0.0 Safari/537.36"
        )

    def _build_headers(self, account: AccountConfig) -> dict[str, str]:
        """构建请求头"""
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Referer": self.provider.domain,
            "Origin": self.provider.domain,
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            self.provider.api_user_key: account.api_user,
        }

    async def get_user_info(self, account: AccountConfig) -> dict[str, Any]:
        """获取用户信息（包含余额）"""
        account_name = account.name
        cookies = account.get_cookies_dict()
        headers = self._build_headers(account)
        user_info_url = f"{self.provider.domain}{self.provider.user_info_path}"

        try:
            async with httpx.AsyncClient(http2=True, timeout=30.0, cookies=cookies) as client:
                response = await client.get(user_info_url, headers=headers)

                if response.status_code == 200:
                    data = response.json()
                    if data.get("success"):
                        user_data = data.get("data", {})
                        quota = round(user_data.get("quota", 0) / 500000, 2)
                        used_quota = round(user_data.get("used_quota", 0) / 500000, 2)
                        return {
                            "success": True,
                            "quota": quota,
                            "used_quota": used_quota,
                            "raw": user_data,
                        }

                    # API 返回 success: false
                    error_msg = data.get("message", "Unknown error")
                    return {"success": False, "error": error_msg}

                # HTTP 错误
                if response.status_code in [401, 403]:
                    logger.warning(f"[{account_name}] Cookie 可能已失效")
                    return {
                        "success": False,
                        "error": "Cookie expired",
                        "cookie_expired": True,
                    }

                return {
                    "success": False,
                    "error": f"HTTP {response.status_code}",
                }

        except httpx.TimeoutException:
            logger.error(f"[{account_name}] 请求超时")
            return {"success": False, "error": "Timeout"}
        except Exception as e:
            logger.error(f"[{account_name}] 获取用户信息异常: {e}")
            return {"success": False, "error": str(e)}
