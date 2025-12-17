"""
同步余额到 NewAPI 数据库
直接更新 channels 表的 balance 字段
"""

import json
import sqlite3
from pathlib import Path
from loguru import logger

# 配置
BALANCES_FILE = Path("D:/agent/anyrouter/anyrouter-stack/data/keeper/balances.json")
NEWAPI_DB = Path("D:/agent/anyrouter/anyrouter-stack/data/new-api/one-api.db")

# waf-proxy 渠道 ID
WAF_PROXY_CHANNEL_ID = 20


def load_balances():
    """加载余额数据"""
    if BALANCES_FILE.exists():
        with open(BALANCES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def update_channel_balance(channel_id: int, balance: float):
    """更新 NewAPI 渠道余额"""
    try:
        conn = sqlite3.connect(NEWAPI_DB)
        cursor = conn.cursor()

        # 更新 balance 字段
        cursor.execute(
            "UPDATE channels SET balance = ?, balance_updated_time = strftime('%s', 'now') WHERE id = ?",
            (balance, channel_id)
        )

        rows_affected = cursor.rowcount
        conn.commit()
        conn.close()

        if rows_affected > 0:
            logger.info(f"Channel {channel_id} balance updated to ${balance:.2f}")
            return True
        else:
            logger.warning(f"Channel {channel_id} not found")
            return False

    except Exception as e:
        logger.error(f"Failed to update channel balance: {e}")
        return False


def sync_balance():
    """同步余额到 NewAPI"""
    # 加载余额数据
    data = load_balances()
    if not data:
        logger.error("Balance data not found. Run balance_checker.py first.")
        return False

    summary = data.get("summary", {})
    total_usd = summary.get("total_quota_usd", 0)
    used_usd = summary.get("total_used_usd", 0)
    remaining_usd = total_usd - used_usd

    logger.info(f"Balance summary: Total=${total_usd:.2f}, Used=${used_usd:.2f}, Remaining=${remaining_usd:.2f}")

    # 更新 waf-proxy 渠道余额
    success = update_channel_balance(WAF_PROXY_CHANNEL_ID, remaining_usd)

    if success:
        logger.info(f"Successfully synced balance to NewAPI channel {WAF_PROXY_CHANNEL_ID}")
    else:
        logger.error(f"Failed to sync balance to NewAPI channel {WAF_PROXY_CHANNEL_ID}")

    return success


def get_channel_info(channel_id: int):
    """获取渠道信息"""
    try:
        conn = sqlite3.connect(NEWAPI_DB)
        cursor = conn.cursor()

        cursor.execute(
            "SELECT id, name, type, balance, balance_updated_time, status FROM channels WHERE id = ?",
            (channel_id,)
        )

        row = cursor.fetchone()
        conn.close()

        if row:
            return {
                "id": row[0],
                "name": row[1],
                "type": row[2],
                "balance": row[3],
                "balance_updated_time": row[4],
                "status": row[5]
            }
        return None

    except Exception as e:
        logger.error(f"Failed to get channel info: {e}")
        return None


if __name__ == "__main__":
    logger.info("Starting balance sync to NewAPI...")

    # 显示当前渠道信息
    info = get_channel_info(WAF_PROXY_CHANNEL_ID)
    if info:
        logger.info(f"Current channel info: {info}")

    # 同步余额
    sync_balance()

    # 显示更新后的渠道信息
    info = get_channel_info(WAF_PROXY_CHANNEL_ID)
    if info:
        logger.info(f"Updated channel info: {info}")
