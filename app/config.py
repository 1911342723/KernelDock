"""
沙箱服务配置模块

使用 Pydantic BaseSettings 实现配置管理，支持从环境变量加载配置。
所有配置必须通过 settings 对象访问，禁止使用 os.getenv。

Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6
"""

import re
from typing import List, Dict, Any
from dataclasses import dataclass, field
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
import logging

logger = logging.getLogger(__name__)


# ============================================================================
# 配置验证常量和正则表达式
# ============================================================================

# Docker 镜像名称正则表达式
# 支持格式: image, image:tag, registry/image, registry/image:tag, registry:port/image:tag
DOCKER_IMAGE_PATTERN = re.compile(
    r'^(?:(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)*[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?'
    r'(?::\d+)?/)?'  # 可选的 registry 部分
    r'[a-z0-9]+(?:[._-][a-z0-9]+)*'  # 镜像名称
    r'(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*'  # 可选的命名空间
    r'(?::[a-zA-Z0-9][a-zA-Z0-9._-]*)?$'  # 可选的标签
)

# Unix socket 路径正则表达式
# 支持格式: unix:///path/to/socket 或 /path/to/socket
UNIX_SOCKET_PATTERN = re.compile(
    r'^(?:unix://)?(/[a-zA-Z0-9._-]+)+(?:\.sock)?$'
)

# Docker 网络名称正则表达式
# 支持字母、数字、下划线、连字符，长度 1-64
DOCKER_NETWORK_NAME_PATTERN = re.compile(
    r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$'
)

# 路径格式正则表达式（Unix 风格绝对路径）
UNIX_PATH_PATTERN = re.compile(
    r'^/(?:[a-zA-Z0-9._-]+/?)*$'
)


@dataclass
class ConfigValidationResult:
    """
    配置验证结果
    
    包含验证是否通过、警告信息和错误信息。
    """
    is_valid: bool = True
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    validated_values: Dict[str, Any] = field(default_factory=dict)
    
    def add_warning(self, message: str) -> None:
        """添加警告信息"""
        self.warnings.append(message)
    
    def add_error(self, message: str) -> None:
        """添加错误信息"""
        self.errors.append(message)
        self.is_valid = False
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "is_valid": self.is_valid,
            "warnings": self.warnings,
            "errors": self.errors,
            "validated_values": self.validated_values
        }


class SandboxResourceConfig(BaseModel):
    """
    沙箱资源配置
    
    定义 CPU、内存、磁盘、进程数的默认值和限制范围。
    Requirements: 11.2 (支持配置默认资源限制)
    """
    # 默认资源限制
    default_cpu: float = Field(
        default=1.0, 
        ge=0.5, 
        le=4.0, 
        description="默认 CPU 核心数"
    )
    default_memory_mb: int = Field(
        default=256, 
        ge=256, 
        le=4096, 
        description="默认内存限制（MB）"
    )
    default_disk_mb: int = Field(
        default=1024, 
        ge=512, 
        le=10240, 
        description="默认磁盘限制（MB）"
    )
    default_pids: int = Field(
        default=100, 
        ge=10, 
        le=500, 
        description="默认进程数限制"
    )
    
    # 最大资源限制
    max_cpu: float = Field(
        default=4.0, 
        ge=0.5, 
        le=8.0,
        description="最大 CPU 核心数"
    )
    max_memory_mb: int = Field(
        default=4096, 
        ge=256, 
        le=16384,
        description="最大内存限制（MB）"
    )
    max_disk_mb: int = Field(
        default=10240, 
        ge=512, 
        le=51200,
        description="最大磁盘限制（MB）"
    )


class SandboxNetworkConfig(BaseModel):
    """
    沙箱网络配置
    
    定义网络访问策略，包括是否允许出站流量、白名单和黑名单。
    Requirements: 11.5 (支持配置网络策略)
    """
    default_allow_outbound: bool = Field(
        default=False, 
        description="默认是否允许出站流量"
    )
    allowed_hosts: List[str] = Field(
        default_factory=list, 
        description="允许访问的主机列表"
    )
    blocked_cidrs: List[str] = Field(
        default_factory=lambda: [
            "10.0.0.0/8",       # 私有网络 A 类
            "172.16.0.0/12",    # 私有网络 B 类
            "192.168.0.0/16",   # 私有网络 C 类
            "169.254.0.0/16",   # 链路本地地址
            "127.0.0.0/8"       # 回环地址
        ],
        description="禁止访问的 CIDR 列表"
    )


