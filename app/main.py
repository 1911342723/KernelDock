"""
KernelDock - FastAPI 服务

提供代码执行 API，支持 Docker 沙箱隔离执行。
集成沙箱管理器、会话存储和健康监控器。

Requirements:
- 10.1: 保持现有的 REST API 接口格式不变
- 10.2: 保持现有的请求和响应数据结构不变
- 10.3: 支持现有的会话管理 API（创建、获取、删除）
- 10.4: 支持现有的代码执行 API（execute）
- 10.5: 支持现有的文件管理 API（upload、download、list）
- 10.6: 新增功能通过新的 API 端点或可选参数提供
- 9.1: 提供服务级别的健康检查端点（/health）
- 9.5: 支持 Prometheus 格式的指标导出（/metrics）
"""

import base64
import hashlib
import json
import logging
import os
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator, List, Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Response, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

try:
    import sentry_sdk
except ImportError:  # pragma: no cover - optional dependency
    sentry_sdk = None

from .config import settings
from .executor import session_manager, StatelessSession
from .models.code_context import CodeContext
from .services.context_manager import ContextManager
from .services.sandbox_manager import SandboxManager, SandboxState
from .services.session_store import SessionStore, get_session_store
from .services.health_monitor import HealthMonitor, get_health_monitor
from .services.execution_queue import ExecutionQueue

# 配置日志
logging.basicConfig(level=getattr(logging, settings.log_level))
logger = logging.getLogger(__name__)

# 全局组件实例
_sandbox_manager: Optional[SandboxManager] = None
_session_store: Optional[SessionStore] = None
_health_monitor: Optional[HealthMonitor] = None
_execution_queue: Optional[ExecutionQueue] = None
_context_manager: Optional[ContextManager] = None


def _execution_code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]


def _log_execution_event(
    event: str,
    *,
    session_id: str,
    context_id: Optional[str],
    container_id: Optional[str],
    code: str,
    duration_ms: Optional[int] = None,
    success: Optional[bool] = None,
    chart_count: Optional[int] = None,
    table_count: Optional[int] = None,
) -> None:
    payload = {
        "event": event,
        "session_id": session_id,
        "context_id": context_id,
        "container_id": container_id,
        "code_hash": _execution_code_hash(code),
        "timestamp": datetime.utcnow().isoformat(),
    }
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if success is not None:
        payload["success"] = success
    if chart_count is not None:
        payload["chart_count"] = chart_count
    if table_count is not None:
        payload["table_count"] = table_count
    logger.info(json.dumps(payload, ensure_ascii=False, default=str))


def _should_report_failure_to_sentry(result: dict[str, Any]) -> bool:
    error_text = str(result.get("error") or result.get("stderr") or "").lower()
    return any(token in error_text for token in ("oom", "out of memory", "timeout", "timed out", "killed", "crash", "segfault", "超时"))


async def _report_execution_failure_to_sentry(
    *,
    session_id: str,
    container_id: Optional[str],
    result: dict[str, Any],
) -> None:
    if not sentry_sdk or not settings.sentry_dsn or not container_id or not _sandbox_manager:
        return
    if not _should_report_failure_to_sentry(result):
        return

    logs_tail = {"stdout": "", "stderr": ""}
    try:
        logs_tail = await _sandbox_manager._docker_client.get_container_logs_tail(container_id)
    except Exception as exc:  # pragma: no cover - best effort diagnostics
        logger.warning(f"读取容器日志尾部失败: {exc}")

    with sentry_sdk.push_scope() as scope:
        scope.set_tag("session_id", session_id)
        scope.set_tag("container_id", container_id)
        scope.set_context("container_logs", logs_tail)
        scope.set_context(
            "execution_result",
            {
                "success": result.get("success", False),
                "error": result.get("error"),
                "stderr": (result.get("stderr") or "")[-1024:],
                "stdout": (result.get("stdout") or "")[-1024:],
            },
        )
        sentry_sdk.capture_exception(RuntimeError(result.get("error") or "sandbox execution failed"))


