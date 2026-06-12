"""
沙箱管理器模块

负责沙箱生命周期管理的核心组件，整合 Docker 客户端、资源限制器、
网络控制器、容器池和安全策略，提供沙箱创建、销毁、状态查询等功能。

按职责拆分：
- sandbox_models.py        SandboxState / SandboxInfo / _SandboxRecord
- sandbox_execution.py     代码执行（有状态 / 流式 / 无状态）Mixin
- sandbox_provisioning.py  容器创建与网络/池配置 Mixin
- 本文件                    生命周期编排（创建/销毁/查询/后台清理）
"""

import asyncio
import logging
import os
import shutil
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import settings
from ..exceptions import (
    ContainerPoolExhaustedError,
    SandboxCreationError,
    SandboxNotFoundError,
)
from ..infrastructure.container_pool import ContainerPool
from ..infrastructure.docker_client import ContainerState, DockerClient
from ..infrastructure.network_controller import NetworkController
from ..infrastructure.resource_limiter import ResourceLimiter, ResourceUsage
from ..infrastructure.security_policy import SecurityPolicy, create_default_security_policy
from .sandbox_agent_ops import SandboxAgentOpsMixin
from .sandbox_execution import SandboxExecutionMixin
from .sandbox_models import SandboxInfo, SandboxState, _SandboxRecord
from .sandbox_provisioning import SandboxProvisioningMixin

logger = logging.getLogger(__name__)

__all__ = ["SandboxManager", "SandboxState", "SandboxInfo"]


