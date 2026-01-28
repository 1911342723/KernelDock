"""
安全策略配置模块

管理沙箱容器的安全配置，包括用户权限、特权模式、文件系统权限、
Linux capabilities 限制和进程数限制等安全相关设置。

Requirements:
- 6.1: 以非 root 用户身份运行沙箱内的代码
- 6.2: 禁用沙箱容器的特权模式
- 6.3: 设置只读的根文件系统（除数据和输出目录外）
- 6.4: 限制沙箱可使用的 Linux capabilities（仅保留必要的能力）
- 6.5: 禁止沙箱内的进程执行 fork bomb 攻击（限制进程数量）
- 6.6: 禁止沙箱访问宿主机的 Docker socket
- 6.7: 检测到可疑行为时记录安全事件到日志
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from ..config import settings

logger = logging.getLogger(__name__)


class SecurityLevel(Enum):
    """
    安全级别枚举
    
    定义不同的安全级别，用于快速配置安全策略。
    """
    LOW = "low"           # 低安全级别（开发/测试环境）
    MEDIUM = "medium"     # 中等安全级别（默认）
    HIGH = "high"         # 高安全级别（生产环境）
    MAXIMUM = "maximum"   # 最高安全级别（高风险场景）


# 默认要移除的 Linux capabilities
# Requirements 6.4: 限制沙箱可使用的 Linux capabilities
# 移除所有危险的 capabilities，只保留必要的最小集合
DEFAULT_CAP_DROP = [
    "ALL"  # 移除所有 capabilities，然后通过 cap_add 添加必要的
]

# 默认要添加的 Linux capabilities（最小必要集合）
# 这些是运行 Python 代码所需的最小 capabilities
DEFAULT_CAP_ADD: List[str] = [
    # 不添加任何额外的 capabilities，使用最小权限原则
    # 如果需要特定功能，可以在此添加
]

# 高风险 capabilities 列表（绝对不应该授予沙箱）
DANGEROUS_CAPABILITIES = [
    "CAP_SYS_ADMIN",      # 系统管理（可以挂载文件系统等）
    "CAP_NET_ADMIN",      # 网络管理
    "CAP_SYS_PTRACE",     # 进程跟踪（可以调试其他进程）
    "CAP_SYS_MODULE",     # 加载内核模块
    "CAP_SYS_RAWIO",      # 原始 I/O 访问
    "CAP_SYS_BOOT",       # 重启系统
    "CAP_SYS_TIME",       # 修改系统时间
    "CAP_SYS_RESOURCE",   # 修改资源限制
    "CAP_MKNOD",          # 创建设备节点
    "CAP_AUDIT_CONTROL",  # 审计控制
    "CAP_AUDIT_WRITE",    # 审计写入
    "CAP_MAC_ADMIN",      # MAC 管理
    "CAP_MAC_OVERRIDE",   # MAC 覆盖
    "CAP_SETFCAP",        # 设置文件 capabilities
    "CAP_DAC_READ_SEARCH", # 绕过文件读取权限检查
    "CAP_LINUX_IMMUTABLE", # 修改不可变文件
    "CAP_NET_BIND_SERVICE", # 绑定低端口
    "CAP_NET_RAW",        # 原始网络访问
    "CAP_IPC_LOCK",       # 锁定内存
    "CAP_IPC_OWNER",      # IPC 所有者
    "CAP_SYS_CHROOT",     # chroot
    "CAP_BLOCK_SUSPEND",  # 阻止系统挂起
    "CAP_WAKE_ALARM",     # 唤醒系统
]

# 默认安全选项
# 这些选项增强容器的安全隔离
DEFAULT_SECURITY_OPT = [
    "no-new-privileges:true",  # 禁止获取新权限
]

# 默认进程数限制
# Requirements 6.5: 防止 fork bomb 攻击
DEFAULT_PIDS_LIMIT = 100
MIN_PIDS_LIMIT = 10
MAX_PIDS_LIMIT = 500

# 默认沙箱用户配置
# Requirements 6.1: 以非 root 用户身份运行
DEFAULT_USER = "sandbox"
DEFAULT_USER_ID = 1000
DEFAULT_GROUP_ID = 1000

# Docker socket 路径（需要禁止访问）
# Requirements 6.6: 禁止沙箱访问宿主机的 Docker socket
DOCKER_SOCKET_PATH = "/var/run/docker.sock"


@dataclass
class SecurityPolicy:
    """
    安全策略配置数据类
    
    定义沙箱容器的安全配置，包括用户权限、特权模式、文件系统权限、
    Linux capabilities 限制和进程数限制等。
    
    Requirements:
    - 6.1: 以非 root 用户身份运行沙箱内的代码
    - 6.2: 禁用沙箱容器的特权模式
    - 6.3: 设置只读的根文件系统（除数据和输出目录外）
    - 6.4: 限制沙箱可使用的 Linux capabilities
    - 6.5: 禁止 fork bomb 攻击（限制进程数量）
    - 6.6: 禁止沙箱访问宿主机的 Docker socket
    
    Attributes:
        user: 运行用户（格式: "uid:gid" 或用户名）
        privileged: 是否启用特权模式（应始终为 False）
        read_only_rootfs: 是否设置只读根文件系统
        cap_drop: 要移除的 Linux capabilities 列表
        cap_add: 要添加的 Linux capabilities 列表
        security_opt: 安全选项列表
        pids_limit: 进程数限制
        no_new_privileges: 是否禁止获取新权限
        writable_paths: 可写路径列表（用于 tmpfs 挂载）
    """
    # 用户配置 - Requirements 6.1
    user: str = f"{DEFAULT_USER_ID}:{DEFAULT_GROUP_ID}"
    
    # 特权模式 - Requirements 6.2
    privileged: bool = False
    
    # 只读根文件系统 - Requirements 6.3
    read_only_rootfs: bool = True
    
    # Linux capabilities - Requirements 6.4
    cap_drop: List[str] = field(default_factory=lambda: list(DEFAULT_CAP_DROP))
    cap_add: List[str] = field(default_factory=lambda: list(DEFAULT_CAP_ADD))
    
    # 安全选项
    security_opt: List[str] = field(default_factory=lambda: list(DEFAULT_SECURITY_OPT))
    
    # 进程数限制 - Requirements 6.5
    pids_limit: int = DEFAULT_PIDS_LIMIT
    
    # 禁止获取新权限
    no_new_privileges: bool = True
    
    # 可写路径（用于 tmpfs 挂载）- Requirements 6.3
    # 这些路径将被挂载为 tmpfs，允许写入
    writable_paths: List[str] = field(default_factory=lambda: [
        "/tmp",           # 临时文件目录
        "/var/tmp",       # 临时文件目录
        "/run",           # 运行时目录
        "/home/sandbox",  # 用户主目录
    ])
    
    # 数据目录和输出目录（通过卷挂载，不使用 tmpfs）
    data_dir: str = "/data"
    output_dir: str = "/output"
    
    def __post_init__(self):
        """初始化后验证和处理"""
        # 验证进程数限制
        self.pids_limit = self._validate_pids_limit(self.pids_limit)
        
        # 确保特权模式始终禁用 - Requirements 6.2
        if self.privileged:
            logger.warning("安全策略: 特权模式被强制禁用")
            self.privileged = False
        
        # 确保危险的 capabilities 不会被添加
        self._validate_capabilities()
        
        logger.debug(f"安全策略初始化完成: {self}")
    
    def _validate_pids_limit(self, pids: int) -> int:
        """
        验证并调整进程数限制
        
        Args:
            pids: 原始进程数限制
            
        Returns:
            验证后的进程数限制
        """
        if pids < MIN_PIDS_LIMIT:
            logger.warning(
                f"进程数限制 {pids} 小于最小值 {MIN_PIDS_LIMIT}，使用最小值"
            )
            return MIN_PIDS_LIMIT
        if pids > MAX_PIDS_LIMIT:
            logger.warning(
                f"进程数限制 {pids} 大于最大值 {MAX_PIDS_LIMIT}，使用最大值"
            )
            return MAX_PIDS_LIMIT
        return pids
    
    def _validate_capabilities(self) -> None:
        """
        验证 capabilities 配置
        
        确保危险的 capabilities 不会被添加到沙箱。
        """
        # 检查是否有危险的 capabilities 被添加
        dangerous_added = set(self.cap_add) & set(DANGEROUS_CAPABILITIES)
        if dangerous_added:
            logger.warning(
                f"安全策略: 移除危险的 capabilities: {dangerous_added}"
            )
            self.cap_add = [
                cap for cap in self.cap_add 
                if cap not in DANGEROUS_CAPABILITIES
            ]
    
    def to_docker_config(self) -> Dict[str, Any]:
        """
        转换为 Docker 容器创建配置
        
        生成可直接用于 Docker SDK 创建容器的配置字典。
        
        Returns:
            Docker 配置字典，包含:
            - user: 运行用户
            - privileged: 特权模式（始终为 False）
            - read_only: 只读根文件系统
            - cap_drop: 移除的 capabilities
            - cap_add: 添加的 capabilities
            - security_opt: 安全选项
            - pids_limit: 进程数限制
            - tmpfs: tmpfs 挂载配置
        """
        config: Dict[str, Any] = {
            # Requirements 6.1: 非 root 用户运行
            "user": self.user,
            
            # Requirements 6.2: 禁用特权模式
            "privileged": False,  # 强制禁用，忽略 self.privileged
            
            # Requirements 6.3: 只读根文件系统
            "read_only": self.read_only_rootfs,
            
            # Requirements 6.4: Linux capabilities 限制
            "cap_drop": self.cap_drop,
            
            # Requirements 6.5: 进程数限制
            "pids_limit": self.pids_limit,
            
            # 安全选项
            "security_opt": self.security_opt,
        }
        
        # 只有在有需要添加的 capabilities 时才添加
        if self.cap_add:
            config["cap_add"] = self.cap_add
        
        return config
    
    def get_tmpfs_config(self) -> Dict[str, str]:
        """
        获取 tmpfs 挂载配置
        
        为可写路径生成 tmpfs 挂载配置。
        Requirements 6.3: 只读根文件系统，但允许特定目录写入
        
        Returns:
            tmpfs 配置字典，格式: {路径: 挂载选项}
        """
        tmpfs_config = {}
        for path in self.writable_paths:
            # 设置 tmpfs 挂载选项
            # - size: 限制大小（防止填满内存）
            # - mode: 权限模式
            # - uid/gid: 所有者
            tmpfs_config[path] = f"size=100M,mode=1777,uid={DEFAULT_USER_ID},gid={DEFAULT_GROUP_ID}"
        
        return tmpfs_config
    
    def get_volume_mounts(
        self,
        data_host_path: str,
        output_host_path: str
    ) -> Dict[str, Dict[str, str]]:
        """
        获取卷挂载配置
        
        为数据目录和输出目录生成卷挂载配置。
        Requirements 6.3: 数据和输出目录需要可写
        
        Args:
            data_host_path: 宿主机数据目录路径
            output_host_path: 宿主机输出目录路径
            
        Returns:
            卷挂载配置字典
        """
        return {
            data_host_path: {
                "bind": self.data_dir,
                "mode": "rw"  # 数据目录可读写
            },
            output_host_path: {
                "bind": self.output_dir,
                "mode": "rw"  # 输出目录可读写
            }
        }
    
    def get_blocked_mounts(self) -> List[str]:
        """
        获取禁止挂载的路径列表
        
        Requirements 6.6: 禁止沙箱访问宿主机的 Docker socket
        
        Returns:
            禁止挂载的路径列表
        """
        return [
            DOCKER_SOCKET_PATH,           # Docker socket
            "/var/run/docker",            # Docker 运行目录
            "/etc/docker",                # Docker 配置目录
            "/root",                      # root 用户目录
            "/etc/shadow",                # 密码文件
            "/etc/passwd",                # 用户文件（可选择性允许只读）
            "/proc/sys",                  # 内核参数
            "/sys/kernel",                # 内核信息
        ]
    
    def validate_mount_path(self, host_path: str) -> bool:
        """
        验证挂载路径是否安全
        
        检查宿主机路径是否在禁止挂载列表中。
        Requirements 6.6: 禁止访问 Docker socket
        
        Args:
            host_path: 宿主机路径
            
        Returns:
            True 如果路径安全可挂载，False 如果路径被禁止
        """
        blocked_paths = self.get_blocked_mounts()
        
        # 检查路径是否在禁止列表中
        for blocked in blocked_paths:
            if host_path.startswith(blocked) or blocked.startswith(host_path):
                logger.warning(
                    f"安全策略: 禁止挂载路径 {host_path}（匹配禁止规则: {blocked}）"
                )
                return False
        
        return True
    
    def log_security_event(
        self,
        event_type: str,
        container_id: str,
        details: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        记录安全事件
        
        Requirements 6.7: 检测到可疑行为时记录安全事件到日志
        
        Args:
            event_type: 事件类型
            container_id: 容器 ID
            details: 事件详情（可选）
        """
        log_message = (
            f"安全事件 [{event_type}]: "
            f"容器={container_id[:12] if len(container_id) > 12 else container_id}"
        )
        if details:
            log_message += f", 详情={details}"
        
        # 根据事件类型选择日志级别
        if event_type in ["privilege_escalation", "capability_abuse", "docker_socket_access"]:
            logger.error(log_message)
        elif event_type in ["suspicious_process", "resource_abuse"]:
            logger.warning(log_message)
        else:
            logger.info(log_message)
    
    def __str__(self) -> str:
        """返回安全策略的字符串表示"""
        return (
            f"SecurityPolicy("
            f"user={self.user}, "
            f"privileged={self.privileged}, "
            f"read_only={self.read_only_rootfs}, "
            f"cap_drop={self.cap_drop}, "
            f"pids_limit={self.pids_limit})"
        )


