"""
健康监控器模块

监控沙箱服务和沙箱容器的健康状态，提供指标采集和 Prometheus 格式导出。
支持服务级别健康检查、沙箱级别资源监控和异常告警。

Requirements:
- 9.1: 提供服务级别的健康检查端点（/health）
- 9.2: 报告当前活跃沙箱数量、容器池状态和系统资源使用
- 9.3: 沙箱容器异常退出时记录异常信息并触发告警
- 9.4: 提供沙箱级别的资源使用指标查询接口
- 9.5: 支持 Prometheus 格式的指标导出（/metrics）
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

import psutil

from ..config import settings

if TYPE_CHECKING:
    from .sandbox_manager import SandboxManager
    from ..infrastructure.container_pool import ContainerPool
    from ..infrastructure.docker_client import DockerClient

logger = logging.getLogger(__name__)


@dataclass
class ServiceHealth:
    """
    服务健康状态数据类
    
    包含服务级别的健康信息，用于 /health 端点响应。
    
    Requirements: 9.1, 9.2
    
    Attributes:
        status: 健康状态（healthy/unhealthy/degraded）
        active_sandboxes: 活跃沙箱数量
        pool_available: 可用预热容器数
        pool_total: 总预热容器数
        cpu_usage_percent: 系统 CPU 使用率
        memory_usage_percent: 系统内存使用率
        uptime_seconds: 服务运行时间（秒）
        last_check: 最后检查时间
    """
    status: str                        # healthy/unhealthy/degraded
    active_sandboxes: int              # 活跃沙箱数
    pool_available: int                # 可用预热容器数
    pool_total: int                    # 总预热容器数
    cpu_usage_percent: float           # 系统 CPU 使用率
    memory_usage_percent: float        # 系统内存使用率
    uptime_seconds: int                # 服务运行时间
    last_check: datetime               # 最后检查时间
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典格式
        
        Returns:
            包含服务健康信息的字典
        """
        return {
            "status": self.status,
            "active_sandboxes": self.active_sandboxes,
            "pool_available": self.pool_available,
            "pool_total": self.pool_total,
            "cpu_usage_percent": round(self.cpu_usage_percent, 2),
            "memory_usage_percent": round(self.memory_usage_percent, 2),
            "uptime_seconds": self.uptime_seconds,
            "last_check": self.last_check.isoformat(),
            "details": self.details,
        }