class SandboxSecurityConfig(BaseModel):
    """
    沙箱安全配置
    
    定义容器运行时、安全加固选项。
    支持 gVisor (runsc) 用户态内核隔离。
    """
    # gVisor 配置
    use_gvisor: bool = Field(
        default=False,
        description="是否使用 gVisor (runsc) 运行时"
    )
    gvisor_runtime: str = Field(
        default="runsc",
        description="gVisor 运行时名称（需与 Docker daemon 配置一致）"
    )
    
    # 安全加固
    read_only_rootfs: bool = Field(
        default=True,
        description="是否使用只读根文件系统"
    )
    disable_network: bool = Field(
        default=True,
        description="是否禁用网络（数据分析场景推荐）"
    )
    drop_all_capabilities: bool = Field(
        default=True,
        description="是否移除所有 Linux capabilities"
    )
    no_new_privileges: bool = Field(
        default=True,
        description="是否禁止提权"
    )


class SandboxTimeoutConfig(BaseModel):
    """
    沙箱超时配置
    
    定义各种超时参数。
    Requirements: 11.3 (支持配置超时参数)
    
    注意: session_idle_timeout 默认为 15 分钟（900 秒），
    这是为了在 4C4G 小机器上提高并发能力，避免僵尸会话浪费资源。
    如果需要更长的空闲时间，可以通过环境变量 SANDBOX_TIMEOUT__SESSION_IDLE_TIMEOUT 配置。
    """
    execution_timeout: int = Field(
        default=300, 
        ge=10, 
        le=3600, 
        description="代码执行超时（秒）"
    )
    session_idle_timeout: int = Field(
        default=900,  # 15 分钟，优化并发能力
        ge=60, 
        le=86400, 
        description="会话空闲超时（秒），默认 15 分钟"
    )
    session_max_timeout: int = Field(
        default=43200, 
        ge=3600, 
        le=172800, 
        description="会话最大时长（秒）"
    )
    sandbox_startup_timeout: int = Field(
        default=30, 
        ge=5, 
        le=120, 
        description="沙箱启动超时（秒）"
    )


class ExecutionQueueConfig(BaseModel):
    """
    执行队列配置

    基于令牌桶的并发控制。由于 Kernel Server 是单线程的，每个容器
    同时只能处理一个请求，因此 max_concurrent 应与容器池大小对齐，
    使每个预热容器都能同时服务一个请求。
    """
    max_concurrent_executions: int = Field(
        default=6,
        ge=1,
        le=50,
        description="最大并发执行数（建议 = 容器池大小）"
    )
    initial_avg_execution_time: float = Field(
        default=5.0,
        ge=1.0,
        le=60.0,
        description="初始平均执行时间估算(秒)"
    )
    queue_timeout: int = Field(
        default=300,
        ge=30,
        le=600,
        description="排队超时时间(秒)"
    )


class FireAndForgetConfig(BaseModel):
    """
    即用即毁执行配置

    无状态执行模式：借容器 → 注入数据 → 执行 → 清理 → 归还容器。
    """
    default_timeout: int = Field(
        default=30,
        ge=5,
        le=600,
        description="默认执行超时（秒）"
    )
    max_data_size_mb: int = Field(
        default=50,
        ge=1,
        le=200,
        description="data_files 总大小上限（MB）"
    )
    cleanup_timeout: int = Field(
        default=10,
        ge=3,
        le=60,
        description="容器清理超时（秒）"
    )


class ContainerPoolConfig(BaseModel):
    """
    容器池配置

    定义预热容器池的参数。
    Requirements: 11.4 (支持配置容器池参数)
    """
    pool_size: int = Field(
        default=3, 
        ge=0, 
        le=20, 
        description="预热容器数量"
    )
    max_concurrent_sandboxes: int = Field(
        default=20, 
        ge=1, 
        le=200, 
        description="最大并发沙箱数"
    )
    container_max_age_seconds: int = Field(
        default=3600, 
        ge=300, 
        le=86400, 
        description="容器最大存活时间（秒）"
    )
    health_check_interval: int = Field(
        default=60, 
        ge=10, 
        le=300, 
        description="健康检查间隔（秒）"
    )


