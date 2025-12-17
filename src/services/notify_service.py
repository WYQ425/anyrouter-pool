"""
通知服务
支持 Telegram、钉钉、飞书等通知渠道
"""

import httpx
from loguru import logger

from config import NotifyConfig


class NotifyService:
    """通知服务"""

    def __init__(self, config: NotifyConfig):
        self.config = config

    async def send_telegram(self, title: str, content: str) -> bool:
        """发送 Telegram 通知"""
        if not self.config.telegram_bot_token or not self.config.telegram_chat_id:
            return False

        message = f"<b>{title}</b>\n\n{content}"
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        data = {
            "chat_id": self.config.telegram_chat_id,
            "text": message,
            "parse_mode": "HTML",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=data)
                if response.status_code == 200:
                    logger.info("[Telegram] 通知发送成功")
                    return True
                else:
                    logger.error(f"[Telegram] 发送失败: {response.status_code}")
                    return False
        except Exception as e:
            logger.error(f"[Telegram] 发送异常: {e}")
            return False

    async def send_dingtalk(self, title: str, content: str) -> bool:
        """发送钉钉通知"""
        if not self.config.dingtalk_webhook:
            return False

        data = {"msgtype": "text", "text": {"content": f"{title}\n{content}"}}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(self.config.dingtalk_webhook, json=data)
                if response.status_code == 200:
                    logger.info("[钉钉] 通知发送成功")
                    return True
                else:
                    logger.error(f"[钉钉] 发送失败: {response.status_code}")
                    return False
        except Exception as e:
            logger.error(f"[钉钉] 发送异常: {e}")
            return False

    async def send_feishu(self, title: str, content: str) -> bool:
        """发送飞书通知"""
        if not self.config.feishu_webhook:
            return False

        data = {
            "msg_type": "interactive",
            "card": {
                "elements": [
                    {"tag": "markdown", "content": content, "text_align": "left"}
                ],
                "header": {
                    "template": "blue",
                    "title": {"content": title, "tag": "plain_text"},
                },
            },
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(self.config.feishu_webhook, json=data)
                if response.status_code == 200:
                    logger.info("[飞书] 通知发送成功")
                    return True
                else:
                    logger.error(f"[飞书] 发送失败: {response.status_code}")
                    return False
        except Exception as e:
            logger.error(f"[飞书] 发送异常: {e}")
            return False

    async def push_message(self, title: str, content: str) -> None:
        """推送消息到所有配置的渠道"""
        # 尝试所有配置的通知渠道
        await self.send_telegram(title, content)
        await self.send_dingtalk(title, content)
        await self.send_feishu(title, content)

    async def send_balance_alert(
        self, account_name: str, balance: float, threshold: float, level: str = "warning"
    ) -> None:
        """发送余额告警"""
        emoji = "⚠️" if level == "warning" else "🚨"
        title = f"{emoji} AnyRouter 余额告警"
        content = (
            f"账号: {account_name}\n"
            f"当前余额: ${balance:.2f}\n"
            f"告警阈值: ${threshold:.2f}\n"
            f"级别: {level.upper()}"
        )
        await self.push_message(title, content)

    async def send_channel_status_change(
        self, channel_name: str, action: str, balance: float
    ) -> None:
        """发送渠道状态变更通知"""
        emoji = "✅" if action == "enabled" else "🔴"
        title = f"{emoji} 渠道状态变更"
        content = (
            f"渠道: {channel_name}\n"
            f"操作: {action}\n"
            f"当前余额: ${balance:.2f}"
        )
        await self.push_message(title, content)

    async def send_daily_report(self, report: dict) -> None:
        """发送每日报告"""
        title = "📊 AnyRouter Keeper 每日报告"

        total_balance = report.get("total_balance", 0)
        active_channels = report.get("active_channels", 0)
        total_channels = report.get("total_channels", 0)

        content = (
            f"总余额: ${total_balance:.2f}\n"
            f"活跃渠道: {active_channels}/{total_channels}\n"
            f"---\n"
        )

        # 添加各账号余额明细
        for acc in report.get("accounts", []):
            status = "✅" if acc.get("enabled") else "🔴"
            content += f"{status} {acc['name']}: ${acc['balance']:.2f}\n"

        await self.push_message(title, content)
