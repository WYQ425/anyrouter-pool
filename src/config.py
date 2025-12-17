"""
配置管理模块
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel


class AccountConfig(BaseModel):
    """单个账号配置"""
    name: str
    cookies: dict[str, str] | str
    api_user: str
    api_key: str = ""  # AnyRouter API Token (用于 API 调用)
    provider: str = "anyrouter"
    enabled: bool = True

    def get_cookies_dict(self) -> dict[str, str]:
        """获取 cookies 字典"""
        if isinstance(self.cookies, dict):
            return self.cookies
        # 解析字符串格式的 cookies
        cookies_dict = {}
        for cookie in self.cookies.split(';'):
            if '=' in cookie:
                key, value = cookie.strip().split('=', 1)
                cookies_dict[key] = value
        return cookies_dict


class ProviderConfig(BaseModel):
    """服务提供商配置"""
    name: str
    domain: str
    login_path: str = "/login"
    sign_in_path: str | None = "/api/user/sign_in"
    user_info_path: str = "/api/user/self"
    api_user_key: str = "new-api-user"
    bypass_method: Literal["waf_cookies"] | None = "waf_cookies"
    waf_cookie_names: list[str] = field(default_factory=lambda: ["acw_tc", "cdn_sec_tc", "acw_sc__v2"])


class NewAPIConfig(BaseModel):
    """NewAPI 配置"""
    url: str = "http://localhost:3000"
    admin_token: str = ""
    user_id: str = "1"


class NotifyConfig(BaseModel):
    """通知配置"""
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    dingtalk_webhook: str | None = None
    feishu_webhook: str | None = None


class AppConfig(BaseModel):
    """应用配置"""
    # 定时任务配置
    cron_signin: str = "0 9 * * *"  # 每天 9:00 签到
    cron_sync: str = "*/5 * * * *"   # 每 5 分钟同步一次

    # 余额阈值配置
    balance_warning_threshold: float = 5.0   # 警告阈值 $5
    balance_critical_threshold: float = 1.0  # 临界阈值 $1
    balance_disable_threshold: float = 0.5   # 禁用阈值 $0.5

    # NewAPI 配置
    newapi: NewAPIConfig = NewAPIConfig()

    # 通知配置
    notify: NotifyConfig = NotifyConfig()

    # 服务提供商配置
    providers: dict[str, ProviderConfig] = {}

    # 账号配置
    accounts: list[AccountConfig] = []

    @classmethod
    def load_from_env(cls) -> "AppConfig":
        """从环境变量加载配置"""
        config = cls()

        # 加载 NewAPI 配置
        config.newapi.url = os.getenv("NEWAPI_URL", "http://localhost:3000")
        config.newapi.admin_token = os.getenv("NEWAPI_TOKEN", "")
        config.newapi.user_id = os.getenv("NEWAPI_USER_ID", "1")

        # 加载定时任务配置
        config.cron_signin = os.getenv("CRON_SIGNIN", "0 9 * * *")
        config.cron_sync = os.getenv("CRON_SYNC", "*/5 * * * *")

        # 加载阈值配置
        config.balance_warning_threshold = float(os.getenv("BALANCE_WARNING_THRESHOLD", "5.0"))
        config.balance_critical_threshold = float(os.getenv("BALANCE_CRITICAL_THRESHOLD", "1.0"))
        config.balance_disable_threshold = float(os.getenv("BALANCE_DISABLE_THRESHOLD", "0.5"))

        # 加载通知配置
        config.notify.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        config.notify.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        config.notify.dingtalk_webhook = os.getenv("DINGTALK_WEBHOOK")
        config.notify.feishu_webhook = os.getenv("FEISHU_WEBHOOK")

        # 加载默认 providers
        config.providers = {
            "anyrouter": ProviderConfig(
                name="anyrouter",
                domain="https://anyrouter.top",
                login_path="/login",
                sign_in_path="/api/user/sign_in",
                user_info_path="/api/user/self",
                api_user_key="new-api-user",
                bypass_method="waf_cookies",
                waf_cookie_names=["acw_tc", "cdn_sec_tc", "acw_sc__v2"],
            ),
        }

        # 加载账号配置
        accounts_file = Path(os.getenv("ACCOUNTS_FILE", "/app/data/accounts.json"))
        if accounts_file.exists():
            try:
                with open(accounts_file, "r", encoding="utf-8") as f:
                    accounts_data = json.load(f)
                    if isinstance(accounts_data, list):
                        for i, acc in enumerate(accounts_data):
                            if not acc.get("name"):
                                acc["name"] = f"Account_{i+1}"
                            config.accounts.append(AccountConfig(**acc))
            except Exception as e:
                print(f"[ERROR] 加载账号配置失败: {e}")

        return config

    def get_provider(self, name: str) -> ProviderConfig | None:
        """获取指定 provider 配置"""
        return self.providers.get(name)


# 全局配置实例
_config: AppConfig | None = None


def get_config() -> AppConfig:
    """获取配置实例"""
    global _config
    if _config is None:
        _config = AppConfig.load_from_env()
    return _config


def reload_config() -> AppConfig:
    """重新加载配置"""
    global _config
    _config = AppConfig.load_from_env()
    return _config
