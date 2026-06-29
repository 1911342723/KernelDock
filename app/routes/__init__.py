"""
HTTP 路由模块

按职责拆分：
- system: 健康检查 / 指标 / 统计 / 队列状态 / 清理
- sessions: 会话生命周期 + 上下文 + 数据与文件
- execution: 有状态执行 / SSE 流式 / 无状态执行
- sandboxes: 沙箱查询与管理（含 admin）
- agent_ops: shell 执行 / 容器内文件系统 / 运行时 pip 装包
- jobs: 长时后台任务（异步提交 + 轮询）
- resource_config: 资源配置查看与运行时调整（含 admin）
- admin_console: 可视化管理控制台页面（资源配置）
"""

from .admin_console import router as admin_console_router
from .agent_ops import router as agent_ops_router
from .execution import router as execution_router
from .jobs import router as jobs_router
from .resource_config import router as resource_config_router
from .sandboxes import router as sandboxes_router
from .sessions import router as sessions_router
from .system import router as system_router

__all__ = [
    "admin_console_router",
    "agent_ops_router",
    "execution_router",
    "jobs_router",
    "resource_config_router",
    "sandboxes_router",
    "sessions_router",
    "system_router",
]