class SecurityPolicyBuilder:
    """
    安全策略构建器
    
    提供流式 API 来构建安全策略配置。
    """
    
    def __init__(self):
        """初始化构建器"""
        self._user = f"{DEFAULT_USER_ID}:{DEFAULT_GROUP_ID}"
        self._privileged = False
        self._read_only_rootfs = True
        self._cap_drop = list(DEFAULT_CAP_DROP)
        self._cap_add = list(DEFAULT_CAP_ADD)
        self._security_opt = list(DEFAULT_SECURITY_OPT)
        self._pids_limit = DEFAULT_PIDS_LIMIT
        self._no_new_privileges = True
        self._writable_paths = ["/tmp", "/var/tmp", "/run", "/home/sandbox"]
    
    def with_user(self, user: str) -> "SecurityPolicyBuilder":
        """
        设置运行用户
        
        Args:
            user: 用户标识（格式: "uid:gid" 或用户名）
            
        Returns:
            构建器实例
        """
        self._user = user
        return self
    
    def with_user_id(self, uid: int, gid: int) -> "SecurityPolicyBuilder":
        """
        设置运行用户 ID
        
        Args:
            uid: 用户 ID
            gid: 组 ID
            
        Returns:
            构建器实例
        """
        self._user = f"{uid}:{gid}"
        return self
    
    def with_read_only_rootfs(self, enabled: bool = True) -> "SecurityPolicyBuilder":
        """
        设置只读根文件系统
        
        Args:
            enabled: 是否启用
            
        Returns:
            构建器实例
        """
        self._read_only_rootfs = enabled
        return self
    
    def with_pids_limit(self, limit: int) -> "SecurityPolicyBuilder":
        """
        设置进程数限制
        
        Args:
            limit: 进程数限制
            
        Returns:
            构建器实例
        """
        self._pids_limit = limit
        return self
    
    def with_capabilities(
        self,
        drop: Optional[List[str]] = None,
        add: Optional[List[str]] = None
    ) -> "SecurityPolicyBuilder":
        """
        设置 Linux capabilities
        
        Args:
            drop: 要移除的 capabilities
            add: 要添加的 capabilities
            
        Returns:
            构建器实例
        """
        if drop is not None:
            self._cap_drop = drop
        if add is not None:
            self._cap_add = add
        return self
    
    def with_writable_paths(self, paths: List[str]) -> "SecurityPolicyBuilder":
        """
        设置可写路径
        
        Args:
            paths: 可写路径列表
            
        Returns:
            构建器实例
        """
        self._writable_paths = paths
        return self
    
    def add_writable_path(self, path: str) -> "SecurityPolicyBuilder":
        """
        添加可写路径
        
        Args:
            path: 可写路径
            
        Returns:
            构建器实例
        """
        if path not in self._writable_paths:
            self._writable_paths.append(path)
        return self
    
    def with_security_level(self, level: SecurityLevel) -> "SecurityPolicyBuilder":
        """
        根据安全级别配置策略
        
        Args:
            level: 安全级别
            
        Returns:
            构建器实例
        """
        if level == SecurityLevel.LOW:
            # 低安全级别（开发/测试）
            self._read_only_rootfs = False
            self._pids_limit = 200
            self._cap_drop = []
        elif level == SecurityLevel.MEDIUM:
            # 中等安全级别（默认）
            self._read_only_rootfs = True
            self._pids_limit = 100
            self._cap_drop = ["ALL"]
        elif level == SecurityLevel.HIGH:
            # 高安全级别
            self._read_only_rootfs = True
            self._pids_limit = 50
            self._cap_drop = ["ALL"]
            self._security_opt = ["no-new-privileges:true", "seccomp=default"]
        elif level == SecurityLevel.MAXIMUM:
            # 最高安全级别
            self._read_only_rootfs = True
            self._pids_limit = 30
            self._cap_drop = ["ALL"]
            self._cap_add = []
            self._security_opt = [
                "no-new-privileges:true",
                "seccomp=default",
            ]
        
        return self
    
    def build(self) -> SecurityPolicy:
        """
        构建安全策略
        
        Returns:
            SecurityPolicy 实例
        """
        return SecurityPolicy(
            user=self._user,
            privileged=self._privileged,
            read_only_rootfs=self._read_only_rootfs,
            cap_drop=self._cap_drop,
            cap_add=self._cap_add,
            security_opt=self._security_opt,
            pids_limit=self._pids_limit,
            no_new_privileges=self._no_new_privileges,
            writable_paths=self._writable_paths
        )


