"""
KernelDock - FastAPI 服务装配

应用入口：生命周期管理、中间件装配、路由注册。
具体实现按职责拆分在：
- runtime.py        运行时组件注册表
- schemas.py        请求/响应模型
- middleware.py     CORS / 认证 / 限流
- observability.py  执行日志 / Sentry / 指标
- context_helpers.py 上下文解析与 bootstrap 渲染
- routes/           system / sessions / execution / sandboxes 路由
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

try:
    import sentry_sdk
except ImportError:  # pragma: no cover - optional dependency
    sentry_sdk = None

from . import runtime
from .config import settings
from .executor import session_manager
from .middleware import install_middleware
from .services.context_manager import ContextManager
from .services.execution_queue import ExecutionQueue
from .services.health_monitor import get_health_monitor
from .services.job_manager import JobManager, JobRecord
from .services.session_store import get_session_store

# 配置日志
logging.basicConfig(level=getattr(logging, settings.log_level))
logger = logging.getLogger(__name__)


async def _job_runner(record: JobRecord):
    """后台任务执行器：session 任务走会话沙箱，否则无状态执行。"""
    manager = runtime.sandbox_manager
    if manager is None:
        raise RuntimeError("沙箱管理器不可用，后台任务需要 Docker 沙箱模式")
    if record.kind == "session":
        sandbox_info = await manager.get_sandbox_by_session(record.session_id)
        if not sandbox_info:
            raise RuntimeError(f"Session 不存在或未绑定沙箱: {record.session_id}")
        # 与 REST 会话执行解析到同一个 kernel context，否则任务跑在
        # default 上下文里看不到会话已有变量（NameError）
        context_id = None
        if runtime.context_manager is not None:
            from .context_helpers import _ensure_default_context

            context_id = _ensure_default_context(record.session_id).context_id
        return await manager.execute_code(
            sandbox_id=sandbox_info.sandbox_id,
            code=record.code,
            timeout=record.timeout,
            context_id=context_id,
        )
    return await manager.execute_stateless(code=record.code, timeout=record.timeout)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理

    启动时初始化沙箱管理器、会话存储和健康监控器。
    关闭时清理所有资源。
    """
    logger.info("KernelDock 启动中...")

    # 初始化执行队列
    runtime.execution_queue = ExecutionQueue(
        max_concurrent=settings.queue.max_concurrent_executions,
        avg_execution_time=settings.queue.initial_avg_execution_time,
        queue_timeout=settings.queue.queue_timeout,
    )
    logger.info(
        f"执行队列初始化完成: max_concurrent={settings.queue.max_concurrent_executions}"
    )

    # 初始化会话存储
    runtime.session_store = get_session_store()
    logger.info("会话存储初始化完成")

    runtime.context_manager = ContextManager()
    logger.info("上下文管理器初始化完成")

    # 初始化健康监控器
    runtime.health_monitor = get_health_monitor()

    # 初始化后台任务管理器
    runtime.job_manager = JobManager(
        runner=_job_runner,
        max_concurrent=settings.jobs.max_concurrent,
        max_timeout=settings.jobs.max_timeout,
        retention_seconds=settings.jobs.retention_seconds,
        max_entries=settings.jobs.max_entries,
    )
    logger.info(f"后台任务管理器初始化完成: max_concurrent={settings.jobs.max_concurrent}")

    # 分布式：配置了 ROUTER_URL 时向 router 自注册并周期心跳
    from .services.router_heartbeat import RouterHeartbeat

    router_heartbeat = RouterHeartbeat()
    router_heartbeat.start()

    if sentry_sdk and settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            traces_sample_rate=settings.sentry_traces_sample_rate,
        )

    # 初始化沙箱管理器（可选，根据环境决定是否启用 Docker 沙箱）
    try:
        sandbox_manager = runtime.get_sandbox_manager()
        await sandbox_manager.initialize()

        # 设置健康监控器的依赖
        runtime.health_monitor.set_sandbox_manager(sandbox_manager)
        if sandbox_manager._container_pool:
            runtime.health_monitor.set_container_pool(sandbox_manager._container_pool)
        if sandbox_manager._docker_client:
            runtime.health_monitor.set_docker_client(sandbox_manager._docker_client)

        # 启动后台监控
        await runtime.health_monitor.start_background_monitoring()

        # 设置 WebSocket 路由的沙箱管理器
        from .websocket_routes import set_execution_queue, set_sandbox_manager
        set_sandbox_manager(sandbox_manager)
        set_execution_queue(runtime.execution_queue)

        logger.info("沙箱管理器初始化完成")
    except Exception as e:
        if settings.allow_local_fallback:
            logger.warning(f"沙箱管理器初始化失败，将使用本地执行模式: {e}")
        else:
            logger.error(f"沙箱管理器初始化失败，本地回退已禁用: {e}")
        runtime.sandbox_manager = None

    logger.info("KernelDock 启动完成")

    yield

    logger.info("KernelDock 关闭中...")

    # 停止 router 心跳
    await router_heartbeat.stop()

    # 取消所有后台任务
    if runtime.job_manager:
        await runtime.job_manager.shutdown()
        runtime.job_manager = None

    # 停止健康监控
    if runtime.health_monitor:
        await runtime.health_monitor.stop_background_monitoring()

    # SandboxManager owns all container lifecycle; shut it down first
    if runtime.sandbox_manager:
        await runtime.sandbox_manager.shutdown()
        runtime.sandbox_manager = None

    # Clean local file-management sessions (workspace dirs)
    for sid in list(session_manager.sessions.keys()):
        session_manager.delete_session(sid)

    # 关闭会话存储（停 DB 写线程、关连接）
    if runtime.session_store:
        await runtime.session_store.close()

    logger.info("KernelDock 已关闭")


app = FastAPI(
    title="KernelDock",
    description="面向 LLM 和数据分析场景的 Docker 沙箱 Python 执行服务",
    version="2.0.0",
    lifespan=lifespan
)

# CORS / API Key 认证 / 限流
install_middleware(app)

# 注册 WebSocket 路由
# 注意：WebSocket 路由直接注册在根路径，不使用 /api/ws 前缀
# 这样后端可以通过 ws://host:port/ws 连接
from .websocket_routes import router as ws_router  # noqa: E402
app.include_router(ws_router, tags=["WebSocket"])

# E2B 风格适配层（PoC，非协议级兼容）：仅 REST 子集，官方 e2b SDK 不可直连
from .e2b_routes import router as e2b_router  # noqa: E402
app.include_router(e2b_router)

# HTTP 路由（按职责拆分，见 routes/）
from .routes import (  # noqa: E402
    agent_ops_router,
    execution_router,
    jobs_router,
    sandboxes_router,
    sessions_router,
    system_router,
)
app.include_router(system_router)
app.include_router(sessions_router)
app.include_router(execution_router)
app.include_router(agent_ops_router)
app.include_router(jobs_router)
app.include_router(sandboxes_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8080,
        ws_max_size=50 * 1024 * 1024  # 50MB WebSocket 消息大小限制
    )
