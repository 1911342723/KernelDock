"""
运行时组件注册表

集中存放服务级单例（沙箱管理器、会话存储、健康监控、执行队列、
上下文管理器）。生命周期由 main.lifespan 负责初始化与清理，
路由模块通过 ``from .. import runtime`` 后以 ``runtime.xxx`` 访问，
保证读到的是最新引用（测试中也可直接 monkeypatch 本模块属性）。
"""

import logging
from typing import Optional

from .services.context_manager import ContextManager
from .services.execution_queue import ExecutionQueue
from .services.health_monitor import HealthMonitor
from .services.job_manager import JobManager
from .services.sandbox_manager import SandboxManager
from .services.session_store import SessionStore

logger = logging.getLogger(__name__)

sandbox_manager: Optional[SandboxManager] = None
session_store: Optional[SessionStore] = None
health_monitor: Optional[HealthMonitor] = None
execution_queue: Optional[ExecutionQueue] = None
context_manager: Optional[ContextManager] = None
job_manager: Optional[JobManager] = None


def get_sandbox_manager() -> SandboxManager:
    """获取（必要时创建）沙箱管理器实例。"""
    global sandbox_manager
    if sandbox_manager is None:
        sandbox_manager = SandboxManager.from_settings()
    return sandbox_manager
