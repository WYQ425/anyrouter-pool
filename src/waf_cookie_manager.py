"""
WAF Cookie 管理器 - 高性能、高可用的 Cookie 缓存系统

特性:
- 内存缓存，可配置 TTL
- 预刷新机制，用户无感知
- 并发安全，同时只有一个刷新操作
- 完善的状态监控
- 自动错误恢复
"""

import asyncio
import os
import time
from datetime import datetime
from typing import Dict, Optional
from enum import Enum

from loguru import logger

from browser_manager import browser_manager


class CookieState(Enum):
    """Cookie 状态"""
    EMPTY = "empty"           # 未初始化
    VALID = "valid"           # 有效
    EXPIRING = "expiring"     # 即将过期（可触发预刷新）
    EXPIRED = "expired"       # 已过期
    REFRESHING = "refreshing" # 正在刷新中


# ============== 配置 ==============
# WAF Cookie 缓存时间（秒）- 优化至 45 分钟，阿里云 WAF Cookie 实际有效期通常 1-2 小时
WAF_COOKIE_TTL = int(os.getenv("WAF_COOKIE_TTL", "2700"))  # 默认 45 分钟

# 预刷新时间（过期前多少秒开始刷新）- 优化至 10 分钟预刷新
WAF_COOKIE_REFRESH_BEFORE = int(os.getenv("WAF_COOKIE_REFRESH_BEFORE", "600"))  # 默认 10 分钟

# 刷新失败后的重试间隔（秒）
WAF_COOKIE_RETRY_INTERVAL = int(os.getenv("WAF_COOKIE_RETRY_INTERVAL", "30"))

# WAF 登录页面 URL
WAF_LOGIN_URL = os.getenv("WAF_LOGIN_URL", "https://anyrouter.top/login")

# 页面加载后等待时间（毫秒）- 配合资源拦截优化，可降低等待时间
WAF_PAGE_WAIT_MS = int(os.getenv("WAF_PAGE_WAIT_MS", "3000"))


