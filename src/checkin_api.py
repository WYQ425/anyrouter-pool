"""
签到 API 路由
提供签到相关的 HTTP 接口
"""

from fastapi import APIRouter, BackgroundTasks
from loguru import logger

from checkin_service import (
    run_checkin_for_all_accounts,
    get_checkin_status,
    checkin_status
)

router = APIRouter(prefix="/checkin", tags=["Check-in"])


@router.get("")
@router.get("/")
async def get_status():
    """获取签到状态"""
    status = get_checkin_status()
    return {
        "success": True,
        "data": status
    }


@router.post("")
@router.post("/")
async def trigger_checkin(background_tasks: BackgroundTasks):
    """手动触发签到（异步执行）"""
    logger.info("Manual check-in triggered via API")

    # 在后台执行签到，避免请求超时
    async def do_checkin():
        result = await run_checkin_for_all_accounts()
        logger.info(f"Background check-in completed: {result.get('message', 'Unknown')}")

    background_tasks.add_task(do_checkin)

    return {
        "success": True,
        "message": "Check-in started in background",
        "hint": "Use GET /checkin to check the status"
    }


@router.post("/sync")
async def trigger_checkin_sync():
    """手动触发签到（同步执行，等待完成）"""
    logger.info("Synchronous check-in triggered via API")

    result = await run_checkin_for_all_accounts()

    return {
        "success": result.get("success", False),
        "data": result
    }


@router.get("/results")
async def get_results():
    """获取最近一次签到结果详情"""
    status = get_checkin_status()
    return {
        "success": True,
        "last_run": status.get("last_run"),
        "next_run": status.get("next_run"),
        "total_success": status.get("total_success", 0),
        "total_failed": status.get("total_failed", 0),
        "results": status.get("results", [])
    }