def _require_admin_token(admin_token: Optional[str]) -> None:
    expected = (settings.admin_token or "").strip()
    if not expected or admin_token != expected:
        raise HTTPException(status_code=403, detail="Invalid admin token")


def get_sandbox_manager() -> SandboxManager:
    """获取沙箱管理器实例"""
    global _sandbox_manager
    if _sandbox_manager is None:
        _sandbox_manager = SandboxManager.from_settings()
    return _sandbox_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理
    
    启动时初始化沙箱管理器、会话存储和健康监控器。
    关闭时清理所有资源。
    """
    global _sandbox_manager, _session_store, _health_monitor, _execution_queue, _context_manager

    logger.info("KernelDock 启动中...")

    # 初始化执行队列
    _execution_queue = ExecutionQueue(
        max_concurrent=settings.queue.max_concurrent_executions,
        avg_execution_time=settings.queue.initial_avg_execution_time,
        queue_timeout=settings.queue.queue_timeout,
    )
    logger.info(
        f"执行队列初始化完成: max_concurrent={settings.queue.max_concurrent_executions}"
    )
    
    # 初始化会话存储
    _session_store = get_session_store()
    logger.info("会话存储初始化完成")

    _context_manager = ContextManager()
    logger.info("上下文管理器初始化完成")
    
    # 初始化健康监控器
    _health_monitor = get_health_monitor()

    if sentry_sdk and settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            traces_sample_rate=settings.sentry_traces_sample_rate,
        )
    
    # 初始化沙箱管理器（可选，根据环境决定是否启用 Docker 沙箱）
    try:
        _sandbox_manager = get_sandbox_manager()
        await _sandbox_manager.initialize()
        
        # 设置健康监控器的依赖
        _health_monitor.set_sandbox_manager(_sandbox_manager)
        if _sandbox_manager._container_pool:
            _health_monitor.set_container_pool(_sandbox_manager._container_pool)
        if _sandbox_manager._docker_client:
            _health_monitor.set_docker_client(_sandbox_manager._docker_client)
        
        # 启动后台监控
        await _health_monitor.start_background_monitoring()
        
        # 设置 WebSocket 路由的沙箱管理器
        from .websocket_routes import set_sandbox_manager, set_execution_queue
        set_sandbox_manager(_sandbox_manager)
        set_execution_queue(_execution_queue)
        
        logger.info("沙箱管理器初始化完成")
    except Exception as e:
        if settings.allow_local_fallback:
            logger.warning(f"沙箱管理器初始化失败，将使用本地执行模式: {e}")
        else:
            logger.error(f"沙箱管理器初始化失败，本地回退已禁用: {e}")
        _sandbox_manager = None
    
    logger.info("KernelDock 启动完成")
    
    yield
    
    logger.info("KernelDock 关闭中...")
    
    # 停止健康监控
    if _health_monitor:
        await _health_monitor.stop_background_monitoring()
    
    # SandboxManager owns all container lifecycle; shut it down first
    if _sandbox_manager:
        await _sandbox_manager.shutdown()
        _sandbox_manager = None
    
    # Clean local file-management sessions (workspace dirs)
    for sid in list(session_manager.sessions.keys()):
        session_manager.delete_session(sid)
    
    logger.info("KernelDock 已关闭")


app = FastAPI(
    title="KernelDock",
    description="面向 LLM 和数据分析场景的 Docker 沙箱 Python 执行服务",
    version="2.0.0",
    lifespan=lifespan
)

# CORS: restrict to known origins via env var; fall back to permissive only in dev.
_cors_origins_raw = os.environ.get("CORS_ALLOWED_ORIGINS", "")
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册 WebSocket 路由
# 注意：WebSocket 路由直接注册在根路径，不使用 /api/ws 前缀
# 这样后端可以通过 ws://host:port/ws 连接
from .websocket_routes import router as ws_router
app.include_router(ws_router, tags=["WebSocket"])

# ===== Request/Response Models =====

class CreateSessionRequest(BaseModel):
    """创建会话请求"""
    session_id: Optional[str] = None


class CreateSessionResponse(BaseModel):
    """创建会话响应"""
    session_id: str
    workspace_dir: str
    data_dir: str
    output_dir: str


class ExecuteCodeRequest(BaseModel):
    """执行代码请求"""
    code: str
    timeout: int = 300
    context_id: Optional[str] = None
    # multi-table-analysis (executor_protocol_multitable.md):
    # 可选字段；成对出现。base64 编码的 parquet 字节 + backend 渲染的
    # DataLoaderBootstrap Python 源码。
    pre_load_parquet: Optional[dict] = None
    bootstrap_source: Optional[str] = None


class ChartData(BaseModel):
    """图表数据"""
    path: Optional[str]
    base64: str
    format: str


class TableData(BaseModel):
    """表格数据"""
    id: str
    name: str
    columns: List[str]
    data: List[dict]
    totalRows: int
    displayedRows: int
    dtypes: dict
    description: Optional[str]
    csvData: Optional[str]


class QueueInfoResponse(BaseModel):
    """排队信息"""
    position_on_entry: int = 0
    waited_seconds: float = 0.0
    estimated_wait_seconds: float = 0.0
    queue_depth: int = 0
    executing_count: int = 0
    max_concurrent: int = 0
    avg_execution_time: float = 0.0
    total_enqueued: int = 0
    total_executed: int = 0


class SandboxInfoResponse(BaseModel):
    """沙箱运行信息"""
    sandbox_id: Optional[str] = None
    container_id_short: Optional[str] = None
    mode: str = "unknown"
    state: Optional[str] = None
    cpu_limit: Optional[float] = None
    memory_limit_mb: Optional[int] = None
    network_enabled: Optional[bool] = None
    pool_available: Optional[int] = None
    pool_total: Optional[int] = None


class ExecutionInfoResponse(BaseModel):
    """执行细节信息"""
    execution_time_ms: int = 0
    execution_path: str = "unknown"
    code_size_bytes: int = 0
    timeout_configured: int = 0
    timed_out: bool = False
    chart_count: int = 0
    table_count: int = 0
    output_truncated: bool = False
    output_size_bytes: int = 0


class ExecuteCodeResponse(BaseModel):
    """执行代码响应"""
    success: bool
    output: str
    stdout: str
    stderr: str
    charts: List[dict]
    tables: List[dict]
    images: List[str]
    error: Optional[str]
    queue_info: Optional[QueueInfoResponse] = None
    sandbox_info: Optional[SandboxInfoResponse] = None
    execution_info: Optional[ExecutionInfoResponse] = None


class StatelessExecuteRequest(BaseModel):
    """无状态执行请求（即用即毁模式）"""
    code: str
    timeout: int = 30
    context_id: Optional[str] = None
    data_files: Optional[dict] = None  # {filename: base64_content}
    # multi-table-analysis
    pre_load_parquet: Optional[dict] = None
    bootstrap_source: Optional[str] = None


class LoadDataRequest(BaseModel):
    """加载数据请求"""
    data_json: str
    filename: str = "data.csv"


class LoadDataResponse(BaseModel):
    """加载数据响应"""
    success: bool
    file_path: Optional[str]
    rows: Optional[int]
    columns: Optional[int]
    column_names: Optional[List[str]]
    error: Optional[str]


class TableSchemaResponse(BaseModel):
    """表格模式响应"""
    name: str
    variable_name: str
    columns: List[str]
    dtypes: dict
    row_count: int
    sample_values: dict


class MultiTableContextResponse(BaseModel):
    """多表上下文响应"""
    tables: List[TableSchemaResponse]
    table_count: int
    total_rows: int
    common_columns: dict
    suggested_joins: List[dict]


class CreateContextRequest(BaseModel):
    """创建代码上下文请求"""
    fork_from: Optional[str] = None
    language: str = "python"


class ContextResponse(BaseModel):
    """代码上下文响应"""
    context_id: str
    session_id: str
    language: str
    created_at: datetime
    last_used_at: datetime
    parent_context_id: Optional[str] = None


def _require_context_manager() -> ContextManager:
    if _context_manager is None:
        raise HTTPException(status_code=503, detail="上下文管理器未初始化")
    return _context_manager


def _serialize_context(context: CodeContext) -> ContextResponse:
    return ContextResponse(
        context_id=context.context_id,
        session_id=context.session_id,
        language=context.language,
        created_at=context.created_at,
        last_used_at=context.last_used_at,
        parent_context_id=context.parent_context_id,
    )


def _ensure_default_context(session_id: str) -> CodeContext:
    context_manager = _require_context_manager()
    existing = context_manager.list_contexts(session_id)
    if existing:
        return existing[0]
    return context_manager.create_context(session_id=session_id)


def _resolve_execute_context_id(session_id: str, requested_context_id: Optional[str]) -> str:
    context_manager = _require_context_manager()
    if requested_context_id:
        context = context_manager.get_context(requested_context_id)
        if context is None or context.session_id != session_id:
            raise HTTPException(status_code=404, detail="Context not found")
        context.touch()
        return context.context_id
    return _ensure_default_context(session_id).context_id


def _render_context_bootstrap(
    context: CodeContext,
    *,
    data_dir: str,
    output_dir: str,
) -> Optional[str]:
    if not context.data_refs:
        return None
    refs_json = json.dumps(list(context.data_refs), ensure_ascii=False)
    focus_json = json.dumps(context.focus_ref, ensure_ascii=False)
    return f"""
