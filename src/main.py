"""
AnyRouter Keeper - 智能守卫服务
主程序入口

功能：
1. 定时获取各账号余额
2. 将账号同步到 NewAPI 作为渠道
3. 根据余额动态管理渠道状态（智能熔断）
4. 根据余额调整渠道权重（动态负载均衡）
"""

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent))

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from loguru import logger

from config import get_config, reload_config
from services import BalanceService, ChannelSyncService, NotifyService


class Keeper:
    """AnyRouter Keeper 主服务"""

    def __init__(self):
        self.config = get_config()
        self.balance_service = BalanceService(self.config)
        self.channel_sync_service = ChannelSyncService(self.config)
        self.notify_service = NotifyService(self.config.notify)
        self.scheduler = AsyncIOScheduler()
        self._running = False

    async def initialize(self) -> bool:
        """初始化服务"""
        logger.info("=" * 50)
        logger.info("AnyRouter Keeper 启动中...")
        logger.info("=" * 50)

        # 检查配置
        if not self.config.accounts:
            logger.error("未找到账号配置，请检查 accounts.json")
            return False

        logger.info(f"已加载 {len(self.config.accounts)} 个账号配置")

        # 初始化渠道同步服务
        if not await self.channel_sync_service.initialize():
            logger.error("渠道同步服务初始化失败")
            return False

        return True

    async def sync_task(self):
        """同步任务 - 获取余额并同步渠道状态"""
        logger.info("-" * 40)
        logger.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 执行同步任务")

        try:
            # 1. 获取所有账号余额
            balance_results = await self.balance_service.fetch_all_balances()

            # 转换为 dict 格式
            balance_info = {
                r["account_name"]: r
                for r in balance_results
                if r.get("account_name")
            }

            # 2. 同步到 NewAPI
            await self.channel_sync_service.sync_all_accounts(balance_info)

            # 3. 检查告警
            await self._check_alerts(balance_info)

            # 4. 输出统计
            total_balance = self.balance_service.get_total_balance()
            logger.info(f"当前总余额: ${total_balance:.2f}")

        except Exception as e:
            logger.error(f"同步任务异常: {e}")
            import traceback
            traceback.print_exc()

    async def _check_alerts(self, balance_info: dict):
        """检查并发送告警"""
        # 检查低余额账号
        for account_name, info in balance_info.items():
            if not info.get("success"):
                continue

            balance = info.get("balance", 0)

            # 临界告警
            if balance < self.config.balance_critical_threshold:
                await self.notify_service.send_balance_alert(
                    account_name, balance,
                    self.config.balance_critical_threshold,
                    level="critical"
                )
            # 警告
            elif balance < self.config.balance_warning_threshold:
                await self.notify_service.send_balance_alert(
                    account_name, balance,
                    self.config.balance_warning_threshold,
                    level="warning"
                )

        # 检查 Cookie 过期
        expired_accounts = self.balance_service.get_expired_cookie_accounts()
        if expired_accounts:
            for acc_name in expired_accounts:
                logger.warning(f"账号 '{acc_name}' Cookie 已过期")
                await self.notify_service.push_message(
                    "🔑 Cookie 过期告警",
                    f"账号 {acc_name} 的 Cookie 已过期，请及时更新"
                )

    async def run_once(self):
        """运行一次同步任务（用于测试）"""
        if not await self.initialize():
            return False

        await self.sync_task()
        return True

    async def start(self):
        """启动服务"""
        if not await self.initialize():
            return

        self._running = True

        # 解析 cron 表达式
        try:
            cron_parts = self.config.cron_sync.split()
            if len(cron_parts) == 5:
                trigger = CronTrigger(
                    minute=cron_parts[0],
                    hour=cron_parts[1],
                    day=cron_parts[2],
                    month=cron_parts[3],
                    day_of_week=cron_parts[4],
                )
            else:
                # 默认每 5 分钟
                trigger = CronTrigger(minute="*/5")
        except Exception as e:
            logger.warning(f"解析 cron 表达式失败: {e}，使用默认值（每5分钟）")
            trigger = CronTrigger(minute="*/5")

        # 添加定时任务
        self.scheduler.add_job(
            self.sync_task,
            trigger=trigger,
            id="sync_task",
            name="余额同步任务",
        )

        # 启动调度器
        self.scheduler.start()
        logger.info(f"定时任务已启动，cron: {self.config.cron_sync}")

        # 立即执行一次
        logger.info("执行首次同步...")
        await self.sync_task()

        # 保持运行
        try:
            while self._running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            logger.info("收到终止信号，正在停止...")
        finally:
            self.scheduler.shutdown()
            logger.info("Keeper 已停止")

    def stop(self):
        """停止服务"""
        self._running = False


def setup_logging():
    """配置日志"""
    logger.remove()

    # 控制台输出
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level="INFO",
    )

    # 文件输出
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)

    logger.add(
        log_dir / "keeper_{time:YYYY-MM-DD}.log",
        rotation="00:00",
        retention="7 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        level="DEBUG",
    )


async def test_balance_only():
    """仅测试余额获取（不需要 NewAPI）"""
    logger.info("=" * 50)
    logger.info("余额测试模式")
    logger.info("=" * 50)

    config = get_config()

    if not config.accounts:
        logger.error("未找到账号配置，请检查 accounts.json")
        return

    logger.info(f"已加载 {len(config.accounts)} 个账号配置")

    balance_service = BalanceService(config)
    results = await balance_service.fetch_all_balances()

    # 输出结果
    logger.info("-" * 40)
    logger.info("余额查询结果:")
    total = 0
    for r in results:
        if r.get("success"):
            balance = r.get("balance", 0)
            total += balance
            logger.info(f"  [OK] {r['account_name']}: ${balance:.2f} (已用: ${r.get('used', 0):.2f})")
        else:
            logger.warning(f"  [FAIL] {r.get('account_name', 'Unknown')}: {r.get('error', 'Unknown error')}")

    logger.info("-" * 40)
    logger.info(f"总余额: ${total:.2f}")


async def main():
    """主函数"""
    # 加载环境变量
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        load_dotenv(env_file)

    # 配置日志
    setup_logging()

    # 检查命令行参数
    if len(sys.argv) > 1:
        if sys.argv[1] == "--balance":
            # 仅测试余额获取
            await test_balance_only()
            return
        elif sys.argv[1] == "--once":
            # 运行一次完整同步
            keeper = Keeper()
            await keeper.run_once()
            return
        elif sys.argv[1] == "--help":
            print("""
AnyRouter Keeper - 智能守卫服务

用法:
  python main.py              持续运行服务
  python main.py --once       运行一次完整同步（需要 NewAPI）
  python main.py --balance    仅测试余额获取（不需要 NewAPI）
  python main.py --help       显示帮助信息
""")
            return

    # 持续运行
    keeper = Keeper()
    await keeper.start()


if __name__ == "__main__":
    asyncio.run(main())
