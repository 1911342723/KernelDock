"""
基础设施层模块

包含 Docker 客户端、资源限制器、网络控制器、容器池、安全策略等基础设施组件。
"""

from .docker_client import (
    DockerClient,
    ContainerInfo,
    ContainerStats,
    ContainerState,
    ExecResult,
)
from .resource_limiter import (
    ResourceLimiter,
    ResourceLimits,
    ResourceUsage,
    MIN_CPU,
    MAX_CPU,
    MIN_MEMORY_MB,
    MAX_MEMORY_MB,
    MIN_DISK_MB,
    MAX_DISK_MB,
    MIN_PIDS,
    MAX_PIDS,
)
from .network_controller import (
    NetworkController,
    NetworkPolicy,
    create_network_policy,
)
from .container_pool import (
    ContainerPool,
    PooledContainer,
)
from .security_policy import (
    SecurityPolicy,
    SecurityPolicyBuilder,
    SecurityLevel,
    create_default_security_policy,
    create_security_policy_from_settings,
    validate_security_config,
    get_security_recommendations,
    DEFAULT_CAP_DROP,
    DEFAULT_CAP_ADD,
    DANGEROUS_CAPABILITIES,
    DEFAULT_SECURITY_OPT,
    DEFAULT_PIDS_LIMIT,
    DEFAULT_USER_ID,
    DEFAULT_GROUP_ID,
    DOCKER_SOCKET_PATH,
)

__all__ = [
    # Docker 客户端
    "DockerClient",
    "ContainerInfo",
    "ContainerStats",
    "ContainerState",
    "ExecResult",
    # 资源限制器
    "ResourceLimiter",
    "ResourceLimits",
    "ResourceUsage",
    "MIN_CPU",
    "MAX_CPU",
    "MIN_MEMORY_MB",
    "MAX_MEMORY_MB",
    "MIN_DISK_MB",
    "MAX_DISK_MB",
    "MIN_PIDS",
    "MAX_PIDS",
    # 网络控制器
    "NetworkController",
    "NetworkPolicy",
    "create_network_policy",
    # 容器池
    "ContainerPool",
    "PooledContainer",
    # 安全策略
    "SecurityPolicy",
    "SecurityPolicyBuilder",
    "SecurityLevel",
    "create_default_security_policy",
    "create_security_policy_from_settings",
    "validate_security_config",
    "get_security_recommendations",
    "DEFAULT_CAP_DROP",
    "DEFAULT_CAP_ADD",
    "DANGEROUS_CAPABILITIES",
    "DEFAULT_SECURITY_OPT",
    "DEFAULT_PIDS_LIMIT",
    "DEFAULT_USER_ID",
    "DEFAULT_GROUP_ID",
    "DOCKER_SOCKET_PATH",
]
