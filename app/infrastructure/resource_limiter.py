"""
资源限制器模块

管理容器的资源限制配置和使用监控。
包括 CPU、内存、磁盘、进程数限制的配置和验证，
以及资源使用情况的查询。

"""

import logging
from dataclasses import dataclass
from typing import Dict, Any, Optional

from ..config import settings
from ..exceptions import ResourceLimitExceededError
from .docker_client import DockerClient

logger = logging.getLogger(__name__)


# 资源限制的「绝对硬护栏」（ABSOLUTE GUARDRAILS）
# ----------------------------------------------------------------------------
# 这是无论 .env / 环境变量 / 运行时配置 API 怎么改，都绝不可逾越的物理与安全天花板。
# 运行时可调的「软上限」(settings.resource.max_*) 只能在 [MIN, MAX] 区间内收紧，
# 不能突破这里的边界。取值与 config.SandboxResourceConfig 各字段的 le 对齐，
# 这样用户在 config 允许范围内配置的上限都能真实生效（修复此前软上限形同虚设的问题）。
MIN_CPU = 0.5
MAX_CPU = 8.0
MIN_MEMORY_MB = 256
MAX_MEMORY_MB = 16384
MIN_DISK_MB = 512
MAX_DISK_MB = 51200
MIN_PIDS = 10
MAX_PIDS = 500


def _clamp_to_guardrail(value: float, lo: float, hi: float) -> float:
    """把数值收敛到 [lo, hi] 绝对护栏区间内。"""
    return min(max(value, lo), hi)


@dataclass
class ResourceLimits:
    """
    资源限制配置数据类
    
    定义沙箱容器的资源限制，包括 CPU、内存、磁盘和进程数。
    提供转换为 Docker 配置格式的方法。
    
    Attributes:
        cpu_count: CPU 核心数（支持小数，如 0.5 表示半个核心）
        memory_mb: 内存限制（MB）
        disk_mb: 磁盘限制（MB）
        pids_limit: 进程数限制
    """
    cpu_count: float           # CPU 核心数（支持小数）
    memory_mb: int             # 内存限制（MB）
    disk_mb: int               # 磁盘限制（MB）
    pids_limit: int            # 进程数限制
    
    def to_docker_config(self) -> Dict[str, Any]:
        """
        转换为 Docker 配置格式
        
        将资源限制转换为 Docker API 可接受的配置字典。
        
        Returns:
            Docker 资源配置字典，包含:
            - nano_cpus: CPU 限制（纳秒单位）
            - mem_limit: 内存限制
            - memswap_limit: 内存+交换限制（设为与 mem_limit 相同以禁用 swap）
            - pids_limit: 进程数限制
            - storage_opt: 存储选项（磁盘限制）
        """
        return {
            "nano_cpus": int(self.cpu_count * 1e9),
            "mem_limit": f"{self.memory_mb}m",
            "memswap_limit": f"{self.memory_mb * 2}m",  # 启用 swap (1:1 ratio)
            "pids_limit": self.pids_limit,
            "storage_opt": {"size": f"{self.disk_mb}M"}
        }
    
    def to_container_create_kwargs(self) -> Dict[str, Any]:
        """
        转换为容器创建参数
        
        将资源限制转换为 DockerClient.create_container 方法可接受的参数。
        使用 cpu_period 和 cpu_quota 来实现 CPU 限制。
        
        Returns:
            容器创建参数字典
        """
        # CPU 限制使用 cpu_period 和 cpu_quota
        # cpu_period 默认 100000 微秒（100ms）
        # cpu_quota = cpu_count * cpu_period
        cpu_period = 100000
        cpu_quota = int(self.cpu_count * cpu_period)
        
        return {
            "cpu_period": cpu_period,
            "cpu_quota": cpu_quota,
            "mem_limit": f"{self.memory_mb}m",
            "memswap_limit": f"{self.memory_mb * 2}m",  # 启用 swap (1:1 ratio)
            "pids_limit": self.pids_limit,
            # 注意: storage_opt 需要特定的存储驱动支持（如 overlay2 with xfs）
            # 在不支持的环境中可能需要忽略此选项
        }
    
    def __str__(self) -> str:
        """返回资源限制的字符串表示"""
        return (
            f"ResourceLimits(cpu={self.cpu_count}, "
            f"memory={self.memory_mb}MB, "
            f"disk={self.disk_mb}MB, "
            f"pids={self.pids_limit})"
        )


