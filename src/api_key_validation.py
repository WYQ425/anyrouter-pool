"""
API Key 验证模块
通过 NewAPI 验证传入的 API Key 是否有效
"""

import os
import time
from typing import Optional, Tuple
from functools import lru_cache

import httpx
from loguru import logger

# 配置
NEWAPI_URL = os.getenv("NEWAPI_URL", "http://new-api:3000")
API_KEY_VALIDATION_ENABLED = os.getenv("API_KEY_VALIDATION_ENABLED", "false").lower() == "true"

# 缓存验证结果（避免频繁调用 NewAPI）
# 格式: {api_key: (is_valid, expire_time)}
_validation_cache: dict[str, Tuple[bool, float]] = {}
CACHE_TTL = 300  # 缓存 5 分钟


def is_validation_enabled() -> bool:
    """检查是否启用了 API Key 验证"""
    return API_KEY_VALIDATION_ENABLED


def extract_api_key(request) -> Optional[str]:
    """
    从请求中提取 API Key
    支持两种格式:
    1. x-api-key: sk-xxx
    2. Authorization: Bearer sk-xxx
    """
    # 尝试从 x-api-key header 获取
    api_key = request.headers.get("x-api-key")
    if api_key:
        return api_key

    # 尝试从 Authorization header 获取
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()

    return None


async def validate_api_key(api_key: str) -> Tuple[bool, Optional[str]]:
    """
    验证 API Key 是否有效

    Returns:
        Tuple[bool, Optional[str]]: (是否有效, 错误信息)
    """
    if not api_key:
        return False, "API key is required"

    # 检查缓存
    cached = _validation_cache.get(api_key)
    if cached:
        is_valid, expire_time = cached
        if time.time() < expire_time:
            logger.debug(f"API key validation cache hit: {'valid' if is_valid else 'invalid'}")
            return is_valid, None if is_valid else "Invalid API key (cached)"

    # 调用 NewAPI 验证
    try:
        # 使用 trust_env=False 避免使用代理访问内部服务
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            response = await client.get(
                f"{NEWAPI_URL}/api/user/self",
                headers={"Authorization": f"Bearer {api_key}"}
            )

            if response.status_code == 200:
                data = response.json()
                if data.get("success") and data.get("data"):
                    # 验证成功，缓存结果
                    _validation_cache[api_key] = (True, time.time() + CACHE_TTL)
                    user_data = data.get("data", {})
                    logger.info(f"API key validated for user: {user_data.get('username', 'unknown')}")
                    return True, None

            # 验证失败
            _validation_cache[api_key] = (False, time.time() + CACHE_TTL)
            logger.warning(f"API key validation failed: status={response.status_code}")
            return False, "Invalid API key"

    except httpx.ConnectError as e:
        logger.error(f"Failed to connect to NewAPI for validation: {e}")
        # 连接失败时不缓存，下次重试
        return False, "Authentication service unavailable"
    except Exception as e:
        logger.error(f"API key validation error: {e}")
        return False, f"Validation error: {str(e)}"


def clear_validation_cache():
    """清除验证缓存"""
    global _validation_cache
    _validation_cache = {}
    logger.info("API key validation cache cleared")


def get_validation_stats() -> dict:
    """获取验证统计信息"""
    now = time.time()
    valid_count = sum(1 for v in _validation_cache.values() if v[0] and v[1] > now)
    invalid_count = sum(1 for v in _validation_cache.values() if not v[0] and v[1] > now)
    expired_count = sum(1 for v in _validation_cache.values() if v[1] <= now)

    return {
        "enabled": API_KEY_VALIDATION_ENABLED,
        "cache_size": len(_validation_cache),
        "valid_keys_cached": valid_count,
        "invalid_keys_cached": invalid_count,
        "expired_entries": expired_count,
        "cache_ttl_seconds": CACHE_TTL
    }