def create_default_security_policy() -> SecurityPolicy:
    """
    创建默认安全策略
    
    使用 settings 配置创建默认的安全策略。
    
    Returns:
        SecurityPolicy 实例
    """
    return SecurityPolicy(
        user=f"{DEFAULT_USER_ID}:{DEFAULT_GROUP_ID}",
        privileged=False,
        read_only_rootfs=True,
        cap_drop=list(DEFAULT_CAP_DROP),
        cap_add=list(DEFAULT_CAP_ADD),
        security_opt=list(DEFAULT_SECURITY_OPT),
        pids_limit=settings.resource.default_pids,
        no_new_privileges=True
    )


def create_security_policy_from_settings() -> SecurityPolicy:
    """
    从 settings 创建安全策略
    
    读取 settings 中的配置创建安全策略。
    
    Returns:
        SecurityPolicy 实例
    """
    return SecurityPolicy(
        user=f"{DEFAULT_USER_ID}:{DEFAULT_GROUP_ID}",
        privileged=False,
        read_only_rootfs=True,
        cap_drop=list(DEFAULT_CAP_DROP),
        cap_add=list(DEFAULT_CAP_ADD),
        security_opt=list(DEFAULT_SECURITY_OPT),
        pids_limit=settings.resource.default_pids,
        no_new_privileges=True
    )


