"""
浏览器管理器 - 常驻单例 Playwright 浏览器

特性:
- 启动时创建浏览器，保持常驻运行
- 自动崩溃恢复
- 定期重启防止内存泄漏
- 并发安全
"""

import asyncio
import os
from datetime import datetime
from typing import Optional, Dict

from loguru import logger
from playwright.async_api import async_playwright, Browser, Playwright

# 配置
HTTP_PROXY = os.getenv("HTTP_PROXY", "http://127.0.0.1:7890")
BROWSER_RESTART_HOURS = int(os.getenv("BROWSER_RESTART_HOURS", "6"))  # 每 6 小时重启


class BrowserManager:
    """
    常驻浏览器管理器

    - 单例模式，全局只有一个浏览器实例
    - 自动处理崩溃和重连
    - 定期重启防止内存泄漏
    """

    def __init__(self):
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._lock = asyncio.Lock()
        self._started = False
        self._start_time: Optional[datetime] = None
        self._restart_count = 0
        self._error_count = 0

    @property
    def is_running(self) -> bool:
        """检查浏览器是否在运行"""
        return self._browser is not None and self._browser.is_connected()

    @property
    def stats(self) -> Dict:
        """获取浏览器状态统计"""
        return {
            "running": self.is_running,
            "started": self._started,
            "start_time": self._start_time.isoformat() if self._start_time else None,
            "uptime_seconds": (datetime.now() - self._start_time).total_seconds() if self._start_time else 0,
            "restart_count": self._restart_count,
            "error_count": self._error_count,
        }

    async def start(self) -> bool:
        """
        启动浏览器

        Returns:
            bool: 是否成功启动
        """
        async with self._lock:
            if self.is_running:
                logger.debug("Browser already running")
                return True

            try:
                logger.info("Starting Playwright browser...")

                # 如果之前有残留，先清理
                await self._cleanup_internal()

                # 启动 Playwright
                self._playwright = await async_playwright().start()

                # 启动浏览器
                # 注意：移除 --single-process，该参数在 Docker 中不稳定，容易导致浏览器断开
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        f"--proxy-server={HTTP_PROXY}",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",  # 使用 /tmp 而非 /dev/shm
                        "--disable-gpu",
                        "--disable-software-rasterizer",
                        "--disable-extensions",
                        "--disable-background-networking",
                        "--disable-sync",
                        "--no-first-run",
                        "--no-zygote",  # 禁用 zygote 进程，提高稳定性
                    ]
                )

                self._started = True
                self._start_time = datetime.now()

                logger.info(f"Browser started successfully, proxy: {HTTP_PROXY}")
                return True

            except Exception as e:
                self._error_count += 1
                logger.error(f"Failed to start browser: {e}")
                await self._cleanup_internal()
                return False

    async def stop(self):
        """停止浏览器"""
        async with self._lock:
            await self._cleanup_internal()
            logger.info("Browser stopped")

    async def _cleanup_internal(self):
        """清理资源（内部方法，不获取锁）"""
        try:
            if self._browser:
                await self._browser.close()
        except Exception as e:
            logger.warning(f"Error closing browser: {e}")
        finally:
            self._browser = None

        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning(f"Error stopping playwright: {e}")
        finally:
            self._playwright = None
            self._started = False

    async def restart(self):
        """重启浏览器（用于定期重启或错误恢复）"""
        async with self._lock:
            logger.info("Restarting browser...")
            await self._cleanup_internal()
            self._restart_count += 1

            # 在锁内启动，避免竞态条件
            try:
                logger.info("Starting Playwright browser...")

                # 启动 Playwright
                self._playwright = await async_playwright().start()

                # 启动浏览器
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        f"--proxy-server={HTTP_PROXY}",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-software-rasterizer",
                        "--disable-extensions",
                        "--disable-background-networking",
                        "--disable-sync",
                        "--no-first-run",
                        "--no-zygote",
                    ]
                )

                self._started = True
                self._start_time = datetime.now()

                logger.info(f"Browser restarted successfully (total restarts: {self._restart_count})")

            except Exception as e:
                self._error_count += 1
                logger.error(f"Failed to restart browser: {e}")
                await self._cleanup_internal()
                raise

    async def ensure_running(self) -> bool:
        """
        确保浏览器正在运行，如果没有则启动

        Returns:
            bool: 浏览器是否可用
        """
        if self.is_running:
            return True

        logger.warning("Browser not running, attempting to start...")
        return await self.start()

    async def get_page_cookies(self, url: str, wait_time: int = 5000) -> Dict[str, str]:
        """
        使用常驻浏览器访问页面并获取 Cookie

        Args:
            url: 要访问的 URL
            wait_time: 等待 JS 执行的时间（毫秒）

        Returns:
            Dict[str, str]: Cookie 字典
        """
        # 确保浏览器运行
        if not await self.ensure_running():
            raise RuntimeError("Browser is not available")

        context = None
        page = None
        try:
            # 创建新的浏览器上下文（隔离 Cookie）
            context = await self._browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )

            page = await context.new_page()

            # 访问页面
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # 等待 JS 执行生成 Cookie
            await page.wait_for_timeout(wait_time)

            # 获取 Cookie
            cookies = await context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}

            logger.debug(f"Got cookies from {url}: {list(cookie_dict.keys())}")
            return cookie_dict

        except Exception as e:
            self._error_count += 1
            logger.error(f"Error getting cookies from {url}: {e}")

            # 如果浏览器断开连接，标记需要重启
            if not self.is_running:
                logger.warning("Browser disconnected, will restart on next request")

            raise

        finally:
            # 先关闭页面
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            # 再关闭上下文
            if context:
                try:
                    await context.close()
                except Exception:
                    pass

    def should_restart(self) -> bool:
        """检查是否应该定期重启（防止内存泄漏）"""
        if not self._start_time:
            return False

        uptime_hours = (datetime.now() - self._start_time).total_seconds() / 3600
        return uptime_hours >= BROWSER_RESTART_HOURS


# 全局单例
browser_manager = BrowserManager()