class SandboxManager(SandboxExecutionMixin, SandboxProvisioningMixin, SandboxAgentOpsMixin):
    """
    沙箱管理器

    负责沙箱的创建、销毁、状态管理和资源监控。
    整合 Docker 客户端、资源限制器、网络控制器、容器池和安全策略。
    执行/供给/Agent 扩展操作分别由 SandboxExecutionMixin /
    SandboxProvisioningMixin / SandboxAgentOpsMixin 提供。

    使用方式:
    ```python
    manager = SandboxManager()
    await manager.initialize()

    # 创建沙箱
    sandbox = await manager.create_sandbox(session_id="session-123")

    # 执行代码
    result = await manager.execute_code(sandbox.sandbox_id, "print('hello')")

    # 销毁沙箱
    await manager.destroy_sandbox(sandbox.sandbox_id)

    # 关闭管理器
    await manager.shutdown()
    ```
    """

    # 沙箱容器标签
    SANDBOX_LABEL_KEY = "sandbox.managed"
    SANDBOX_LABEL_VALUE = "true"
    # 实例标签：多实例共享 daemon 时区分归属，启动清理只清本实例
    INSTANCE_LABEL_KEY = "sandbox.instance"

    def __init__(
        self,
        docker_client: Optional[DockerClient] = None,
        resource_limiter: Optional[ResourceLimiter] = None,
        network_controller: Optional[NetworkController] = None,
        container_pool: Optional[ContainerPool] = None,
        security_policy: Optional[SecurityPolicy] = None,
        max_concurrent_sandboxes: Optional[int] = None,
        workspace_base: Optional[str] = None,
        docker_image: Optional[str] = None,
    ):
        """
        初始化沙箱管理器

        Args:
            docker_client: Docker 客户端实例（可选，默认创建新实例）
            resource_limiter: 资源限制器实例（可选）
            network_controller: 网络控制器实例（可选）
            container_pool: 容器池实例（可选）
            security_policy: 默认安全策略（可选）
            max_concurrent_sandboxes: 最大并发沙箱数（可选，默认使用 settings）
            workspace_base: 工作空间基础目录（可选，默认使用 settings）
            docker_image: Docker 镜像名称（可选，默认使用 settings）
        """
        # 初始化组件
        self._docker_client = docker_client or DockerClient()
        self._resource_limiter = resource_limiter or ResourceLimiter.from_settings(
            docker_client=self._docker_client
        )
        self._network_controller = network_controller or NetworkController.from_settings()
        self._security_policy = security_policy or create_default_security_policy()

        # 配置参数
        self._max_concurrent = max_concurrent_sandboxes or settings.pool.max_concurrent_sandboxes
        self._workspace_base = workspace_base or settings.workspace_base
        self._docker_image = docker_image or settings.docker_image

        # 容器池（延迟初始化）
        self._container_pool = container_pool
        self._pool_initialized = False

        # 沙箱存储
        self._sandboxes: Dict[str, _SandboxRecord] = {}
        self._session_to_sandbox: Dict[str, str] = {}  # session_id -> sandbox_id 映射

        # 同步锁
        self._lock = asyncio.Lock()

        # 后台任务
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False

        # 统计信息
        self._total_created = 0
        self._total_destroyed = 0

        logger.info(
            f"沙箱管理器初始化，最大并发: {self._max_concurrent}, "
            f"工作空间: {self._workspace_base}, "
            f"镜像: {self._docker_image}"
        )

    @classmethod
    def from_settings(cls) -> "SandboxManager":
        """
        从 settings 配置创建沙箱管理器

        Returns:
            SandboxManager 实例
        """
        return cls()

    @property
    def active_count(self) -> int:
        """获取当前活跃沙箱数量"""
        return len(self._sandboxes)

    @property
    def max_concurrent(self) -> int:
        """获取最大并发沙箱数"""
        return self._max_concurrent

    @property
    def is_running(self) -> bool:
        """检查管理器是否正在运行"""
        return self._running

    async def initialize(self) -> None:
        """
        初始化沙箱管理器

        创建必要的目录、初始化容器池、启动后台清理任务。
        """
        if self._running:
            logger.warning("沙箱管理器已经在运行中")
            return

        logger.info("初始化沙箱管理器...")
        self._running = True

        # 确保工作空间目录存在
        os.makedirs(self._workspace_base, exist_ok=True)

        # 初始化网络
        await self._network_controller.create_network()

        # 沙箱互联网络：direct 直连接网关自身，egress proxy 接白名单代理
        await self._setup_sandbox_networks()

        # 初始化容器池
        if self._container_pool is None:
            self._container_pool = ContainerPool.from_settings(
                docker_client=self._docker_client,
                container_config=self._get_pool_container_config()
            )

        if not self._pool_initialized:
            await self._container_pool.initialize()
            self._pool_initialized = True

        # 启动后台清理任务
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(),
            name="sandbox_cleanup"
        )

        # 清理可能存在的旧沙箱容器
        await self._cleanup_stale_sandboxes()

        logger.info("沙箱管理器初始化完成")

    async def shutdown(self) -> None:
        """
        关闭沙箱管理器

        停止后台任务、销毁所有沙箱、关闭容器池。
        """
        logger.info("关闭沙箱管理器...")
        self._running = False

        # 取消后台任务
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        # 销毁所有沙箱
        async with self._lock:
            sandbox_ids = list(self._sandboxes.keys())

        for sandbox_id in sandbox_ids:
            try:
                await self.destroy_sandbox(sandbox_id)
            except Exception as e:
                logger.warning(f"关闭时销毁沙箱失败: {sandbox_id}, 错误: {e}")

        # 关闭容器池
        if self._container_pool and self._pool_initialized:
            await self._container_pool.shutdown()
            self._pool_initialized = False

        # 关闭网络控制器
        await self._network_controller.close()

        # 关闭 Docker 客户端
        await self._docker_client.close()

        logger.info("沙箱管理器已关闭")

    async def create_sandbox(
        self,
        session_id: str,
        cpu_limit: Optional[float] = None,
        memory_limit_mb: Optional[int] = None,
        disk_limit_mb: Optional[int] = None,
        network_enabled: Optional[bool] = None,
        timeout_seconds: Optional[int] = None,
    ) -> SandboxInfo:
        """
        创建新的沙箱实例

        Requirements: 1.1 (在 5 秒内创建并启动沙箱容器)

        Args:
            session_id: 会话标识符
            cpu_limit: CPU 核心数限制（可选）
            memory_limit_mb: 内存限制（MB，可选）
            disk_limit_mb: 磁盘限制（MB，可选）
            network_enabled: 是否启用网络访问（可选）
            timeout_seconds: 沙箱超时时间（秒，可选）

        Returns:
            SandboxInfo 对象

        Raises:
            ContainerPoolExhaustedError: 达到最大并发数
            SandboxCreationError: 创建失败
        """
        start_time = time.monotonic()

        # 检查并发限制
        async with self._lock:
            if len(self._sandboxes) >= self._max_concurrent:
                raise ContainerPoolExhaustedError(
                    pool_size=self._container_pool.pool_size if self._container_pool else 0,
                    active_count=len(self._sandboxes),
                    max_concurrent=self._max_concurrent
                )

        # 生成沙箱 ID
        sandbox_id = self._generate_sandbox_id()

        logger.info(f"创建沙箱: {sandbox_id}, 会话: {session_id}")

        try:
            # 获取资源限制配置
            resource_limits = self._resource_limiter.get_limits(
                cpu=cpu_limit,
                memory_mb=memory_limit_mb,
                disk_mb=disk_limit_mb
            )

            # 获取网络策略
            network_policy = self._network_controller.get_default_policy()
            if network_enabled is not None:
                network_policy.enabled = network_enabled
                network_policy.allow_outbound = network_enabled

            # 获取超时配置
            timeout = timeout_seconds or settings.timeout.session_idle_timeout

            # 创建工作空间目录
            data_dir, output_dir, data_dir_readonly = self._create_workspace_dirs(session_id)

            # 创建新容器（不使用预热容器池，因为有状态会话需要自定义配置）
            container_id = await self._create_container(
                sandbox_id=sandbox_id,
                resource_limits=resource_limits,
                network_policy=network_policy,
                data_dir=data_dir,
                output_dir=output_dir,
                data_dir_readonly=data_dir_readonly,
            )

            # 创建沙箱信息
            now = datetime.now()
            sandbox_info = SandboxInfo(
                sandbox_id=sandbox_id,
                session_id=session_id,
                container_id=container_id,
                state=SandboxState.RUNNING,
                created_at=now,
                last_activity=now,
                cpu_limit=resource_limits.cpu_count,
                memory_limit_mb=resource_limits.memory_mb,
                disk_limit_mb=resource_limits.disk_mb,
                network_enabled=network_policy.enabled and network_policy.allow_outbound,
                data_dir=data_dir,
                output_dir=output_dir
            )

            # 创建内部记录
            record = _SandboxRecord(
                info=sandbox_info,
                resource_limits=resource_limits,
                network_policy=network_policy,
                security_policy=self._security_policy,
                timeout_seconds=timeout
            )

            # 存储沙箱记录
            async with self._lock:
                self._sandboxes[sandbox_id] = record
                self._session_to_sandbox[session_id] = sandbox_id
                self._total_created += 1

            elapsed = time.monotonic() - start_time
            logger.info(
                f"沙箱创建成功: {sandbox_id}, "
                f"容器: {container_id[:12]}, "
                f"耗时: {elapsed*1000:.2f}ms"
            )

            if elapsed > 5.0:
                logger.warning(f"沙箱创建耗时超过 5 秒: {elapsed:.2f}s")

            return sandbox_info

        except Exception as e:
            # 清理可能创建的资源
            logger.error(f"创建沙箱失败: {sandbox_id}, 错误: {e}")
            await self._cleanup_sandbox_resources(session_id)

            if isinstance(e, (ContainerPoolExhaustedError, SandboxCreationError)):
                raise

            raise SandboxCreationError(
                reason=str(e),
                session_id=session_id,
                original_error=str(e)
            )

    async def get_sandbox(self, sandbox_id: str) -> Optional[SandboxInfo]:
        """
        获取沙箱信息

        Requirements: 1.3 (查询沙箱状态)

        Args:
            sandbox_id: 沙箱 ID

        Returns:
            SandboxInfo 对象，如果不存在则返回 None
        """
        async with self._lock:
            record = self._sandboxes.get(sandbox_id)
            if record is None:
                return None

            # 更新状态
            await self._update_sandbox_state(record)

            return record.info

    async def get_sandbox_by_session(self, session_id: str) -> Optional[SandboxInfo]:
        """
        通过会话 ID 获取沙箱信息

        Args:
            session_id: 会话 ID

        Returns:
            SandboxInfo 对象，如果不存在则返回 None
        """
        async with self._lock:
            sandbox_id = self._session_to_sandbox.get(session_id)
            if sandbox_id is None:
                return None

        return await self.get_sandbox(sandbox_id)

    async def destroy_sandbox(self, sandbox_id: str) -> bool:
        """
        销毁沙箱

        Requirements: 1.4 (强制停止并删除沙箱容器)

        Args:
            sandbox_id: 沙箱 ID

        Returns:
            True 如果销毁成功，False 如果沙箱不存在
        """
        logger.info(f"销毁沙箱: {sandbox_id}")

        async with self._lock:
            record = self._sandboxes.pop(sandbox_id, None)
            if record is None:
                logger.warning(f"沙箱不存在: {sandbox_id}")
                return False

            # 移除会话映射
            session_id = record.info.session_id
            if session_id in self._session_to_sandbox:
                del self._session_to_sandbox[session_id]

            self._total_destroyed += 1

        # 停止并删除容器
        container_id = record.info.container_id
        try:
            # 断开网络连接
            await self._network_controller.disconnect_container(container_id)

            # 停止容器
            await self._docker_client.stop_container(container_id, timeout=5)

            # 删除容器
            await self._docker_client.remove_container(container_id, force=True)

            logger.info(f"容器已删除: {container_id[:12]}")

        except Exception as e:
            logger.warning(f"删除容器时出错: {container_id[:12]}, 错误: {e}")
        finally:
            # 主动失效 direct 直连 IP 缓存
            try:
                from ..executor import CodeExecutor

                CodeExecutor.invalidate_ip_cache(container_id)
            except Exception:
                pass

        # 清理工作空间目录
        await self._cleanup_sandbox_resources(session_id)

        logger.info(f"沙箱销毁完成: {sandbox_id}")
        return True

    async def list_sandboxes(
        self,
        state: Optional[SandboxState] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[SandboxInfo]:
        """
        列出沙箱

        Args:
            state: 按状态过滤（可选）
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            SandboxInfo 列表
        """
        async with self._lock:
            # 获取所有沙箱
            sandboxes = list(self._sandboxes.values())

        # 更新状态
        for record in sandboxes:
            await self._update_sandbox_state(record)

        # 按状态过滤
        if state is not None:
            sandboxes = [s for s in sandboxes if s.info.state == state]

        # 按创建时间排序（最新的在前）
        sandboxes.sort(key=lambda s: s.info.created_at, reverse=True)

        # 分页
        sandboxes = sandboxes[offset:offset + limit]

        return [s.info for s in sandboxes]

    async def get_resource_usage(self, sandbox_id: str) -> ResourceUsage:
        """
        获取沙箱资源使用情况

        Requirements: 1.3 (查询沙箱状态 - 资源使用情况)

        Args:
            sandbox_id: 沙箱 ID

        Returns:
            ResourceUsage 对象

        Raises:
            SandboxNotFoundError: 沙箱不存在
        """
        async with self._lock:
            record = self._sandboxes.get(sandbox_id)
            if record is None:
                raise SandboxNotFoundError(sandbox_id=sandbox_id)

        return await self._resource_limiter.get_usage(record.info.container_id)

    async def update_activity(self, sandbox_id: str) -> None:
        """
        更新沙箱活动时间

        Args:
            sandbox_id: 沙箱 ID

        Raises:
            SandboxNotFoundError: 沙箱不存在
        """
        async with self._lock:
            record = self._sandboxes.get(sandbox_id)
            if record is None:
                raise SandboxNotFoundError(sandbox_id=sandbox_id)

            record.info.last_activity = datetime.now()

    async def get_statistics(self) -> Dict[str, Any]:
        """
        获取沙箱管理器统计信息

        Returns:
            统计信息字典
        """
        async with self._lock:
            active_count = len(self._sandboxes)

            # 按状态统计
            state_counts = {}
            for record in self._sandboxes.values():
                state = record.info.state.value
                state_counts[state] = state_counts.get(state, 0) + 1

        pool_stats = {}
        if self._container_pool:
            pool_stats = self._container_pool.statistics

        return {
            "active_sandboxes": active_count,
            "max_concurrent": self._max_concurrent,
            "total_created": self._total_created,
            "total_destroyed": self._total_destroyed,
            "state_counts": state_counts,
            "pool": pool_stats,
            "is_running": self._running,
        }

    # ==================== 私有方法 ====================

    def _generate_sandbox_id(self) -> str:
        """
        生成唯一的沙箱 ID

        Returns:
            沙箱 ID 字符串
        """
        return f"sandbox-{uuid.uuid4().hex[:12]}"

    def _create_workspace_dirs(self, session_id: str) -> tuple[str, str, bool]:
        """
        创建沙箱工作空间目录

        Args:
            session_id: 会话 ID

        Returns:
            (data_dir, output_dir, data_dir_readonly) 元组
        """
        workspace_dir = os.path.join(self._workspace_base, session_id)
        output_dir = os.path.join(workspace_dir, "output")
        shared_data_root = (settings.shared_data_root or "").strip()
        data_dir_readonly = bool(shared_data_root)
        if shared_data_root:
            data_dir = os.path.join(shared_data_root, session_id)
        else:
            data_dir = os.path.join(workspace_dir, "data")

        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        logger.debug(f"创建工作空间目录: {workspace_dir}")
        return data_dir, output_dir, data_dir_readonly

    async def _update_sandbox_state(self, record: _SandboxRecord) -> None:
        """
        更新沙箱状态

        根据容器实际状态更新沙箱信息。

        Args:
            record: 沙箱记录
        """
        try:
            container_info = await self._docker_client.get_container_status(
                record.info.container_id
            )

            # 映射容器状态到沙箱状态
            state_mapping = {
                ContainerState.CREATED: SandboxState.CREATING,
                ContainerState.RUNNING: SandboxState.RUNNING,
                ContainerState.PAUSED: SandboxState.PAUSED,
                ContainerState.EXITED: SandboxState.STOPPED,
                ContainerState.DEAD: SandboxState.ERROR,
                ContainerState.RESTARTING: SandboxState.CREATING,
                ContainerState.UNKNOWN: SandboxState.ERROR,
            }

            record.info.state = state_mapping.get(
                container_info.state,
                SandboxState.ERROR
            )

        except Exception as e:
            logger.warning(f"更新沙箱状态失败: {record.info.sandbox_id}, 错误: {e}")
            record.info.state = SandboxState.ERROR

    async def _cleanup_sandbox_resources(self, session_id: str) -> None:
        """
        清理沙箱资源

        删除沙箱的工作空间目录，包括数据目录和输出目录中的所有文件。

        Requirements 5.6: 沙箱销毁时清理所有关联的文件

        Args:
            session_id: 会话 ID
        """
        workspace_dir = os.path.join(self._workspace_base, session_id)

        if os.path.exists(workspace_dir):
            try:
                shutil.rmtree(workspace_dir)
                logger.debug(f"清理工作空间目录: {workspace_dir}")
            except Exception as e:
                logger.warning(f"清理工作空间目录失败: {workspace_dir}, 错误: {e}")

    async def _cleanup_loop(self) -> None:
        """
        后台清理循环

        定期检查并清理空闲超时的沙箱。
        Requirements 1.2: 沙箱空闲超过配置的超时时间时自动销毁
        """
        logger.info("启动沙箱清理循环")

        check_interval = 60  # 每 60 秒检查一次

        while self._running:
            try:
                await asyncio.sleep(check_interval)

                if not self._running:
                    break

                await self._cleanup_idle_sandboxes()

            except asyncio.CancelledError:
                logger.debug("清理循环被取消")
                break
            except Exception as e:
                logger.error(f"清理循环出错: {e}")
                await asyncio.sleep(5)

        logger.info("沙箱清理循环已停止")

    async def _cleanup_idle_sandboxes(self) -> None:
        """
        清理空闲超时的沙箱

        Requirements 1.2: 沙箱空闲超过配置的超时时间时自动销毁
        """
        now = datetime.now()
        sandboxes_to_destroy: List[str] = []

        async with self._lock:
            for sandbox_id, record in self._sandboxes.items():
                idle_seconds = (now - record.info.last_activity).total_seconds()

                if idle_seconds > record.timeout_seconds:
                    logger.info(
                        f"沙箱空闲超时: {sandbox_id}, "
                        f"空闲时间: {idle_seconds:.0f}s, "
                        f"超时配置: {record.timeout_seconds}s"
                    )
                    sandboxes_to_destroy.append(sandbox_id)

        # 销毁超时的沙箱
        for sandbox_id in sandboxes_to_destroy:
            try:
                await self.destroy_sandbox(sandbox_id)
            except Exception as e:
                logger.error(f"清理超时沙箱失败: {sandbox_id}, 错误: {e}")

        if sandboxes_to_destroy:
            logger.info(f"清理了 {len(sandboxes_to_destroy)} 个空闲超时的沙箱")

    async def _cleanup_stale_sandboxes(self) -> None:
        """
        清理可能存在的旧沙箱容器

        在初始化时清理之前运行遗留的沙箱容器。
        """
        try:
            # 只清理本实例的旧沙箱容器（多实例共享 daemon 时不误删兄弟实例的）
            from ..config import resolve_instance_id

            instance_id = resolve_instance_id()
            containers = await self._docker_client.list_containers(
                all=True,
                filters={
                    "label": [
                        f"{self.SANDBOX_LABEL_KEY}={self.SANDBOX_LABEL_VALUE}",
                        f"{self.INSTANCE_LABEL_KEY}={instance_id}",
                    ]
                }
            )

            if containers:
                logger.info(
                    f"发现 {len(containers)} 个本实例（{instance_id}）旧沙箱容器，正在清理..."
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

                logger.info("旧沙箱容器清理完成")

        except Exception as e:
            logger.warning(f"清理旧沙箱容器时出错: {e}")

    async def __aenter__(self) -> "SandboxManager":
        """异步上下文管理器入口"""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """异步上下文管理器出口"""
        await self.shutdown()
