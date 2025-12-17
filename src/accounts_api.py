"""
Accounts Management API
提供账号的增删改查功能
"""

import json
import os
import uuid
from pathlib import Path
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from loguru import logger

# 配置
_default_accounts_file = "/app/data/accounts.json"
if os.name == 'nt' and not os.path.exists(_default_accounts_file):
    _default_accounts_file = str(Path(__file__).parent.parent / "data" / "keeper" / "accounts.json")
ACCOUNTS_FILE = Path(os.getenv("ACCOUNTS_FILE", _default_accounts_file))

# 余额文件路径（用于检测 session 过期）
_default_balances_file = "/app/data/balances.json"
if os.name == 'nt' and not os.path.exists(_default_balances_file):
    _default_balances_file = str(Path(__file__).parent.parent / "data" / "keeper" / "balances.json")
BALANCES_FILE = Path(os.getenv("BALANCES_FILE", _default_balances_file))

# 创建路由器
router = APIRouter(prefix="/accounts", tags=["Accounts"])


# Pydantic 模型
class AccountCreate(BaseModel):
    name: str
    session_cookie: str
    api_user: str
    api_key: Optional[str] = ""
    provider: str = "anyrouter"
    enabled: bool = True


class AccountUpdate(BaseModel):
    name: Optional[str] = None
    session_cookie: Optional[str] = None
    api_user: Optional[str] = None
    api_key: Optional[str] = None
    provider: Optional[str] = None
    enabled: Optional[bool] = None