import json as _ctx_json
import pandas as pd

DATA_DIR = r'{data_dir}'
OUTPUT_DIR = r'{output_dir}'
TABLE_REFS = _ctx_json.loads({refs_json!r})
FOCUS_REF = _ctx_json.loads({focus_json!r})
_loaded_tables = {{}}

for _ref in TABLE_REFS:
    _loaded_tables[_ref] = pd.read_parquet(f"{{DATA_DIR}}/{{_ref}}.parquet", engine="pyarrow")
    globals()[_ref] = _loaded_tables[_ref]

if FOCUS_REF and FOCUS_REF in _loaded_tables:
    df = _loaded_tables[FOCUS_REF]
elif TABLE_REFS:
    df = _loaded_tables[min(TABLE_REFS)]
else:
    df = pd.DataFrame()
""".strip()


# ===== 健康检查和指标端点 =====
# Requirements 9.1, 9.5

@app.get("/health")
async def health_check():
    """
    健康检查端点
    
    Requirements 9.1: 提供服务级别的健康检查端点
    Requirements 9.2: 报告当前活跃沙箱数量、容器池状态和系统资源使用
    
    Returns:
        服务健康状态信息
    """
    if _health_monitor:
        try:
            health = await _health_monitor.get_service_health()
            return {
                "status": health.status,
                "service": "code-executor",
                "active_sandboxes": health.active_sandboxes,
                "pool_available": health.pool_available,
                "pool_total": health.pool_total,
                "cpu_usage_percent": round(health.cpu_usage_percent, 2),
                "memory_usage_percent": round(health.memory_usage_percent, 2),
                "uptime_seconds": health.uptime_seconds,
                "last_check": health.last_check.isoformat(),
                "details": health.details,
            }
        except Exception as e:
            logger.warning(f"获取健康状态失败: {e}")
    
    # 回退到基本健康检查
    return {"status": "healthy", "service": "code-executor"}


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    """
    Prometheus 指标端点
    
    Requirements 9.5: 支持 Prometheus 格式的指标导出
    
    Returns:
        Prometheus 格式的指标文本
    """
    if _health_monitor:
        try:
            return _health_monitor.export_prometheus_metrics()
        except Exception as e:
            logger.warning(f"导出 Prometheus 指标失败: {e}")
    
    # 回退到基本指标
    return "# No metrics available\n"


# ===== 会话管理 API =====
# Requirements 10.3: 支持现有的会话管理 API（创建、获取、删除）

@app.post("/sessions", response_model=CreateSessionResponse)
async def create_session(request: CreateSessionRequest):
    """
    创建新的执行会话
    
    Requirements 10.1, 10.3: 保持现有 API 接口格式不变
    
    如果沙箱管理器可用，会创建 Docker 沙箱；
    否则使用本地执行模式。
    """
    session_id = request.session_id
    
    # 使用新的会话存储记录会话
    if _session_store:
        session_info = await _session_store.create_session(
            session_id=session_id,
            metadata={"mode": "sandbox" if _sandbox_manager else "local"}
        )
        session_id = session_info.session_id
    
    # 创建沙箱（如果沙箱管理器可用）
    sandbox_info = None
    if _sandbox_manager:
        try:
            sandbox_info = await _sandbox_manager.create_sandbox(session_id=session_id)
            
            # 更新会话存储中的沙箱 ID
            if _session_store:
                await _session_store.update_sandbox_id(session_id, sandbox_info.sandbox_id)
            
            logger.info(f"创建沙箱会话: {session_id}, 沙箱: {sandbox_info.sandbox_id}")
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
        output_dir=session.output_dir
    )


@app.get("/sessions/{session_id}/contexts", response_model=List[ContextResponse])
async def list_session_contexts(session_id: str):
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    context_manager = _require_context_manager()
    return [_serialize_context(context) for context in context_manager.list_contexts(session_id)]


@app.post("/sessions/{session_id}/contexts", response_model=ContextResponse)
async def create_session_context(session_id: str, request: CreateContextRequest):
    session = session_manager.get_or_create_session(session_id)
    if _session_store:
        await _session_store.update_activity(session.session_id)
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


@app.get("/sessions/{session_id}")
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
    if _sandbox_manager:
        sandbox_info = await _sandbox_manager.get_sandbox_by_session(session_id)
        if sandbox_info:
            response["sandbox_id"] = sandbox_info.sandbox_id
            response["sandbox_state"] = sandbox_info.state.value
    
    return response


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """
    删除会话
    
    Requirements 10.1, 10.3: 保持现有 API 接口格式不变
    """
    # 1. Destroy sandbox container (SandboxManager owns container lifecycle)
    if _sandbox_manager:
        sandbox_info = await _sandbox_manager.get_sandbox_by_session(session_id)
        if sandbox_info:
            await _sandbox_manager.destroy_sandbox(sandbox_info.sandbox_id)
    
    # 2. Remove from session metadata store
    if _session_store:
        await _session_store.delete_session(session_id)
    
    # 3. Clean local workspace files via StatelessSession
    success = session_manager.delete_session(session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")

    if _context_manager:
        for context in list(_context_manager.list_contexts(session_id)):
            _context_manager.delete_context(context.context_id)
    
    return {"success": True, "message": f"Session {session_id} deleted"}


@app.delete("/sessions/{session_id}/contexts/{context_id}", status_code=204)
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


# ===== 代码执行 API =====
# Requirements 10.4: 支持现有的代码执行 API（execute）

def _build_queue_info(ticket) -> QueueInfoResponse:
    """Build QueueInfoResponse from a QueueTicket and global queue state."""
    waited = 0.0
    if ticket.started_at:
        waited = round(ticket.started_at - ticket.enqueued_at, 2)
    global_status = _execution_queue.get_global_status() if _execution_queue else {}
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
    if _sandbox_manager and _sandbox_manager._container_pool:
        pool_available = _sandbox_manager._container_pool.available_count
        pool_total = _sandbox_manager._container_pool.pool_size

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


def _record_execution_metrics(result: dict[str, Any]) -> None:
    if not _health_monitor or not isinstance(result, dict):
        return
    _health_monitor.record_execution(
        duration_seconds=max(0.0, float(result.get("execution_time_ms", 0) or 0) / 1000.0),
        success=bool(result.get("success", False)),
    )


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


async def _execute_streaming(session_id: str, request: ExecuteCodeRequest) -> AsyncIterator[str]:
    session = session_manager.get_or_create_session(session_id)
    if _session_store:
        await _session_store.update_activity(session_id)

    request.context_id = _resolve_execute_context_id(session_id, request.context_id)
    context = _require_context_manager().get_context(request.context_id)
    start_sandbox = await _sandbox_manager.get_sandbox_by_session(session_id) if _sandbox_manager else None
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

        if _sandbox_manager:
            sandbox_info = await _sandbox_manager.get_sandbox_by_session(session_id)
            if sandbox_info:
                bootstrap_source = request.bootstrap_source
                if bootstrap_source is None and context is not None:
                    bootstrap_source = _render_context_bootstrap(
                        context,
                        data_dir="/data",
                        output_dir="/output",
                    )
                async for event in _sandbox_manager.stream_execute_code(
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

    if _execution_queue:
        async with _execution_queue.acquire(session_id) as ticket:
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


@app.post("/sessions/{session_id}/execute", response_model=ExecuteCodeResponse)
async def execute_code(session_id: str, request: ExecuteCodeRequest):
    """
    执行代码

    Requirements 10.1, 10.4: 保持现有 API 接口格式不变

    如果执行队列可用，通过令牌桶控制并发；
    如果会话有关联的沙箱，使用 Docker exec 执行；
    否则使用本地 subprocess 执行。
    """
    session = session_manager.get_or_create_session(session_id)

    if _session_store:
        await _session_store.update_activity(session_id)

    request.context_id = _resolve_execute_context_id(session_id, request.context_id)
    context = _require_context_manager().get_context(request.context_id)
    start_sandbox = await _sandbox_manager.get_sandbox_by_session(session_id) if _sandbox_manager else None
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

    if _execution_queue:
        async with _execution_queue.acquire(session_id) as ticket:
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


@app.post("/v2/sessions/{session_id}/execute")
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
    if request.context_id and _context_manager:
        context = _context_manager.get_context(request.context_id)

    if _sandbox_manager:
        sandbox_info = await _sandbox_manager.get_sandbox_by_session(session_id)
        if sandbox_info:
            try:
                bootstrap_source = request.bootstrap_source
                if bootstrap_source is None and context is not None:
                    bootstrap_source = _render_context_bootstrap(
                        context,
                        data_dir="/data",
                        output_dir="/output",
                    )
                result = await _sandbox_manager.execute_code(
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


@app.post("/sessions/{session_id}/load-data", response_model=LoadDataResponse)
async def load_data(session_id: str, request: LoadDataRequest):
    """
    加载 JSON 数据
    
    Requirements 10.1: 保持现有 API 接口格式不变
    """
    session = session_manager.get_or_create_session(session_id)
    
    # 更新会话活动时间
    if _session_store:
        await _session_store.update_activity(session_id)
        await _session_store.add_data_file(session_id, request.filename)
    
    result = await session.load_data(request.data_json, request.filename)
    return LoadDataResponse(**result)


# ===== 文件管理 API =====
# Requirements 10.5: 支持现有的文件管理 API（upload、download、list）

@app.post("/sessions/{session_id}/upload")
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
    if _session_store:
        await _session_store.update_activity(session_id)
        await _session_store.add_data_file(session_id, target_filename)
    
    result = await session.load_file(content, target_filename)
    return result


@app.get("/sessions/{session_id}/schemas", response_model=List[TableSchemaResponse])
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


@app.get("/sessions/{session_id}/context", response_model=MultiTableContextResponse)
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


@app.get("/sessions/{session_id}/files")
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


@app.get("/sessions/{session_id}/files/{file_type}/{filename}")
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


# ===== 管理 API =====

@app.post("/cleanup")
async def cleanup_old_sessions(max_age_hours: float = 12):
    """
    清理过期会话
    
    Requirements 10.1: 保持现有 API 接口格式不变
    """
    # 清理会话存储中的过期会话
    expired_count = 0
    if _session_store:
        expired_count = await _session_store.cleanup_expired()
    
    # 清理兼容的 session_manager 中的过期会话
    count = await session_manager.cleanup_old_sessions(max_age_hours)
    
    return {"cleaned": count + expired_count}


# ===== 新增 API 端点 =====
# Requirements 10.6: 新增功能通过新的 API 端点提供

@app.get("/sandboxes")
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
    if not _sandbox_manager:
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
    
    sandboxes = await _sandbox_manager.list_sandboxes(
        state=sandbox_state,
        limit=limit,
        offset=offset
    )
    
    return {
        "sandboxes": [s.to_dict() for s in sandboxes],
        "total": len(sandboxes),
        "active_count": _sandbox_manager.active_count,
        "max_concurrent": _sandbox_manager.max_concurrent
    }


@app.get("/sandboxes/{sandbox_id}")
async def get_sandbox(sandbox_id: str):
    """
    获取沙箱详情
    
    Requirements 10.6: 新增功能通过新的 API 端点提供
    """
    if not _sandbox_manager:
        raise HTTPException(status_code=503, detail="沙箱管理器未启用")
    
    sandbox_info = await _sandbox_manager.get_sandbox(sandbox_id)
    if not sandbox_info:
        raise HTTPException(status_code=404, detail="沙箱不存在")
    
    return sandbox_info.to_dict()


@app.delete("/sandboxes/{sandbox_id}")
async def destroy_sandbox(sandbox_id: str):
    """
    销毁沙箱
    
    Requirements 10.6: 新增功能通过新的 API 端点提供
    """
    if not _sandbox_manager:
        raise HTTPException(status_code=503, detail="沙箱管理器未启用")
    
    success = await _sandbox_manager.destroy_sandbox(sandbox_id)
    if not success:
        raise HTTPException(status_code=404, detail="沙箱不存在")
    
    return {"success": True, "message": f"沙箱 {sandbox_id} 已销毁"}


@app.get("/sandboxes/{sandbox_id}/metrics")
async def get_sandbox_metrics(sandbox_id: str):
    """
    获取沙箱资源使用指标
    
    Requirements 9.4: 提供沙箱级别的资源使用指标查询接口
    Requirements 10.6: 新增功能通过新的 API 端点提供
    """
    if not _health_monitor:
        raise HTTPException(status_code=503, detail="健康监控器未启用")
    
    metrics = await _health_monitor.get_sandbox_metrics(sandbox_id)
    if not metrics:
        raise HTTPException(status_code=404, detail="沙箱不存在或无法获取指标")
    
    return metrics.to_dict()


@app.get("/statistics")
async def get_statistics():
    """
    获取服务统计信息

    Requirements 10.6: 新增功能通过新的 API 端点提供
    """
    stats = {
        "service": "code-executor",
        "version": "2.0.0",
        "sandbox_manager_enabled": _sandbox_manager is not None,
        "session_count": len(session_manager.sessions),
    }

    if _sandbox_manager:
        sandbox_stats = await _sandbox_manager.get_statistics()
        stats["sandbox"] = sandbox_stats

    if _session_store:
        stats["session_store"] = {
            "total_sessions": await _session_store.get_session_count(),
            "active_sessions": await _session_store.get_active_session_count(),
        }

    if _execution_queue:
        stats["execution_queue"] = _execution_queue.get_global_status()

    return stats


@app.get("/queue/status")
async def get_queue_status():
    """
    获取执行队列状态

    返回当前排队数、执行数、平均耗时等信息。
    """
    if not _execution_queue:
        return {"enabled": False, "message": "执行队列未启用"}
    return {"enabled": True, **_execution_queue.get_global_status()}


# ===== 即用即毁（Fire-and-Forget）无状态执行端点 =====

@app.post("/execute", response_model=ExecuteCodeResponse)
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
        if _sandbox_manager:
            exec_path = "stateless_pool_kernel"
            try:
                return await _sandbox_manager.execute_stateless(
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

    if _execution_queue:
        async with _execution_queue.acquire("stateless") as ticket:
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


@app.get("/admin/sandboxes")
async def admin_list_sandboxes(x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token")):
    _require_admin_token(x_admin_token)
    if not _sandbox_manager:
        return []

    sandboxes = await _sandbox_manager.list_sandboxes()
    items = []
    for sandbox in sandboxes:
        metrics = await _health_monitor.get_sandbox_metrics(sandbox.sandbox_id) if _health_monitor else None
        context_count = len(_context_manager.list_contexts(sandbox.session_id)) if _context_manager else 0
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=8080,
        ws_max_size=50 * 1024 * 1024  # 50MB WebSocket 消息大小限制
    )
