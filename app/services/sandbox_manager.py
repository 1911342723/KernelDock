"""
沙箱管理器模块

负责沙箱生命周期管理的核心组件，整合 Docker 客户端、资源限制器、
网络控制器、容器池和安全策略，提供沙箱创建、销毁、状态查询等功能。

Requirements:
- 1.1: 在 5 秒内创建并启动沙箱容器
- 1.2: 沙箱空闲超过配置的超时时间时自动销毁
- 1.3: 查询沙箱状态（运行状态、资源使用情况、创建时间）
- 1.4: 强制停止并删除沙箱容器
- 1.5: 支持同时管理至少 50 个并发沙箱实例
"""

import asyncio
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from ..config import settings
from ..exceptions import (
    ContainerPoolExhaustedError,
    InternalError,
    SandboxCreationError,
    SandboxNotFoundError,
)
from ..infrastructure.container_pool import ContainerPool
from ..infrastructure.docker_client import ContainerState, DockerClient
from ..infrastructure.network_controller import NetworkController, NetworkPolicy
from ..infrastructure.resource_limiter import ResourceLimiter, ResourceLimits, ResourceUsage
from ..infrastructure.security_policy import SecurityPolicy, create_default_security_policy

logger = logging.getLogger(__name__)


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


class SandboxManager:
    """
    沙箱管理器
    
    负责沙箱的创建、销毁、状态管理和资源监控。
    整合 Docker 客户端、资源限制器、网络控制器、容器池和安全策略。
    
    Requirements:
    - 1.1: 在 5 秒内创建并启动沙箱容器
    - 1.2: 沙箱空闲超过配置的超时时间时自动销毁
    - 1.3: 查询沙箱状态（运行状态、资源使用情况、创建时间）
    - 1.4: 强制停止并删除沙箱容器
    - 1.5: 支持同时管理至少 50 个并发沙箱实例
    
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
        # Requirements 1.5: 支持同时管理至少 50 个并发沙箱实例
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
        # Requirements 1.2: 沙箱空闲超过配置的超时时间时自动销毁
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
        # Requirements 1.5: 支持同时管理至少 50 个并发沙箱实例
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
            data_dir, output_dir = self._create_workspace_dirs(sandbox_id)
            
            # 尝试从容器池获取预热容器
            # Requirements 7.2: 在 1 秒内分配预热容器
            container_id = None
            if self._container_pool and self._container_pool.available_count > 0:
                pooled = await self._container_pool.acquire()
                if pooled:
                    container_id = pooled.container_id
                    logger.info(f"使用预热容器: {container_id[:12]}")
                    # 需要重新配置容器（预热容器使用默认配置）
                    # 由于 Docker 不支持修改运行中容器的资源限制，
                    # 我们需要停止并重新创建容器
                    await self._docker_client.stop_container(container_id, timeout=5)
                    await self._docker_client.remove_container(container_id, force=True)
                    container_id = None  # 重新创建
            
            # 如果没有预热容器，创建新容器
            if container_id is None:
                container_id = await self._create_container(
                    sandbox_id=sandbox_id,
                    resource_limits=resource_limits,
                    network_policy=network_policy,
                    data_dir=data_dir,
                    output_dir=output_dir
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
            
            # Requirements 1.1: 验证是否在 5 秒内完成
            if elapsed > 5.0:
                logger.warning(f"沙箱创建耗时超过 5 秒: {elapsed:.2f}s")
            
            return sandbox_info
            
        except Exception as e:
            # 清理可能创建的资源
            logger.error(f"创建沙箱失败: {sandbox_id}, 错误: {e}")
            await self._cleanup_sandbox_resources(sandbox_id)
            
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
        
        # 清理工作空间目录
        await self._cleanup_sandbox_resources(sandbox_id)
        
        logger.info(f"沙箱销毁完成: {sandbox_id}")
        return True

    async def execute_code(
        self,
        sandbox_id: str,
        code: str,
        timeout: int = 300
    ) -> Dict[str, Any]:
        """
        在沙箱中执行代码
        
        使用 CodeExecutor 在 Docker 容器内执行代码，支持图表和表格捕获。
        
        Requirements:
        - 4.1: 在 Sandbox_Container 内执行代码并返回结果
        - 4.2: 代码执行超过配置的超时时间时终止执行并返回超时错误
        - 4.3: 捕获代码执行的标准输出、标准错误和返回值
        - 4.4: 自动捕获 matplotlib 生成的图表并转换为 SVG 格式
        - 4.5: 支持 display_table 函数捕获 DataFrame 数据
        - 4.6: 代码执行产生异常时返回完整的异常堆栈信息
        
        Args:
            sandbox_id: 沙箱 ID
            code: Python 代码
            timeout: 执行超时（秒）
            
        Returns:
            执行结果字典，包含:
            - success: 是否成功
            - output: 输出内容
            - stdout: 标准输出
            - stderr: 标准错误
            - charts: 图表列表（SVG base64）
            - tables: 表格数据列表
            - images: 图片路径列表
            - error: 错误信息（如果有）
            - execution_time_ms: 执行时间（毫秒）
            
        Raises:
            SandboxNotFoundError: 沙箱不存在
        """
        # 导入 CodeExecutor
        from ..executor import CodeExecutor
        
        # 获取沙箱记录
        async with self._lock:
            record = self._sandboxes.get(sandbox_id)
            if record is None:
                raise SandboxNotFoundError(sandbox_id=sandbox_id)
            
            # 更新最后活动时间
            record.info.last_activity = datetime.now()
        
        container_id = record.info.container_id
        
        # 使用 CodeExecutor 执行代码
        # 容器内的数据目录和输出目录路径
        container_data_dir = "/data"
        container_output_dir = "/output"
        
        # 代码安全验证（AST 静态分析）
        from .code_validator import validate_code
        validation_result = validate_code(code)
        if not validation_result.is_valid:
            # 返回验证失败结果
            issues_msg = "; ".join(
                f"Line {i.line}: {i.message}" for i in validation_result.issues
            )
            return {
                "success": False,
                "output": "",
                "stdout": "",
                "stderr": f"代码安全验证失败: {issues_msg}",
                "charts": [],
                "tables": [],
                "images": [],
                "error": f"CodeValidationError: {issues_msg}",
                "execution_time_ms": 0
            }
        
        executor = CodeExecutor(self._docker_client)
        try:
            result = await executor.execute_in_container(
                container_id=container_id,
                code=code,
                data_dir=container_data_dir,
                output_dir=container_output_dir,
                timeout=timeout
            )
            return result
        finally:
            # 注意：不关闭 executor，因为它使用的是共享的 docker_client
            pass
    
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
    
    def _create_workspace_dirs(self, sandbox_id: str) -> tuple[str, str]:
        """
        创建沙箱工作空间目录
        
        Args:
            sandbox_id: 沙箱 ID
            
        Returns:
            (data_dir, output_dir) 元组
        """
        workspace_dir = os.path.join(self._workspace_base, sandbox_id)
        data_dir = os.path.join(workspace_dir, "data")
        output_dir = os.path.join(workspace_dir, "output")
        
        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)
        
        logger.debug(f"创建工作空间目录: {workspace_dir}")
        return data_dir, output_dir
    
    async def _create_container(
        self,
        sandbox_id: str,
        resource_limits: ResourceLimits,
        network_policy: NetworkPolicy,
        data_dir: str,
        output_dir: str
    ) -> str:
        """
        创建沙箱容器
        
        Args:
            sandbox_id: 沙箱 ID
            resource_limits: 资源限制配置
            network_policy: 网络策略
            data_dir: 数据目录路径
            output_dir: 输出目录路径
            
        Returns:
            容器 ID
        """
        # 获取资源限制参数
        resource_kwargs = resource_limits.to_container_create_kwargs()
        
        # 获取安全策略参数
        security_kwargs = self._security_policy.to_docker_config()
        
        # 移除安全策略中的 pids_limit，使用资源限制中的值
        # 避免参数重复
        security_kwargs.pop("pids_limit", None)
        
        # 获取 tmpfs 配置
        tmpfs_config = self._security_policy.get_tmpfs_config()
        
        # 获取网络模式
        network_mode = self._network_controller.get_network_mode_for_container(network_policy)
        
        # 卷挂载配置
        volumes = {
            data_dir: {"bind": "/data", "mode": "rw"},
            output_dir: {"bind": "/output", "mode": "rw"},
        }
        
        # 容器标签
        labels = {
            self.SANDBOX_LABEL_KEY: self.SANDBOX_LABEL_VALUE,
            "sandbox.id": sandbox_id,
            "sandbox.created_at": datetime.now().isoformat(),
        }
        
        # gVisor 运行时配置
        runtime = None
        if settings.security.use_gvisor:
            runtime = settings.security.gvisor_runtime
            logger.info(f"使用 gVisor 运行时: {runtime}")
        
        # 创建容器
        container_info = await self._docker_client.create_container(
            image=self._docker_image,
            name=f"sandbox-{sandbox_id[:8]}",
            command="sleep infinity",  # 保持容器运行
            labels=labels,
            volumes=volumes,
            network_mode=network_mode,
            tmpfs=tmpfs_config,
            working_dir="/home/sandbox",
            runtime=runtime,  # gVisor 支持
            **resource_kwargs,
            **security_kwargs,
        )
        
        # 启动容器
        await self._docker_client.start_container(container_info.container_id)
        
        # 如果需要网络，连接到隔离网络
        if network_policy.enabled and network_policy.allow_outbound:
            await self._network_controller.connect_container(
                container_info.container_id,
                network_policy
            )
        
        return container_info.container_id
    
    def _get_pool_container_config(self) -> Dict[str, Any]:
        """
        获取容器池的容器配置
        
        Returns:
            容器配置字典
        """
        # 使用默认资源限制
        resource_limits = self._resource_limiter.get_limits()
        resource_kwargs = resource_limits.to_container_create_kwargs()
        
        # 使用默认安全策略
        security_kwargs = self._security_policy.to_docker_config()
        
        return {
            **resource_kwargs,
            **security_kwargs,
            "network_mode": "none",  # 预热容器默认无网络
        }

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
    
    async def _cleanup_sandbox_resources(self, sandbox_id: str) -> None:
        """
        清理沙箱资源
        
        删除沙箱的工作空间目录，包括数据目录和输出目录中的所有文件。
        
        Requirements 5.6: 沙箱销毁时清理所有关联的文件
        
        Args:
            sandbox_id: 沙箱 ID
        """
        workspace_dir = os.path.join(self._workspace_base, sandbox_id)
        
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
            # 查找带有沙箱标签的容器
            containers = await self._docker_client.list_containers(
                all=True,
                filters={
                    "label": f"{self.SANDBOX_LABEL_KEY}={self.SANDBOX_LABEL_VALUE}"
                }
            )
            
            if containers:
                logger.info(f"发现 {len(containers)} 个旧沙箱容器，正在清理...")
                
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
