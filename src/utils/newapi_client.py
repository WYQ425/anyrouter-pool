"""
NewAPI 客户端
用于与 NewAPI 服务交互，管理渠道
"""

from typing import Any

import httpx
from loguru import logger

from config import NewAPIConfig


class NewAPIClient:
    """NewAPI API 客户端"""

    def __init__(self, config: NewAPIConfig):
        self.config = config
        self.base_url = config.url.rstrip("/")
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.admin_token}",
            "New-Api-User": config.user_id,
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        json_data: dict | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
        """发送请求到 NewAPI"""
        url = f"{self.base_url}{endpoint}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    headers=self.headers,
                    json=json_data,
                    params=params,
                )

                if response.status_code == 200:
                    return response.json()
                else:
                    logger.error(f"NewAPI 请求失败: {response.status_code} - {response.text}")
                    return {"success": False, "message": f"HTTP {response.status_code}"}
        except Exception as e:
            logger.error(f"NewAPI 请求异常: {e}")
            return {"success": False, "message": str(e)}

    async def get_channels(
        self, page: int = 1, page_size: int = 100
    ) -> dict[str, Any]:
        """获取渠道列表"""
        params = {
            "p": page,
            "page_size": page_size,
            "id_sort": "false",
            "tag_mode": "false",
        }
        return await self._request("GET", "/api/channel/", params=params)

    async def get_channel_by_id(self, channel_id: int) -> dict[str, Any]:
        """获取单个渠道详情"""
        return await self._request("GET", f"/api/channel/{channel_id}")

    async def create_channel(self, channel_data: dict) -> dict[str, Any]:
        """创建渠道"""
        data = {
            "mode": "single",
            "channel": channel_data,
        }
        return await self._request("POST", "/api/channel/", json_data=data)

    async def update_channel(self, channel_data: dict) -> dict[str, Any]:
        """更新渠道"""
        return await self._request("PUT", "/api/channel/", json_data=channel_data)

    async def delete_channel(self, channel_id: int) -> dict[str, Any]:
        """删除渠道"""
        return await self._request("DELETE", f"/api/channel/{channel_id}")

    async def enable_channel(self, channel_id: int) -> dict[str, Any]:
        """启用渠道"""
        return await self.update_channel({"id": channel_id, "status": 1})

    async def disable_channel(self, channel_id: int) -> dict[str, Any]:
        """禁用渠道"""
        return await self.update_channel({"id": channel_id, "status": 2})

    async def enable_channels_by_tag(self, tag: str) -> dict[str, Any]:
        """按标签批量启用渠道"""
        return await self._request(
            "POST", "/api/channel/tag/enabled", json_data={"tag": tag}
        )

    async def disable_channels_by_tag(self, tag: str) -> dict[str, Any]:
        """按标签批量禁用渠道"""
        return await self._request(
            "POST", "/api/channel/tag/disabled", json_data={"tag": tag}
        )

    async def update_channel_balance(self, channel_id: int) -> dict[str, Any]:
        """更新渠道余额"""
        return await self._request("GET", f"/api/channel/update_balance/{channel_id}")

    async def batch_update_balance(self) -> dict[str, Any]:
        """批量更新所有渠道余额"""
        return await self._request("GET", "/api/channel/update_balance")

    async def set_channel_weight(self, channel_id: int, weight: int) -> dict[str, Any]:
        """设置渠道权重"""
        return await self.update_channel({"id": channel_id, "weight": weight})

    async def set_channel_priority(
        self, channel_id: int, priority: int
    ) -> dict[str, Any]:
        """设置渠道优先级"""
        return await self.update_channel({"id": channel_id, "priority": priority})

    async def search_channels(
        self, keyword: str = "", tag: str = "", status: str = ""
    ) -> dict[str, Any]:
        """搜索渠道"""
        params = {
            "keyword": keyword,
            "p": 1,
            "page_size": 100,
        }
        if tag:
            params["tag"] = tag
        if status:
            params["status"] = status
        return await self._request("GET", "/api/channel/search", params=params)

    async def get_status(self) -> dict[str, Any]:
        """获取 NewAPI 状态"""
        return await self._request("GET", "/api/status")

    async def test_connection(self) -> bool:
        """测试连接"""
        try:
            result = await self.get_status()
            return result.get("success", False) or "data" in result
        except Exception:
            return False