def load_accounts():
    """加载账号数据"""
    if ACCOUNTS_FILE.exists():
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def load_balances():
    """加载余额数据（用于检测 session 过期）"""
    if BALANCES_FILE.exists():
        with open(BALANCES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_accounts(accounts: list):
    """保存账号数据"""
    # 确保目录存在
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # 备份原文件
    if ACCOUNTS_FILE.exists():
        backup_file = ACCOUNTS_FILE.with_suffix(f".backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            import shutil
            shutil.copy(ACCOUNTS_FILE, backup_file)
            logger.info(f"Backed up accounts to {backup_file}")
        except Exception as e:
            logger.warning(f"Failed to backup accounts: {e}")

    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved {len(accounts)} accounts to {ACCOUNTS_FILE}")

    # 自动重新加载 waf_proxy 的账号列表
    try:
        from waf_proxy import load_accounts as waf_load_accounts
        waf_load_accounts()
        logger.info("Reloaded accounts in waf_proxy")
    except Exception as e:
        logger.warning(f"Failed to reload accounts in waf_proxy: {e}")


def find_account_index(accounts: list, identifier: str) -> int:
    """通过 name 或 api_user 查找账号索引"""
    for i, acc in enumerate(accounts):
        if acc.get("name") == identifier or acc.get("api_user") == identifier:
            return i
    return -1


@router.get("")
async def list_accounts():
    """获取所有账号列表"""
    accounts = load_accounts()
    balance_data = load_balances()

    # 创建余额数据索引（按账号名）
    balance_by_name = {}
    if balance_data:
        for acc in balance_data.get("accounts", []):
            balance_by_name[acc.get("name", "")] = acc

    # 返回时隐藏敏感信息
    safe_accounts = []
    for acc in accounts:
        name = acc.get("name", "")
        balance_info = balance_by_name.get(name, {})

        # 检查 session 是否可能过期（根据签到结果判断）
        session_warning = False
        if balance_info:
            checkin_message = balance_info.get("checkin_message", "").lower()
            if balance_info.get("status") != "ok":
                # 检查失败原因是否与 session 相关
                if "session" in checkin_message or "cookie" in checkin_message or "401" in checkin_message or "unauthorized" in checkin_message:
                    session_warning = True

        safe_acc = {
            "name": name,
            "api_user": acc.get("api_user", ""),
            "provider": acc.get("provider", "anyrouter"),
            "enabled": acc.get("enabled", True),
            "has_session": bool(acc.get("cookies", {}).get("session")),
            "has_api_key": bool(acc.get("api_key")),
            "session_warning": session_warning,
        }
        safe_accounts.append(safe_acc)

    return {
        "success": True,
        "data": {
            "accounts": safe_accounts,
            "total": len(safe_accounts),
            "enabled_count": sum(1 for a in safe_accounts if a["enabled"])
        }
    }


@router.get("/{identifier}")
async def get_account(identifier: str):
    """获取单个账号详情"""
    accounts = load_accounts()
    idx = find_account_index(accounts, identifier)

    if idx == -1:
        raise HTTPException(status_code=404, detail=f"Account not found: {identifier}")

    acc = accounts[idx]
    # 返回时隐藏完整的敏感信息，只显示部分
    return {
        "success": True,
        "data": {
            "name": acc.get("name", ""),
            "api_user": acc.get("api_user", ""),
            "provider": acc.get("provider", "anyrouter"),
            "enabled": acc.get("enabled", True),
            "session_cookie": acc.get("cookies", {}).get("session", "")[:50] + "..." if acc.get("cookies", {}).get("session") else "",
            "api_key": acc.get("api_key", "")[:20] + "..." if acc.get("api_key") else "",
        }
    }


@router.post("")
async def create_account(account: AccountCreate):
    """创建新账号"""
    accounts = load_accounts()

    # 检查是否已存在同名账号
    if find_account_index(accounts, account.name) != -1:
        raise HTTPException(status_code=400, detail=f"Account already exists: {account.name}")

    # 检查 api_user 是否已存在
    if find_account_index(accounts, account.api_user) != -1:
        raise HTTPException(status_code=400, detail=f"api_user already exists: {account.api_user}")

    # 创建新账号
    new_account = {
        "name": account.name,
        "cookies": {
            "session": account.session_cookie
        },
        "api_user": account.api_user,
        "provider": account.provider,
        "api_key": account.api_key,
        "enabled": account.enabled
    }

    accounts.append(new_account)
    save_accounts(accounts)

    logger.info(f"Created account: {account.name}")

    return {
        "success": True,
        "message": f"Account created: {account.name}",
        "data": {
            "name": account.name,
            "api_user": account.api_user
        }
    }


@router.put("/{identifier}")
async def update_account(identifier: str, account: AccountUpdate):
    """更新账号信息"""
    accounts = load_accounts()
    idx = find_account_index(accounts, identifier)

    if idx == -1:
        raise HTTPException(status_code=404, detail=f"Account not found: {identifier}")

    # 更新字段
    if account.name is not None:
        # 检查新名称是否与其他账号冲突
        for i, acc in enumerate(accounts):
            if i != idx and acc.get("name") == account.name:
                raise HTTPException(status_code=400, detail=f"Account name already exists: {account.name}")
        accounts[idx]["name"] = account.name

    if account.session_cookie is not None:
        if "cookies" not in accounts[idx]:
            accounts[idx]["cookies"] = {}
        accounts[idx]["cookies"]["session"] = account.session_cookie

    if account.api_user is not None:
        # 检查新 api_user 是否与其他账号冲突
        for i, acc in enumerate(accounts):
            if i != idx and acc.get("api_user") == account.api_user:
                raise HTTPException(status_code=400, detail=f"api_user already exists: {account.api_user}")
        accounts[idx]["api_user"] = account.api_user

    if account.api_key is not None:
        accounts[idx]["api_key"] = account.api_key

    if account.provider is not None:
        accounts[idx]["provider"] = account.provider

    if account.enabled is not None:
        accounts[idx]["enabled"] = account.enabled

    save_accounts(accounts)

    logger.info(f"Updated account: {identifier}")

    return {
        "success": True,
        "message": f"Account updated: {accounts[idx]['name']}",
        "data": {
            "name": accounts[idx]["name"],
            "api_user": accounts[idx]["api_user"]
        }
    }


@router.delete("/{identifier}")
async def delete_account(identifier: str):
    """删除账号"""
    accounts = load_accounts()
    idx = find_account_index(accounts, identifier)

    if idx == -1:
        raise HTTPException(status_code=404, detail=f"Account not found: {identifier}")

    deleted_account = accounts.pop(idx)
    save_accounts(accounts)

    logger.info(f"Deleted account: {deleted_account['name']}")

    return {
        "success": True,
        "message": f"Account deleted: {deleted_account['name']}",
        "data": {
            "name": deleted_account["name"],
            "api_user": deleted_account.get("api_user", "")
        }
    }


@router.post("/{identifier}/toggle")
async def toggle_account(identifier: str):
    """切换账号启用/禁用状态"""
    accounts = load_accounts()
    idx = find_account_index(accounts, identifier)

    if idx == -1:
        raise HTTPException(status_code=404, detail=f"Account not found: {identifier}")

    accounts[idx]["enabled"] = not accounts[idx].get("enabled", True)
    save_accounts(accounts)

    status = "enabled" if accounts[idx]["enabled"] else "disabled"
    logger.info(f"Account {accounts[idx]['name']} is now {status}")

    return {
        "success": True,
        "message": f"Account {status}: {accounts[idx]['name']}",
        "data": {
            "name": accounts[idx]["name"],
            "enabled": accounts[idx]["enabled"]
        }
    }


@router.post("/reload")
async def reload_accounts():
    """重新加载账号配置（通知 waf_proxy 重新加载）"""
    # 这个端点触发 waf_proxy.py 中的 load_accounts 函数
    # 通过导入 waf_proxy 模块并调用其函数
    try:
        from waf_proxy import load_accounts as waf_load_accounts
        waf_load_accounts()
        return {
            "success": True,
            "message": "Accounts reloaded successfully"
        }
    except Exception as e:
        logger.error(f"Failed to reload accounts in waf_proxy: {e}")
        return {
            "success": True,
            "message": "Accounts file saved, but waf_proxy reload failed. Please restart the service.",
            "warning": str(e)
        }
