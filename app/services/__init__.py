"""
沙箱服务层模块

包含沙箱管理器、会话存储、健康监控、文件管理等服务组件。
"""

from .sandbox_manager import (
    SandboxManager,
    SandboxState,
    SandboxInfo,
)
from .file_manager import (
    FileManager,
    FileInfo,
    UploadResult,
    file_manager,
    generate_variable_name,
    is_data_file,
    SUPPORTED_DATA_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
)
from .session_store import (
    SessionStore,
    SessionInfo,
    get_session_store,
    reset_session_store,
)
from .health_monitor import (
    HealthMonitor,
    ServiceHealth,
    SandboxMetrics,
    ContainerExitEvent,
    get_health_monitor,
    reset_health_monitor,
)

__all__ = [
    # 沙箱管理器
    "SandboxManager",
    "SandboxState",
    "SandboxInfo",
    # 文件管理器
    "FileManager",
    "FileInfo",
    "UploadResult",
    "file_manager",
    "generate_variable_name",
    "is_data_file",
    "SUPPORTED_DATA_EXTENSIONS",
    "SUPPORTED_EXTENSIONS",
    # 会话存储
    "SessionStore",
    "SessionInfo",
    "get_session_store",
    "reset_session_store",
    # 健康监控器
    "HealthMonitor",
    "ServiceHealth",
    "SandboxMetrics",
    "ContainerExitEvent",
    "get_health_monitor",
    "reset_health_monitor",
]
