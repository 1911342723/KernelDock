"""
资源配置管理路由：查看与运行时调整沙箱资源默认值 / 软上限。

- GET  /resource-config        只读查看（默认值 / 软上限 / 绝对护栏 / 持久化状态）
- PUT  /admin/resource-config  运行时调整（admin token 保护，热生效 + 持久化）
"""

import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from ..config import settings
from ..schemas import ResourceConfigUpdateRequest
from ..services.resource_config import (
    apply_config,
    build_view,
    get_resource_config_store,
    validate_and_clamp,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Resource Config"])


def _require_admin_token(admin_token: Optional[str]) -> None:
    expected = (settings.admin_token or "").strip()
    if not expected or admin_token != expected:
        raise HTTPException(status_code=403, detail="Invalid admin token")


@router.get("/resource-config")
async def get_resource_config():
    """
    查看当前资源配置。

    返回当前生效的默认值、单沙箱可分配软上限、绝对护栏区间与持久化状态。
    只读，无需鉴权，便于运维/前端展示与 Agent 自适应分配。
    """
    return build_view()


@router.put("/admin/resource-config")
async def update_resource_config(
    request: ResourceConfigUpdateRequest,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """
    运行时调整资源默认值 / 软上限（admin token 保护）。

    - 仅传需要修改的字段，省略的保持不变；
    - 超出绝对护栏的值自动收敛(clamp)，默认值不得超过软上限（超过则下调到上限）；
    - 变更立即热生效（刷新资源限制器），并持久化到文件（重启后仍保留）。
    """
    _require_admin_token(x_admin_token)

    payload = request.model_dump(exclude_none=True)
    merged, warnings = validate_and_clamp(payload)
    apply_config(merged)

    persisted = get_resource_config_store().save(merged)
    if not persisted:
        warnings.append(
            "持久化未生效（WORKSPACE_DIR 不可用），本次变更仅进程内有效，重启会丢失"
        )

    logger.info(f"资源配置已更新: {merged}, warnings={warnings}")
    return {
        "success": True,
        "applied": merged,
        "warnings": warnings,
        "persisted": persisted,
        "config": build_view(),
    }
