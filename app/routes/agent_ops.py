"""
Agent 扩展操作路由：shell 命令执行、容器内文件系统、运行时 pip 装包

这些端点都要求 Docker 沙箱模式（本地 fallback 不支持）。
session 级操作要求会话已绑定沙箱，否则 404。
"""

import base64
import logging
from typing import List

from fastapi import APIRouter, HTTPException

from .. import runtime
from ..exceptions import SandboxNotFoundError
from ..schemas import (
    FsEntry,
    FsWriteRequest,
    InstallPackagesRequest,
    InstallPackagesResponse,
    ShellExecuteRequest,
    ShellExecuteResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Agent Ops"])


def _require_sandbox_manager():
    if not runtime.sandbox_manager:
        raise HTTPException(status_code=503, detail="沙箱管理器未启用（agent ops 需要 Docker 沙箱模式）")
    return runtime.sandbox_manager


async def _resolve_session_sandbox_id(session_id: str) -> str:
    manager = _require_sandbox_manager()
    sandbox_info = await manager.get_sandbox_by_session(session_id)
    if not sandbox_info:
        raise HTTPException(status_code=404, detail="Session 不存在或未绑定沙箱")
    return sandbox_info.sandbox_id


# ===== Shell 执行 =====

@router.post("/sessions/{session_id}/shell", response_model=ShellExecuteResponse)
async def session_shell(session_id: str, request: ShellExecuteRequest):
    """在 session 沙箱内执行 shell 命令（隔离边界与代码执行一致）。"""
    sandbox_id = await _resolve_session_sandbox_id(session_id)
    manager = _require_sandbox_manager()
    try:
        result = await manager.execute_shell(
            sandbox_id,
            request.command,
            timeout=max(1, min(request.timeout, 600)),
            workdir=request.workdir,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SandboxNotFoundError:
        raise HTTPException(status_code=404, detail="沙箱不存在")
    return ShellExecuteResponse(**result)


@router.post("/execute/shell", response_model=ShellExecuteResponse)
async def stateless_shell(request: ShellExecuteRequest):
    """
    无状态 shell 执行（独占借池容器，执行后健康检查再归还）。

    注意：比 Python 无状态执行重（无 fork 隔离、独占租约），
    高频场景请用 session shell。
    """
    manager = _require_sandbox_manager()
    if runtime.execution_queue:
        async with runtime.execution_queue.acquire("stateless-shell"):
            result = await manager.execute_shell_stateless(
                request.command, timeout=max(1, min(request.timeout, 600))
            )
    else:
        result = await manager.execute_shell_stateless(
            request.command, timeout=max(1, min(request.timeout, 600))
        )
    return ShellExecuteResponse(**result)


# ===== 容器内文件系统 =====

@router.get("/sessions/{session_id}/fs/list", response_model=List[FsEntry])
async def fs_list(session_id: str, path: str = "/data"):
    """列出容器内目录（白名单：/data /output /tmp /home/sandbox）。"""
    sandbox_id = await _resolve_session_sandbox_id(session_id)
    manager = _require_sandbox_manager()
    try:
        entries = await manager.fs_list(sandbox_id, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"路径不存在: {path}")
    except NotADirectoryError:
        raise HTTPException(status_code=400, detail=f"不是目录: {path}")
    return [FsEntry(**entry) for entry in entries]


@router.get("/sessions/{session_id}/fs/read")
async def fs_read(session_id: str, path: str):
    """读取容器内单个文件，返回 base64 内容。"""
    sandbox_id = await _resolve_session_sandbox_id(session_id)
    manager = _require_sandbox_manager()
    try:
        content = await manager.fs_read(sandbox_id, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
    except IsADirectoryError:
        raise HTTPException(status_code=400, detail=f"是目录不是文件: {path}")
    return {
        "path": path,
        "size": len(content),
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


@router.put("/sessions/{session_id}/fs/write")
async def fs_write(session_id: str, request: FsWriteRequest):
    """写入容器内单个文件（自动创建父目录）。"""
    sandbox_id = await _resolve_session_sandbox_id(session_id)
    manager = _require_sandbox_manager()
    try:
        content = base64.b64decode(request.content_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="content_base64 解码失败")
    try:
        result = await manager.fs_write(sandbox_id, request.path, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@router.delete("/sessions/{session_id}/fs")
async def fs_delete(session_id: str, path: str):
    """删除容器内文件或目录（白名单根目录本身不可删）。"""
    sandbox_id = await _resolve_session_sandbox_id(session_id)
    manager = _require_sandbox_manager()
    try:
        result = await manager.fs_delete(sandbox_id, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


# ===== 运行时 pip 装包 =====

@router.post("/sessions/{session_id}/packages", response_model=InstallPackagesResponse)
async def install_packages(session_id: str, request: InstallPackagesRequest):
    """
    在 session 沙箱内 pip install（--user，装入 tmpfs 用户 site）。

    前置条件：SANDBOX_NETWORK__EGRESS_MODE=proxy（白名单代理放行 pypi）。
    安装成功后新包对后续执行立即可 import；包随容器销毁消失（不持久）。
    """
    sandbox_id = await _resolve_session_sandbox_id(session_id)
    manager = _require_sandbox_manager()
    try:
        result = await manager.install_packages(
            sandbox_id,
            request.packages,
            timeout=max(30, min(request.timeout, 900)),
        )
    except ValueError as e:
        # egress 模式不满足 / 包规格非法
        status = 409 if "egress" in str(e).lower() or "EGRESS" in str(e) else 400
        raise HTTPException(status_code=status, detail=str(e))
    except SandboxNotFoundError:
        raise HTTPException(status_code=404, detail="沙箱不存在")
    return InstallPackagesResponse(**result)
