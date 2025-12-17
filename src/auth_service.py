"""
认证服务模块
通过 NewAPI 验证超级管理员身份
"""

import os
import time
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import httpx
from loguru import logger

# 配置
NEWAPI_URL = os.getenv("NEWAPI_URL", "http://localhost:13000")
# JWT 密钥（如果不设置，每次启动时随机生成）
JWT_SECRET = os.getenv("JWT_SECRET", secrets.token_hex(32))
# Token 有效期（小时）
TOKEN_EXPIRE_HOURS = int(os.getenv("TOKEN_EXPIRE_HOURS", "24"))

# 内存中的 Token 存储（简单实现，重启后失效）
_active_tokens: Dict[str, Dict[str, Any]] = {}


def generate_token() -> str:
    """生成随机 token"""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """对 token 进行哈希"""
    return hashlib.sha256(f"{token}{JWT_SECRET}".encode()).hexdigest()


async def verify_newapi_login(username: str, password: str) -> Optional[Dict[str, Any]]:
    """
    通过 NewAPI 登录 API 验证用户

    Args:
        username: 用户名
        password: 密码

    Returns:
        验证成功返回用户信息，失败返回 None
    """
    try:
        # 禁用代理，因为这是容器内部通信
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            # 调用 NewAPI 登录接口
            response = await client.post(
                f"{NEWAPI_URL}/api/user/login",
                json={
                    "username": username,
                    "password": password
                }
            )

            if response.status_code != 200:
                logger.warning(f"NewAPI login failed with status {response.status_code}")
                return None

            data = response.json()

            # 检查登录是否成功
            if not data.get("success", False):
                logger.warning(f"NewAPI login failed: {data.get('message', 'Unknown error')}")
                return None

            user_data = data.get("data", {})

            # 检查是否是超级管理员 (role=100)
            role = user_data.get("role", 0)
            if role < 100:
                logger.warning(f"User {username} is not a super admin (role={role})")
                return None

            logger.info(f"Super admin {username} logged in successfully")
            return {
                "id": user_data.get("id"),
                "username": user_data.get("username"),
                "display_name": user_data.get("display_name"),
                "role": role
            }

    except httpx.RequestError as e:
        logger.error(f"Failed to connect to NewAPI: {e}")
        return None
    except Exception as e:
        logger.error(f"Login verification error: {e}")
        return None


async def create_session(username: str, password: str) -> Optional[Dict[str, Any]]:
    """
    创建用户会话

    Args:
        username: 用户名
        password: 密码

    Returns:
        成功返回 {token, expires_at, user}，失败返回 None
    """
    # 验证登录
    user = await verify_newapi_login(username, password)
    if not user:
        return None

    # 生成 token
    token = generate_token()
    token_hash = hash_token(token)
    expires_at = datetime.now() + timedelta(hours=TOKEN_EXPIRE_HOURS)

    # 存储 session
    _active_tokens[token_hash] = {
        "user": user,
        "expires_at": expires_at.timestamp(),
        "created_at": datetime.now().isoformat()
    }

    logger.info(f"Session created for user {username}, expires at {expires_at}")

    return {
        "token": token,
        "expires_at": expires_at.isoformat(),
        "user": user
    }


def verify_token(token: str) -> Optional[Dict[str, Any]]:
    """
    验证 token 是否有效

    Args:
        token: 用户 token

    Returns:
        有效返回用户信息，无效返回 None
    """
    if not token:
        return None

    token_hash = hash_token(token)
    session = _active_tokens.get(token_hash)

    if not session:
        return None

    # 检查是否过期
    if time.time() > session["expires_at"]:
        # 清理过期 token
        del _active_tokens[token_hash]
        logger.info("Token expired and removed")
        return None

    return session["user"]


def revoke_token(token: str) -> bool:
    """
    撤销 token（登出）

    Args:
        token: 用户 token

    Returns:
        是否成功撤销
    """
    token_hash = hash_token(token)
    if token_hash in _active_tokens:
        del _active_tokens[token_hash]
        logger.info("Token revoked successfully")
        return True
    return False


def get_active_sessions_count() -> int:
    """获取当前活跃会话数量"""
    # 清理过期的 token
    now = time.time()
    expired = [k for k, v in _active_tokens.items() if now > v["expires_at"]]
    for k in expired:
        del _active_tokens[k]

    return len(_active_tokens)


def cleanup_expired_tokens():
    """清理所有过期的 token"""
    now = time.time()
    expired = [k for k, v in _active_tokens.items() if now > v["expires_at"]]
    for k in expired:
        del _active_tokens[k]

    if expired:
        logger.info(f"Cleaned up {len(expired)} expired tokens")