def validate_security_config(config: Dict[str, Any]) -> bool:
    """
    验证安全配置
    
    检查配置是否符合安全要求。
    
    Args:
        config: Docker 容器配置字典
        
    Returns:
        True 如果配置安全，False 如果存在安全风险
    """
    is_secure = True
    
    # 检查特权模式 - Requirements 6.2
    if config.get("privileged", False):
        logger.error("安全验证失败: 特权模式已启用")
        is_secure = False
    
    # 检查用户 - Requirements 6.1
    user = config.get("user", "")
    if not user or user == "root" or user == "0" or user == "0:0":
        logger.warning("安全验证警告: 容器将以 root 用户运行")
        is_secure = False
    
    # 检查 capabilities - Requirements 6.4
    cap_add = config.get("cap_add", [])
    dangerous_caps = set(cap_add) & set(DANGEROUS_CAPABILITIES)
    if dangerous_caps:
        logger.error(f"安全验证失败: 添加了危险的 capabilities: {dangerous_caps}")
        is_secure = False
    
    # 检查进程数限制 - Requirements 6.5
    pids_limit = config.get("pids_limit")
    if pids_limit is None or pids_limit <= 0:
        logger.warning("安全验证警告: 未设置进程数限制")
    elif pids_limit > MAX_PIDS_LIMIT:
        logger.warning(f"安全验证警告: 进程数限制过高: {pids_limit}")
    
    # 检查卷挂载 - Requirements 6.6
    volumes = config.get("volumes", {})
    for host_path in volumes.keys():
        if DOCKER_SOCKET_PATH in host_path:
            logger.error("安全验证失败: 尝试挂载 Docker socket")
            is_secure = False
    
    return is_secure


def get_security_recommendations() -> List[str]:
    """
    获取安全建议
    
    返回沙箱安全配置的最佳实践建议。
    
    Returns:
        安全建议列表
    """
    return [
        "始终以非 root 用户运行容器 (Requirements 6.1)",
        "禁用特权模式 (Requirements 6.2)",
        "使用只读根文件系统，仅允许必要目录可写 (Requirements 6.3)",
        "移除所有不必要的 Linux capabilities (Requirements 6.4)",
        "设置合理的进程数限制以防止 fork bomb (Requirements 6.5)",
        "禁止挂载 Docker socket 和其他敏感路径 (Requirements 6.6)",
        "启用 no-new-privileges 安全选项",
        "使用 seccomp 配置文件限制系统调用",
        "定期审计安全日志",
    ]
