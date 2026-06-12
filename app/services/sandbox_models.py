"""
沙箱数据模型：状态枚举、对外信息结构、内部记录
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict

from ..infrastructure.network_controller import NetworkPolicy
from ..infrastructure.resource_limiter import ResourceLimits
from ..infrastructure.security_policy import SecurityPolicy


class SandboxState(Enum):
    """
    沙箱状态枚举

    定义沙箱的各种运行状态。
    """
    CREATING = "creating"      # 创建中
    RUNNING = "running"        # 运行中
    PAUSED = "paused"          # 已暂停
    STOPPED = "stopped"        # 已停止
    ERROR = "error"            # 错误状态


@dataclass
class SandboxInfo:
    """
    沙箱信息数据类

    包含沙箱的完整信息，包括标识、状态、资源配置和目录路径。

    Requirements: 1.3 (查询沙箱状态 - 运行状态、资源使用情况、创建时间)

    Attributes:
        sandbox_id: 沙箱唯一标识
        session_id: 关联的会话 ID
        container_id: Docker 容器 ID
        state: 当前状态
        created_at: 创建时间
        last_activity: 最后活动时间
        cpu_limit: CPU 限制（核心数）
        memory_limit_mb: 内存限制（MB）
        disk_limit_mb: 磁盘限制（MB）
        network_enabled: 是否启用网络
        data_dir: 数据目录路径
        output_dir: 输出目录路径
    """
    sandbox_id: str                    # 沙箱唯一标识
    session_id: str                    # 关联的会话 ID
    container_id: str                  # Docker 容器 ID
    state: SandboxState                # 当前状态
    created_at: datetime               # 创建时间
    last_activity: datetime            # 最后活动时间
    cpu_limit: float                   # CPU 限制（核心数）
    memory_limit_mb: int               # 内存限制（MB）
    disk_limit_mb: int                 # 磁盘限制（MB）
    network_enabled: bool              # 是否启用网络
    data_dir: str                      # 数据目录路径
    output_dir: str                    # 输出目录路径

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典格式

        Returns:
            包含沙箱信息的字典
        """
        return {
            "sandbox_id": self.sandbox_id,
            "session_id": self.session_id,
            "container_id": self.container_id,
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "cpu_limit": self.cpu_limit,
            "memory_limit_mb": self.memory_limit_mb,
            "disk_limit_mb": self.disk_limit_mb,
            "network_enabled": self.network_enabled,
            "data_dir": self.data_dir,
            "output_dir": self.output_dir,
        }

    def __str__(self) -> str:
        """返回沙箱信息的字符串表示"""
        return (
            f"SandboxInfo(id={self.sandbox_id[:8]}, "
            f"session={self.session_id[:8]}, "
            f"state={self.state.value}, "
            f"cpu={self.cpu_limit}, "
            f"memory={self.memory_limit_mb}MB)"
        )


@dataclass
class _SandboxRecord:
    """
    内部沙箱记录数据类

    用于在 SandboxManager 内部存储沙箱的完整信息。
    """
    info: SandboxInfo                  # 沙箱信息
    resource_limits: ResourceLimits    # 资源限制配置
    network_policy: NetworkPolicy      # 网络策略
    security_policy: SecurityPolicy    # 安全策略
    timeout_seconds: int               # 超时时间（秒）