class SandboxSettings(BaseSettings):
    """
    沙箱服务完整配置
    
    整合所有配置项，支持从环境变量加载。
    所有配置必须通过此 settings 对象访问，禁止使用 os.getenv。
    
    Requirements: 11.1 (通过 settings 对象读取所有配置)
    
    环境变量前缀: SANDBOX_
    例如: SANDBOX_DOCKER_IMAGE, SANDBOX_LOG_LEVEL
    """
    
    model_config = SettingsConfigDict(
        env_prefix="SANDBOX_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore"
    )
    
    # 嵌套配置
    resource: SandboxResourceConfig = Field(
        default_factory=SandboxResourceConfig,
        description="资源限制配置"
    )
    network: SandboxNetworkConfig = Field(
        default_factory=SandboxNetworkConfig,
        description="网络配置"
    )
    timeout: SandboxTimeoutConfig = Field(
        default_factory=SandboxTimeoutConfig,
        description="超时配置"
    )
    pool: ContainerPoolConfig = Field(
        default_factory=ContainerPoolConfig,
        description="容器池配置"
    )
    security: SandboxSecurityConfig = Field(
        default_factory=SandboxSecurityConfig,
        description="安全配置（gVisor 等）"
    )
    queue: ExecutionQueueConfig = Field(
        default_factory=ExecutionQueueConfig,
        description="执行队列配置（令牌桶并发控制）"
    )
    fire_and_forget: FireAndForgetConfig = Field(
        default_factory=FireAndForgetConfig,
        description="即用即毁执行配置"
    )
    
    # 基础配置
    docker_image: str = Field(
        default="code-executor:latest", 
        description="沙箱 Docker 镜像"
    )
    workspace_base: str = Field(
        default="/var/sandbox/workspaces", 
        description="工作空间基础目录"
    )
    log_level: str = Field(
        default="INFO", 
        description="日志级别"
    )
    
    # Docker 相关配置
    docker_socket: str = Field(
        default="unix:///var/run/docker.sock",
        description="Docker socket 路径"
    )
    network_name: str = Field(
        default="sandbox_network",
        description="沙箱网络名称"
    )
    
    # Sentry 配置
    sentry_dsn: str = Field(
        default="",
        description="Sentry DSN"
    )
    sentry_traces_sample_rate: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Sentry 追踪采样率"
    )
    
    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """
        验证日志级别
        
        如果提供的日志级别无效，记录警告并使用默认值 'INFO'。
        Requirements: 11.6 (配置参数无效时使用默认值并记录警告日志)
        """
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        upper_v = v.upper()
        if upper_v not in valid_levels:
            logger.warning(
                f"无效的日志级别 '{v}'，使用默认值 'INFO'。"
                f"有效值: {valid_levels}"
            )
            return "INFO"
        return upper_v
    
    @field_validator("docker_image")
    @classmethod
    def validate_docker_image(cls, v: str) -> str:
        """
        验证 Docker 镜像名称格式
        
        Docker 镜像名称应符合以下格式之一：
        - image
        - image:tag
        - registry/image
        - registry/image:tag
        - registry:port/image:tag
        
        如果格式无效，记录警告并使用默认值。
        Requirements: 11.6 (配置参数无效时使用默认值并记录警告日志)
        """
        default_image = "code-executor:latest"
        
        if not v or not v.strip():
            logger.warning(
                f"Docker 镜像名称为空，使用默认值 '{default_image}'"
            )
            return default_image
        
        v = v.strip()
        
        # 简化的验证：检查基本格式
        # 镜像名称不能包含空格，不能以 : 或 / 开头或结尾
        if ' ' in v or v.startswith(':') or v.startswith('/') or v.endswith(':') or v.endswith('/'):
            logger.warning(
                f"无效的 Docker 镜像名称 '{v}'，使用默认值 '{default_image}'。"
                f"镜像名称不能包含空格，不能以 ':' 或 '/' 开头或结尾"
            )
            return default_image
        
        # 检查是否包含非法字符
        # 允许的字符：字母、数字、点、连字符、下划线、冒号、斜杠
        if not re.match(r'^[a-zA-Z0-9._:/-]+$', v):
            logger.warning(
                f"无效的 Docker 镜像名称 '{v}'，包含非法字符，使用默认值 '{default_image}'"
            )
            return default_image
        
        return v
    
    @field_validator("workspace_base")
    @classmethod
    def validate_workspace_base(cls, v: str) -> str:
        """
        验证工作空间基础目录路径格式
        
        路径应为有效的 Unix 风格绝对路径。
        如果格式无效，记录警告并使用默认值。
        Requirements: 11.6 (配置参数无效时使用默认值并记录警告日志)
        """
        default_path = "/var/sandbox/workspaces"
        
        if not v or not v.strip():
            logger.warning(
                f"工作空间路径为空，使用默认值 '{default_path}'"
            )
            return default_path
        
        v = v.strip()
        
        # 必须是绝对路径（以 / 开头）
        if not v.startswith('/'):
            logger.warning(
                f"无效的工作空间路径 '{v}'，必须是绝对路径（以 '/' 开头），"
                f"使用默认值 '{default_path}'"
            )
            return default_path
        
        # 检查路径格式：不能包含连续的斜杠、不能包含特殊字符
        if '//' in v or '\x00' in v:
            logger.warning(
                f"无效的工作空间路径 '{v}'，路径格式不正确，"
                f"使用默认值 '{default_path}'"
            )
            return default_path
        
        # 检查是否包含非法字符（只允许字母、数字、点、连字符、下划线、斜杠）
        if not re.match(r'^/[a-zA-Z0-9._/-]*$', v):
            logger.warning(
                f"无效的工作空间路径 '{v}'，包含非法字符，"
                f"使用默认值 '{default_path}'"
            )
            return default_path
        
        return v
    
    @field_validator("docker_socket")
    @classmethod
    def validate_docker_socket(cls, v: str) -> str:
        """
        验证 Docker socket 路径格式
        
        支持以下格式：
        - unix:///var/run/docker.sock
        - /var/run/docker.sock
        - tcp://host:port (用于远程 Docker)
        
        如果格式无效，记录警告并使用默认值。
        Requirements: 11.6 (配置参数无效时使用默认值并记录警告日志)
        """
        default_socket = "unix:///var/run/docker.sock"
        
        if not v or not v.strip():
            logger.warning(
                f"Docker socket 路径为空，使用默认值 '{default_socket}'"
            )
            return default_socket
        
        v = v.strip()
        
        # 支持 unix:// 协议
        if v.startswith('unix://'):
            path = v[7:]  # 移除 'unix://' 前缀
            if not path.startswith('/'):
                logger.warning(
                    f"无效的 Docker socket 路径 '{v}'，unix:// 后必须是绝对路径，"
                    f"使用默认值 '{default_socket}'"
                )
                return default_socket
            # 检查路径格式
            if not re.match(r'^/[a-zA-Z0-9._/-]+$', path):
                logger.warning(
                    f"无效的 Docker socket 路径 '{v}'，路径格式不正确，"
                    f"使用默认值 '{default_socket}'"
                )
                return default_socket
            return v
        
        # 支持 tcp:// 协议（用于远程 Docker）
        if v.startswith('tcp://'):
            # 简单验证 tcp://host:port 格式
            tcp_part = v[6:]  # 移除 'tcp://' 前缀
            if not re.match(r'^[a-zA-Z0-9.-]+:\d+$', tcp_part):
                logger.warning(
                    f"无效的 Docker socket 路径 '{v}'，tcp:// 格式应为 tcp://host:port，"
                    f"使用默认值 '{default_socket}'"
                )
                return default_socket
            return v
        
        # 支持直接的 Unix 路径
        if v.startswith('/'):
            if not re.match(r'^/[a-zA-Z0-9._/-]+$', v):
                logger.warning(
                    f"无效的 Docker socket 路径 '{v}'，路径格式不正确，"
                    f"使用默认值 '{default_socket}'"
                )
                return default_socket
            return v
        
        # 其他格式无效
        logger.warning(
            f"无效的 Docker socket 路径 '{v}'，"
            f"支持的格式: unix:///path, tcp://host:port, /path，"
            f"使用默认值 '{default_socket}'"
        )
        return default_socket
    
    @field_validator("network_name")
    @classmethod
    def validate_network_name(cls, v: str) -> str:
        """
        验证 Docker 网络名称格式
        
        Docker 网络名称规则：
        - 长度 1-64 字符
        - 只能包含字母、数字、下划线、连字符、点
        - 必须以字母或数字开头
        
        如果格式无效，记录警告并使用默认值。
        Requirements: 11.6 (配置参数无效时使用默认值并记录警告日志)
        """
        default_network = "sandbox_network"
        
        if not v or not v.strip():
            logger.warning(
                f"Docker 网络名称为空，使用默认值 '{default_network}'"
            )
            return default_network
        
        v = v.strip()
        
        # 检查长度
        if len(v) > 64:
            logger.warning(
                f"Docker 网络名称 '{v}' 超过最大长度 64 字符，"
                f"使用默认值 '{default_network}'"
            )
            return default_network
        
        # 检查格式：必须以字母或数字开头，只能包含字母、数字、下划线、连字符、点
        if not DOCKER_NETWORK_NAME_PATTERN.match(v):
            logger.warning(
                f"无效的 Docker 网络名称 '{v}'，"
                f"名称必须以字母或数字开头，只能包含字母、数字、下划线、连字符、点，"
                f"使用默认值 '{default_network}'"
            )
            return default_network
        
        return v
    
    def validate_resource_limits(
        self, 
        cpu: float | None = None,
        memory_mb: int | None = None,
        disk_mb: int | None = None
    ) -> tuple[float, int, int]:
        """
        验证并返回有效的资源限制值
        
        如果提供的值超出范围，则使用默认值并记录警告。
        
        Args:
            cpu: CPU 核心数
            memory_mb: 内存限制（MB）
            disk_mb: 磁盘限制（MB）
            
        Returns:
            (cpu, memory_mb, disk_mb) 元组
        """
        # CPU 验证
        if cpu is None:
            cpu = self.resource.default_cpu
        elif cpu < 0.5 or cpu > self.resource.max_cpu:
            logger.warning(
                f"CPU 限制 {cpu} 超出范围 [0.5, {self.resource.max_cpu}]，"
                f"使用默认值 {self.resource.default_cpu}"
            )
            cpu = self.resource.default_cpu
        
        # 内存验证
        if memory_mb is None:
            memory_mb = self.resource.default_memory_mb
        elif memory_mb < 256 or memory_mb > self.resource.max_memory_mb:
            logger.warning(
                f"内存限制 {memory_mb}MB 超出范围 [256, {self.resource.max_memory_mb}]，"
                f"使用默认值 {self.resource.default_memory_mb}MB"
            )
            memory_mb = self.resource.default_memory_mb
        
        # 磁盘验证
        if disk_mb is None:
            disk_mb = self.resource.default_disk_mb
        elif disk_mb < 512 or disk_mb > self.resource.max_disk_mb:
            logger.warning(
                f"磁盘限制 {disk_mb}MB 超出范围 [512, {self.resource.max_disk_mb}]，"
                f"使用默认值 {self.resource.default_disk_mb}MB"
            )
            disk_mb = self.resource.default_disk_mb
        
        return cpu, memory_mb, disk_mb
    
    def validate_all(self) -> ConfigValidationResult:
        """
        验证整个配置并返回验证报告
        
        检查所有配置项的有效性，收集警告和错误信息。
        此方法不会修改配置值，只是报告当前配置的状态。
        
        Requirements: 11.6 (配置参数无效时使用默认值并记录警告日志)
        
        Returns:
            ConfigValidationResult: 包含验证结果、警告和错误信息
        """
        result = ConfigValidationResult()
        
        # 记录已验证的配置值
        result.validated_values = {
            "docker_image": self.docker_image,
            "workspace_base": self.workspace_base,
            "docker_socket": self.docker_socket,
            "network_name": self.network_name,
            "log_level": self.log_level,
        }
        
        # 验证 Docker 镜像名称
        if self.docker_image == "code-executor:latest":
            # 检查是否是因为验证失败而使用默认值
            # 这里我们只是记录当前使用的是默认值
            pass
        
        # 验证资源配置范围
        resource = self.resource
        if resource.default_cpu > resource.max_cpu:
            result.add_warning(
                f"默认 CPU ({resource.default_cpu}) 大于最大 CPU ({resource.max_cpu})，"
                f"可能导致资源分配问题"
            )
        
        if resource.default_memory_mb > resource.max_memory_mb:
            result.add_warning(
                f"默认内存 ({resource.default_memory_mb}MB) 大于最大内存 ({resource.max_memory_mb}MB)，"
                f"可能导致资源分配问题"
            )
        
        if resource.default_disk_mb > resource.max_disk_mb:
            result.add_warning(
                f"默认磁盘 ({resource.default_disk_mb}MB) 大于最大磁盘 ({resource.max_disk_mb}MB)，"
                f"可能导致资源分配问题"
            )
        
        # 验证超时配置逻辑
        timeout = self.timeout
        if timeout.session_idle_timeout > timeout.session_max_timeout:
            result.add_warning(
                f"会话空闲超时 ({timeout.session_idle_timeout}s) 大于会话最大超时 ({timeout.session_max_timeout}s)，"
                f"空闲超时将永远不会触发"
            )
        
        if timeout.execution_timeout > timeout.session_idle_timeout:
            result.add_warning(
                f"执行超时 ({timeout.execution_timeout}s) 大于会话空闲超时 ({timeout.session_idle_timeout}s)，"
                f"长时间执行可能导致会话被意外终止"
            )
        
        # 验证容器池配置
        pool = self.pool
        if pool.pool_size > pool.max_concurrent_sandboxes:
            result.add_warning(
                f"预热容器数量 ({pool.pool_size}) 大于最大并发沙箱数 ({pool.max_concurrent_sandboxes})，"
                f"预热容器数量将被限制"
            )
        
        # 验证网络配置
        network = self.network
        if network.default_allow_outbound and not network.allowed_hosts:
            result.add_warning(
                "已启用出站网络访问但未配置允许的主机列表，"
                "沙箱将可以访问任意外部地址"
            )
        
        # 记录资源配置
        result.validated_values["resource"] = {
            "default_cpu": resource.default_cpu,
            "default_memory_mb": resource.default_memory_mb,
            "default_disk_mb": resource.default_disk_mb,
            "max_cpu": resource.max_cpu,
            "max_memory_mb": resource.max_memory_mb,
            "max_disk_mb": resource.max_disk_mb,
        }
        
        # 记录超时配置
        result.validated_values["timeout"] = {
            "execution_timeout": timeout.execution_timeout,
            "session_idle_timeout": timeout.session_idle_timeout,
            "session_max_timeout": timeout.session_max_timeout,
            "sandbox_startup_timeout": timeout.sandbox_startup_timeout,
        }
        
        # 记录容器池配置
        result.validated_values["pool"] = {
            "pool_size": pool.pool_size,
            "max_concurrent_sandboxes": pool.max_concurrent_sandboxes,
            "container_max_age_seconds": pool.container_max_age_seconds,
            "health_check_interval": pool.health_check_interval,
        }
        
        # 记录网络配置
        result.validated_values["network"] = {
            "default_allow_outbound": network.default_allow_outbound,
            "allowed_hosts_count": len(network.allowed_hosts),
            "blocked_cidrs_count": len(network.blocked_cidrs),
        }
        
        # 记录验证结果到日志
        if result.warnings:
            for warning in result.warnings:
                logger.warning(f"配置验证警告: {warning}")
        
        if result.errors:
            for error in result.errors:
                logger.error(f"配置验证错误: {error}")
        
        if result.is_valid and not result.warnings:
            logger.info("配置验证通过，所有配置项有效")
        
        return result


def get_settings() -> SandboxSettings:
    """
    获取配置单例
    
    使用 lru_cache 确保只创建一个配置实例。
    
    Returns:
        SandboxSettings 实例
    """
    return _settings


def _create_settings() -> SandboxSettings:
    """
    创建配置实例
    
    处理配置加载过程中的异常，确保服务能够启动。
    Requirements: 11.6 (配置参数无效时使用默认值)
    """
    try:
        return SandboxSettings()
    except Exception as e:
        logger.error(f"配置加载失败: {e}，使用默认配置")
        # 返回默认配置
        return SandboxSettings.model_construct(
            resource=SandboxResourceConfig(),
            network=SandboxNetworkConfig(),
            timeout=SandboxTimeoutConfig(),
            pool=ContainerPoolConfig(),
            docker_image="code-executor:latest",
            workspace_base="/var/sandbox/workspaces",
            log_level="INFO",
            docker_socket="unix:///var/run/docker.sock",
            network_name="sandbox_network"
        )


# 全局配置单例
_settings = _create_settings()


# 导出 settings 对象供其他模块使用
settings = _settings