@dataclass
class ResourceUsage:
    """
    资源使用情况数据类
    
    记录容器当前的资源使用情况。
    
    Requirements: 2.6 (采集 CPU、内存、磁盘使用指标)
    
    Attributes:
        cpu_percent: CPU 使用率（百分比）
        memory_used_mb: 已用内存（MB）
        memory_limit_mb: 内存限制（MB）
        disk_used_mb: 已用磁盘（MB）
        disk_limit_mb: 磁盘限制（MB）
    """
    cpu_percent: float         # CPU 使用率
    memory_used_mb: float      # 已用内存（MB）
    memory_limit_mb: int       # 内存限制（MB）
    disk_used_mb: float        # 已用磁盘（MB）
    disk_limit_mb: int         # 磁盘限制（MB）
    
    @property
    def memory_percent(self) -> float:
        """计算内存使用率（百分比）"""
        if self.memory_limit_mb <= 0:
            return 0.0
        return round((self.memory_used_mb / self.memory_limit_mb) * 100, 2)
    
    @property
    def disk_percent(self) -> float:
        """计算磁盘使用率（百分比）"""
        if self.disk_limit_mb <= 0:
            return 0.0
        return round((self.disk_used_mb / self.disk_limit_mb) * 100, 2)
    
    def __str__(self) -> str:
        """返回资源使用情况的字符串表示"""
        return (
            f"ResourceUsage(cpu={self.cpu_percent}%, "
            f"memory={self.memory_used_mb}/{self.memory_limit_mb}MB ({self.memory_percent}%), "
            f"disk={self.disk_used_mb}/{self.disk_limit_mb}MB ({self.disk_percent}%))"
        )


