"""
执行路由：有状态执行、SSE 流式执行、无状态（即用即毁）执行

包含队列/沙箱/执行三类响应元数据的组装辅助。
"""

import asyncio
import base64
import json
import logging
import os
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from .. import runtime
from ..config import settings
from ..context_helpers import (
    _render_context_bootstrap,
    _require_context_manager,
    _resolve_execute_context_id,
)
from ..executor import session_manager
from ..observability import (
    _log_execution_event,
    _record_execution_metrics,
    _report_execution_failure_to_sentry,
)
from ..schemas import (
    ExecuteCodeRequest,
    ExecuteCodeResponse,
    ExecutionInfoResponse,
    QueueInfoResponse,
    SandboxInfoResponse,
    StatelessExecuteRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Execution"])


# ===== 响应元数据组装 =====

def _build_queue_info(ticket) -> QueueInfoResponse:
    """Build QueueInfoResponse from a QueueTicket and global queue state."""
    waited = 0.0
    if ticket.started_at:
        waited = round(ticket.started_at - ticket.enqueued_at, 2)
    global_status = runtime.execution_queue.get_global_status() if runtime.execution_queue else {}
    return QueueInfoResponse(
        position_on_entry=ticket.position,
        waited_seconds=waited,
        estimated_wait_seconds=round(ticket.estimated_wait_seconds, 2),
        queue_depth=global_status.get("queued_count", 0),
        executing_count=global_status.get("executing_count", 0),
        max_concurrent=global_status.get("max_concurrent", 0),
        avg_execution_time=global_status.get("avg_execution_time", 0),
        total_enqueued=global_status.get("total_enqueued", 0),
        total_executed=global_status.get("total_executed", 0),
    )


async def _build_sandbox_info(
    session_id: str,
    *,
    mode: str = "unknown",
    sandbox_info_obj=None,
) -> SandboxInfoResponse:
    """Build SandboxInfoResponse from SandboxManager state."""
    pool_available = None
    pool_total = None
    if runtime.sandbox_manager and runtime.sandbox_manager._container_pool:
        pool_available = runtime.sandbox_manager._container_pool.available_count
        pool_total = runtime.sandbox_manager._container_pool.pool_size

    if sandbox_info_obj:
        return SandboxInfoResponse(
            sandbox_id=sandbox_info_obj.sandbox_id,
            container_id_short=sandbox_info_obj.container_id[:12] if sandbox_info_obj.container_id else None,
            mode=mode,
            state=sandbox_info_obj.state.value if sandbox_info_obj.state else None,
            cpu_limit=sandbox_info_obj.cpu_limit,
            memory_limit_mb=sandbox_info_obj.memory_limit_mb,
            network_enabled=sandbox_info_obj.network_enabled,
            pool_available=pool_available,
            pool_total=pool_total,
        )

    return SandboxInfoResponse(
        mode=mode,
        pool_available=pool_available,
        pool_total=pool_total,
    )


def _build_execution_info(
    result: dict,
    *,
    code: str,
    timeout: int,
    execution_path: str = "unknown",
) -> ExecutionInfoResponse:
    """Build ExecutionInfoResponse from the raw execution result dict."""
    output_text = result.get("output", "")
    return ExecutionInfoResponse(
        execution_time_ms=result.get("execution_time_ms", 0),
        execution_path=execution_path,
        code_size_bytes=len(code.encode("utf-8")),
        timeout_configured=timeout,
        timed_out="超时" in result.get("error", "") if result.get("error") else False,
        chart_count=len(result.get("charts", [])),
        table_count=len(result.get("tables", [])),
        output_truncated=output_text.endswith("(输出已截断)"),
        output_size_bytes=len(output_text.encode("utf-8")),
    )


def _encode_sse(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def _yield_result_as_events(result: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    stdout = result.get("stdout") or ""
    if stdout:
        yield {"type": "stdout", "text": stdout}

    stderr = result.get("stderr") or ""
    if stderr:
        yield {"type": "stderr", "text": stderr}

    for chart in result.get("charts", []) or []:
        yield {"type": "chart", "chart": chart}

    for table in result.get("tables", []) or []:
        yield {"type": "table", "table": table}

    if result.get("error"):
        yield {"type": "error", "error": result.get("error")}

    yield {
        "type": "done",
        "success": result.get("success", False),
        "error": result.get("error"),
        "execution_time_ms": result.get("execution_time_ms", 0),
        "context_id": result.get("context_id"),
        "timed_out": "超时" in (result.get("error") or ""),
    }


# ===== 有状态执行 =====

async def _execute_streaming(session_id: str, request: ExecuteCodeRequest) -> AsyncIterator[str]:
    session = session_manager.get_or_create_session(session_id)
    if runtime.session_store:
        await runtime.session_store.update_activity(session_id)

    request.context_id = _resolve_execute_context_id(session_id, request.context_id)
    context = _require_context_manager().get_context(request.context_id)
    start_sandbox = await runtime.sandbox_manager.get_sandbox_by_session(session_id) if runtime.sandbox_manager else None
    _log_execution_event(
        "execute_start",
        session_id=session_id,
        context_id=request.context_id,
        container_id=start_sandbox.container_id if start_sandbox else None,
        code=request.code,
    )
    if context and request.pre_load_parquet:
        refs = tuple(sorted(request.pre_load_parquet.keys()))
        context.data_refs = refs
        if refs and (context.focus_ref is None or context.focus_ref not in refs):
            context.focus_ref = min(refs)
        context.touch()

    async def _stream_kernel_events() -> AsyncIterator[dict[str, Any]]:
        if request.pre_load_parquet and not request.bootstrap_source:
            yield {
                "type": "error",
                "error": "ProtocolError: pre_load_parquet requires bootstrap_source",
            }
            yield {
                "type": "done",
                "success": False,
                "error": "ProtocolError: pre_load_parquet requires bootstrap_source",
                "execution_time_ms": 0,
                "context_id": request.context_id,
                "timed_out": False,
            }
            return

        if runtime.sandbox_manager:
            sandbox_info = await runtime.sandbox_manager.get_sandbox_by_session(session_id)
            if sandbox_info:
                bootstrap_source = request.bootstrap_source
                if bootstrap_source is None and context is not None:
                    bootstrap_source = _render_context_bootstrap(
                        context,
                        data_dir="/data",
                        output_dir="/output",
                    )
                async for event in runtime.sandbox_manager.stream_execute_code(
                    sandbox_id=sandbox_info.sandbox_id,
                    code=request.code,
                    timeout=request.timeout,
                    pre_load_parquet=request.pre_load_parquet,
                    bootstrap_source=bootstrap_source,
                    context_id=request.context_id,
                ):
                    yield event
                return

        result, _, _ = await _do_execute_code(session_id, session, request)
        async for event in _yield_result_as_events(result):
            yield event

    if runtime.execution_queue:
        async with runtime.execution_queue.acquire(session_id) as ticket:
            yield _encode_sse("queue", _build_queue_info(ticket).model_dump())
            async for event in _stream_kernel_events():
                if event.get("type") == "done":
                    _record_execution_metrics(
                        {
                            "success": event.get("success", False),
                            "execution_time_ms": event.get("execution_time_ms", 0),
                        }
                    )
                    _log_execution_event(
                        "execute_end",
                        session_id=session_id,
                        context_id=request.context_id,
                        container_id=start_sandbox.container_id if start_sandbox else None,
                        code=request.code,
                        duration_ms=int(event.get("execution_time_ms", 0) or 0),
                        success=bool(event.get("success", False)),
                        chart_count=1 if event.get("chart") else 0,
                        table_count=1 if event.get("table") else 0,
                    )
                    if not event.get("success", False):
                        await _report_execution_failure_to_sentry(
                            session_id=session_id,
                            container_id=start_sandbox.container_id if start_sandbox else None,
                            result={
                                "success": event.get("success", False),
                                "error": event.get("error"),
                                "execution_time_ms": event.get("execution_time_ms", 0),
                            },
                        )
                yield _encode_sse(event.get("type", "message"), event)
                await asyncio.sleep(0)
    else:
        async for event in _stream_kernel_events():
            yield _encode_sse(event.get("type", "message"), event)
            await asyncio.sleep(0)


@router.post("/sessions/{session_id}/execute", response_model=ExecuteCodeResponse)
async def execute_code(session_id: str, request: ExecuteCodeRequest):
    """
    执行代码

    Requirements 10.1, 10.4: 保持现有 API 接口格式不变

    如果执行队列可用，通过令牌桶控制并发；
    如果会话有关联的沙箱，使用 Docker exec 执行；
    否则使用本地 subprocess 执行。
    """
    session = session_manager.get_or_create_session(session_id)

    if runtime.session_store:
        await runtime.session_store.update_activity(session_id)

    request.context_id = _resolve_execute_context_id(session_id, request.context_id)
    context = _require_context_manager().get_context(request.context_id)
    start_sandbox = await runtime.sandbox_manager.get_sandbox_by_session(session_id) if runtime.sandbox_manager else None
    _log_execution_event(
        "execute_start",
        session_id=session_id,
        context_id=request.context_id,
        container_id=start_sandbox.container_id if start_sandbox else None,
        code=request.code,
    )
    if context and request.pre_load_parquet:
        refs = tuple(sorted(request.pre_load_parquet.keys()))
        context.data_refs = refs
        if refs and (context.focus_ref is None or context.focus_ref not in refs):
            context.focus_ref = min(refs)
        context.touch()

    if runtime.execution_queue:
        async with runtime.execution_queue.acquire(session_id) as ticket:
            result, exec_path, sb_info = await _do_execute_code(session_id, session, request)
            _record_execution_metrics(result)
            _log_execution_event(
                "execute_end",
                session_id=session_id,
                context_id=request.context_id,
                container_id=sb_info.container_id if sb_info else (start_sandbox.container_id if start_sandbox else None),
                code=request.code,
                duration_ms=int(result.get("execution_time_ms", 0) or 0),
                success=bool(result.get("success", False)),
                chart_count=len(result.get("charts", []) or []),
                table_count=len(result.get("tables", []) or []),
            )
            if not result.get("success", False):
                await _report_execution_failure_to_sentry(
                    session_id=session_id,
                    container_id=sb_info.container_id if sb_info else (start_sandbox.container_id if start_sandbox else None),
                    result=result,
                )
            queue_info = _build_queue_info(ticket)
            sandbox_info = await _build_sandbox_info(session_id, mode=exec_path, sandbox_info_obj=sb_info)
            exec_info = _build_execution_info(result, code=request.code, timeout=request.timeout, execution_path=exec_path)
            if isinstance(result, dict):
                result["queue_info"] = queue_info
                result["sandbox_info"] = sandbox_info
                result["execution_info"] = exec_info
                return ExecuteCodeResponse(**result)
            return result
    else:
        result, exec_path, sb_info = await _do_execute_code(session_id, session, request)
        _record_execution_metrics(result)
        _log_execution_event(
            "execute_end",
            session_id=session_id,
            context_id=request.context_id,
            container_id=sb_info.container_id if sb_info else (start_sandbox.container_id if start_sandbox else None),
            code=request.code,
            duration_ms=int(result.get("execution_time_ms", 0) or 0),
            success=bool(result.get("success", False)),
            chart_count=len(result.get("charts", []) or []),
            table_count=len(result.get("tables", []) or []),
        )
        if not result.get("success", False):
            await _report_execution_failure_to_sentry(
                session_id=session_id,
                container_id=sb_info.container_id if sb_info else (start_sandbox.container_id if start_sandbox else None),
                result=result,
            )
        sandbox_info = await _build_sandbox_info(session_id, mode=exec_path, sandbox_info_obj=sb_info)
        exec_info = _build_execution_info(result, code=request.code, timeout=request.timeout, execution_path=exec_path)
        if isinstance(result, dict):
            result["sandbox_info"] = sandbox_info
            result["execution_info"] = exec_info
            return ExecuteCodeResponse(**result)
        return result


@router.post("/v2/sessions/{session_id}/execute")
async def execute_code_stream_v2(session_id: str, request: ExecuteCodeRequest):
    return StreamingResponse(
        _execute_streaming(session_id, request),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


async def _do_execute_code(session_id: str, session, request: ExecuteCodeRequest):
    """
    Execute code via SandboxManager (Docker) when available, otherwise fall
    back to local subprocess execution through StatelessSession.

    Returns:
        (result_dict, execution_path, sandbox_info_obj_or_None)
    """
    # multi-table-analysis: 协议合约—pre_load_parquet 必须与 bootstrap_source 配对
    if request.pre_load_parquet and not request.bootstrap_source:
        return {
            "success": False,
            "stdout": "",
            "stderr": "pre_load_parquet provided without bootstrap_source",
            "output": "[ProtocolError]: pre_load_parquet requires bootstrap_source",
            "charts": [],
            "tables": [],
            "images": [],
            "error": "ProtocolError: pre_load_parquet requires bootstrap_source",
            "execution_time_ms": 0,
        }, "protocol_error", None

    context = None
    if request.context_id and runtime.context_manager:
        context = runtime.context_manager.get_context(request.context_id)

    if runtime.sandbox_manager:
        sandbox_info = await runtime.sandbox_manager.get_sandbox_by_session(session_id)
        if sandbox_info:
            try:
                bootstrap_source = request.bootstrap_source
                if bootstrap_source is None and context is not None:
                    bootstrap_source = _render_context_bootstrap(
                        context,
                        data_dir="/data",
                        output_dir="/output",
                    )
                result = await runtime.sandbox_manager.execute_code(
                    sandbox_id=sandbox_info.sandbox_id,
                    code=request.code,
                    timeout=request.timeout,
                    pre_load_parquet=request.pre_load_parquet,
                    bootstrap_source=bootstrap_source,
                    context_id=request.context_id,
                )
                return result, "sandbox_kernel", sandbox_info
            except Exception as e:
                if not settings.allow_local_fallback:
                    logger.error(f"沙箱执行失败，本地回退已禁用: {e}")
                    return {
                        "success": False,
                        "stdout": "",
                        "stderr": str(e),
                        "output": f"[SandboxError]: {e}",
                        "charts": [],
                        "tables": [],
                        "images": [],
                        "error": f"Sandbox execution failed: {e}",
                        "execution_time_ms": 0,
                    }, "sandbox_kernel", sandbox_info
                logger.error(f"沙箱执行失败，回退到本地执行: {e}")

    if not settings.allow_local_fallback:
        return {
            "success": False,
            "stdout": "",
            "stderr": "Sandbox manager is unavailable and local fallback is disabled",
            "output": "[SandboxError]: 沙箱服务不可用，且本地执行回退已禁用",
            "charts": [],
            "tables": [],
            "images": [],
            "error": "Sandbox manager unavailable",
            "execution_time_ms": 0,
        }, "sandbox_unavailable", None

    # 本地 fallback：把 pre_load_parquet 落到 session.data_dir 供 bootstrap_source 读
    if request.pre_load_parquet:
        import base64 as _b64
        os.makedirs(session.data_dir, exist_ok=True)
        for ref, b64_content in request.pre_load_parquet.items():
            safe_ref = os.path.basename(ref)
            if not safe_ref or safe_ref != ref:
                logger.warning(f"[REST] skip invalid ref={ref!r}")
                continue
            try:
                raw = _b64.b64decode(b64_content)
            except Exception as e:
                logger.warning(f"[REST] decode parquet failed ref={ref}: {e}")
                continue
            with open(os.path.join(session.data_dir, f"{safe_ref}.parquet"), "wb") as f:
                f.write(raw)

    result = await session.execute_code(
        request.code,
        request.timeout,
        bootstrap_source=request.bootstrap_source
        or (
            _render_context_bootstrap(
                context,
                data_dir=session.data_dir,
                output_dir=session.output_dir,
            )
            if context is not None
            else None
        ),
    )
    return result, "local_subprocess", None


# ===== 即用即毁（Fire-and-Forget）无状态执行 =====

@router.post("/execute", response_model=ExecuteCodeResponse)
async def execute_stateless(request: StatelessExecuteRequest):
    """
    无状态代码执行（即用即毁模式）。

    优先使用沙箱管理器（借容器 → 执行 → 归还），
    沙箱不可用时回退到本地 StatelessSession 执行。

    请求体:
        code: Python 代码
        timeout: 执行超时（秒，默认 30）
        data_files: 数据文件字典 {filename: base64_content}（可选）
    """
    if request.data_files:
        total_size = sum(len(v) for v in request.data_files.values())
        max_size = settings.fire_and_forget.max_data_size_mb * 1024 * 1024
        if total_size > max_size:
            raise HTTPException(
                status_code=413,
                detail=f"数据文件总大小超过限制（{settings.fire_and_forget.max_data_size_mb}MB）"
            )

    # multi-table-analysis: 协议合约
    if request.pre_load_parquet and not request.bootstrap_source:
        raise HTTPException(
            status_code=400,
            detail="pre_load_parquet provided without bootstrap_source",
        )

    exec_path = "unknown"
    _log_execution_event(
        "execute_start",
        session_id="stateless",
        context_id=request.context_id,
        container_id=None,
        code=request.code,
    )

    async def _do_execute():
        nonlocal exec_path
        if runtime.sandbox_manager:
            exec_path = "stateless_pool_kernel"
            try:
                return await runtime.sandbox_manager.execute_stateless(
                    code=request.code,
                    data_files=request.data_files,
                    timeout=request.timeout,
                    pre_load_parquet=request.pre_load_parquet,
                    bootstrap_source=request.bootstrap_source,
                    context_id=request.context_id,
                )
            except Exception as e:
                if not settings.allow_local_fallback:
                    logger.error(f"无状态沙箱执行失败，本地回退已禁用: {e}")
                    return {
                        "success": False,
                        "stdout": "",
                        "stderr": str(e),
                        "output": f"[SandboxError]: {e}",
                        "charts": [],
                        "tables": [],
                        "images": [],
                        "error": f"Sandbox execution failed: {e}",
                        "execution_time_ms": 0,
                    }
                logger.error(f"无状态沙箱执行失败，回退到本地执行: {e}")

        if not settings.allow_local_fallback:
            exec_path = "sandbox_unavailable"
            return {
                "success": False,
                "stdout": "",
                "stderr": "Sandbox manager is unavailable and local fallback is disabled",
                "output": "[SandboxError]: 沙箱服务不可用，且本地执行回退已禁用",
                "charts": [],
                "tables": [],
                "images": [],
                "error": "Sandbox manager unavailable",
                "execution_time_ms": 0,
            }
        logger.info("沙箱管理器不可用，使用本地执行模式")
        exec_path = "local_subprocess"
        import uuid as _uuid
        tmp_session_id = f"stateless-{_uuid.uuid4().hex[:8]}"
        tmp_session = session_manager.create_session(tmp_session_id)
        try:
            if request.data_files:
                for fname, b64_content in request.data_files.items():
                    file_bytes = base64.b64decode(b64_content)
                    await tmp_session.load_file(file_bytes, fname)
            # multi-table-analysis: 本地 fallback 下把 parquet 落到 tmp session
            if request.pre_load_parquet:
                os.makedirs(tmp_session.data_dir, exist_ok=True)
                for ref, b64_content in request.pre_load_parquet.items():
                    safe_ref = os.path.basename(ref)
                    if not safe_ref or safe_ref != ref:
                        logger.warning(f"[stateless] skip invalid ref={ref!r}")
                        continue
                    try:
                        raw = base64.b64decode(b64_content)
                    except Exception as e:
                        logger.warning(f"[stateless] decode parquet failed ref={ref}: {e}")
                        continue
                    with open(
                        os.path.join(tmp_session.data_dir, f"{safe_ref}.parquet"),
                        "wb",
                    ) as f:
                        f.write(raw)
            return await tmp_session.execute_code(
                request.code,
                request.timeout,
                bootstrap_source=request.bootstrap_source,
            )
        finally:
            session_manager.delete_session(tmp_session_id)

    if runtime.execution_queue:
        async with runtime.execution_queue.acquire("stateless") as ticket:
            result = await _do_execute()
            _record_execution_metrics(result)
            _log_execution_event(
                "execute_end",
                session_id="stateless",
                context_id=request.context_id,
                container_id=None,
                code=request.code,
                duration_ms=int(result.get("execution_time_ms", 0) or 0),
                success=bool(result.get("success", False)),
                chart_count=len(result.get("charts", []) or []),
                table_count=len(result.get("tables", []) or []),
            )
            queue_info = _build_queue_info(ticket)
            sandbox_info = await _build_sandbox_info("stateless", mode=exec_path)
            exec_info = _build_execution_info(result, code=request.code, timeout=request.timeout, execution_path=exec_path)
            result["queue_info"] = queue_info
            result["sandbox_info"] = sandbox_info
            result["execution_info"] = exec_info
            return ExecuteCodeResponse(**result)
    else:
        result = await _do_execute()
        _record_execution_metrics(result)
        _log_execution_event(
            "execute_end",
            session_id="stateless",
            context_id=request.context_id,
            container_id=None,
            code=request.code,
            duration_ms=int(result.get("execution_time_ms", 0) or 0),
            success=bool(result.get("success", False)),
            chart_count=len(result.get("charts", []) or []),
            table_count=len(result.get("tables", []) or []),
        )
        sandbox_info = await _build_sandbox_info("stateless", mode=exec_path)
        exec_info = _build_execution_info(result, code=request.code, timeout=request.timeout, execution_path=exec_path)
        result["sandbox_info"] = sandbox_info
        result["execution_info"] = exec_info
        return ExecuteCodeResponse(**result)
