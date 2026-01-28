"""
Code Executor Service - FastAPI 服务

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
import logging
import os
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .config import settings
from .executor import session_manager, StatelessSession
from .services.sandbox_manager import SandboxManager, SandboxState
from .services.session_store import SessionStore, get_session_store
from .services.health_monitor import HealthMonitor, get_health_monitor

# 配置日志
logging.basicConfig(level=getattr(logging, settings.log_level))
logger = logging.getLogger(__name__)

# 全局组件实例
_sandbox_manager: Optional[SandboxManager] = None
_session_store: Optional[SessionStore] = None
_health_monitor: Optional[HealthMonitor] = None


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
    global _sandbox_manager, _session_store, _health_monitor
    
    logger.info("Code Executor Service 启动中...")
    
    # 初始化会话存储
    _session_store = get_session_store()
    logger.info("会话存储初始化完成")
    
    # 初始化健康监控器
    _health_monitor = get_health_monitor()
    
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
        from .websocket_routes import set_sandbox_manager
        set_sandbox_manager(_sandbox_manager)
        
        logger.info("沙箱管理器初始化完成")
    except Exception as e:
        logger.warning(f"沙箱管理器初始化失败，将使用本地执行模式: {e}")
        _sandbox_manager = None
    
    logger.info("Code Executor Service 启动完成")
    
    yield
    
    logger.info("Code Executor Service 关闭中...")
    
    # 停止健康监控
    if _health_monitor:
        await _health_monitor.stop_background_monitoring()
    
    # 关闭沙箱管理器
    if _sandbox_manager:
        await _sandbox_manager.shutdown()
        _sandbox_manager = None
    
    # 清理所有会话（兼容旧的 session_manager）
    for session_id in list(session_manager.sessions.keys()):
        session_manager.delete_session(session_id)
    
    logger.info("Code Executor Service 已关闭")


app = FastAPI(
    title="Code Executor Service",
    description="Docker 沙箱 Python 代码执行服务，支持数据分析和可视化",
    version="2.0.0",
    lifespan=lifespan
)

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
    
    # 创建兼容的 StatelessSession（用于文件管理等）
    session = session_manager.create_session(session_id)
    
    # 如果有沙箱，设置容器信息
    if sandbox_info and _sandbox_manager:
        session.set_container(
            sandbox_info.container_id,
            _sandbox_manager._docker_client
        )
    
    return CreateSessionResponse(
        session_id=session.session_id,
        workspace_dir=session.workspace_dir,
        data_dir=session.data_dir,
        output_dir=session.output_dir
    )


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
    # 删除沙箱（如果有）
    if _sandbox_manager:
        sandbox_info = await _sandbox_manager.get_sandbox_by_session(session_id)
        if sandbox_info:
            await _sandbox_manager.destroy_sandbox(sandbox_info.sandbox_id)
    
    # 删除会话存储中的记录
    if _session_store:
        await _session_store.delete_session(session_id)
    
    # 删除兼容的 StatelessSession
    success = session_manager.delete_session(session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    
    return {"success": True, "message": f"Session {session_id} deleted"}


# ===== 代码执行 API =====
# Requirements 10.4: 支持现有的代码执行 API（execute）

@app.post("/sessions/{session_id}/execute", response_model=ExecuteCodeResponse)
async def execute_code(session_id: str, request: ExecuteCodeRequest):
    """
    执行代码
    
    Requirements 10.1, 10.4: 保持现有 API 接口格式不变
    
    如果会话有关联的沙箱，使用 Docker exec 执行；
    否则使用本地 subprocess 执行。
    """
    session = session_manager.get_or_create_session(session_id)
    
    # 更新会话活动时间
    if _session_store:
        await _session_store.update_activity(session_id)
    
    # 检查是否有沙箱
    if _sandbox_manager:
        sandbox_info = await _sandbox_manager.get_sandbox_by_session(session_id)
        if sandbox_info:
            # 使用沙箱管理器执行代码
            try:
                result = await _sandbox_manager.execute_code(
                    sandbox_id=sandbox_info.sandbox_id,
                    code=request.code,
                    timeout=request.timeout
                )
                return ExecuteCodeResponse(**result)
            except Exception as e:
                logger.error(f"沙箱执行失败: {e}")
                # 回退到本地执行
    
    # 使用本地执行（兼容模式）
    result = await session.execute_code(request.code, request.timeout)
    return ExecuteCodeResponse(**result)


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
    
    return stats


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
