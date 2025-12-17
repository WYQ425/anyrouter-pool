"""
Balance API endpoints for WAF Proxy
提供余额查询接口
"""

import json
import os
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter
from fastapi.responses import JSONResponse

# 配置 (支持环境变量)
BALANCES_FILE = Path(os.getenv("BALANCES_FILE", "/app/data/balances.json"))
# 账号配置文件
_default_accounts_file = "/app/data/accounts.json"
if os.name == 'nt' and not os.path.exists(_default_accounts_file):
    _default_accounts_file = str(Path(__file__).parent.parent / "data" / "keeper" / "accounts.json")
ACCOUNTS_FILE = Path(os.getenv("ACCOUNTS_FILE", _default_accounts_file))

# NewAPI 配额单位：500000 = $1
QUOTA_PER_DOLLAR = 500000

# 创建路由器
router = APIRouter()


def load_balances():
    """加载余额数据"""
    if BALANCES_FILE.exists():
        with open(BALANCES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def load_accounts():
    """加载账号列表"""
    if ACCOUNTS_FILE.exists():
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def get_account_health(account: dict, balance_info: dict) -> dict:
    """
    计算账号健康状态
    返回健康度评分和详细状态
    """
    health = {
        "score": 0,  # 0-100 分
        "level": "unknown",  # healthy, warning, error, unknown
        "issues": [],
        "has_session": False,
        "has_api_key": False,
        "session_valid": None,  # True/False/None(unknown)
        "last_checkin_success": None,
        "last_checkin_message": None,
        "last_checkin_time": None
    }

    max_score = 100
    current_score = 0

    # 1. 检查 session (30分)
    session = account.get("cookies", {}).get("session", "")
    if session:
        health["has_session"] = True
        current_score += 15  # 有 session 得 15 分

        # 检查最近签到是否成功来判断 session 有效性
        if balance_info:
            checkin_status = balance_info.get("status")
            checkin_message = balance_info.get("checkin_message", "")
            health["last_checkin_message"] = checkin_message
            health["last_checkin_time"] = balance_info.get("last_checkin")

            if checkin_status == "ok":
                health["session_valid"] = True
                health["last_checkin_success"] = True
                current_score += 15  # session 有效再得 15 分
            else:
                # 检查失败原因是否是 session 问题
                if "session" in checkin_message.lower() or "cookie" in checkin_message.lower() or "401" in checkin_message:
                    health["session_valid"] = False
                    health["issues"].append("Session 可能已过期，请更新 Cookie")
                else:
                    health["session_valid"] = None  # 其他原因，不确定
                    health["last_checkin_success"] = False
                    health["issues"].append(f"签到失败: {checkin_message}")
    else:
        health["issues"].append("缺少 Session Cookie，无法签到")

    # 2. 检查 API Key (40分)
    api_key = account.get("api_key", "")
    if api_key:
        health["has_api_key"] = True
        current_score += 40  # 有 API Key 得 40 分
    else:
        health["issues"].append("缺少 API Key，无法用于 API 代理")

    # 3. 检查余额 (30分)
    if balance_info and balance_info.get("status") == "ok":
        # 注意：AnyRouter 的 quota 是剩余额度，不是总额度
        remaining_usd = balance_info.get("quota_usd", 0)  # 剩余
        used_usd = balance_info.get("used_usd", 0)  # 已用
        total_usd = remaining_usd + used_usd  # 总额度

        if total_usd > 0:
            remaining_percent = (remaining_usd / total_usd) * 100
            if remaining_percent >= 50:
                current_score += 30
            elif remaining_percent >= 20:
                current_score += 20
                health["issues"].append(f"余额较低 ({remaining_percent:.1f}% 剩余)")
            elif remaining_percent >= 5:
                current_score += 10
                health["issues"].append(f"余额不足 ({remaining_percent:.1f}% 剩余)")
            else:
                health["issues"].append(f"余额即将耗尽 ({remaining_percent:.1f}% 剩余)")

    # 计算健康等级
    health["score"] = int(current_score)
    if current_score >= 80:
        health["level"] = "healthy"
    elif current_score >= 50:
        health["level"] = "warning"
    elif current_score > 0:
        health["level"] = "error"
    else:
        health["level"] = "unknown"

    return health


@router.get("/balance")
async def get_balance():
    """获取账号余额汇总"""
    data = load_balances()
    if not data:
        return JSONResponse(
            status_code=404,
            content={"success": False, "message": "Balance data not found. Run balance_checker.py first."}
        )

    summary = data.get("summary", {})
    return {
        "success": True,
        "data": {
            "total_quota_usd": summary.get("total_quota_usd", 0),
            "total_used_usd": summary.get("total_used_usd", 0),
            "remaining_usd": summary.get("total_quota_usd", 0) - summary.get("total_used_usd", 0),
            "total_requests": summary.get("total_requests", 0),
            "success_count": summary.get("success_count", 0),
            "total_count": summary.get("total_count", 0),
            # NewAPI 格式的配额
            "total_quota": summary.get("total_quota", 0),
            "total_used": summary.get("total_used", 0),
            "remaining_quota": summary.get("total_quota", 0) - summary.get("total_used", 0)
        }
    }


@router.get("/balance/detail")
async def get_balance_detail():
    """获取每个账号的详细余额和健康状态"""
    balance_data = load_balances()
    accounts = load_accounts()

    # 创建余额数据的索引（按账号名称）
    balance_by_name = {}
    if balance_data:
        for acc in balance_data.get("accounts", []):
            balance_by_name[acc.get("name", "")] = acc

    # 获取最后更新时间
    last_updated = balance_data.get("last_updated") if balance_data else None

    detailed = []
    total_quota_usd = 0
    total_used_usd = 0
    health_stats = {"healthy": 0, "warning": 0, "error": 0, "unknown": 0}

    # 遍历所有账号，合并余额数据
    for account in accounts:
        if not account.get("enabled", True):
            continue

        name = account.get("name", "")
        balance_info = balance_by_name.get(name, {})

        # 计算健康状态
        health = get_account_health(account, balance_info)
        health_stats[health["level"]] += 1

        if balance_info.get("status") == "ok":
            # 有余额数据
            # 注意：AnyRouter API 返回的 quota 是"剩余额度"，不是"总额度"
            # 总额度 = 剩余额度 + 已用额度
            quota = balance_info.get("quota", 0)  # 这是剩余额度（原始单位）
            used = balance_info.get("used_quota", 0)  # 已使用额度
            total = quota + used  # 总额度 = 剩余 + 已用

            remaining_usd = quota / QUOTA_PER_DOLLAR  # 剩余（美元）
            used_usd = used / QUOTA_PER_DOLLAR  # 已用（美元）
            total_usd = total / QUOTA_PER_DOLLAR  # 总额度（美元）

            total_quota_usd += total_usd
            total_used_usd += used_usd

            detailed.append({
                "name": name,
                "username": balance_info.get("username", name),
                "quota": total,  # 总额度
                "used_quota": used,
                "remaining_quota": quota,  # 剩余额度
                "quota_usd": total_usd,  # 总额度（美元）
                "used_usd": used_usd,
                "remaining_usd": remaining_usd,  # 剩余（美元）
                "usage_percent": round((used_usd / total_usd * 100), 1) if total_usd > 0 else 0,
                "request_count": balance_info.get("request_count", 0),
                "status": "ok",
                "last_checkin": balance_info.get("last_checkin"),
                "checkin_message": balance_info.get("checkin_message", ""),
                "health": health
            })
        else:
            # 没有余额数据，显示为待同步
            detailed.append({
                "name": name,
                "username": account.get("api_user", name),
                "quota": 0,
                "used_quota": 0,
                "remaining_quota": 0,
                "quota_usd": 0,
                "used_usd": 0,
                "remaining_usd": 0,
                "usage_percent": 0,
                "request_count": 0,
                "status": "pending",
                "message": "余额待同步，请执行签到获取",
                "health": health
            })

    return {
        "success": True,
        "data": {
            "accounts": detailed,
            "summary": {
                "total_quota_usd": total_quota_usd,
                "total_used_usd": total_used_usd,
                "remaining_usd": total_quota_usd - total_used_usd,
                "usage_percent": round((total_used_usd / total_quota_usd * 100), 1) if total_quota_usd > 0 else 0,
                "total_count": len(detailed),
                "synced_count": sum(1 for d in detailed if d["status"] == "ok"),
                "pending_count": sum(1 for d in detailed if d["status"] == "pending"),
                "health_stats": health_stats
            },
            "last_updated": last_updated,
            "data_source": "签到同步（余额数据来自最后一次签到）"
        }
    }


@router.get("/balance/newapi-format")
async def get_balance_newapi_format():
    """
    返回 NewAPI 渠道余额格式
    可用于更新 NewAPI 渠道配置显示
    """
    data = load_balances()
    if not data:
        return JSONResponse(
            status_code=404,
            content={"success": False, "message": "Balance data not found"}
        )

    summary = data.get("summary", {})
    remaining = summary.get("total_quota", 0) - summary.get("total_used", 0)
    remaining_usd = remaining / QUOTA_PER_DOLLAR

    return {
        "success": True,
        "data": {
            # NewAPI 渠道余额格式
            "balance": remaining_usd,
            "balance_str": f"${remaining_usd:.2f}",
            "quota": summary.get("total_quota", 0),
            "used_quota": summary.get("total_used", 0),
            "remaining_quota": remaining,
            "accounts_count": summary.get("total_count", 0),
            "healthy_accounts": summary.get("success_count", 0)
        }
    }
