"""
沙箱管理路由：沙箱查询、销毁、资源指标与 admin 接口
"""

import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from .. import runtime
from ..config import settings
from ..services.sandbox_manager import SandboxState

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Sandboxes"])


def _require_admin_token(admin_token: Optional[str]) -> None:
    expected = (settings.admin_token or "").strip()
    if not expected or admin_token != expected:
        raise HTTPException(status_code=403, detail="Invalid admin token")


@router.get("/sandboxes")
async def list_sandboxes(
    state: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
):
    """
    列出所有沙箱

    Requirements 10.6: 新增功能通过新的 API 端点提供

    Args:
        state: 按状态过滤（creating, running, paused, stopped, error）
        limit: 返回数量限制
        offset: 偏移量

    Returns:
        沙箱列表
    """
    if not runtime.sandbox_manager:
        return {"sandboxes": [], "total": 0, "message": "沙箱管理器未启用"}

    # 解析状态过滤
    sandbox_state = None
    if state:
        try:
            sandbox_state = SandboxState(state)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"无效的状态值: {state}，有效值: creating, running, paused, stopped, error"
            )

    sandboxes = await runtime.sandbox_manager.list_sandboxes(
        state=sandbox_state,
        limit=limit,
        offset=offset
    )

    return {
        "sandboxes": [s.to_dict() for s in sandboxes],
        "total": len(sandboxes),
        "active_count": runtime.sandbox_manager.active_count,
        "max_concurrent": runtime.sandbox_manager.max_concurrent
    }


@router.get("/sandboxes/{sandbox_id}")
async def get_sandbox(sandbox_id: str):
    """
    获取沙箱详情

    Requirements 10.6: 新增功能通过新的 API 端点提供
    """
    if not runtime.sandbox_manager:
        raise HTTPException(status_code=503, detail="沙箱管理器未启用")

    sandbox_info = await runtime.sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox_info:
        raise HTTPException(status_code=404, detail="沙箱不存在")

    return sandbox_info.to_dict()


@router.delete("/sandboxes/{sandbox_id}")
async def destroy_sandbox(sandbox_id: str):
    """
    销毁沙箱

    Requirements 10.6: 新增功能通过新的 API 端点提供
    """
    if not runtime.sandbox_manager:
        raise HTTPException(status_code=503, detail="沙箱管理器未启用")

    success = await runtime.sandbox_manager.destroy_sandbox(sandbox_id)
    if not success:
        raise HTTPException(status_code=404, detail="沙箱不存在")

    return {"success": True, "message": f"沙箱 {sandbox_id} 已销毁"}


@router.get("/sandboxes/{sandbox_id}/metrics")
async def get_sandbox_metrics(sandbox_id: str):
    """
    获取沙箱资源使用指标

    Requirements 9.4: 提供沙箱级别的资源使用指标查询接口
    Requirements 10.6: 新增功能通过新的 API 端点提供
    """
    if not runtime.health_monitor:
        raise HTTPException(status_code=503, detail="健康监控器未启用")

    metrics = await runtime.health_monitor.get_sandbox_metrics(sandbox_id)
    if not metrics:
        raise HTTPException(status_code=404, detail="沙箱不存在或无法获取指标")

    return metrics.to_dict()


@router.get("/admin/sandboxes")
async def admin_list_sandboxes(x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token")):
    _require_admin_token(x_admin_token)
    if not runtime.sandbox_manager:
        return []

    sandboxes = await runtime.sandbox_manager.list_sandboxes()
    items = []
    for sandbox in sandboxes:
        metrics = await runtime.health_monitor.get_sandbox_metrics(sandbox.sandbox_id) if runtime.health_monitor else None
        context_count = len(runtime.context_manager.list_contexts(sandbox.session_id)) if runtime.context_manager else 0
        items.append(
            {
                "container_id": sandbox.container_id,
                "session_id": sandbox.session_id,
                "context_count": context_count,
                "created_at": sandbox.created_at.isoformat(),
                "last_execution_at": sandbox.last_activity.isoformat(),
                "cpu_usage": round(metrics.cpu_percent, 2) if metrics else 0.0,
                "memory_mb": round(metrics.memory_used_mb, 2) if metrics else 0.0,
            }
        )
    return items
