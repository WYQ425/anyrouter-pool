"""
渠道同步服务
将 AnyRouter 账号同步到 NewAPI 作为渠道，并根据余额动态管理
"""

from typing import Any

from loguru import logger

from config import AccountConfig, AppConfig
from utils.newapi_client import ChannelManager, NewAPIClient


class ChannelSyncService:
    """渠道同步服务"""

    # AnyRouter 支持的模型列表
    ANYROUTER_MODELS = [
        # Claude 4 系列 (最新)
        "claude-opus-4-5-20251101",
        "claude-sonnet-4-5-20250929",
        "claude-sonnet-4-20250514",
        "claude-opus-4-20250514",
        # Claude 3.5 系列
        "claude-3-5-sonnet-20241022",
        "claude-3-5-sonnet-20240620",
        "claude-3-5-haiku-20241022",
        # Claude 3 系列
        "claude-3-opus-20240229",
        "claude-3-sonnet-20240229",
        "claude-3-haiku-20240307",
    ]

    def __init__(self, config: AppConfig):
        self.config = config
        self.client = NewAPIClient(config.newapi)
        self.channel_manager = ChannelManager(self.client)
        self._synced_channels: dict[str, int] = {}  # account_name -> channel_id

    async def initialize(self) -> bool:
        """初始化服务，测试连接并加载渠道缓存"""
        logger.info("初始化渠道同步服务...")

        # 测试 NewAPI 连接
        if not await self.client.test_connection():
            logger.error("无法连接到 NewAPI，请检查配置")
            return False

        logger.info("NewAPI 连接成功")

        # 加载渠道缓存
        await self.channel_manager.refresh_cache()

        return True

    def _get_channel_name(self, account: AccountConfig) -> str:
        """生成渠道名称"""
        return f"anyrouter_{account.name}"

    def _get_channel_tag(self) -> str:
        """获取渠道标签"""
        return "anyrouter-keeper"

    async def sync_account_to_channel(
        self, account: AccountConfig, balance: float
    ) -> dict[str, Any]:
        """将单个账号同步为 NewAPI 渠道"""
        channel_name = self._get_channel_name(account)

        logger.info(f"同步账号 '{account.name}' -> 渠道 '{channel_name}'")

        # 优先使用 api_key，没有则使用 session cookie
        api_key = account.api_key
        if not api_key:
            cookies = account.get_cookies_dict()
            api_key = cookies.get("session", "")
            if api_key:
                logger.warning(f"[{account.name}] 未配置 api_key，使用 session cookie (可能无法用于 API 调用)")

        if not api_key:
            logger.error(f"[{account.name}] 缺少 api_key 和 session cookie")
            return {"success": False, "error": "Missing api_key"}

        # 获取 provider 配置
        provider = self.config.providers.get(account.provider)
        if not provider:
            logger.error(f"[{account.name}] Provider '{account.provider}' 不存在")
            return {"success": False, "error": "Provider not found"}

        # 确保渠道存在
        result = await self.channel_manager.ensure_channel_exists(
            name=channel_name,
            api_key=api_key,
            base_url=provider.domain,
            models=self.ANYROUTER_MODELS,
            channel_type=1,  # OpenAI 兼容类型
            groups=["default"],
            priority=0,
            weight=100,
            tag=self._get_channel_tag(),
        )

        if result.get("success"):
            channel = result.get("channel")
            if channel:
                self._synced_channels[account.name] = channel["id"]

                # 根据余额同步渠道状态
                await self.sync_channel_status(account.name, balance)

                # 根据余额更新权重
                await self.update_channel_weight(account.name, balance)

        return result

    async def sync_all_accounts(
        self, balance_info: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """同步所有账号到 NewAPI"""
        logger.info(f"开始同步 {len(self.config.accounts)} 个账号...")

        results = {
            "success": 0,
            "failed": 0,
            "details": [],
        }

        for account in self.config.accounts:
            if not account.enabled:
                continue

            # 获取该账号的余额
            acc_balance = balance_info.get(account.name, {})
            balance = acc_balance.get("balance", 0) if acc_balance.get("success") else 0

            result = await self.sync_account_to_channel(account, balance)

            if result.get("success"):
                results["success"] += 1
            else:
                results["failed"] += 1

            results["details"].append({
                "account": account.name,
                "result": result,
            })

        logger.info(f"同步完成: {results['success']} 成功, {results['failed']} 失败")
        return results

    async def sync_channel_status(self, account_name: str, balance: float) -> dict[str, Any]:
        """根据余额同步渠道状态（启用/禁用）"""
        channel_name = self._get_channel_name(
            next((a for a in self.config.accounts if a.name == account_name), None)
            or AccountConfig(name=account_name, cookies={}, api_user="")
        )

        thresholds = {
            "disable": self.config.balance_disable_threshold,
            "enable": self.config.balance_critical_threshold,
        }

        result = await self.channel_manager.sync_channel_status(
            channel_name, balance, thresholds
        )

        if result.get("action") in ["enabled", "disabled"]:
            logger.info(
                f"渠道 '{channel_name}' 状态变更: {result['action']} (余额: ${balance})"
            )

        return result

    async def update_channel_weight(self, account_name: str, balance: float) -> dict[str, Any]:
        """根据余额更新渠道权重"""
        channel_name = self._get_channel_name(
            next((a for a in self.config.accounts if a.name == account_name), None)
            or AccountConfig(name=account_name, cookies={}, api_user="")
        )

        # 计算最大可能余额（用于归一化）
        max_balance = 50.0  # 假设单账号最大余额为 $50

        return await self.channel_manager.update_channel_weight_by_balance(
            channel_name, balance, max_balance
        )

    async def disable_account_channel(self, account_name: str) -> dict[str, Any]:
        """禁用指定账号的渠道"""
        channel_id = self._synced_channels.get(account_name)
        if not channel_id:
            # 尝试从缓存获取
            channel_name = f"anyrouter_{account_name}"
            channel = self.channel_manager.get_channel_by_name(channel_name)
            if channel:
                channel_id = channel["id"]
            else:
                return {"success": False, "error": "Channel not found"}

        result = await self.client.disable_channel(channel_id)
        if result.get("success"):
            logger.warning(f"已禁用渠道: {account_name}")
        return result

    async def enable_account_channel(self, account_name: str) -> dict[str, Any]:
        """启用指定账号的渠道"""
        channel_id = self._synced_channels.get(account_name)
        if not channel_id:
            channel_name = f"anyrouter_{account_name}"
            channel = self.channel_manager.get_channel_by_name(channel_name)
            if channel:
                channel_id = channel["id"]
            else:
                return {"success": False, "error": "Channel not found"}

        result = await self.client.enable_channel(channel_id)
        if result.get("success"):
            logger.info(f"已启用渠道: {account_name}")
        return result

    async def get_channel_status_report(self) -> dict[str, Any]:
        """获取渠道状态报告"""
        await self.channel_manager.refresh_cache()

        report = {
            "total": 0,
            "enabled": 0,
            "disabled": 0,
            "channels": [],
        }

        for account in self.config.accounts:
            channel_name = self._get_channel_name(account)
            channel = self.channel_manager.get_channel_by_name(channel_name)

            if channel:
                report["total"] += 1
                status = channel.get("status", 0)
                if status == 1:
                    report["enabled"] += 1
                else:
                    report["disabled"] += 1

                report["channels"].append({
                    "name": channel_name,
                    "account": account.name,
                    "status": "enabled" if status == 1 else "disabled",
                    "weight": channel.get("weight", 0),
                    "priority": channel.get("priority", 0),
                })

        return report