class ChannelManager:
    """渠道管理器 - 高级封装"""

    def __init__(self, client: NewAPIClient):
        self.client = client
        self._channel_cache: dict[str, dict] = {}  # name -> channel_data

    async def refresh_cache(self):
        """刷新渠道缓存"""
        result = await self.client.get_channels(page_size=500)
        if result.get("success") and "data" in result:
            channels = result["data"]
            if isinstance(channels, list):
                self._channel_cache = {ch["name"]: ch for ch in channels}
                logger.info(f"已加载 {len(self._channel_cache)} 个渠道到缓存")

    def get_channel_by_name(self, name: str) -> dict | None:
        """根据名称获取渠道"""
        return self._channel_cache.get(name)

    async def ensure_channel_exists(
        self,
        name: str,
        api_key: str,
        base_url: str,
        models: list[str],
        channel_type: int = 1,  # 1 = OpenAI 兼容
        groups: list[str] | None = None,
        priority: int = 0,
        weight: int = 100,
        tag: str = "",
    ) -> dict[str, Any]:
        """确保渠道存在，不存在则创建"""
        existing = self.get_channel_by_name(name)

        if existing:
            logger.info(f"渠道 '{name}' 已存在 (ID: {existing['id']})")
            return {"success": True, "channel": existing, "created": False}

        # 创建新渠道
        channel_data = {
            "name": name,
            "type": channel_type,
            "key": api_key,
            "base_url": base_url,
            "models": ",".join(models),
            "groups": groups or ["default"],
            "priority": priority,
            "weight": weight,
        }

        if tag:
            channel_data["tag"] = tag

        result = await self.client.create_channel(channel_data)
        if result.get("success"):
            logger.info(f"成功创建渠道 '{name}'")
            # 刷新缓存
            await self.refresh_cache()
            return {"success": True, "channel": self.get_channel_by_name(name), "created": True}
        else:
            logger.error(f"创建渠道 '{name}' 失败: {result.get('message')}")
            return {"success": False, "message": result.get("message")}

    async def sync_channel_status(
        self, name: str, balance: float, thresholds: dict
    ) -> dict[str, Any]:
        """根据余额同步渠道状态"""
        channel = self.get_channel_by_name(name)
        if not channel:
            return {"success": False, "message": f"渠道 '{name}' 不存在"}

        channel_id = channel["id"]
        current_status = channel.get("status", 1)

        disable_threshold = thresholds.get("disable", 0.5)
        enable_threshold = thresholds.get("enable", 1.0)

        if balance < disable_threshold and current_status == 1:
            # 余额过低，禁用渠道
            result = await self.client.disable_channel(channel_id)
            if result.get("success"):
                logger.warning(f"渠道 '{name}' 余额过低 (${balance})，已禁用")
                return {"success": True, "action": "disabled", "balance": balance}
            return {"success": False, "message": "禁用渠道失败"}

        elif balance >= enable_threshold and current_status != 1:
            # 余额恢复，启用渠道
            result = await self.client.enable_channel(channel_id)
            if result.get("success"):
                logger.info(f"渠道 '{name}' 余额恢复 (${balance})，已启用")
                return {"success": True, "action": "enabled", "balance": balance}
            return {"success": False, "message": "启用渠道失败"}

        return {"success": True, "action": "no_change", "balance": balance}

    async def update_channel_weight_by_balance(
        self, name: str, balance: float, max_balance: float = 100.0
    ) -> dict[str, Any]:
        """根据余额动态调整渠道权重"""
        channel = self.get_channel_by_name(name)
        if not channel:
            return {"success": False, "message": f"渠道 '{name}' 不存在"}

        # 计算权重：余额越高权重越大 (10-100)
        weight = max(10, min(100, int((balance / max_balance) * 100)))

        result = await self.client.set_channel_weight(channel["id"], weight)
        if result.get("success"):
            logger.debug(f"渠道 '{name}' 权重已更新为 {weight} (余额: ${balance})")
            return {"success": True, "weight": weight}

        return {"success": False, "message": "更新权重失败"}
