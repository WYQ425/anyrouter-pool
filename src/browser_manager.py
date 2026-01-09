"""
浏览器管理器 v2 - 按需创建/销毁模式

特性:
- 仅在签到时创建浏览器实例
- 签到完成后立即销毁，释放资源
- 不再常驻运行，节省内存

使用方式:
    async with BrowserSession() as session:
        cookies = await session.get_page_cookies(url)
"""

import asyncio
import gc
import os
from datetime import datetime
from typing import Optional, Dict
from contextlib import asynccontextmanager

from loguru import logger
from playwright.async_api import async_playwright, Browser, Playwright, BrowserContext, Page


# 配置
HTTP_PROXY = os.getenv("HTTP_PROXY", "http://127.0.0.1:7890")


class BrowserSession:
    """
    浏览器会话 - 按需创建，用完即销毁

    使用方式:
        async with BrowserSession() as session:
            cookies = await session.get_page_cookies("https://example.com/login")
    """

    def __init__(self, use_proxy: bool = True):
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._use_proxy = use_proxy
        self._created_at: Optional[datetime] = None

    async def __aenter__(self) -> "BrowserSession":
        """进入上下文时创建浏览器"""
        await self._start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """退出上下文时销毁浏览器"""
        await self._stop()
        # 主动触发 GC 清理资源
        gc.collect()

    async def _start(self) -> bool:
        """启动浏览器"""
        try:
            logger.info("[BrowserSession] Starting browser...")

            # 启动 Playwright
            self._playwright = await async_playwright().start()

            # 构建启动参数
            args = [
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
                # 内存优化参数
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-site-isolation-trials",
                "--disable-features=TranslateUI",
                "--disable-ipc-flooding-protection",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--disable-breakpad",
                "--disable-component-extensions-with-background-pages",
                "--disable-default-apps",
                "--disable-hang-monitor",
                "--disable-popup-blocking",
                "--disable-prompt-on-repost",
                "--disable-client-side-phishing-detection",
                "--metrics-recording-only",
                "--no-default-browser-check",
                "--js-flags=--max-old-space-size=128",
            ]

            # 如果需要代理
            if self._use_proxy and HTTP_PROXY:
                args.insert(0, f"--proxy-server={HTTP_PROXY}")

            # 启动浏览器
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=args
            )

            self._created_at = datetime.now()
            proxy_info = f", proxy: {HTTP_PROXY}" if self._use_proxy else ""
            logger.info(f"[BrowserSession] Browser started successfully{proxy_info}")
            return True

        except Exception as e:
            logger.error(f"[BrowserSession] Failed to start browser: {e}")
            await self._stop()
            raise

    async def _stop(self):
        """停止浏览器并清理资源"""
        try:
            if self._browser:
                await self._browser.close()
        except Exception as e:
            logger.warning(f"[BrowserSession] Error closing browser: {e}")
        finally:
            self._browser = None

        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning(f"[BrowserSession] Error stopping playwright: {e}")
        finally:
            self._playwright = None

        if self._created_at:
            duration = (datetime.now() - self._created_at).total_seconds()
            logger.info(f"[BrowserSession] Browser stopped, session lasted {duration:.1f}s")
            self._created_at = None

    async def get_page_cookies(self, url: str, wait_time: int = 5000) -> Dict[str, str]:
        """
        访问页面并获取 Cookie

        Args:
            url: 要访问的 URL
            wait_time: 等待 JS 执行的时间（毫秒）

        Returns:
            Dict[str, str]: Cookie 字典
        """
        if not self._browser:
            raise RuntimeError("Browser is not started")

        context: Optional[BrowserContext] = None
        page: Optional[Page] = None

        try:
            # 创建浏览器上下文
            context = await self._browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )

            page = await context.new_page()

            # 资源拦截 - 只加载 HTML 和 JS，拦截图片/媒体/字体/样式
            async def block_heavy_resources(route):
                resource_type = route.request.resource_type
                if resource_type in ['image', 'media', 'font', 'stylesheet']:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route('**/*', block_heavy_resources)

            # 访问页面
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # 等待 JS 执行生成 Cookie
            await page.wait_for_timeout(wait_time)

            # 获取 Cookie
            cookies = await context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}

            logger.debug(f"[BrowserSession] Got cookies from {url}: {list(cookie_dict.keys())}")
            return cookie_dict

        except Exception as e:
            logger.error(f"[BrowserSession] Error getting cookies from {url}: {e}")
            raise

        finally:
            # 清理页面和上下文
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            if context:
                try:
                    await context.close()
                except Exception:
                    pass


# ==================== 便捷函数 ====================

async def get_waf_cookies(url: str, use_proxy: bool = True) -> Dict[str, str]:
    """
    便捷函数：获取指定 URL 的 WAF Cookie

    创建临时浏览器会话，获取 Cookie 后立即销毁。

    Args:
        url: 登录页面 URL
        use_proxy: 是否使用代理

    Returns:
        Dict[str, str]: Cookie 字典
    """
    async with BrowserSession(use_proxy=use_proxy) as session:
        return await session.get_page_cookies(url)


# ==================== 兼容旧代码的包装器 ====================
# 以下代码用于兼容 checkin_service.py 等现有代码
# 在完成迁移后可以删除

class _LegacyBrowserManager:
    """
    兼容旧代码的包装器

    警告：此类仅用于过渡期兼容，新代码请直接使用 BrowserSession
    """

    def __init__(self):
        self._session: Optional[BrowserSession] = None
        self._lock = asyncio.Lock()
        self._start_count = 0
        self._stop_count = 0

    @property
    def is_running(self) -> bool:
        """检查是否有活跃会话"""
        return self._session is not None and self._session._browser is not None

    @property
    def stats(self) -> Dict:
        """获取统计信息"""
        return {
            "running": self.is_running,
            "started": self._start_count > 0,
            "start_time": self._session._created_at.isoformat() if self._session and self._session._created_at else None,
            "uptime_seconds": (datetime.now() - self._session._created_at).total_seconds() if self._session and self._session._created_at else 0,
            "restart_count": self._start_count,
            "error_count": 0,
            "mode": "on_demand",  # 标记为按需模式
        }

    async def start(self) -> bool:
        """启动浏览器（兼容旧接口）"""
        async with self._lock:
            if self.is_running:
                return True

            self._session = BrowserSession()
            await self._session._start()
            self._start_count += 1
            return True

    async def stop(self):
        """停止浏览器（兼容旧接口）"""
        async with self._lock:
            if self._session:
                await self._session._stop()
                self._session = None
                self._stop_count += 1
                gc.collect()

    async def restart(self):
        """重启浏览器（兼容旧接口）"""
        await self.stop()
        await self.start()

    async def ensure_running(self) -> bool:
        """确保浏览器运行（兼容旧接口）"""
        if self.is_running:
            return True
        return await self.start()

    async def get_page_cookies(self, url: str, wait_time: int = 5000) -> Dict[str, str]:
        """获取页面 Cookie（兼容旧接口）"""
        if not self.is_running:
            await self.start()

        if self._session:
            return await self._session.get_page_cookies(url, wait_time)
        raise RuntimeError("Browser session not available")


# 全局单例（兼容旧代码）
browser_manager = _LegacyBrowserManager()