class WAFCookieManager:
    """
    WAF Cookie 管理器

    负责获取和缓存 Cloudflare WAF Cookie，确保：
    1. 高效：使用缓存，避免频繁刷新
    2. 可靠：预刷新机制，用户无感知
    3. 安全：并发控制，防止资源耗尽
    """

    def __init__(self):
        # Cookie 缓存
        self._cookies: Dict[str, str] = {}
        self._expire_time: float = 0

        # 并发控制
        self._refresh_lock = asyncio.Lock()
        self._is_refreshing = False

        # 用于等待刷新完成的条件变量
        self._refresh_condition: Optional[asyncio.Condition] = None

        # 预刷新任务
        self._pre_refresh_task: Optional[asyncio.Task] = None

        # 统计信息
        self._stats = {
            "total_refreshes": 0,
            "successful_refreshes": 0,
            "failed_refreshes": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "last_refresh_time": None,
            "last_refresh_duration": 0,
            "last_error": None,
        }

    def _get_condition(self) -> asyncio.Condition:
        """获取或创建条件变量（延迟初始化以支持不同事件循环）"""
        if self._refresh_condition is None:
            self._refresh_condition = asyncio.Condition()
        return self._refresh_condition

    @property
    def state(self) -> CookieState:
        """获取当前 Cookie 状态"""
        if self._is_refreshing:
            return CookieState.REFRESHING

        if not self._cookies:
            return CookieState.EMPTY

        now = time.time()
        if now >= self._expire_time:
            return CookieState.EXPIRED

        if now >= self._expire_time - WAF_COOKIE_REFRESH_BEFORE:
            return CookieState.EXPIRING

        return CookieState.VALID

    @property
    def cookies(self) -> Dict[str, str]:
        """获取当前缓存的 Cookie（可能已过期）"""
        return self._cookies.copy()

    @property
    def is_valid(self) -> bool:
        """Cookie 是否有效"""
        return self.state in (CookieState.VALID, CookieState.EXPIRING)

    @property
    def ttl(self) -> float:
        """Cookie 剩余有效时间（秒）"""
        return max(0, self._expire_time - time.time())

    @property
    def stats(self) -> Dict:
        """获取统计信息"""
        return {
            **self._stats,
            "state": self.state.value,
            "ttl_seconds": round(self.ttl, 1),
            "cookie_count": len(self._cookies),
            "cookie_keys": list(self._cookies.keys()),
            "config": {
                "ttl": WAF_COOKIE_TTL,
                "refresh_before": WAF_COOKIE_REFRESH_BEFORE,
                "retry_interval": WAF_COOKIE_RETRY_INTERVAL,
            }
        }

    async def get_cookies(self) -> Dict[str, str]:
        """
        获取 WAF Cookie（主要接口）

        工作流程:
        1. 如果缓存有效，直接返回
        2. 如果正在刷新，等待刷新完成
        3. 如果过期或为空，触发刷新

        Returns:
            Dict[str, str]: WAF Cookie 字典

        Raises:
            RuntimeError: 无法获取 Cookie
        """
        # 1. 快速路径：缓存有效，直接返回
        if self.state == CookieState.VALID:
            self._stats["cache_hits"] += 1
            logger.debug(f"Cookie cache hit, TTL: {self.ttl:.0f}s")
            return self._cookies.copy()

        # 2. 即将过期：返回现有 Cookie，并在后台触发预刷新
        if self.state == CookieState.EXPIRING:
            self._stats["cache_hits"] += 1
            self._trigger_pre_refresh()
            logger.debug(f"Cookie expiring soon, TTL: {self.ttl:.0f}s, pre-refresh triggered")
            return self._cookies.copy()

        # 3. 需要刷新
        self._stats["cache_misses"] += 1
        return await self._refresh_with_lock()

    async def _refresh_with_lock(self) -> Dict[str, str]:
        """
        带锁的刷新操作

        确保同一时刻只有一个刷新操作在进行
        """
        condition = self._get_condition()

        async with condition:
            # 如果已经有请求在刷新，等待它完成
            while self._is_refreshing:
                logger.info("Waiting for ongoing cookie refresh...")
                try:
                    await asyncio.wait_for(condition.wait(), timeout=120)
                except asyncio.TimeoutError:
                    logger.error("Timeout waiting for cookie refresh")
                    if self._cookies:
                        logger.warning("Using stale cookies as fallback")
                        return self._cookies.copy()
                    raise RuntimeError("Cookie refresh timeout")

            # 双重检查：可能在等待期间已被其他请求刷新
            if self.is_valid:
                logger.debug("Cookie already refreshed by another request")
                return self._cookies.copy()

            # 执行刷新
            return await self._do_refresh_internal()

    async def _do_refresh_internal(self, retry_count: int = 0) -> Dict[str, str]:
        """
        执行实际的刷新操作（内部方法，必须在条件变量锁内调用）

        Args:
            retry_count: 当前重试次数
        """
        MAX_RETRIES = 2  # 最多重试2次
        condition = self._get_condition()

        self._is_refreshing = True
        self._stats["total_refreshes"] += 1

        start_time = time.time()
        logger.info(f"Refreshing WAF cookies from {WAF_LOGIN_URL}..." + (f" (retry {retry_count})" if retry_count > 0 else ""))

        try:
            # 使用常驻浏览器获取 Cookie
            cookies = await browser_manager.get_page_cookies(
                url=WAF_LOGIN_URL,
                wait_time=WAF_PAGE_WAIT_MS
            )

            if not cookies:
                raise RuntimeError("No cookies returned from browser")

            # 更新缓存
            self._cookies = cookies
            self._expire_time = time.time() + WAF_COOKIE_TTL

            # 更新统计
            duration = time.time() - start_time
            self._stats["successful_refreshes"] += 1
            self._stats["last_refresh_time"] = datetime.now().isoformat()
            self._stats["last_refresh_duration"] = round(duration, 2)
            self._stats["last_error"] = None

            logger.info(
                f"WAF cookies refreshed successfully in {duration:.2f}s, "
                f"keys: {list(cookies.keys())}, TTL: {WAF_COOKIE_TTL}s"
            )

            return self._cookies.copy()

        except Exception as e:
            self._stats["failed_refreshes"] += 1
            self._stats["last_error"] = str(e)
            logger.error(f"Failed to refresh WAF cookies: {e}")

            # 检查是否是浏览器断开连接导致的错误
            error_msg = str(e).lower()
            is_browser_error = any(x in error_msg for x in [
                "browser has been closed",
                "target page",
                "context",
                "disconnected",
                "connection refused"
            ])

            # 如果是浏览器错误且还有重试次数，重启浏览器并重试
            # 注意：不要在这里设置 _is_refreshing = False，保持刷新状态
            if is_browser_error and retry_count < MAX_RETRIES:
                logger.warning(f"Browser error detected, restarting browser and retrying... (attempt {retry_count + 1}/{MAX_RETRIES})")
                try:
                    await browser_manager.restart()
                    # 递归重试，保持在同一个锁内
                    return await self._do_refresh_internal(retry_count + 1)
                except Exception as restart_error:
                    logger.error(f"Failed to restart browser: {restart_error}")

            # 如果有旧 Cookie，返回旧的（降级策略）
            if self._cookies:
                logger.warning("Using stale cookies as fallback")
                return self._cookies.copy()

            raise RuntimeError(f"Failed to get WAF cookies: {e}")

        finally:
            self._is_refreshing = False
            # 通知所有等待的协程
            condition.notify_all()

    def _trigger_pre_refresh(self):
        """触发预刷新（后台执行，不阻塞当前请求）"""
        if self._is_refreshing:
            return

        if self._pre_refresh_task and not self._pre_refresh_task.done():
            return

        self._pre_refresh_task = asyncio.create_task(self._pre_refresh())

    async def _pre_refresh(self):
        """预刷新任务"""
        try:
            logger.info("Pre-refresh: starting background cookie refresh")
            # 使用带锁的方法，确保并发安全
            await self._refresh_with_lock()
            logger.info("Pre-refresh: completed successfully")
        except Exception as e:
            logger.error(f"Pre-refresh failed: {e}")
            # 预刷新失败不影响现有 Cookie，会在下次请求时重试

    async def force_refresh(self) -> Dict[str, str]:
        """
        强制刷新 Cookie（忽略缓存）

        用于手动刷新或错误恢复
        """
        logger.info("Force refresh requested")
        self._expire_time = 0  # 标记为过期
        return await self._refresh_with_lock()

    async def start_background_refresh(self):
        """
        启动后台定期刷新任务

        确保 Cookie 始终保持有效，用户永远不需要等待
        """
        logger.info(f"Starting background refresh task (TTL: {WAF_COOKIE_TTL}s, refresh before: {WAF_COOKIE_REFRESH_BEFORE}s)")

        while True:
            try:
                # 检查浏览器是否需要定期重启
                if browser_manager.should_restart():
                    logger.info("Browser scheduled restart")
                    await browser_manager.restart()

                # 检查 Cookie 状态
                state = self.state
                if state == CookieState.EMPTY:
                    # 首次获取 - 使用带锁的方法
                    await self._refresh_with_lock()
                elif state in (CookieState.EXPIRING, CookieState.EXPIRED):
                    # 即将过期或已过期，刷新 - 使用带锁的方法
                    await self._refresh_with_lock()

                # 计算下次检查时间
                if self.is_valid:
                    # 在过期前 refresh_before 秒时刷新
                    sleep_time = max(60, self.ttl - WAF_COOKIE_REFRESH_BEFORE)
                else:
                    # 刷新失败，短时间后重试
                    sleep_time = WAF_COOKIE_RETRY_INTERVAL

                logger.debug(f"Background refresh: next check in {sleep_time:.0f}s")
                await asyncio.sleep(sleep_time)

            except asyncio.CancelledError:
                logger.info("Background refresh task cancelled")
                break
            except Exception as e:
                logger.error(f"Background refresh error: {e}")
                await asyncio.sleep(WAF_COOKIE_RETRY_INTERVAL)

    def clear(self):
        """清除缓存"""
        self._cookies = {}
        self._expire_time = 0
        logger.info("Cookie cache cleared")


# 全局单例
waf_cookie_manager = WAFCookieManager()


# ============== 便捷函数（兼容旧代码）==============

async def get_waf_cookies() -> Dict[str, str]:
    """获取 WAF Cookie（兼容旧接口）"""
    return await waf_cookie_manager.get_cookies()


async def refresh_waf_cookies() -> Dict[str, str]:
    """强制刷新 WAF Cookie（兼容旧接口）"""
    return await waf_cookie_manager.force_refresh()
