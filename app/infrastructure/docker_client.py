"""
Docker 客户端封装模块

封装 Docker SDK for Python，提供容器管理的异步接口。
包括容器创建、启动、停止、删除、执行命令等方法，
以及容器状态查询和资源使用监控。

Requirements:
- 1.1: 在 5 秒内创建并启动沙箱容器
- 1.3: 查询沙箱状态（运行状态、资源使用、创建时间）
- 1.4: 强制停止并删除容器
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

import docker
from docker.errors import APIError, ContainerError, ImageNotFound, NotFound
from docker.models.containers import Container

from ..config import settings
from ..exceptions import (
    InternalError,
    SandboxCreationError,
    SandboxNotFoundError,
)

logger = logging.getLogger(__name__)


class ContainerState(Enum):
    """容器状态枚举"""
    CREATED = "created"      # 已创建
    RUNNING = "running"      # 运行中
    PAUSED = "paused"        # 已暂停
    RESTARTING = "restarting"  # 重启中
    EXITED = "exited"        # 已退出
    DEAD = "dead"            # 已死亡
    UNKNOWN = "unknown"      # 未知状态


@dataclass
class ContainerInfo:
    """
    容器信息数据类
    
    包含容器的基本信息和状态。
    """
    container_id: str                  # 容器 ID
    name: str                          # 容器名称
    state: ContainerState              # 当前状态
    created_at: datetime               # 创建时间
    started_at: Optional[datetime]     # 启动时间
    image: str                         # 镜像名称
    labels: Dict[str, str] = field(default_factory=dict)  # 标签


@dataclass
class ContainerStats:
    """
    容器资源使用统计数据类
    
    包含 CPU、内存、网络等资源使用情况。
    Requirements: 1.3 (查询沙箱状态 - 资源使用情况)
    """
    container_id: str                  # 容器 ID
    cpu_percent: float                 # CPU 使用率（百分比）
    memory_used_bytes: int             # 已用内存（字节）
    memory_limit_bytes: int            # 内存限制（字节）
    memory_percent: float              # 内存使用率（百分比）
    network_rx_bytes: int              # 网络接收字节数
    network_tx_bytes: int              # 网络发送字节数
    block_read_bytes: int              # 块设备读取字节数
    block_write_bytes: int             # 块设备写入字节数
    pids: int                          # 进程数
    timestamp: datetime                # 采集时间


@dataclass
class ExecResult:
    """
    命令执行结果数据类
    
    包含命令执行的退出码和输出。
    """
    exit_code: int                     # 退出码
    stdout: str                        # 标准输出
    stderr: str                        # 标准错误


class DockerClient:
    """
    Docker 客户端封装类
    
    封装 Docker SDK，提供容器管理的异步接口。
    使用 settings 中的 docker_socket 配置连接 Docker daemon。
    
    主要功能:
    - 容器创建、启动、停止、删除
    - 容器内命令执行
    - 容器状态查询
    - 资源使用监控
    """
    
    def __init__(self, docker_socket: Optional[str] = None):
        """
        初始化 Docker 客户端
        
        Args:
            docker_socket: Docker socket 路径，默认使用 settings 中的配置
        """
        self._socket = docker_socket or settings.docker_socket
        self._client: Optional[docker.DockerClient] = None
        self._lock = asyncio.Lock()
        self._control_plane_timeout_seconds = max(
            5,
            min(settings.timeout.sandbox_startup_timeout, 10),
        )
        logger.info(f"初始化 Docker 客户端，socket: {self._socket}")

    async def get_container_logs_tail(
        self,
        container_id: str,
        *,
        tail: int = 200,
        max_bytes: int = 1024,
    ) -> Dict[str, str]:
        """Return the last chunk of container stdout/stderr for diagnostics."""

        client = await self._ensure_client()

        def _fetch() -> Dict[str, str]:
            container = client.containers.get(container_id)
            stdout_raw = container.logs(stdout=True, stderr=False, tail=tail) or b""
            stderr_raw = container.logs(stdout=False, stderr=True, tail=tail) or b""
            return {
                "stdout": stdout_raw.decode("utf-8", errors="ignore")[-max_bytes:],
                "stderr": stderr_raw.decode("utf-8", errors="ignore")[-max_bytes:],
            }

        return await asyncio.to_thread(_fetch)
    @property
    def client(self) -> docker.DockerClient:
        """
        获取 Docker 客户端实例（懒加载）
        
        Returns:
            Docker 客户端实例
            
        Raises:
            InternalError: 无法连接到 Docker daemon
        """
        if self._client is None:
            try:
                probe_client = docker.DockerClient(
                    base_url=self._socket,
                    timeout=self._control_plane_timeout_seconds,
                )
                # 测试连接；使用短超时避免 daemon 卡死时把上游请求拖满 60s。
                probe_client.ping()
                probe_client.close()
                self._client = docker.DockerClient(base_url=self._socket)
                logger.debug("Docker 客户端连接成功")
            except Exception as e:
                logger.error(f"无法连接到 Docker daemon: {e}")
                raise InternalError(
                    message=f"无法连接到 Docker daemon: {self._socket}",
                    original_error=e
                )
        return self._client
    
    async def _run_in_executor(self, func, *args, **kwargs) -> Any:
        """
        在线程池中运行同步函数
        
        Docker SDK 是同步的，需要在线程池中运行以避免阻塞事件循环。
        
        Args:
            func: 要执行的函数
            *args: 位置参数
            **kwargs: 关键字参数
            
        Returns:
            函数执行结果
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, 
            lambda: func(*args, **kwargs)
        )

    async def create_container(
        self,
        image: str,
        name: Optional[str] = None,
        command: Optional[str] = None,
        environment: Optional[Dict[str, str]] = None,
        volumes: Optional[Dict[str, Dict[str, str]]] = None,
        network_mode: Optional[str] = None,
        labels: Optional[Dict[str, str]] = None,
        cpu_period: int = 100000,
        cpu_quota: Optional[int] = None,
        mem_limit: Optional[str] = None,
        memswap_limit: Optional[str] = None,
        pids_limit: Optional[int] = None,
        read_only: bool = False,
        user: Optional[str] = None,
        cap_drop: Optional[List[str]] = None,
        cap_add: Optional[List[str]] = None,
        security_opt: Optional[List[str]] = None,
        tmpfs: Optional[Dict[str, str]] = None,
        working_dir: Optional[str] = None,
        detach: bool = True,
        runtime: Optional[str] = None,  # gVisor: "runsc"
    ) -> ContainerInfo:
        """
        创建新容器
        
        Requirements: 1.1 (在 5 秒内创建并启动沙箱容器)
        
        Args:
            image: Docker 镜像名称
            name: 容器名称（可选）
            command: 启动命令（可选）
            environment: 环境变量字典
            volumes: 卷挂载配置
            network_mode: 网络模式
            labels: 容器标签
            cpu_period: CPU 周期（微秒）
            cpu_quota: CPU 配额（微秒）
            mem_limit: 内存限制（如 "512m"）
            memswap_limit: 内存+交换限制
            pids_limit: 进程数限制
            read_only: 是否只读根文件系统
            user: 运行用户
            cap_drop: 要移除的 Linux capabilities
            cap_add: 要添加的 Linux capabilities
            security_opt: 安全选项
            tmpfs: tmpfs 挂载配置
            working_dir: 工作目录
            detach: 是否后台运行
            
        Returns:
            ContainerInfo 对象
            
        Raises:
            SandboxCreationError: 容器创建失败
        """
        try:
            logger.info(f"创建容器，镜像: {image}, 名称: {name}")
            
            # 构建容器配置
            container_config = {
                "image": image,
                "detach": detach,
            }

            # 可选参数
            if name:
                container_config["name"] = name
            if command:
                container_config["command"] = command
            if environment:
                container_config["environment"] = environment
            if volumes:
                container_config["volumes"] = volumes
            if network_mode:
                container_config["network_mode"] = network_mode
            if labels:
                container_config["labels"] = labels
            if working_dir:
                container_config["working_dir"] = working_dir
            
            # 资源限制
            if cpu_quota:
                container_config["cpu_period"] = cpu_period
                container_config["cpu_quota"] = cpu_quota
            if mem_limit:
                container_config["mem_limit"] = mem_limit
            if memswap_limit:
                container_config["memswap_limit"] = memswap_limit
            if pids_limit:
                container_config["pids_limit"] = pids_limit
            
            # 安全配置
            if read_only:
                container_config["read_only"] = read_only
            if user:
                container_config["user"] = user
            if cap_drop:
                container_config["cap_drop"] = cap_drop
            if cap_add:
                container_config["cap_add"] = cap_add
            if security_opt:
                container_config["security_opt"] = security_opt
            if tmpfs:
                container_config["tmpfs"] = tmpfs
            
            # gVisor 运行时
            if runtime:
                container_config["runtime"] = runtime
                logger.info(f"使用运行时: {runtime}")
            
            # 在线程池中创建容器
            container = await asyncio.wait_for(
                self._run_in_executor(
                    self.client.containers.create,
                    **container_config
                ),
                timeout=self._control_plane_timeout_seconds,
            )
            
            logger.info(f"容器创建成功，ID: {container.id[:12]}")
            return self._container_to_info(container)
            
        except ImageNotFound as e:
            logger.error(f"镜像不存在: {image}")
            raise SandboxCreationError(
                reason=f"镜像不存在: {image}",
                original_error=str(e)
            )
        except APIError as e:
            logger.error(f"Docker API 错误: {e}")
            raise SandboxCreationError(
                reason=f"Docker API 错误: {e.explanation}",
                original_error=str(e)
            )
        except asyncio.TimeoutError as e:
            logger.error(
                f"创建容器超时: socket={self._socket}, timeout={self._control_plane_timeout_seconds}s"
            )
            raise SandboxCreationError(
                reason=f"Docker daemon 响应超时（>{self._control_plane_timeout_seconds}秒）",
                original_error=str(e),
            )
        except Exception as e:
            logger.error(f"创建容器失败: {e}")
            raise SandboxCreationError(
                reason=str(e),
                original_error=str(e)
            )

    async def start_container(self, container_id: str) -> None:
        """
        启动容器
        
        Requirements: 1.1 (在 5 秒内创建并启动沙箱容器)
        
        Args:
            container_id: 容器 ID 或名称
            
        Raises:
            SandboxNotFoundError: 容器不存在
            SandboxCreationError: 启动失败
        """
        try:
            logger.info(f"启动容器: {container_id[:12] if len(container_id) > 12 else container_id}")
            container = await self._get_container(container_id)
            await asyncio.wait_for(
                self._run_in_executor(container.start),
                timeout=self._control_plane_timeout_seconds,
            )
            logger.info(f"容器启动成功: {container_id[:12] if len(container_id) > 12 else container_id}")
        except SandboxNotFoundError:
            raise
        except asyncio.TimeoutError as e:
            logger.error(
                f"启动容器超时: container={container_id[:12] if len(container_id) > 12 else container_id}, "
                f"timeout={self._control_plane_timeout_seconds}s"
            )
            raise SandboxCreationError(
                reason=f"Docker daemon 启动容器超时（>{self._control_plane_timeout_seconds}秒）",
                original_error=str(e),
            )
        except APIError as e:
            logger.error(f"启动容器失败: {e}")
            raise SandboxCreationError(
                reason=f"启动容器失败: {e.explanation}",
                original_error=str(e)
            )
    
    async def stop_container(
        self, 
        container_id: str, 
        timeout: int = 10
    ) -> None:
        """
        停止容器
        
        Requirements: 1.4 (强制停止并删除容器)
        
        Args:
            container_id: 容器 ID 或名称
            timeout: 等待超时时间（秒），超时后强制终止
            
        Raises:
            SandboxNotFoundError: 容器不存在
        """
        try:
            logger.info(f"停止容器: {container_id[:12] if len(container_id) > 12 else container_id}")
            container = await self._get_container(container_id)
            await self._run_in_executor(container.stop, timeout=timeout)
            logger.info(f"容器已停止: {container_id[:12] if len(container_id) > 12 else container_id}")
        except SandboxNotFoundError:
            raise
        except APIError as e:
            # 如果容器已经停止，忽略错误
            if "is not running" in str(e):
                logger.debug(f"容器已经停止: {container_id}")
            else:
                logger.warning(f"停止容器时出错: {e}")

    async def remove_container(
        self, 
        container_id: str, 
        force: bool = True,
        v: bool = True
    ) -> None:
        """
        删除容器
        
        Requirements: 1.4 (强制停止并删除容器)
        
        Args:
            container_id: 容器 ID 或名称
            force: 是否强制删除（即使正在运行）
            v: 是否同时删除关联的卷
            
        Raises:
            SandboxNotFoundError: 容器不存在
        """
        try:
            logger.info(f"删除容器: {container_id[:12] if len(container_id) > 12 else container_id}")
            container = await self._get_container(container_id)
            await self._run_in_executor(container.remove, force=force, v=v)
            logger.info(f"容器已删除: {container_id[:12] if len(container_id) > 12 else container_id}")
        except SandboxNotFoundError:
            raise
        except APIError as e:
            # 如果容器已经被删除，忽略错误
            if "No such container" in str(e):
                logger.debug(f"容器已经被删除: {container_id}")
            else:
                logger.warning(f"删除容器时出错: {e}")
    
    async def kill_container(
        self, 
        container_id: str, 
        signal: str = "SIGKILL"
    ) -> None:
        """
        强制终止容器
        
        Args:
            container_id: 容器 ID 或名称
            signal: 信号名称
            
        Raises:
            SandboxNotFoundError: 容器不存在
        """
        try:
            logger.info(f"强制终止容器: {container_id[:12] if len(container_id) > 12 else container_id}")
            container = await self._get_container(container_id)
            await self._run_in_executor(container.kill, signal=signal)
            logger.info(f"容器已终止: {container_id[:12] if len(container_id) > 12 else container_id}")
        except SandboxNotFoundError:
            raise
        except APIError as e:
            if "is not running" in str(e):
                logger.debug(f"容器已经停止: {container_id}")
            else:
                logger.warning(f"终止容器时出错: {e}")

    async def exec_command(
        self,
        container_id: str,
        command: str | List[str],
        user: Optional[str] = None,
        workdir: Optional[str] = None,
        environment: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        detach: bool = False,
    ) -> ExecResult:
        """
        在容器内执行命令
        
        Args:
            container_id: 容器 ID 或名称
            command: 要执行的命令（字符串或列表）
            user: 执行用户
            workdir: 工作目录
            environment: 环境变量
            timeout: 执行超时（秒）
            detach: 是否后台执行
            
        Returns:
            ExecResult 对象，包含退出码和输出
            
        Raises:
            SandboxNotFoundError: 容器不存在
            InternalError: 执行失败
        """
        try:
            container = await self._get_container(container_id)
            
            # 构建 exec 配置
            exec_kwargs = {
                "cmd": command if isinstance(command, list) else ["/bin/sh", "-c", command],
                "stdout": True,
                "stderr": True,
                "demux": True,  # 分离 stdout 和 stderr
            }
            
            if user:
                exec_kwargs["user"] = user
            if workdir:
                exec_kwargs["workdir"] = workdir
            if environment:
                exec_kwargs["environment"] = environment
            if detach:
                exec_kwargs["detach"] = detach
            
            logger.debug(f"在容器 {container_id[:12]} 中执行命令: {command}")
            
            # 执行命令
            if timeout:
                # 使用 asyncio.wait_for 实现超时
                result = await asyncio.wait_for(
                    self._run_in_executor(
                        container.exec_run,
                        **exec_kwargs
                    ),
                    timeout=timeout
                )
            else:
                result = await self._run_in_executor(
                    container.exec_run,
                    **exec_kwargs
                )
            
            # 解析结果
            exit_code = result.exit_code
            output = result.output
            
            # demux=True 时，output 是 (stdout, stderr) 元组
            if isinstance(output, tuple):
                stdout = (output[0] or b"").decode("utf-8", errors="replace")
                stderr = (output[1] or b"").decode("utf-8", errors="replace")
            else:
                stdout = (output or b"").decode("utf-8", errors="replace")
                stderr = ""
            
            logger.debug(f"命令执行完成，退出码: {exit_code}")
            
            return ExecResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr
            )
            
        except asyncio.TimeoutError:
            logger.warning(f"命令执行超时: {timeout}秒")
            raise InternalError(
                message=f"命令执行超时: {timeout}秒"
            )
        except SandboxNotFoundError:
            raise
        except Exception as e:
            logger.error(f"执行命令失败: {e}")
            raise InternalError(
                message=f"执行命令失败: {str(e)}",
                original_error=e
            )

    async def exec_command_stream(
        self,
        container_id: str,
        command: str | List[str],
        user: Optional[str] = None,
        workdir: Optional[str] = None,
        environment: Optional[Dict[str, str]] = None,
    ) -> AsyncIterator[tuple[bytes, bytes]]:
        """在容器内流式执行命令，按块返回 stdout/stderr。"""
        try:
            container = await self._get_container(container_id)
            exec_kwargs = {
                "cmd": command if isinstance(command, list) else ["/bin/sh", "-c", command],
                "stdout": True,
                "stderr": True,
                "demux": True,
                "stream": True,
            }
            if user:
                exec_kwargs["user"] = user
            if workdir:
                exec_kwargs["workdir"] = workdir
            if environment:
                exec_kwargs["environment"] = environment

            stream_result = await self._run_in_executor(container.exec_run, **exec_kwargs)
            iterator = stream_result.output
            while True:
                try:
                    chunk = await self._run_in_executor(next, iterator)
                except StopIteration:
                    break
                if isinstance(chunk, tuple):
                    yield chunk
                else:
                    yield chunk or b"", b""
        except SandboxNotFoundError:
            raise
        except Exception as e:
            logger.error(f"流式执行命令失败: {e}")
            raise InternalError(
                message=f"流式执行命令失败: {str(e)}",
                original_error=e,
            )

    async def get_container_status(self, container_id: str) -> ContainerInfo:
        """
        获取容器状态信息
        
        Requirements: 1.3 (查询沙箱状态 - 运行状态、创建时间)
        
        Args:
            container_id: 容器 ID 或名称
            
        Returns:
            ContainerInfo 对象
            
        Raises:
            SandboxNotFoundError: 容器不存在
        """
        container = await self._get_container(container_id)
        # 刷新容器状态
        await self._run_in_executor(container.reload)
        return self._container_to_info(container)
    
    async def get_container_stats(
        self, 
        container_id: str,
        stream: bool = False
    ) -> ContainerStats:
        """
        获取容器资源使用统计
        
        Requirements: 1.3 (查询沙箱状态 - 资源使用情况)
        
        Args:
            container_id: 容器 ID 或名称
            stream: 是否流式获取（默认 False，获取单次快照）
            
        Returns:
            ContainerStats 对象
            
        Raises:
            SandboxNotFoundError: 容器不存在
        """
        container = await self._get_container(container_id)
        
        # 获取统计信息（非流式）
        stats = await self._run_in_executor(
            container.stats,
            stream=stream
        )
        
        return self._parse_container_stats(container_id, stats)

    async def list_containers(
        self,
        all: bool = True,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[ContainerInfo]:
        """
        列出容器
        
        Args:
            all: 是否包含已停止的容器
            filters: 过滤条件
            
        Returns:
            ContainerInfo 列表
        """
        try:
            containers = await self._run_in_executor(
                self.client.containers.list,
                all=all,
                filters=filters
            )
            return [self._container_to_info(c) for c in containers]
        except Exception as e:
            logger.error(f"列出容器失败: {e}")
            return []
    
    async def container_exists(self, container_id: str) -> bool:
        """
        检查容器是否存在
        
        Args:
            container_id: 容器 ID 或名称
            
        Returns:
            容器是否存在
        """
        try:
            await self._get_container(container_id)
            return True
        except SandboxNotFoundError:
            return False
    
    async def is_container_running(self, container_id: str) -> bool:
        """
        检查容器是否正在运行
        
        Args:
            container_id: 容器 ID 或名称
            
        Returns:
            容器是否正在运行
        """
        try:
            info = await self.get_container_status(container_id)
            return info.state == ContainerState.RUNNING
        except SandboxNotFoundError:
            return False

    async def pull_image(self, image: str) -> bool:
        """
        拉取 Docker 镜像
        
        Args:
            image: 镜像名称（包含标签）
            
        Returns:
            是否成功
        """
        try:
            logger.info(f"拉取镜像: {image}")
            await self._run_in_executor(
                self.client.images.pull,
                image
            )
            logger.info(f"镜像拉取成功: {image}")
            return True
        except Exception as e:
            logger.error(f"拉取镜像失败: {e}")
            return False
    
    async def image_exists(self, image: str) -> bool:
        """
        检查镜像是否存在
        
        Args:
            image: 镜像名称
            
        Returns:
            镜像是否存在
        """
        try:
            await self._run_in_executor(
                self.client.images.get,
                image
            )
            return True
        except ImageNotFound:
            return False
        except Exception:
            return False
    
    def ping(self) -> bool:
        """
        测试 Docker daemon 连接
        
        Returns:
            连接是否正常
        """
        try:
            self.client.ping()
            return True
        except Exception as e:
            logger.error(f"Docker daemon 连接失败: {e}")
            return False

    async def _get_container(self, container_id: str) -> Container:
        """
        获取容器对象
        
        Args:
            container_id: 容器 ID 或名称
            
        Returns:
            Container 对象
            
        Raises:
            SandboxNotFoundError: 容器不存在
        """
        try:
            return await self._run_in_executor(
                self.client.containers.get,
                container_id
            )
        except NotFound:
            raise SandboxNotFoundError(sandbox_id=container_id)
        except Exception as e:
            logger.error(f"获取容器失败: {e}")
            raise SandboxNotFoundError(sandbox_id=container_id)
    
    def _container_to_info(self, container: Container) -> ContainerInfo:
        """
        将 Docker Container 对象转换为 ContainerInfo
        
        Args:
            container: Docker Container 对象
            
        Returns:
            ContainerInfo 对象
        """
        attrs = container.attrs
        state_str = attrs.get("State", {}).get("Status", "unknown")
        
        # 解析状态
        try:
            state = ContainerState(state_str)
        except ValueError:
            state = ContainerState.UNKNOWN
        
        # 解析时间
        created_str = attrs.get("Created", "")
        created_at = self._parse_docker_time(created_str)
        
        started_str = attrs.get("State", {}).get("StartedAt", "")
        started_at = self._parse_docker_time(started_str) if started_str else None
        
        return ContainerInfo(
            container_id=container.id,
            name=container.name,
            state=state,
            created_at=created_at,
            started_at=started_at,
            image=attrs.get("Config", {}).get("Image", ""),
            labels=attrs.get("Config", {}).get("Labels", {}) or {}
        )

    def _parse_container_stats(
        self, 
        container_id: str, 
        stats: Dict[str, Any]
    ) -> ContainerStats:
        """
        解析容器统计信息
        
        Args:
            container_id: 容器 ID
            stats: Docker stats API 返回的原始数据
            
        Returns:
            ContainerStats 对象
        """
        # CPU 使用率计算
        cpu_stats = stats.get("cpu_stats", {})
        precpu_stats = stats.get("precpu_stats", {})
        
        cpu_percent = 0.0
        cpu_delta = (
            cpu_stats.get("cpu_usage", {}).get("total_usage", 0) -
            precpu_stats.get("cpu_usage", {}).get("total_usage", 0)
        )
        system_delta = (
            cpu_stats.get("system_cpu_usage", 0) -
            precpu_stats.get("system_cpu_usage", 0)
        )
        
        if system_delta > 0 and cpu_delta > 0:
            num_cpus = len(cpu_stats.get("cpu_usage", {}).get("percpu_usage", [])) or 1
            cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0
        
        # 内存使用
        memory_stats = stats.get("memory_stats", {})
        memory_used = memory_stats.get("usage", 0)
        memory_limit = memory_stats.get("limit", 0)
        memory_percent = (memory_used / memory_limit * 100.0) if memory_limit > 0 else 0.0
        
        # 网络 I/O
        networks = stats.get("networks", {})
        network_rx = sum(n.get("rx_bytes", 0) for n in networks.values())
        network_tx = sum(n.get("tx_bytes", 0) for n in networks.values())
        
        # 块设备 I/O
        blkio_stats = stats.get("blkio_stats", {})
        io_service_bytes = blkio_stats.get("io_service_bytes_recursive", []) or []
        block_read = sum(
            item.get("value", 0) 
            for item in io_service_bytes 
            if item.get("op") == "Read"
        )
        block_write = sum(
            item.get("value", 0) 
            for item in io_service_bytes 
            if item.get("op") == "Write"
        )
        
        # 进程数
        pids = stats.get("pids_stats", {}).get("current", 0)
        
        return ContainerStats(
            container_id=container_id,
            cpu_percent=round(cpu_percent, 2),
            memory_used_bytes=memory_used,
            memory_limit_bytes=memory_limit,
            memory_percent=round(memory_percent, 2),
            network_rx_bytes=network_rx,
            network_tx_bytes=network_tx,
            block_read_bytes=block_read,
            block_write_bytes=block_write,
            pids=pids,
            timestamp=datetime.now()
        )

    def _parse_docker_time(self, time_str: str) -> datetime:
        """
        解析 Docker 时间字符串
        
        Docker 使用 ISO 8601 格式，但可能包含纳秒精度。
        
        Args:
            time_str: Docker 时间字符串
            
        Returns:
            datetime 对象
        """
        if not time_str or time_str == "0001-01-01T00:00:00Z":
            return datetime.min
        
        # 移除纳秒部分（Python datetime 只支持微秒）
        # 格式: 2024-01-01T12:00:00.123456789Z
        try:
            # 尝试处理带纳秒的格式
            if "." in time_str:
                base, frac = time_str.rsplit(".", 1)
                # 移除时区标识并截断到微秒
                frac = frac.rstrip("Z")[:6]
                time_str = f"{base}.{frac}"
            else:
                time_str = time_str.rstrip("Z")
            
            return datetime.fromisoformat(time_str)
        except Exception:
            return datetime.now()

    async def put_archive(
        self,
        container_id: str,
        path: str,
        data: bytes,
    ) -> bool:
        """
        通过 Docker put_archive API 将 tar 归档写入容器。

        比 echo+base64 分块注入高效得多：单次 API 调用完成全部写入，
        无需 Base64 编码/解码，无 shell ARG_MAX 限制。

        Args:
            container_id: 容器 ID
            path: 容器内的目标目录路径（文件将被解压到此目录）
            data: tar 格式的归档数据（bytes）

        Returns:
            是否成功

        Raises:
            SandboxNotFoundError: 容器不存在
            InternalError: 写入失败
        """
        try:
            container = await self._get_container(container_id)
            result = await self._run_in_executor(
                container.put_archive, path, data
            )
            return result
        except SandboxNotFoundError:
            raise
        except Exception as e:
            logger.debug(f"put_archive 不可用（只读 rootfs）: {e}")
            raise InternalError(
                message=f"put_archive 失败: {str(e)}",
                original_error=e
            )
    
    async def close(self) -> None:
        """
        关闭 Docker 客户端连接
        """
        if self._client:
            try:
                self._client.close()
                logger.debug("Docker 客户端连接已关闭")
            except Exception as e:
                logger.warning(f"关闭 Docker 客户端时出错: {e}")
            finally:
                self._client = None
    
    async def __aenter__(self) -> "DockerClient":
        """异步上下文管理器入口"""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """异步上下文管理器出口"""
        await self.close()
