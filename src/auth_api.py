"""
认证 API 路由
提供登录/登出接口和认证中间件
"""

import os
from typing import Optional, Callable
from functools import wraps

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from loguru import logger

from auth_service import (
    create_session,
    verify_token,
    revoke_token,
    get_active_sessions_count
)

# Dashboard 认证开关（设为 false 可跳过登录直接访问管理界面）
DASHBOARD_AUTH_ENABLED = os.getenv("DASHBOARD_AUTH_ENABLED", "true").lower() == "true"

router = APIRouter(prefix="/auth", tags=["Authentication"])

# HTTP Bearer Token 认证
security = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    """登录请求"""
    username: str
    password: str


class LoginResponse(BaseModel):
    """登录响应"""
    success: bool
    message: str
    data: Optional[dict] = None


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> Optional[dict]:
    """
    从请求中获取当前用户（用于依赖注入）

    Returns:
        用户信息或 None
    """
    if not credentials:
        return None

    token = credentials.credentials
    return verify_token(token)


async def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> dict:
    """
    要求认证的依赖项（如果未认证则抛出 401 错误）

    Returns:
        用户信息

    Raises:
        HTTPException: 未认证或 token 无效
    """
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"}
        )

    token = credentials.credentials
    user = verify_token(token)

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"}
        )

    return user


def require_admin(func: Callable):
    """
    装饰器：要求管理员权限

    用法:
        @router.get("/admin-only")
        @require_admin
        async def admin_endpoint(user: dict = Depends(require_auth)):
            ...
    """
    @wraps(func)
    async def wrapper(*args, **kwargs):
        # 从 kwargs 获取 user
        user = kwargs.get("user")
        if not user or user.get("role", 0) < 100:
            raise HTTPException(
                status_code=403,
                detail="Admin privileges required"
            )
        return await func(*args, **kwargs)
    return wrapper


@router.post("/login")
async def login(request: LoginRequest) -> dict:
    """
    用户登录

    使用 NewAPI 的超级管理员账号密码登录
    """
    logger.info(f"Login attempt for user: {request.username}")

    result = await create_session(request.username, request.password)

    if not result:
        logger.warning(f"Login failed for user: {request.username}")
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials or insufficient privileges. Only super admins can login."
        )

    return {
        "success": True,
        "message": "Login successful",
        "data": {
            "token": result["token"],
            "expires_at": result["expires_at"],
            "user": result["user"]
        }
    }


@router.post("/logout")
async def logout(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> dict:
    """
    用户登出
    """
    if credentials:
        revoke_token(credentials.credentials)

    return {
        "success": True,
        "message": "Logged out successfully"
    }


@router.get("/me")
async def get_me(user: dict = Depends(require_auth)) -> dict:
    """
    获取当前登录用户信息
    """
    return {
        "success": True,
        "data": user
    }


@router.get("/status")
async def get_auth_status(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> dict:
    """
    获取认证状态（无需认证）
    """
    user = None
    authenticated = False

    if credentials:
        user = verify_token(credentials.credentials)
        authenticated = user is not None

    return {
        "success": True,
        "data": {
            "authenticated": authenticated,
            "user": user,
            "active_sessions": get_active_sessions_count()
        }
    }


# 用于检查请求是否需要认证的辅助函数
def is_public_path(path: str) -> bool:
    """
    判断路径是否是公开的（不需要认证）

    公开路径:
    - /v1/* - API 代理路径
    - /health - 健康检查
    - /auth/* - 认证相关路由
    - / - 首页（会在前端检查认证）
    """
    public_paths = [
        "/v1/",
        "/health",
        "/auth/",
        "/docs",
        "/openapi.json",
        "/redoc"
    ]

    # 根路径需要返回 index.html，认证在前端处理
    if path == "/":
        return True

    for public_path in public_paths:
        if path.startswith(public_path):
            return True

    return False


async def auth_middleware(request: Request, call_next):
    """
    认证中间件

    检查请求是否携带有效的认证 token
    对于非公开路径，未认证请求返回 401

    可通过 DASHBOARD_AUTH_ENABLED=false 环境变量禁用认证
    """
    path = request.url.path

    # 如果禁用了 Dashboard 认证，直接放行所有请求
    if not DASHBOARD_AUTH_ENABLED:
        return await call_next(request)

    # 公开路径直接放行
    if is_public_path(path):
        return await call_next(request)

    # 检查 Authorization header
    auth_header = request.headers.get("Authorization")

    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"}
        )

    token = auth_header[7:]  # 去掉 "Bearer " 前缀
    user = verify_token(token)

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"}
        )

    # 将用户信息存储到 request.state
    request.state.user = user

    return await call_next(request)


def is_dashboard_auth_enabled() -> bool:
    """获取 Dashboard 认证是否启用"""
    return DASHBOARD_AUTH_ENABLED