class ResourceLimiter:
    """
    资源限制器
    
    管理容器的资源限制和使用监控。
    提供资源限制配置的获取、验证和使用情况查询功能。
    
    """
    
    def __init__(
        self,
        default_cpu: float = 1.0,
        default_memory_mb: int = 512,
        default_disk_mb: int = 1024,
        default_pids: int = 100,
        max_cpu: float = MAX_CPU,
        max_memory_mb: int = MAX_MEMORY_MB,
        max_disk_mb: int = MAX_DISK_MB,
        max_pids: int = MAX_PIDS,
        docker_client: Optional[DockerClient] = None
    ):
        """
        初始化资源限制器
        
        Args:
            default_cpu: 默认 CPU 核心数（默认 1.0）
            default_memory_mb: 默认内存限制（MB，默认 512）
            default_disk_mb: 默认磁盘限制（MB，默认 1024）
            default_pids: 默认进程数限制（默认 100）
            max_cpu: 可分配的 CPU 软上限（默认取绝对护栏 MAX_CPU）
            max_memory_mb: 可分配的内存软上限（MB）
            max_disk_mb: 可分配的磁盘软上限（MB）
            max_pids: 可分配的进程数软上限
            docker_client: Docker 客户端实例（可选，用于查询资源使用）
        """
        # 先确定「软上限」：用户/配置给的上限不得超出绝对护栏 [MIN, MAX]
        self._max_cpu = _clamp_to_guardrail(max_cpu, MIN_CPU, MAX_CPU)
        self._max_memory_mb = int(_clamp_to_guardrail(max_memory_mb, MIN_MEMORY_MB, MAX_MEMORY_MB))
        self._max_disk_mb = int(_clamp_to_guardrail(max_disk_mb, MIN_DISK_MB, MAX_DISK_MB))
        self._max_pids = int(_clamp_to_guardrail(max_pids, MIN_PIDS, MAX_PIDS))

        # 再把默认值收敛到 [绝对下限, 软上限]（clamp 依赖上面的软上限）
        self._default_cpu = self._clamp_cpu(default_cpu)
        self._default_memory_mb = self._clamp_memory(default_memory_mb)
        self._default_disk_mb = self._clamp_disk(default_disk_mb)
        self._default_pids = self._clamp_pids(default_pids)
        self._docker_client = docker_client
        
        logger.info(
            f"资源限制器初始化完成，默认配置: "
            f"CPU={self._default_cpu}, "
            f"内存={self._default_memory_mb}MB, "
            f"磁盘={self._default_disk_mb}MB, "
            f"进程数={self._default_pids}；"
            f"软上限: CPU={self._max_cpu}, 内存={self._max_memory_mb}MB, "
            f"磁盘={self._max_disk_mb}MB, 进程数={self._max_pids}"
        )
    
    @classmethod
    def from_settings(cls, docker_client: Optional[DockerClient] = None) -> "ResourceLimiter":
        """
        从 settings 配置创建资源限制器
        
        Args:
            docker_client: Docker 客户端实例（可选）
            
        Returns:
            ResourceLimiter 实例
        """
        return cls(
            default_cpu=settings.resource.default_cpu,
            default_memory_mb=settings.resource.default_memory_mb,
            default_disk_mb=settings.resource.default_disk_mb,
            default_pids=settings.resource.default_pids,
            max_cpu=settings.resource.max_cpu,
            max_memory_mb=settings.resource.max_memory_mb,
            max_disk_mb=settings.resource.max_disk_mb,
            # config 暂无 max_pids 字段，进程数沿用绝对护栏作为软上限
            max_pids=MAX_PIDS,
            docker_client=docker_client
        )
    
    @property
    def default_cpu(self) -> float:
        """获取默认 CPU 限制"""
        return self._default_cpu
    
    @property
    def default_memory_mb(self) -> int:
        """获取默认内存限制（MB）"""
        return self._default_memory_mb
    
    @property
    def default_disk_mb(self) -> int:
        """获取默认磁盘限制（MB）"""
        return self._default_disk_mb
    
    @property
    def default_pids(self) -> int:
        """获取默认进程数限制"""
        return self._default_pids

    @property
    def max_cpu(self) -> float:
        """获取可分配的 CPU 软上限"""
        return self._max_cpu

    @property
    def max_memory_mb(self) -> int:
        """获取可分配的内存软上限（MB）"""
        return self._max_memory_mb

    @property
    def max_disk_mb(self) -> int:
        """获取可分配的磁盘软上限（MB）"""
        return self._max_disk_mb

    @property
    def max_pids(self) -> int:
        """获取可分配的进程数软上限"""
        return self._max_pids

    def get_effective_config(self) -> Dict[str, Any]:
        """
        返回当前生效的资源配置（默认值 + 软上限）。

        供资源配置管理 API 查询展示。
        """
        return {
            "default_cpu": self._default_cpu,
            "default_memory_mb": self._default_memory_mb,
            "default_disk_mb": self._default_disk_mb,
            "default_pids": self._default_pids,
            "max_cpu": self._max_cpu,
            "max_memory_mb": self._max_memory_mb,
            "max_disk_mb": self._max_disk_mb,
            "max_pids": self._max_pids,
        }

    def apply_config(
        self,
        *,
        default_cpu: Optional[float] = None,
        default_memory_mb: Optional[int] = None,
        default_disk_mb: Optional[int] = None,
        default_pids: Optional[int] = None,
        max_cpu: Optional[float] = None,
        max_memory_mb: Optional[int] = None,
        max_disk_mb: Optional[int] = None,
        max_pids: Optional[int] = None,
    ) -> None:
        """
        运行时热更新资源配置（默认值与软上限）。

        - 软上限先更新（会收敛到绝对护栏内），再据此收敛默认值；
        - 仅传入的字段会被更新，未传入的保持不变；
        - 调整软上限后，已有默认值若超过新上限会自动下调（clamp）。
        """
        # 1. 先更新软上限（决定后续默认值 clamp 的天花板）
        if max_cpu is not None:
            self._max_cpu = _clamp_to_guardrail(max_cpu, MIN_CPU, MAX_CPU)
        if max_memory_mb is not None:
            self._max_memory_mb = int(_clamp_to_guardrail(max_memory_mb, MIN_MEMORY_MB, MAX_MEMORY_MB))
        if max_disk_mb is not None:
            self._max_disk_mb = int(_clamp_to_guardrail(max_disk_mb, MIN_DISK_MB, MAX_DISK_MB))
        if max_pids is not None:
            self._max_pids = int(_clamp_to_guardrail(max_pids, MIN_PIDS, MAX_PIDS))

        # 2. 更新默认值（仅对传入的字段）
        if default_cpu is not None:
            self._default_cpu = default_cpu
        if default_memory_mb is not None:
            self._default_memory_mb = default_memory_mb
        if default_disk_mb is not None:
            self._default_disk_mb = default_disk_mb
        if default_pids is not None:
            self._default_pids = default_pids

        # 3. 统一把默认值收敛到 [绝对下限, 当前软上限]（覆盖上限被调小的情况）
        self._default_cpu = self._clamp_cpu(self._default_cpu)
        self._default_memory_mb = self._clamp_memory(self._default_memory_mb)
        self._default_disk_mb = self._clamp_disk(self._default_disk_mb)
        self._default_pids = self._clamp_pids(self._default_pids)

        logger.info(f"资源限制器配置已热更新: {self.get_effective_config()}")

    def reload_from_settings(self) -> None:
        """
        从当前 settings.resource 重新加载默认值与软上限。

        用于运行时配置变更（PUT /admin/resource-config 写入 settings 后）把变更
        同步到本限制器实例。config 暂无 max_pids，进程数软上限保持不变。
        """
        self.apply_config(
            default_cpu=settings.resource.default_cpu,
            default_memory_mb=settings.resource.default_memory_mb,
            default_disk_mb=settings.resource.default_disk_mb,
            default_pids=settings.resource.default_pids,
            max_cpu=settings.resource.max_cpu,
            max_memory_mb=settings.resource.max_memory_mb,
            max_disk_mb=settings.resource.max_disk_mb,
        )

    def get_limits(
        self,
        cpu: Optional[float] = None,
        memory_mb: Optional[int] = None,
        disk_mb: Optional[int] = None,
        pids: Optional[int] = None
    ) -> ResourceLimits:
        """
        获取资源限制配置
        
        根据提供的参数生成资源限制配置。
        如果参数为 None，则使用默认值。
        如果参数超出允许范围，则使用边界值并记录警告。
        
        Args:
            cpu: CPU 核心数（可选）
            memory_mb: 内存限制（MB，可选）
            disk_mb: 磁盘限制（MB，可选）
            pids: 进程数限制（可选）
            
        Returns:
            ResourceLimits 对象
        """
        # 使用默认值或验证后的值
        final_cpu = self._default_cpu if cpu is None else self._clamp_cpu(cpu)
        final_memory = self._default_memory_mb if memory_mb is None else self._clamp_memory(memory_mb)
        final_disk = self._default_disk_mb if disk_mb is None else self._clamp_disk(disk_mb)
        final_pids = self._default_pids if pids is None else self._clamp_pids(pids)
        
        limits = ResourceLimits(
            cpu_count=final_cpu,
            memory_mb=final_memory,
            disk_mb=final_disk,
            pids_limit=final_pids
        )
        
        logger.debug(f"生成资源限制配置: {limits}")
        return limits
    
    async def get_usage(self, container_id: str) -> ResourceUsage:
        """
        获取容器资源使用情况
        
        查询指定容器的当前资源使用情况，包括 CPU、内存和磁盘。
        
        Requirements: 2.6 (采集 CPU、内存、磁盘使用指标)
        
        Args:
            container_id: 容器 ID
            
        Returns:
            ResourceUsage 对象
            
        Raises:
            RuntimeError: Docker 客户端未初始化
            SandboxNotFoundError: 容器不存在
        """
        if self._docker_client is None:
            raise RuntimeError("Docker 客户端未初始化，无法查询资源使用情况")
        
        # 获取容器统计信息
        stats = await self._docker_client.get_container_stats(container_id)
        
        # 转换内存单位（字节 -> MB）
        memory_used_mb = stats.memory_used_bytes / (1024 * 1024)
        memory_limit_mb = stats.memory_limit_bytes / (1024 * 1024)
        
        # 获取磁盘使用情况
        # 注意: Docker stats API 不直接提供磁盘使用量
        # 需要通过 exec 命令查询或使用其他方式
        disk_used_mb, disk_limit_mb = await self._get_disk_usage(container_id)
        
        usage = ResourceUsage(
            cpu_percent=stats.cpu_percent,
            memory_used_mb=round(memory_used_mb, 2),
            memory_limit_mb=int(memory_limit_mb),
            disk_used_mb=round(disk_used_mb, 2),
            disk_limit_mb=disk_limit_mb
        )
        
        logger.debug(f"容器 {container_id[:12]} 资源使用: {usage}")
        return usage
    
    async def _get_disk_usage(self, container_id: str) -> tuple[float, int]:
        """
        获取容器磁盘使用情况
        
        通过在容器内执行 df 命令获取磁盘使用量。
        
        Args:
            container_id: 容器 ID
            
        Returns:
            (已用磁盘 MB, 磁盘限制 MB) 元组
        """
        if self._docker_client is None:
            return 0.0, 0
        
        try:
            # 执行 df 命令获取根文件系统使用情况
            result = await self._docker_client.exec_command(
                container_id,
                "df -m / | tail -1 | awk '{print $3, $2}'"
            )
            
            if result.exit_code == 0 and result.stdout.strip():
                parts = result.stdout.strip().split()
                if len(parts) >= 2:
                    disk_used_mb = float(parts[0])
                    disk_limit_mb = int(parts[1])
                    return disk_used_mb, disk_limit_mb
        except Exception as e:
            logger.warning(f"获取磁盘使用情况失败: {e}")
        
        return 0.0, 0
    
    def validate_limits(self, limits: ResourceLimits) -> bool:
        """
        验证资源限制是否在允许范围内
        
        检查资源限制配置是否符合系统定义的边界。
        
        Args:
            limits: 要验证的资源限制配置
            
        Returns:
            True 如果所有限制都在允许范围内，否则 False
        """
        is_valid = True
        
        # 验证 CPU
        if not (MIN_CPU <= limits.cpu_count <= self._max_cpu):
            logger.warning(
                f"CPU 限制 {limits.cpu_count} 超出范围 [{MIN_CPU}, {self._max_cpu}]"
            )
            is_valid = False
        
        # 验证内存
        if not (MIN_MEMORY_MB <= limits.memory_mb <= self._max_memory_mb):
            logger.warning(
                f"内存限制 {limits.memory_mb}MB 超出范围 [{MIN_MEMORY_MB}, {self._max_memory_mb}]"
            )
            is_valid = False
        
        # 验证磁盘
        if not (MIN_DISK_MB <= limits.disk_mb <= self._max_disk_mb):
            logger.warning(
                f"磁盘限制 {limits.disk_mb}MB 超出范围 [{MIN_DISK_MB}, {self._max_disk_mb}]"
            )
            is_valid = False
        
        # 验证进程数
        if not (MIN_PIDS <= limits.pids_limit <= self._max_pids):
            logger.warning(
                f"进程数限制 {limits.pids_limit} 超出范围 [{MIN_PIDS}, {self._max_pids}]"
            )
            is_valid = False
        
        return is_valid
    
    def validate_and_raise(self, limits: ResourceLimits) -> None:
        """
        验证资源限制，如果无效则抛出异常
        
        Args:
            limits: 要验证的资源限制配置
            
        Raises:
            ResourceLimitExceededError: 资源限制超出允许范围
        """
        # 验证 CPU
        if limits.cpu_count < MIN_CPU:
            raise ResourceLimitExceededError(
                resource_type="cpu",
                requested=limits.cpu_count,
                limit=MIN_CPU,
                suggestion=f"CPU 核心数不能小于 {MIN_CPU}"
            )
        if limits.cpu_count > self._max_cpu:
            raise ResourceLimitExceededError(
                resource_type="cpu",
                requested=limits.cpu_count,
                limit=self._max_cpu,
                suggestion=f"CPU 核心数不能大于 {self._max_cpu}"
            )
        
        # 验证内存
        if limits.memory_mb < MIN_MEMORY_MB:
            raise ResourceLimitExceededError(
                resource_type="memory",
                requested=f"{limits.memory_mb}MB",
                limit=f"{MIN_MEMORY_MB}MB",
                suggestion=f"内存限制不能小于 {MIN_MEMORY_MB}MB"
            )
        if limits.memory_mb > self._max_memory_mb:
            raise ResourceLimitExceededError(
                resource_type="memory",
                requested=f"{limits.memory_mb}MB",
                limit=f"{self._max_memory_mb}MB",
                suggestion=f"内存限制不能大于 {self._max_memory_mb}MB"
            )
        
        # 验证磁盘
        if limits.disk_mb < MIN_DISK_MB:
            raise ResourceLimitExceededError(
                resource_type="disk",
                requested=f"{limits.disk_mb}MB",
                limit=f"{MIN_DISK_MB}MB",
                suggestion=f"磁盘限制不能小于 {MIN_DISK_MB}MB"
            )
        if limits.disk_mb > self._max_disk_mb:
            raise ResourceLimitExceededError(
                resource_type="disk",
                requested=f"{limits.disk_mb}MB",
                limit=f"{self._max_disk_mb}MB",
                suggestion=f"磁盘限制不能大于 {self._max_disk_mb}MB"
            )
        
        # 验证进程数
        if limits.pids_limit < MIN_PIDS:
            raise ResourceLimitExceededError(
                resource_type="pids",
                requested=limits.pids_limit,
                limit=MIN_PIDS,
                suggestion=f"进程数限制不能小于 {MIN_PIDS}"
            )
        if limits.pids_limit > self._max_pids:
            raise ResourceLimitExceededError(
                resource_type="pids",
                requested=limits.pids_limit,
                limit=self._max_pids,
                suggestion=f"进程数限制不能大于 {self._max_pids}"
            )
    
    def _clamp_cpu(self, cpu: float) -> float:
        """
        将 CPU 值限制在允许范围内
        
        Args:
            cpu: 原始 CPU 值
            
        Returns:
            限制后的 CPU 值
        """
        if cpu < MIN_CPU:
            logger.warning(
                f"CPU 限制 {cpu} 小于最小值 {MIN_CPU}，使用最小值"
            )
            return MIN_CPU
        if cpu > self._max_cpu:
            logger.warning(
                f"CPU 限制 {cpu} 大于软上限 {self._max_cpu}，收敛到软上限"
            )
            return self._max_cpu
        return cpu
    
    def _clamp_memory(self, memory_mb: int) -> int:
        """
        将内存值限制在允许范围内
        
        Args:
            memory_mb: 原始内存值（MB）
            
        Returns:
            限制后的内存值（MB）
        """
        if memory_mb < MIN_MEMORY_MB:
            logger.warning(
                f"内存限制 {memory_mb}MB 小于最小值 {MIN_MEMORY_MB}MB，使用最小值"
            )
            return MIN_MEMORY_MB
        if memory_mb > self._max_memory_mb:
            logger.warning(
                f"内存限制 {memory_mb}MB 大于软上限 {self._max_memory_mb}MB，收敛到软上限"
            )
            return self._max_memory_mb
        return memory_mb
    
    def _clamp_disk(self, disk_mb: int) -> int:
        """
        将磁盘值限制在允许范围内
        
        Args:
            disk_mb: 原始磁盘值（MB）
            
        Returns:
            限制后的磁盘值（MB）
        """
        if disk_mb < MIN_DISK_MB:
            logger.warning(
                f"磁盘限制 {disk_mb}MB 小于最小值 {MIN_DISK_MB}MB，使用最小值"
            )
            return MIN_DISK_MB
        if disk_mb > self._max_disk_mb:
            logger.warning(
                f"磁盘限制 {disk_mb}MB 大于软上限 {self._max_disk_mb}MB，收敛到软上限"
            )
            return self._max_disk_mb
        return disk_mb
    
    def _clamp_pids(self, pids: int) -> int:
        """
        将进程数限制在允许范围内
        
        Args:
            pids: 原始进程数
            
        Returns:
            限制后的进程数
        """
        if pids < MIN_PIDS:
            logger.warning(
                f"进程数限制 {pids} 小于最小值 {MIN_PIDS}，使用最小值"
            )
            return MIN_PIDS
        if pids > self._max_pids:
            logger.warning(
                f"进程数限制 {pids} 大于软上限 {self._max_pids}，收敛到软上限"
            )
            return self._max_pids
        return pids
