"""
容器池管理器模块

管理预热容器池，提供快速的容器分配以加速沙箱创建。
包括预热容器创建和维护、容器获取和释放、健康检查和自动补充等功能。

"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import settings
from ..exceptions import (
    ContainerPoolExhaustedError,
    InternalError,
    SandboxCreationError,
)
from .docker_client import DockerClient, ContainerState

logger = logging.getLogger(__name__)


@dataclass
class PooledContainer:
    """
    池化容器信息数据类
    
    记录预热容器的基本信息和状态。
    
    Attributes:
        container_id: 容器 ID
        created_at: 创建时间
        is_healthy: 健康状态
        last_health_check: 最后健康检查时间
    """
    container_id: str                  # 容器 ID
    created_at: datetime               # 创建时间
    is_healthy: bool = True            # 健康状态
    last_health_check: datetime = field(default_factory=datetime.now)  # 最后健康检查时间
    
    def __str__(self) -> str:
        """返回池化容器的字符串表示"""
        return (
            f"PooledContainer(id={self.container_id[:12]}, "
            f"healthy={self.is_healthy}, "
            f"age={self.age_seconds:.0f}s)"
        )
    
    @property
    def age_seconds(self) -> float:
        """获取容器存活时间（秒）"""
        return (datetime.now() - self.created_at).total_seconds()
    
    @property
    def seconds_since_health_check(self) -> float:
        """获取距离上次健康检查的时间（秒）"""
        return (datetime.now() - self.last_health_check).total_seconds()


class ContainerPool:
    """
    容器池管理器
    
    维护预热容器池，提供快速的容器分配。
    通过预热容器机制减少沙箱创建延迟，提升用户体验。
    
    
    使用方式:
    ```python
    pool = ContainerPool(docker_client=docker_client)
    await pool.initialize()  # 初始化并预热容器
    
    # 获取预热容器
    container = await pool.acquire()
    if container:
        # 使用容器...
        pass
    
    # 停止容器池
    await pool.shutdown()
    ```
    """
    
    # 容器标签，用于标识预热容器
    POOL_LABEL_KEY = "sandbox.pool"
    POOL_LABEL_VALUE = "warm"
    # 实例标签：多实例共享 daemon 时区分归属，启动清理只清本实例
    INSTANCE_LABEL_KEY = "sandbox.instance"
    
    def __init__(
        self,
        docker_client: DockerClient,
        pool_size: int = 3,
        max_container_age_seconds: int = 3600,
        health_check_interval_seconds: int = 60,
        image: Optional[str] = None,
        container_config: Optional[Dict[str, Any]] = None,
        min_pool_size: Optional[int] = None,
        max_pool_size: Optional[int] = None,
        idle_shrink_seconds: int = 600,
    ):
        """
        初始化容器池（支持弹性伸缩）

        Args:
            docker_client: Docker 客户端实例
            pool_size: 常态预热容器数量（弹性伸缩基准）
            max_container_age_seconds: 容器最大存活时间（秒，默认 3600）
            health_check_interval_seconds: 健康检查间隔（秒，默认 60）
            image: Docker 镜像名称（可选，默认使用 settings 配置）
            container_config: 容器创建配置（可选）
            min_pool_size: 空闲期缩容下限（默认 = pool_size，即不缩容）
            max_pool_size: 高峰期扩容上限（默认 = pool_size，即不扩容）
            idle_shrink_seconds: 连续空闲多少秒后缩容一档
        """
        self._docker_client = docker_client
        self._pool_size = pool_size
        self._min_pool_size = min(
            pool_size, min_pool_size if min_pool_size is not None else pool_size
        )
        self._max_pool_size = max(
            pool_size, max_pool_size if max_pool_size is not None else pool_size
        )
        self._idle_shrink_seconds = idle_shrink_seconds
        # 弹性目标值：常态 = pool_size；借空时上调，空闲时下调
        self._target_size = pool_size
        self._last_demand_at = time.monotonic()
        self._max_container_age_seconds = max_container_age_seconds
        self._health_check_interval_seconds = health_check_interval_seconds
        self._image = image or settings.docker_image
        self._container_config = container_config or {}
        
        # 容器池存储
        self._available_containers: List[PooledContainer] = []
        self._all_containers: Dict[str, PooledContainer] = {}
        self._leased_containers: Dict[str, PooledContainer] = {}
        # 共享租约：container_id -> 当前并发 fork 数（容器仍留在 available 列表）
        self._shared_inflight: Dict[str, int] = {}
        
        # 同步锁
        self._lock = asyncio.Lock()
        
        # 后台任务
        self._health_check_task: Optional[asyncio.Task] = None
        self._replenish_task: Optional[asyncio.Task] = None
        self._running = False
        
        # 统计信息
        self._total_acquired = 0
        self._total_created = 0
        self._total_removed = 0
        
        logger.info(
            f"容器池初始化，池大小: {self._pool_size}, "
            f"最大存活时间: {self._max_container_age_seconds}s, "
            f"健康检查间隔: {self._health_check_interval_seconds}s"
        )
    
    @classmethod
    def from_settings(
        cls,
        docker_client: DockerClient,
        container_config: Optional[Dict[str, Any]] = None
    ) -> "ContainerPool":
        """
        从 settings 配置创建容器池
        
        Args:
            docker_client: Docker 客户端实例
            container_config: 容器创建配置（可选）
            
        Returns:
            ContainerPool 实例
        """
        return cls(
            docker_client=docker_client,
            pool_size=settings.pool.pool_size,
            max_container_age_seconds=settings.pool.container_max_age_seconds,
            health_check_interval_seconds=settings.pool.health_check_interval,
            image=settings.docker_image,
            container_config=container_config,
            min_pool_size=settings.pool.min_pool_size,
            max_pool_size=settings.pool.max_pool_size,
            idle_shrink_seconds=settings.pool.idle_shrink_seconds,
        )
    
    @property
    def available_count(self) -> int:
        """
        获取可用容器数量
        
        Returns:
            当前可用的预热容器数量
        """
        return len(self._available_containers)
    
    @property
    def total_count(self) -> int:
        """
        获取总容器数量
        
        Returns:
            容器池管理的总容器数量
        """
        return len(self._all_containers) + len(self._leased_containers)

    @property
    def leased_count(self) -> int:
        """获取当前借出中的容器数量。"""
        return len(self._leased_containers)
    
    @property
    def pool_size(self) -> int:
        """获取配置的池大小"""
        return self._pool_size
    
    @property
    def is_running(self) -> bool:
        """检查容器池是否正在运行"""
        return self._running
    
    @property
    def statistics(self) -> Dict[str, int]:
        """
        获取容器池统计信息
        
        Returns:
            包含统计信息的字典
        """
        return {
            "pool_size": self._pool_size,
            "target_size": self._target_size,
            "min_pool_size": self._min_pool_size,
            "max_pool_size": self._max_pool_size,
            "available_count": self.available_count,
            "total_count": self.total_count,
            "leased_count": self.leased_count,
            "total_acquired": self._total_acquired,
            "total_created": self._total_created,
            "total_removed": self._total_removed,
            "shared_inflight": sum(self._shared_inflight.values()),
        }
    
    async def initialize(self) -> None:
        """
        初始化容器池
        
        创建预热容器并启动后台任务。
        
        Requirements: 7.1 (维护可配置数量的预热容器)
        """
        if self._running:
            logger.warning("容器池已经在运行中")
            return
        
        logger.info(f"初始化容器池，目标大小: {self._pool_size}")
        self._running = True
        
        # 清理可能存在的旧预热容器
        await self._cleanup_stale_containers()
        
        # 创建初始预热容器
        await self._fill_pool()
        
        # 启动后台健康检查任务
        self._health_check_task = asyncio.create_task(
            self._health_check_loop(),
            name="container_pool_health_check"
        )
        
        logger.info(
            f"容器池初始化完成，当前可用: {self.available_count}/{self._pool_size}"
        )
    
    async def shutdown(self) -> None:
        """
        关闭容器池
        
        停止后台任务并清理所有预热容器。
        """
        logger.info("关闭容器池...")
        self._running = False
        
        # 取消后台任务
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
            self._health_check_task = None
        
        if self._replenish_task:
            self._replenish_task.cancel()
            try:
                await self._replenish_task
            except asyncio.CancelledError:
                pass
            self._replenish_task = None
        
        # 清理所有容器
        async with self._lock:
            for container_id in list(self._all_containers.keys()):
                await self._remove_container(container_id)
            for container_id in list(self._leased_containers.keys()):
                await self._remove_container(container_id)
            
            self._available_containers.clear()
            self._all_containers.clear()
            self._leased_containers.clear()
        
        logger.info("容器池已关闭")
    
    async def acquire(self) -> Optional[PooledContainer]:
        """
        获取一个预热容器
        
        从容器池中获取一个可用的预热容器。
        如果池为空，返回 None。
        获取后会异步触发容器补充。
        
        
        Returns:
            PooledContainer 对象，如果池为空则返回 None
        """
        start_time = time.monotonic()
        
        async with self._lock:
            self._last_demand_at = time.monotonic()
            # 查找可用的健康容器
            container = self._get_available_container()
            
            if container:
                # 从可用列表中移除
                self._available_containers.remove(container)
                # 从总容器列表中移除（容器已被分配出去）
                del self._all_containers[container.container_id]
                self._leased_containers[container.container_id] = container
                self._total_acquired += 1
                
                elapsed = time.monotonic() - start_time
                logger.info(
                    f"分配预热容器: {container.container_id[:12]}, "
                    f"耗时: {elapsed*1000:.2f}ms, "
                    f"剩余可用: {self.available_count}"
                )
                
                # 异步触发容器补充
                asyncio.create_task(
                    self._trigger_replenish(),
                    name="container_pool_replenish"
                )
                
                return container
            
            self._scale_up_on_miss()
            logger.warning("容器池为空，无可用预热容器")
            return None
    
    async def acquire_for_execution(self) -> Optional[PooledContainer]:
        """
        获取一个预热容器用于临时执行（执行完毕后应调用 release() 归还）

        与 acquire() 的区别：不触发异步补充，因为容器会被归还。

        Returns:
            PooledContainer 对象，如果池为空则返回 None
        """
        start_time = time.monotonic()

        async with self._lock:
            self._last_demand_at = time.monotonic()
            container = self._get_available_container()

            if container:
                self._available_containers.remove(container)
                del self._all_containers[container.container_id]
                self._leased_containers[container.container_id] = container
                self._total_acquired += 1

                elapsed = time.monotonic() - start_time
                logger.info(
                    f"分配临时执行容器: {container.container_id[:12]}, "
                    f"耗时: {elapsed*1000:.2f}ms, "
                    f"剩余可用: {self.available_count}"
                )

                return container

            self._scale_up_on_miss()
            logger.warning("容器池为空，无可用预热容器（临时执行）")
            return None

    async def acquire_shared(self, max_per_container: int) -> Optional[str]:
        """
        共享租约：返回一个可共享的池容器 ID 并将其并发计数 +1。

        与 acquire/acquire_for_execution（独占借出）不同，容器仍留在池中，
        多个 isolated 执行可在同一容器内并发 fork。
        选择策略：在并发数 < max_per_container 的健康容器中取计数最小者
        （负载均衡）。全部满载或池空时返回 None（调用方回退独占路径）。
        """
        async with self._lock:
            self._last_demand_at = time.monotonic()
            best: Optional[PooledContainer] = None
            best_inflight = max_per_container
            for container in self._available_containers:
                if container.age_seconds > self._max_container_age_seconds:
                    continue
                if not container.is_healthy:
                    continue
                inflight = self._shared_inflight.get(container.container_id, 0)
                if inflight < best_inflight:
                    best = container
                    best_inflight = inflight
            if best is None:
                self._scale_up_on_miss()
                return None
            self._shared_inflight[best.container_id] = best_inflight + 1
            self._total_acquired += 1
            return best.container_id

    async def release_shared(self, container_id: str) -> None:
        """共享租约归还：并发计数 -1，归零即清除标记。"""
        async with self._lock:
            inflight = self._shared_inflight.get(container_id, 0)
            if inflight <= 1:
                self._shared_inflight.pop(container_id, None)
            else:
                self._shared_inflight[container_id] = inflight - 1

    def _shared_inflight_count(self, container_id: str) -> int:
        return self._shared_inflight.get(container_id, 0)

    def _scale_up_on_miss(self) -> None:
        """借空（miss）时把弹性目标上调一档，并异步触发补池。"""
        if self._target_size < self._max_pool_size:
            self._target_size += 1
            logger.info(f"池借空，弹性目标上调至 {self._target_size}")
        asyncio.create_task(
            self._trigger_replenish(), name="container_pool_scale_up"
        )

    async def release(self, container_id: str) -> None:
        """
        释放容器回池
        
        将容器释放回容器池（用于错误恢复场景）。
        如果容器不健康或池已满，则销毁容器。
        
        Args:
            container_id: 容器 ID
        """
        async with self._lock:
            leased = self._leased_containers.pop(container_id, None)
            if leased is None:
                logger.warning(f"容器 {container_id[:12]} 未处于借出状态，跳过回池")
                return

            # 检查容器是否健康
            is_healthy = await self._check_container_health(container_id)
            
            if is_healthy and len(self._available_containers) < self._target_size:
                # 容器健康且池未满，放回池中
                container = PooledContainer(
                    container_id=container_id,
                    created_at=leased.created_at,
                    is_healthy=True,
                    last_health_check=datetime.now()
                )
                self._available_containers.append(container)
                self._all_containers[container_id] = container
                logger.info(f"容器 {container_id[:12]} 已释放回池")
            else:
                # 容器不健康或池已满，销毁容器
                await self._remove_container(container_id)
                logger.info(f"容器 {container_id[:12]} 已销毁（不健康或池已满）")
    
    async def replenish(self) -> None:
        """
        补充预热容器
        
        将容器池补充到配置的大小。
        
        Requirements: 7.3 (异步创建新的预热容器补充池)
        """
        async with self._lock:
            needed = self._target_size - len(self._available_containers)
            
            if needed <= 0:
                logger.debug("容器池已满，无需补充")
                return
            
            logger.info(f"补充预热容器，需要: {needed}")
            
            # 并发创建容器
            tasks = [self._create_warm_container() for _ in range(needed)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 处理结果
            success_count = 0
            for result in results:
                if isinstance(result, PooledContainer):
                    self._available_containers.append(result)
                    self._all_containers[result.container_id] = result
                    success_count += 1
                elif isinstance(result, Exception):
                    logger.error(f"创建预热容器失败: {result}")
            
            logger.info(
                f"容器补充完成，成功: {success_count}/{needed}, "
                f"当前可用: {self.available_count}"
            )
    
    async def health_check(self) -> None:
        """
        执行健康检查
        
        检查所有预热容器的健康状态，移除不健康或过期的容器。
        
        Requirements: 7.4 (定期检查预热容器健康状态并替换不健康的容器)
        """
        async with self._lock:
            unhealthy_containers: List[str] = []
            expired_containers: List[str] = []
            
            for container in list(self._available_containers):
                # 共享租约使用中的容器跳过本轮（等并发归零再处理）
                if self._shared_inflight.get(container.container_id, 0) > 0:
                    continue

                # 检查容器是否过期
                if container.age_seconds > self._max_container_age_seconds:
                    expired_containers.append(container.container_id)
                    continue
                
                # 检查容器健康状态
                is_healthy = await self._check_container_health(container.container_id)
                container.is_healthy = is_healthy
                container.last_health_check = datetime.now()
                
                if not is_healthy:
                    unhealthy_containers.append(container.container_id)
            
            # 移除不健康的容器
            for container_id in unhealthy_containers:
                logger.warning(f"移除不健康的容器: {container_id[:12]}")
                await self._remove_pooled_container(container_id)
            
            # 移除过期的容器
            for container_id in expired_containers:
                logger.info(f"移除过期的容器: {container_id[:12]}")
                await self._remove_pooled_container(container_id)
            
            removed_count = len(unhealthy_containers) + len(expired_containers)
            if removed_count > 0:
                logger.info(
                    f"健康检查完成，移除: {removed_count} 个容器 "
                    f"(不健康: {len(unhealthy_containers)}, 过期: {len(expired_containers)})"
                )
        
        # 前瞻性扩容：高水位时提前补容器，别等池借空才被动扩（缩短长尾冷启动）
        await self._maybe_scale_up_proactive()

        # 空闲缩容：连续无请求超过阈值时把弹性目标下调一档
        await self._maybe_shrink()

        # 触发补充
        if self.available_count < self._target_size:
            await self.replenish()

    async def _maybe_scale_up_proactive(self) -> None:
        """
        前瞻性扩容（production-hardening #8）。

        共享租约模式下，容器容量 = 池容器数 × 每容器并发上限。当在途并发
        （shared_inflight + 独占借出）逼近容量高水位（默认 75%）时，提前把
        弹性目标上调一档并补池——而不是等池彻底借空、超额请求落到临时容器
        冷启动（10~25s 长尾）。空闲时此路径不触发（在途为 0）。
        """
        if self._target_size >= self._max_pool_size:
            return

        max_per = max(1, int(getattr(settings.pool, "shared_max_per_container", 1)))
        async with self._lock:
            pool_containers = len(self._all_containers)
            if pool_containers == 0:
                return
            inflight = sum(self._shared_inflight.values()) + len(self._leased_containers)
            capacity = pool_containers * max_per
            # 高水位阈值 75%：留出补一档容器的提前量
            if capacity > 0 and inflight >= 0.75 * capacity:
                self._target_size += 1
                self._last_demand_at = time.monotonic()
                logger.info(
                    f"池在途 {inflight}/{capacity}（≥75%），前瞻扩容目标上调至 {self._target_size}"
                )
                should_replenish = True
            else:
                should_replenish = False
        if should_replenish:
            await self.replenish()

    async def _maybe_shrink(self) -> None:
        """
        空闲缩容。

        距离最后一次借用请求超过 idle_shrink_seconds 时，
        把弹性目标下调一档（不低于 min_pool_size），并销毁多余容器，
        释放小型服务器上的常驻内存。
        """
        idle_seconds = time.monotonic() - self._last_demand_at
        if idle_seconds < self._idle_shrink_seconds:
            return
        if self._target_size <= self._min_pool_size:
            return

        self._target_size -= 1
        # 重置空闲计时，避免一次健康检查里连续缩到底
        self._last_demand_at = time.monotonic()
        logger.info(
            f"池空闲 {idle_seconds:.0f}s，弹性目标下调至 {self._target_size}"
        )

        async with self._lock:
            removable = [
                c for c in self._available_containers
                if self._shared_inflight.get(c.container_id, 0) == 0
            ]
            while (
                len(self._available_containers) > self._target_size and removable
            ):
                container = removable.pop()
                await self._remove_pooled_container(container.container_id)

    async def _fill_pool(self) -> None:
        """
        填充容器池到目标大小
        
        Requirements: 7.1 (维护可配置数量的预热容器)
        """
        needed = self._target_size - len(self._available_containers)
        
        if needed <= 0:
            return
        
        logger.info(f"填充容器池，需要创建: {needed} 个容器")
        
        # 并发创建容器
        tasks = [self._create_warm_container() for _ in range(needed)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 处理结果
        for result in results:
            if isinstance(result, PooledContainer):
                self._available_containers.append(result)
                self._all_containers[result.container_id] = result
            elif isinstance(result, Exception):
                logger.error(f"创建预热容器失败: {result}")
        
        logger.info(f"容器池填充完成，当前可用: {self.available_count}")
    
    async def _create_warm_container(self) -> PooledContainer:
        """
        创建一个预热容器
        
        Returns:
            PooledContainer 对象
            
        Raises:
            SandboxCreationError: 容器创建失败
        """
        container_id: Optional[str] = None
        try:
            # 构建容器配置（含实例 ID：多实例共享 daemon 时清理只清本实例）
            from ..config import resolve_instance_id

            labels = {
                self.POOL_LABEL_KEY: self.POOL_LABEL_VALUE,
                self.INSTANCE_LABEL_KEY: resolve_instance_id(),
                "sandbox.created_at": datetime.now().isoformat(),
            }
            
            # 合并用户配置
            config = {
                **self._container_config,
                "labels": {
                    **self._container_config.get("labels", {}),
                    **labels
                }
            }
            
            # 创建容器
            container_info = await self._docker_client.create_container(
                image=self._image,
                command=None,  # 使用 Dockerfile CMD（kernel_server）
                detach=True,
                **config
            )
            container_id = container_info.container_id
            
            # 启动容器
            await self._docker_client.start_container(container_id)
            
            # 验证容器是否仍在运行
            is_running = await self._docker_client.is_container_running(container_id)
            if not is_running:
                logger.error(f"容器启动后立即退出: {container_id[:12]}")
                # 尝试获取容器日志以诊断问题
                try:
                    logs_result = await self._docker_client.exec_command(
                        container_id,
                        "echo 'Container exited'",
                        timeout=5
                    )
                except Exception:
                    pass
                raise SandboxCreationError(
                    reason="容器启动后立即退出，可能是 CMD 失败",
                    original_error="Container not running after start"
                )

            is_healthy = await self._wait_for_container_ready(container_id)
            if not is_healthy:
                raise SandboxCreationError(
                    reason="预热容器 kernel 启动超时",
                    original_error="Kernel ping readiness timeout"
                )

            self._total_created += 1
            
            pooled_container = PooledContainer(
                container_id=container_id,
                created_at=datetime.now(),
                is_healthy=True,
                last_health_check=datetime.now()
            )
            
            logger.debug(f"预热容器创建成功: {container_id[:12]}")
            return pooled_container
            
        except Exception as e:
            if container_id:
                await self._remove_container(container_id)
            logger.error(f"创建预热容器失败: {e}")
            raise SandboxCreationError(
                reason=f"创建预热容器失败: {str(e)}",
                original_error=str(e)
            )

    async def _wait_for_container_ready(
        self,
        container_id: str,
        timeout_seconds: int = 30,
        interval_seconds: float = 1.0,
    ) -> bool:
        deadline = datetime.now().timestamp() + timeout_seconds
        last_log_at = 0.0
        while datetime.now().timestamp() < deadline:
            if await self._check_container_health(container_id, log_failures=False):
                return True
            now = datetime.now().timestamp()
            if now - last_log_at >= 5:
                logger.info(f"等待 kernel server 就绪: {container_id[:12]}")
                last_log_at = now
            await asyncio.sleep(interval_seconds)
        logger.warning(f"等待 kernel server 就绪超时: {container_id[:12]}")
        return False
    
    async def _check_container_health(
        self,
        container_id: str,
        log_failures: bool = True,
    ) -> bool:
        """
        检查容器健康状态
        
        Args:
            container_id: 容器 ID
            
        Returns:
            True 如果容器健康，False 如果不健康
        """
        try:
            # 检查容器是否存在且正在运行
            is_running = await self._docker_client.is_container_running(container_id)
            
            if not is_running:
                if log_failures:
                    logger.debug(f"容器 {container_id[:12]} 未运行")
                return False
            
            ping_cmd = (
                'python -c "'
                "import socket,json;"
                "s=socket.socket();"
                "s.settimeout(3);"
                "s.connect(('127.0.0.1',9999));"
                "p=json.dumps({'action':'ping'}).encode();"
                "s.sendall(len(p).to_bytes(4,'big')+p);"
                "data=s.recv(4096);"
                "s.close();"
                "raise SystemExit(0 if b'\\\"ok\\\"' in data else 1)"
                '"'
            )
            result = await self._docker_client.exec_command(
                container_id,
                ping_cmd,
                timeout=5,
            )
            if result.exit_code != 0:
                if log_failures:
                    logger.warning(
                        f"Kernel 健康检查失败: {container_id[:12]}, "
                        f"exit={result.exit_code}, stderr={result.stderr[:200]}"
                    )
                return False
            
            return True
            
        except Exception as e:
            if log_failures:
                logger.warning(f"健康检查失败: {container_id[:12]}, 错误: {e}")
            return False
    
    async def _remove_container(self, container_id: str) -> None:
        """
        移除容器
        
        停止并删除指定的容器。
        
        Args:
            container_id: 容器 ID
        """
        try:
            await self._docker_client.stop_container(container_id, timeout=5)
            await self._docker_client.remove_container(container_id, force=True)
            self._total_removed += 1
            logger.debug(f"容器已移除: {container_id[:12]}")
        except Exception as e:
            logger.warning(f"移除容器失败: {container_id[:12]}, 错误: {e}")
        finally:
            # 主动失效 direct 直连 IP 缓存（容器没了，缓存的 IP 必然失效）
            try:
                from ..executor import CodeExecutor

                CodeExecutor.invalidate_ip_cache(container_id)
            except Exception:
                pass
    
    async def _remove_pooled_container(self, container_id: str) -> None:
        """
        从池中移除容器
        
        从容器池中移除指定容器并销毁。
        
        Args:
            container_id: 容器 ID
        """
        # 从可用列表中移除
        self._available_containers = [
            c for c in self._available_containers 
            if c.container_id != container_id
        ]
        
        # 从总容器列表中移除
        if container_id in self._all_containers:
            del self._all_containers[container_id]
        
        # 销毁容器
        await self._remove_container(container_id)
    
    def _get_available_container(self) -> Optional[PooledContainer]:
        """
        获取一个可用的健康容器
        
        从可用容器列表中选择一个健康且未过期的容器。
        
        Returns:
            PooledContainer 对象，如果没有可用容器则返回 None
        """
        for container in self._available_containers:
            # 检查是否过期
            if container.age_seconds > self._max_container_age_seconds:
                continue
            
            # 检查是否健康
            if not container.is_healthy:
                continue
            
            # 正被共享租约使用的容器不可独占借出
            # （独占方会注入数据/清理 workspace，打断并发执行）
            if self._shared_inflight.get(container.container_id, 0) > 0:
                continue
            
            return container
        
        return None
    
    async def _trigger_replenish(self) -> None:
        """
        触发容器补充
        
        异步触发容器池补充，不阻塞当前操作。
        """
        try:
            await self.replenish()
        except Exception as e:
            logger.error(f"容器补充失败: {e}")
    
    async def _health_check_loop(self) -> None:
        """
        健康检查循环
        
        后台任务，定期执行健康检查。
        
        Requirements: 7.4 (定期检查预热容器健康状态)
        """
        logger.info(
            f"启动健康检查循环，间隔: {self._health_check_interval_seconds}s"
        )
        
        while self._running:
            try:
                await asyncio.sleep(self._health_check_interval_seconds)
                
                if not self._running:
                    break
                
                await self.health_check()
                
            except asyncio.CancelledError:
                logger.debug("健康检查循环被取消")
                break
            except Exception as e:
                logger.error(f"健康检查循环出错: {e}")
                # 继续运行，不因单次错误而停止
                await asyncio.sleep(5)
        
        logger.info("健康检查循环已停止")
    
    async def _cleanup_stale_containers(self) -> None:
        """
        清理可能存在的旧预热容器
        
        在初始化时清理之前运行遗留的预热容器。
        """
        try:
            # 只清理本实例的旧预热容器（多实例共享 daemon 时不误删兄弟实例的）
            from ..config import resolve_instance_id

            instance_id = resolve_instance_id()
            containers = await self._docker_client.list_containers(
                all=True,
                filters={
                    "label": [
                        f"{self.POOL_LABEL_KEY}={self.POOL_LABEL_VALUE}",
                        f"{self.INSTANCE_LABEL_KEY}={instance_id}",
                    ]
                }
            )
            
            if containers:
                logger.info(
                    f"发现 {len(containers)} 个本实例（{instance_id}）旧预热容器，正在清理..."
                )
                
                for container in containers:
                    try:
                        await self._docker_client.remove_container(
                            container.container_id,
                            force=True
                        )
                        logger.debug(f"清理旧容器: {container.container_id[:12]}")
                    except Exception as e:
                        logger.warning(f"清理旧容器失败: {e}")
                
                logger.info("旧预热容器清理完成")
                
        except Exception as e:
            logger.warning(f"清理旧预热容器时出错: {e}")
    
    async def get_pool_status(self) -> Dict[str, Any]:
        """
        获取容器池状态
        
        Returns:
            包含容器池状态信息的字典
        """
        async with self._lock:
            containers_info = []
            for container in self._available_containers:
                containers_info.append({
                    "container_id": container.container_id[:12],
                    "age_seconds": round(container.age_seconds, 1),
                    "is_healthy": container.is_healthy,
                    "last_health_check": container.last_health_check.isoformat(),
                })
            
            return {
                "pool_size": self._pool_size,
                "available_count": self.available_count,
                "total_count": self.total_count,
                "leased_count": self.leased_count,
                "is_running": self._running,
                "statistics": self.statistics,
                "containers": containers_info,
            }
    
    async def __aenter__(self) -> "ContainerPool":
        """异步上下文管理器入口"""
        await self.initialize()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """异步上下文管理器出口"""
        await self.shutdown()
