"""
会话路由：会话生命周期、代码上下文、数据加载与文件管理
"""

import base64
import logging
import os
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile

from .. import runtime
from ..context_helpers import _require_context_manager, _serialize_context
from ..executor import session_manager
from ..schemas import (
    ContextResponse,
    CreateContextRequest,
    CreateSessionRequest,
    CreateSessionResponse,
    LoadDataRequest,
    LoadDataResponse,
    MultiTableContextResponse,
    TableSchemaResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Sessions"])


@router.post("/sessions", response_model=CreateSessionResponse)
async def create_session(request: CreateSessionRequest):
    """
    创建新的执行会话

    Requirements 10.1, 10.3: 保持现有 API 接口格式不变

    如果沙箱管理器可用，会创建 Docker 沙箱；
    否则使用本地执行模式。
    """
    session_id = request.session_id

    # 使用新的会话存储记录会话
    if runtime.session_store:
        session_info = await runtime.session_store.create_session(
            session_id=session_id,
            metadata={"mode": "sandbox" if runtime.sandbox_manager else "local"}
        )
        session_id = session_info.session_id

    # 创建沙箱（如果沙箱管理器可用）
    sandbox_info = None
    if runtime.sandbox_manager:
        try:
            # 透传 per-沙箱资源分配（超软上限自动收敛，省略则用全局默认值）
            sandbox_info = await runtime.sandbox_manager.create_sandbox(
                session_id=session_id,
                cpu_limit=request.cpu_limit,
                memory_limit_mb=request.memory_limit_mb,
                disk_limit_mb=request.disk_limit_mb,
                pids_limit=request.pids_limit,
            )

            # 更新会话存储中的沙箱 ID
            if runtime.session_store:
                await runtime.session_store.update_sandbox_id(session_id, sandbox_info.sandbox_id)

            logger.info(
                f"创建沙箱会话: {session_id}, 沙箱: {sandbox_info.sandbox_id}, "
                f"资源: CPU={sandbox_info.cpu_limit} 内存={sandbox_info.memory_limit_mb}MB "
                f"磁盘={sandbox_info.disk_limit_mb}MB 进程={sandbox_info.pids_limit}"
            )
        except Exception as e:
            logger.warning(f"创建沙箱失败，回退到本地模式: {e}")
            sandbox_info = None

    # StatelessSession is only used for local file management (schemas, data loading).
    # Container lifecycle is managed exclusively by SandboxManager.
    session = session_manager.create_session(session_id)

    return CreateSessionResponse(
        session_id=session.session_id,
        workspace_dir=session.workspace_dir,
        data_dir=session.data_dir,
        output_dir=session.output_dir,
        # 回显经软上限收敛后真正生效的资源（本地回退无沙箱时为 None）
        cpu_limit=sandbox_info.cpu_limit if sandbox_info else None,
        memory_limit_mb=sandbox_info.memory_limit_mb if sandbox_info else None,
        disk_limit_mb=sandbox_info.disk_limit_mb if sandbox_info else None,
        pids_limit=sandbox_info.pids_limit if sandbox_info else None,
    )


@router.get("/sessions/{session_id}/contexts", response_model=List[ContextResponse])
async def list_session_contexts(session_id: str):
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    context_manager = _require_context_manager()
    return [_serialize_context(context) for context in context_manager.list_contexts(session_id)]


@router.post("/sessions/{session_id}/contexts", response_model=ContextResponse)
async def create_session_context(session_id: str, request: CreateContextRequest):
    session = session_manager.get_or_create_session(session_id)
    if runtime.session_store:
        await runtime.session_store.update_activity(session.session_id)
    context_manager = _require_context_manager()
    if request.fork_from:
        fork_source = context_manager.get_context(request.fork_from)
        if fork_source is None or fork_source.session_id != session_id:
            raise HTTPException(status_code=404, detail="Fork source context not found")
    context = context_manager.create_context(
        session_id=session_id,
        fork_from=request.fork_from,
        language=request.language,
    )
    return _serialize_context(context)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """
    获取会话信息

    Requirements 10.1, 10.3: 保持现有 API 接口格式不变
    """
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    response = {
        "session_id": session.session_id,
        "workspace_dir": session.workspace_dir,
        "data_dir": session.data_dir,
        "output_dir": session.output_dir,
        "data_files": session.data_files,
        "created_at": session.created_at.isoformat()
    }

    # 添加沙箱信息（如果有）
    if runtime.sandbox_manager:
        sandbox_info = await runtime.sandbox_manager.get_sandbox_by_session(session_id)
        if sandbox_info:
            response["sandbox_id"] = sandbox_info.sandbox_id
            response["sandbox_state"] = sandbox_info.state.value

    return response


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """
    删除会话

    Requirements 10.1, 10.3: 保持现有 API 接口格式不变
    """
    # 1. Destroy sandbox container (SandboxManager owns container lifecycle)
    if runtime.sandbox_manager:
        sandbox_info = await runtime.sandbox_manager.get_sandbox_by_session(session_id)
        if sandbox_info:
            await runtime.sandbox_manager.destroy_sandbox(sandbox_info.sandbox_id)

    # 2. Remove from session metadata store
    if runtime.session_store:
        await runtime.session_store.delete_session(session_id)

    # 3. Clean local workspace files via StatelessSession
    success = session_manager.delete_session(session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")

    if runtime.context_manager:
        for context in list(runtime.context_manager.list_contexts(session_id)):
            runtime.context_manager.delete_context(context.context_id)

    return {"success": True, "message": f"Session {session_id} deleted"}


@router.delete("/sessions/{session_id}/contexts/{context_id}", status_code=204)
async def delete_session_context(session_id: str, context_id: str):
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    context_manager = _require_context_manager()
    context = context_manager.get_context(context_id)
    if context is None or context.session_id != session_id:
        raise HTTPException(status_code=404, detail="Context not found")
    context_manager.delete_context(context_id)
    return Response(status_code=204)


# ===== 数据与文件 API =====

@router.post("/sessions/{session_id}/load-data", response_model=LoadDataResponse)
async def load_data(session_id: str, request: LoadDataRequest):
    """
    加载 JSON 数据

    Requirements 10.1: 保持现有 API 接口格式不变
    """
    session = session_manager.get_or_create_session(session_id)

    # 更新会话活动时间
    if runtime.session_store:
        await runtime.session_store.update_activity(session_id)
        await runtime.session_store.add_data_file(session_id, request.filename)

    result = await session.load_data(request.data_json, request.filename)
    return LoadDataResponse(**result)


@router.post("/sessions/{session_id}/upload")
async def upload_file(
    session_id: str,
    file: UploadFile = File(...),
    filename: Optional[str] = Form(None)
):
    """
    上传文件

    Requirements 10.1, 10.5: 保持现有 API 接口格式不变
    """
    session = session_manager.get_or_create_session(session_id)
    content = await file.read()
    target_filename = filename or file.filename

    # 更新会话活动时间和文件列表
    if runtime.session_store:
        await runtime.session_store.update_activity(session_id)
        await runtime.session_store.add_data_file(session_id, target_filename)

    result = await session.load_file(content, target_filename)
    return result


@router.get("/sessions/{session_id}/schemas", response_model=List[TableSchemaResponse])
async def get_table_schemas(session_id: str):
    """
    获取表格模式

    Requirements 10.1: 保持现有 API 接口格式不变
    """
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    schemas = session.get_table_schemas()
    return schemas


@router.get("/sessions/{session_id}/context", response_model=MultiTableContextResponse)
async def get_multi_table_context(session_id: str):
    """
    获取多表格上下文

    Requirements 10.1: 保持现有 API 接口格式不变
    """
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    context = session.get_multi_table_context()
    return context


@router.get("/sessions/{session_id}/files")
async def list_files(session_id: str):
    """
    列出会话中的文件

    Requirements 10.1, 10.5: 保持现有 API 接口格式不变
    """
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    files = {"data": [], "output": []}

    if os.path.exists(session.data_dir):
        for f in os.listdir(session.data_dir):
            file_path = os.path.join(session.data_dir, f)
            if os.path.isfile(file_path):
                files["data"].append({
                    "name": f,
                    "size": os.path.getsize(file_path),
                    "path": file_path
                })

    if os.path.exists(session.output_dir):
        for f in os.listdir(session.output_dir):
            file_path = os.path.join(session.output_dir, f)
            if os.path.isfile(file_path):
                files["output"].append({
                    "name": f,
                    "size": os.path.getsize(file_path),
                    "path": file_path
                })

    return files


@router.get("/sessions/{session_id}/files/{file_type}/{filename}")
async def download_file(session_id: str, file_type: str, filename: str):
    """
    下载文件

    Requirements 10.1, 10.5: 保持现有 API 接口格式不变
    """
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if file_type == "data":
        file_path = os.path.join(session.data_dir, filename)
    elif file_type == "output":
        file_path = os.path.join(session.output_dir, filename)
    else:
        raise HTTPException(status_code=400, detail="Invalid file type")

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    with open(file_path, 'rb') as f:
        content = base64.b64encode(f.read()).decode('utf-8')

    return {
        "filename": filename,
        "content_base64": content,
        "size": os.path.getsize(file_path)
    }
