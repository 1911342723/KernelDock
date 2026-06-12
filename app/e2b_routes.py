"""
E2B 风格适配层（PoC）—— 不是协议级 E2B 兼容

提供与 E2B Code Interpreter 语义对齐的最小 REST 接口子集，
让已经按 E2B 风格（create sandbox → run_code → results）编写的
Agent 应用可以低成本切换到本服务，只需替换 base URL 并改用本路由。

映射关系：
    E2B sandbox            ↔ 本服务 session（有状态，变量跨请求保留）
    run_code results       ↔ charts（png/svg → e2b result 的 image 字段）
    logs.stdout / stderr   ↔ stdout / stderr 行
    error                  ↔ error 字段（含 traceback）

边界（重要）：
    仅覆盖 REST 子集，不实现 E2B 传输协议——无 envd 文件系统、
    WebSocket、PTY、端口转发。官方 e2b / e2b_code_interpreter SDK
    无法直连本服务；协议级兼容是独立待办（见 任务.md）。
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/e2b", tags=["E2B-style API (PoC, 非协议级兼容)"])


# ===== 请求/响应模型（对齐 E2B SDK 的字段命名习惯） =====

class E2BCreateSandboxRequest(BaseModel):
    templateID: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    timeout: Optional[int] = None


class E2BSandboxResponse(BaseModel):
    sandboxID: str
    templateID: str = "kerneldock-python"
    alias: Optional[str] = None
    metadata: Dict[str, Any] = {}


class E2BRunCodeRequest(BaseModel):
    code: str
    language: str = "python"
    timeout: Optional[int] = None


class E2BResult(BaseModel):
    """对齐 e2b_code_interpreter 的 Result 结构（子集）。"""
    text: Optional[str] = None
    png: Optional[str] = None
    svg: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None


class E2BLogs(BaseModel):
    stdout: List[str] = []
    stderr: List[str] = []


class E2BError(BaseModel):
    name: str
    value: str
    traceback: str


class E2BExecutionResponse(BaseModel):
    results: List[E2BResult] = []
    logs: E2BLogs = E2BLogs()
    error: Optional[E2BError] = None


@router.post("/sandboxes", response_model=E2BSandboxResponse)
async def e2b_create_sandbox(request: E2BCreateSandboxRequest):
    """创建沙箱（复用 /sessions 完整逻辑：含 Docker 沙箱创建与绑定）。"""
    from . import runtime
    from .routes.sessions import create_session
    from .schemas import CreateSessionRequest

    resp = await create_session(CreateSessionRequest())
    session_id = resp.session_id if hasattr(resp, "session_id") else resp["session_id"]

    if request.metadata and runtime.session_store:
        await runtime.session_store.update_metadata(session_id, request.metadata)

    return E2BSandboxResponse(
        sandboxID=session_id,
        metadata=request.metadata or {},
    )


@router.get("/sandboxes", response_model=List[E2BSandboxResponse])
async def e2b_list_sandboxes():
    """列出沙箱（映射为列出 session）。"""
    from . import runtime

    session_store = runtime.session_store
    if session_store is None:
        raise HTTPException(status_code=503, detail="Session store unavailable")

    sessions = await session_store.list_sessions()
    return [
        E2BSandboxResponse(sandboxID=s.session_id, metadata=s.metadata)
        for s in sessions
    ]


@router.delete("/sandboxes/{sandbox_id}", status_code=204)
async def e2b_delete_sandbox(sandbox_id: str):
    """销毁沙箱（映射为删除 session 及其关联容器）。"""
    from .routes.sessions import delete_session

    try:
        await delete_session(sandbox_id)
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(status_code=404, detail="Sandbox not found")
        raise


@router.post("/sandboxes/{sandbox_id}/code", response_model=E2BExecutionResponse)
async def e2b_run_code(sandbox_id: str, request: E2BRunCodeRequest):
    """
    在沙箱内执行代码（映射为 session 有状态执行）。

    返回结构对齐 e2b_code_interpreter 的 Execution 对象：
    results（图表等富结果）、logs（stdout/stderr 行）、error。
    """
    if request.language not in ("python", "py"):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported language: {request.language}（本服务仅支持 python）",
        )

    from .routes.execution import execute_code
    from .schemas import ExecuteCodeRequest

    exec_request = ExecuteCodeRequest(
        code=request.code,
        timeout=request.timeout or 300,
    )
    try:
        result = await execute_code(sandbox_id, exec_request)
    except HTTPException as e:
        if e.status_code == 404:
            raise HTTPException(status_code=404, detail="Sandbox not found")
        raise

    # ExecuteCodeResponse → E2B Execution
    payload = result.model_dump() if hasattr(result, "model_dump") else dict(result)

    results: List[E2BResult] = []
    for chart in payload.get("charts") or []:
        fmt = (chart.get("format") or "").lower()
        if fmt == "svg":
            results.append(E2BResult(svg=chart.get("base64")))
        elif fmt == "png":
            results.append(E2BResult(png=chart.get("base64")))
    for table in payload.get("tables") or []:
        results.append(E2BResult(text=None, extra={"table": table}))

    stdout_text = payload.get("stdout") or ""
    stderr_text = payload.get("stderr") or ""
    logs = E2BLogs(
        stdout=[line + "\n" for line in stdout_text.splitlines()],
        stderr=[line + "\n" for line in stderr_text.splitlines()],
    )

    error = None
    if not payload.get("success", False) and payload.get("error"):
        error_text = str(payload["error"])
        lines = error_text.strip().splitlines()
        last_line = lines[-1] if lines else "ExecutionError"
        error = E2BError(
            name=last_line.split(":")[0].strip() or "ExecutionError",
            value=last_line,
            traceback=error_text,
        )

    return E2BExecutionResponse(results=results, logs=logs, error=error)