@dataclass
class SandboxMetrics:
    """
    沙箱指标数据类
    
    包含单个沙箱的资源使用指标。
    
    Requirements: 9.4
    
    Attributes:
        sandbox_id: 沙箱 ID
        cpu_percent: CPU 使用率（百分比）
        memory_used_mb: 已用内存（MB）
        memory_limit_mb: 内存限制（MB）
        disk_used_mb: 已用磁盘（MB）
        disk_limit_mb: 磁盘限制（MB）
        network_rx_bytes: 网络接收字节数
        network_tx_bytes: 网络发送字节数
        timestamp: 采集时间
    """
    sandbox_id: str
    cpu_percent: float
    memory_used_mb: float
    memory_limit_mb: int
    disk_used_mb: float
    disk_limit_mb: int
    network_rx_bytes: int
    network_tx_bytes: int
    timestamp: datetime
    
    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典格式
        
        Returns:
            包含沙箱指标的字典
        """
        return {
            "sandbox_id": self.sandbox_id,
            "cpu_percent": round(self.cpu_percent, 2),
            "memory_used_mb": round(self.memory_used_mb, 2),
            "memory_limit_mb": self.memory_limit_mb,
            "disk_used_mb": round(self.disk_used_mb, 2),
            "disk_limit_mb": self.disk_limit_mb,
            "network_rx_bytes": self.network_rx_bytes,
            "network_tx_bytes": self.network_tx_bytes,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class ContainerExitEvent:
    """
    容器异常退出事件数据类
    
    记录沙箱容器异常退出的信息，用于告警。
    
    Requirements: 9.3
    
    Attributes:
        sandbox_id: 沙箱 ID
        container_id: 容器 ID
        exit_code: 退出码
        exit_reason: 退出原因
        timestamp: 事件时间
    """
    sandbox_id: str
    container_id: str
    exit_code: int
    exit_reason: str
    timestamp: datetime
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "sandbox_id": self.sandbox_id,
            "container_id": self.container_id,
            "exit_code": self.exit_code,
            "exit_reason": self.exit_reason,
            "timestamp": self.timestamp.isoformat(),
        }


# 告警回调类型
AlertCallback = Callable[[ContainerExitEvent], None]


class HealthMonitor:
    """
    健康监控器
    
    监控服务和沙箱的健康状态，提供指标导出。
    支持后台监控任务、异常告警和 Prometheus 格式指标导出。
    
    Requirements:
    - 9.1: 提供服务级别的健康检查端点
    - 9.2: 报告活跃沙箱数量、容器池状态和系统资源使用
    - 9.3: 沙箱容器异常退出时记录异常信息并触发告警
    - 9.4: 提供沙箱级别的资源使用指标查询接口
    - 9.5: 支持 Prometheus 格式的指标导出
    
    使用方式:
    ```python
    monitor = HealthMonitor(
        sandbox_manager=sandbox_manager,
        container_pool=container_pool,
        docker_client=docker_client
    )
    await monitor.start_background_monitoring()
    
    # 获取服务健康状态
    health = await monitor.get_service_health()
    
    # 获取沙箱指标
    metrics = await monitor.get_sandbox_metrics(sandbox_id)
    
    # 导出 Prometheus 指标
    prometheus_output = monitor.export_prometheus_metrics()
    
    # 停止监控
    await monitor.stop_background_monitoring()
    ```
    """
    
    def __init__(
        self,
        sandbox_manager: Optional["SandboxManager"] = None,
        container_pool: Optional["ContainerPool"] = None,
        docker_client: Optional["DockerClient"] = None,
        check_interval_seconds: int = 10
    ):
        """
        初始化健康监控器
        
        Args:
            sandbox_manager: 沙箱管理器实例（可选）
            container_pool: 容器池实例（可选）
            docker_client: Docker 客户端实例（可选）
            check_interval_seconds: 检查间隔（秒），默认 10 秒
        """
        self._sandbox_manager = sandbox_manager
        self._container_pool = container_pool
        self._docker_client = docker_client
        self._check_interval = check_interval_seconds
        
        # 服务启动时间
        self._start_time = time.time()
        
        # 缓存的指标数据
        self._cached_metrics: Dict[str, SandboxMetrics] = {}
        self._last_metrics_update: Optional[datetime] = None
        
        # 缓存的服务健康状态
        self._cached_health: Optional[ServiceHealth] = None

        # 执行指标缓存
        self._execution_histogram_buckets = [0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]
        self._execution_histogram_counts = [0 for _ in self._execution_histogram_buckets]
        self._execution_count = 0
        self._execution_sum_seconds = 0.0
        self._execution_status_counts: Dict[str, int] = {"success": 0, "failure": 0}
        
        # 异常退出事件记录
        self._exit_events: List[ContainerExitEvent] = []
        self._max_exit_events = 100  # 最多保留 100 条记录
        
        # 告警回调列表
        self._alert_callbacks: List[AlertCallback] = []
        
        # 后台任务
        self._monitoring_task: Optional[asyncio.Task] = None
        self._running = False
        
        # 同步锁
        self._lock = asyncio.Lock()
        
        logger.info(
            f"健康监控器初始化，检查间隔: {self._check_interval}s"
        )

    def set_sandbox_manager(self, sandbox_manager: "SandboxManager") -> None:
        """
        设置沙箱管理器
        
        Args:
            sandbox_manager: 沙箱管理器实例
        """
        self._sandbox_manager = sandbox_manager
    
    def set_container_pool(self, container_pool: "ContainerPool") -> None:
        """
        设置容器池
        
        Args:
            container_pool: 容器池实例
        """
        self._container_pool = container_pool
    
    def set_docker_client(self, docker_client: "DockerClient") -> None:
        """
        设置 Docker 客户端
        
        Args:
            docker_client: Docker 客户端实例
        """
        self._docker_client = docker_client
    
    def register_alert_callback(self, callback: AlertCallback) -> None:
        """
        注册告警回调
        
        当沙箱容器异常退出时，会调用注册的回调函数。
        
        Requirements: 9.3
        
        Args:
            callback: 告警回调函数
        """
        self._alert_callbacks.append(callback)
        logger.debug(f"注册告警回调，当前回调数: {len(self._alert_callbacks)}")

    def record_execution(self, *, duration_seconds: float, success: bool) -> None:
        duration = max(0.0, float(duration_seconds))
        self._execution_count += 1
        self._execution_sum_seconds += duration
        status = "success" if success else "failure"
        self._execution_status_counts[status] = self._execution_status_counts.get(status, 0) + 1
        for index, bucket in enumerate(self._execution_histogram_buckets):
            if duration <= bucket:
                self._execution_histogram_counts[index] += 1
                break
    
    def unregister_alert_callback(self, callback: AlertCallback) -> bool:
        """
        取消注册告警回调
        
        Args:
            callback: 要取消的回调函数
            
        Returns:
            True 如果成功取消，False 如果回调不存在
        """
        if callback in self._alert_callbacks:
            self._alert_callbacks.remove(callback)
            return True
        return False
    
    @property
    def uptime_seconds(self) -> int:
        """获取服务运行时间（秒）"""
        return int(time.time() - self._start_time)
    
    @property
    def is_running(self) -> bool:
        """检查监控器是否正在运行"""
        return self._running

    async def get_service_health(self) -> ServiceHealth:
        """
        获取服务健康状态
        
        收集服务级别的健康信息，包括活跃沙箱数、容器池状态和系统资源使用。
        
        Requirements: 9.1, 9.2
        
        Returns:
            ServiceHealth 对象
        """
        now = datetime.now()
        
        # 获取活跃沙箱数量
        active_sandboxes = 0
        if self._sandbox_manager:
            active_sandboxes = self._sandbox_manager.active_count
        
        # 获取容器池状态
        pool_available = 0
        pool_total = 0
        pool_running = False
        pool_size = 0
        if self._container_pool:
            pool_available = self._container_pool.available_count
            pool_total = self._container_pool.total_count
            pool_running = self._container_pool.is_running
            pool_size = self._container_pool.pool_size
        
        # 获取系统资源使用
        cpu_usage = psutil.cpu_percent(interval=0.1)
        memory = psutil.virtual_memory()
        memory_usage = memory.percent
        
        docker_ok = self._check_docker_connection()
        
        details = {
            "docker": {
                "configured": self._docker_client is not None,
                "ok": docker_ok,
            },
            "container_pool": {
                "configured": self._container_pool is not None,
                "running": pool_running,
                "pool_size": pool_size,
                "available": pool_available,
                "total": pool_total,
            },
            "sandbox_manager": {
                "configured": self._sandbox_manager is not None,
            },
            "monitor": {
                "running": self._running,
                "last_metrics_update": (
                    self._last_metrics_update.isoformat()
                    if self._last_metrics_update else None
                ),
            },
            "local_fallback_allowed": settings.allow_local_fallback,
        }
        
        # 确定健康状态
        status = self._determine_health_status(
            pool_available=pool_available,
            pool_total=pool_total,
            pool_size=pool_size,
            pool_running=pool_running,
            docker_ok=docker_ok,
            cpu_usage=cpu_usage,
            memory_usage=memory_usage
        )
        
        health = ServiceHealth(
            status=status,
            active_sandboxes=active_sandboxes,
            pool_available=pool_available,
            pool_total=pool_total,
            cpu_usage_percent=cpu_usage,
            memory_usage_percent=memory_usage,
            uptime_seconds=self.uptime_seconds,
            last_check=now,
            details=details
        )
        
        # 缓存健康状态
        self._cached_health = health
        
        return health
    
    def _determine_health_status(
        self,
        pool_available: int,
        pool_total: int,
        pool_size: int,
        pool_running: bool,
        docker_ok: bool,
        cpu_usage: float,
        memory_usage: float
    ) -> str:
        """
        确定服务健康状态
        
        根据各项指标判断服务是否健康。
        
        Args:
            active_sandboxes: 活跃沙箱数
            pool_available: 可用预热容器数
            cpu_usage: CPU 使用率
            memory_usage: 内存使用率
            
        Returns:
            健康状态字符串（healthy/degraded/unhealthy）
        """
        if not docker_ok:
            return "unhealthy"
        
        # 检查资源使用是否过高
        if cpu_usage > 95 or memory_usage > 95:
            return "unhealthy"
        
        # 检查是否处于降级状态
        if cpu_usage > 80 or memory_usage > 80:
            return "degraded"
        
        # 检查容器池是否耗尽
        if self._container_pool and pool_size > 0:
            if not pool_running:
                return "unhealthy"
            if pool_total == 0:
                return "unhealthy"
            if pool_available == 0:
                return "degraded"
        
        return "healthy"

    def _check_docker_connection(self) -> bool:
        if not self._docker_client:
            return False
        try:
            return self._docker_client.ping()
        except Exception:
            return False

    async def get_sandbox_metrics(self, sandbox_id: str) -> Optional[SandboxMetrics]:
        """
        获取沙箱指标
        
        获取指定沙箱的资源使用指标。
        
        Requirements: 9.4
        
        Args:
            sandbox_id: 沙箱 ID
            
        Returns:
            SandboxMetrics 对象，如果沙箱不存在则返回 None
        """
        if not self._sandbox_manager or not self._docker_client:
            logger.warning("沙箱管理器或 Docker 客户端未设置")
            return None
        
        # 获取沙箱信息
        sandbox_info = await self._sandbox_manager.get_sandbox(sandbox_id)
        if not sandbox_info:
            logger.debug(f"沙箱不存在: {sandbox_id}")
            return None
        
        try:
            # 获取容器统计信息
            stats = await self._docker_client.get_container_stats(
                sandbox_info.container_id
            )
            
            # 计算磁盘使用（从沙箱信息获取限制）
            disk_used_mb = 0.0
            disk_limit_mb = sandbox_info.disk_limit_mb
            
            # 尝试获取磁盘使用量
            # 注意：Docker stats 不直接提供磁盘使用，需要通过其他方式获取
            # 这里使用块设备写入量作为近似值
            disk_used_mb = stats.block_write_bytes / (1024 * 1024)
            
            metrics = SandboxMetrics(
                sandbox_id=sandbox_id,
                cpu_percent=stats.cpu_percent,
                memory_used_mb=stats.memory_used_bytes / (1024 * 1024),
                memory_limit_mb=sandbox_info.memory_limit_mb,
                disk_used_mb=disk_used_mb,
                disk_limit_mb=disk_limit_mb,
                network_rx_bytes=stats.network_rx_bytes,
                network_tx_bytes=stats.network_tx_bytes,
                timestamp=stats.timestamp
            )
            
            # 缓存指标
            async with self._lock:
                self._cached_metrics[sandbox_id] = metrics
            
            return metrics
            
        except Exception as e:
            logger.warning(f"获取沙箱指标失败: {sandbox_id}, 错误: {e}")
            return None
    
    async def get_all_metrics(self) -> List[SandboxMetrics]:
        """
        获取所有沙箱指标
        
        获取所有活跃沙箱的资源使用指标。
        
        Requirements: 9.4
        
        Returns:
            SandboxMetrics 列表
        """
        if not self._sandbox_manager:
            return []
        
        # 获取所有沙箱
        sandboxes = await self._sandbox_manager.list_sandboxes()
        
        # 并发获取所有沙箱的指标
        tasks = [
            self.get_sandbox_metrics(sandbox.sandbox_id)
            for sandbox in sandboxes
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 过滤有效结果
        metrics_list = []
        for result in results:
            if isinstance(result, SandboxMetrics):
                metrics_list.append(result)
            elif isinstance(result, Exception):
                logger.debug(f"获取指标时出错: {result}")
        
        # 更新缓存时间
        self._last_metrics_update = datetime.now()
        
        return metrics_list

    def export_prometheus_metrics(self) -> str:
        """
        导出 Prometheus 格式指标
        
        将服务和沙箱指标导出为 Prometheus 文本格式。
        
        Requirements: 9.5
        
        Returns:
            Prometheus 格式的指标字符串
        """
        lines: List[str] = []
        
        # 添加帮助信息和类型声明
        lines.append("# HELP sandbox_service_up 沙箱服务是否运行")
        lines.append("# TYPE sandbox_service_up gauge")
        lines.append(f"sandbox_service_up 1")
        lines.append("")
        
        # 服务运行时间
        lines.append("# HELP sandbox_service_uptime_seconds 服务运行时间（秒）")
        lines.append("# TYPE sandbox_service_uptime_seconds counter")
        lines.append(f"sandbox_service_uptime_seconds {self.uptime_seconds}")
        lines.append("")
        
        docker_up = 1 if self._check_docker_connection() else 0
        lines.append("# HELP sandbox_docker_up Docker daemon 是否可用")
        lines.append("# TYPE sandbox_docker_up gauge")
        lines.append(f"sandbox_docker_up {docker_up}")
        lines.append("")
        
        status_value = 0
        if self._cached_health:
            status_value = {"healthy": 2, "degraded": 1, "unhealthy": 0}.get(
                self._cached_health.status, 0
            )
        lines.append("# HELP sandbox_health_status 服务健康状态 healthy=2 degraded=1 unhealthy=0")
        lines.append("# TYPE sandbox_health_status gauge")
        lines.append(f"sandbox_health_status {status_value}")
        lines.append("")
        
        # 活跃沙箱数量
        active_sandboxes = 0
        if self._sandbox_manager:
            active_sandboxes = self._sandbox_manager.active_count
        
        lines.append("# HELP sandbox_active_count 当前活跃沙箱数量")
        lines.append("# TYPE sandbox_active_count gauge")
        lines.append(f"sandbox_active_count {active_sandboxes}")
        lines.append("")
        
        # 最大并发沙箱数
        max_concurrent = 50
        if self._sandbox_manager:
            max_concurrent = self._sandbox_manager.max_concurrent
        
        lines.append("# HELP sandbox_max_concurrent 最大并发沙箱数")
        lines.append("# TYPE sandbox_max_concurrent gauge")
        lines.append(f"sandbox_max_concurrent {max_concurrent}")
        lines.append("")
        
        # 容器池指标
        if self._container_pool:
            pool_available = self._container_pool.available_count
            pool_total = self._container_pool.total_count
            pool_size = self._container_pool.pool_size
            
            lines.append("# HELP sandbox_pool_available 可用预热容器数")
            lines.append("# TYPE sandbox_pool_available gauge")
            lines.append(f"sandbox_pool_available {pool_available}")
            lines.append("")
            
            lines.append("# HELP sandbox_pool_total 总预热容器数")
            lines.append("# TYPE sandbox_pool_total gauge")
            lines.append(f"sandbox_pool_total {pool_total}")
            lines.append("")
            
            lines.append("# HELP sandbox_pool_size 配置的容器池大小")
            lines.append("# TYPE sandbox_pool_size gauge")
            lines.append(f"sandbox_pool_size {pool_size}")
            lines.append("")
            
            lines.append("# HELP sandbox_pool_running 容器池后台任务是否运行")
            lines.append("# TYPE sandbox_pool_running gauge")
            lines.append(f"sandbox_pool_running {1 if self._container_pool.is_running else 0}")
            lines.append("")
            
            # 容器池统计
            stats = self._container_pool.statistics
            lines.append("# HELP sandbox_pool_acquired_total 已分配容器总数")
            lines.append("# TYPE sandbox_pool_acquired_total counter")
            lines.append(f"sandbox_pool_acquired_total {stats.get('total_acquired', 0)}")
            lines.append("")
            
            lines.append("# HELP sandbox_pool_created_total 已创建容器总数")
            lines.append("# TYPE sandbox_pool_created_total counter")
            lines.append(f"sandbox_pool_created_total {stats.get('total_created', 0)}")
            lines.append("")

        lines.append("# HELP execution_duration_seconds 代码执行耗时分布（秒）")
        lines.append("# TYPE execution_duration_seconds histogram")
        cumulative = 0
        for bucket, count in zip(self._execution_histogram_buckets, self._execution_histogram_counts):
            cumulative += count
            lines.append(f'execution_duration_seconds_bucket{{le="{bucket}"}} {cumulative}')
        lines.append(f'execution_duration_seconds_bucket{{le="+Inf"}} {self._execution_count}')
        lines.append(f"execution_duration_seconds_sum {self._execution_sum_seconds:.6f}")
        lines.append(f"execution_duration_seconds_count {self._execution_count}")
        lines.append("")

        lines.append("# HELP execution_total 代码执行次数")
        lines.append("# TYPE execution_total counter")
        for status, count in sorted(self._execution_status_counts.items()):
            lines.append(f'execution_total{{status="{status}"}} {count}')
        lines.append("")
        
        # 系统资源指标
        cpu_usage = psutil.cpu_percent(interval=0.1)
        memory = psutil.virtual_memory()
        
        lines.append("# HELP system_cpu_usage_percent 系统 CPU 使用率")
        lines.append("# TYPE system_cpu_usage_percent gauge")
        lines.append(f"system_cpu_usage_percent {cpu_usage:.2f}")
        lines.append("")
        
        lines.append("# HELP system_memory_usage_percent 系统内存使用率")
        lines.append("# TYPE system_memory_usage_percent gauge")
        lines.append(f"system_memory_usage_percent {memory.percent:.2f}")
        lines.append("")
        
        lines.append("# HELP system_memory_total_bytes 系统总内存（字节）")
        lines.append("# TYPE system_memory_total_bytes gauge")
        lines.append(f"system_memory_total_bytes {memory.total}")
        lines.append("")
        
        lines.append("# HELP system_memory_available_bytes 系统可用内存（字节）")
        lines.append("# TYPE system_memory_available_bytes gauge")
        lines.append(f"system_memory_available_bytes {memory.available}")
        lines.append("")
        
        # 沙箱级别指标（从缓存获取）
        if self._cached_metrics:
            lines.append("# HELP sandbox_cpu_percent 沙箱 CPU 使用率")
            lines.append("# TYPE sandbox_cpu_percent gauge")
            for sandbox_id, metrics in self._cached_metrics.items():
                lines.append(
                    f'sandbox_cpu_percent{{sandbox_id="{sandbox_id}"}} '
                    f'{metrics.cpu_percent:.2f}'
                )
            lines.append("")
            
            lines.append("# HELP sandbox_memory_used_bytes 沙箱已用内存（字节）")
            lines.append("# TYPE sandbox_memory_used_bytes gauge")
            for sandbox_id, metrics in self._cached_metrics.items():
                memory_bytes = int(metrics.memory_used_mb * 1024 * 1024)
                lines.append(
                    f'sandbox_memory_used_bytes{{sandbox_id="{sandbox_id}"}} '
                    f'{memory_bytes}'
                )
            lines.append("")

            lines.append("# HELP container_memory_bytes 容器已用内存（字节）")
            lines.append("# TYPE container_memory_bytes gauge")
            for sandbox_id, metrics in self._cached_metrics.items():
                memory_bytes = int(metrics.memory_used_mb * 1024 * 1024)
                lines.append(
                    f'container_memory_bytes{{sandbox_id="{sandbox_id}"}} '
                    f'{memory_bytes}'
                )
            lines.append("")
            
            lines.append("# HELP sandbox_network_rx_bytes 沙箱网络接收字节数")
            lines.append("# TYPE sandbox_network_rx_bytes counter")
            for sandbox_id, metrics in self._cached_metrics.items():
                lines.append(
                    f'sandbox_network_rx_bytes{{sandbox_id="{sandbox_id}"}} '
                    f'{metrics.network_rx_bytes}'
                )
            lines.append("")
            
            lines.append("# HELP sandbox_network_tx_bytes 沙箱网络发送字节数")
            lines.append("# TYPE sandbox_network_tx_bytes counter")
            for sandbox_id, metrics in self._cached_metrics.items():
                lines.append(
                    f'sandbox_network_tx_bytes{{sandbox_id="{sandbox_id}"}} '
                    f'{metrics.network_tx_bytes}'
                )
            lines.append("")
        
        # 异常退出事件计数
        lines.append("# HELP sandbox_exit_events_total 沙箱异常退出事件总数")
        lines.append("# TYPE sandbox_exit_events_total counter")
        lines.append(f"sandbox_exit_events_total {len(self._exit_events)}")
        lines.append("")
        
        return "\n".join(lines)

    async def start_background_monitoring(self) -> None:
        """
        启动后台监控任务
        
        启动定期采集指标和检查容器状态的后台任务。
        
        Requirements: 2.6 (每 10 秒采集一次沙箱的 CPU、内存、磁盘使用指标)
        """
        if self._running:
            logger.warning("后台监控任务已经在运行中")
            return
        
        logger.info(f"启动后台监控任务，间隔: {self._check_interval}s")
        self._running = True
        
        self._monitoring_task = asyncio.create_task(
            self._monitoring_loop(),
            name="health_monitor_loop"
        )
    
    async def stop_background_monitoring(self) -> None:
        """
        停止后台监控任务
        """
        logger.info("停止后台监控任务...")
        self._running = False
        
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
            self._monitoring_task = None
        
        logger.info("后台监控任务已停止")
    
    async def _monitoring_loop(self) -> None:
        """
        监控循环
        
        定期采集指标和检查容器状态。
        """
        logger.info("监控循环已启动")
        
        while self._running:
            try:
                await asyncio.sleep(self._check_interval)
                
                if not self._running:
                    break
                
                # 采集所有沙箱指标
                await self.get_all_metrics()
                
                # 检查容器异常退出
                await self._check_container_exits()
                
                # 更新服务健康状态
                await self.get_service_health()
                
            except asyncio.CancelledError:
                logger.debug("监控循环被取消")
                break
            except Exception as e:
                logger.error(f"监控循环出错: {e}")
                # 继续运行，不因单次错误而停止
                await asyncio.sleep(1)
        
        logger.info("监控循环已停止")

    async def _check_container_exits(self) -> None:
        """
        检查容器异常退出
        
        检测沙箱容器是否异常退出，记录事件并触发告警。
        
        Requirements: 9.3
        """
        if not self._sandbox_manager or not self._docker_client:
            return
        
        # 获取所有沙箱
        sandboxes = await self._sandbox_manager.list_sandboxes()
        
        for sandbox in sandboxes:
            try:
                # 检查容器状态
                container_info = await self._docker_client.get_container_status(
                    sandbox.container_id
                )
                
                # 检查是否异常退出
                from ..infrastructure.docker_client import ContainerState
                if container_info.state in (
                    ContainerState.EXITED,
                    ContainerState.DEAD
                ):
                    # 记录异常退出事件
                    await self._record_exit_event(
                        sandbox_id=sandbox.sandbox_id,
                        container_id=sandbox.container_id,
                        exit_code=-1,  # 实际退出码需要从容器详情获取
                        exit_reason=f"容器状态: {container_info.state.value}"
                    )
                    
            except Exception as e:
                logger.debug(f"检查容器状态时出错: {sandbox.sandbox_id}, {e}")
    
    async def _record_exit_event(
        self,
        sandbox_id: str,
        container_id: str,
        exit_code: int,
        exit_reason: str
    ) -> None:
        """
        记录容器异常退出事件
        
        记录事件并触发告警回调。
        
        Requirements: 9.3
        
        Args:
            sandbox_id: 沙箱 ID
            container_id: 容器 ID
            exit_code: 退出码
            exit_reason: 退出原因
        """
        event = ContainerExitEvent(
            sandbox_id=sandbox_id,
            container_id=container_id,
            exit_code=exit_code,
            exit_reason=exit_reason,
            timestamp=datetime.now()
        )
        
        # 检查是否已记录过该事件（避免重复）
        async with self._lock:
            for existing in self._exit_events:
                if (existing.sandbox_id == sandbox_id and 
                    existing.container_id == container_id):
                    return  # 已记录过
            
            # 添加事件
            self._exit_events.append(event)
            
            # 限制事件数量
            if len(self._exit_events) > self._max_exit_events:
                self._exit_events = self._exit_events[-self._max_exit_events:]
        
        # 记录日志
        logger.warning(
            f"沙箱容器异常退出: sandbox={sandbox_id}, "
            f"container={container_id[:12]}, "
            f"exit_code={exit_code}, reason={exit_reason}"
        )
        
        # 触发告警回调
        for callback in self._alert_callbacks:
            try:
                callback(event)
            except Exception as e:
                logger.error(f"告警回调执行失败: {e}")

    def get_exit_events(
        self,
        limit: int = 50,
        sandbox_id: Optional[str] = None
    ) -> List[ContainerExitEvent]:
        """
        获取异常退出事件列表
        
        Args:
            limit: 返回的最大事件数
            sandbox_id: 按沙箱 ID 过滤（可选）
            
        Returns:
            ContainerExitEvent 列表
        """
        events = self._exit_events.copy()
        
        # 按沙箱 ID 过滤
        if sandbox_id:
            events = [e for e in events if e.sandbox_id == sandbox_id]
        
        # 按时间倒序排列（最新的在前）
        events.sort(key=lambda e: e.timestamp, reverse=True)
        
        # 限制数量
        return events[:limit]
    
    def clear_exit_events(self) -> int:
        """
        清除所有异常退出事件
        
        Returns:
            清除的事件数量
        """
        count = len(self._exit_events)
        self._exit_events.clear()
        logger.info(f"清除 {count} 个异常退出事件")
        return count
    
    def get_cached_health(self) -> Optional[ServiceHealth]:
        """
        获取缓存的服务健康状态
        
        Returns:
            缓存的 ServiceHealth 对象，如果没有缓存则返回 None
        """
        return self._cached_health
    
    def get_cached_metrics(self) -> Dict[str, SandboxMetrics]:
        """
        获取缓存的沙箱指标
        
        Returns:
            沙箱 ID 到 SandboxMetrics 的映射字典
        """
        return self._cached_metrics.copy()
    
    async def cleanup_stale_metrics(self) -> int:
        """
        清理过期的指标缓存
        
        移除已不存在的沙箱的指标缓存。
        
        Returns:
            清理的指标数量
        """
        if not self._sandbox_manager:
            return 0
        
        # 获取当前活跃的沙箱 ID
        sandboxes = await self._sandbox_manager.list_sandboxes()
        active_ids = {s.sandbox_id for s in sandboxes}
        
        # 清理不存在的沙箱的指标
        async with self._lock:
            stale_ids = [
                sid for sid in self._cached_metrics.keys()
                if sid not in active_ids
            ]
            
            for sid in stale_ids:
                del self._cached_metrics[sid]
        
        if stale_ids:
            logger.debug(f"清理 {len(stale_ids)} 个过期指标缓存")
        
        return len(stale_ids)


# 全局健康监控器单例
_health_monitor: Optional[HealthMonitor] = None


def get_health_monitor() -> HealthMonitor:
    """
    获取全局健康监控器单例
    
    Returns:
        HealthMonitor 实例
    """
    global _health_monitor
    if _health_monitor is None:
        _health_monitor = HealthMonitor()
    return _health_monitor


def reset_health_monitor() -> None:
    """
    重置全局健康监控器单例
    
    用于测试场景，重新创建健康监控器实例。
    """
    global _health_monitor
    _health_monitor = None
