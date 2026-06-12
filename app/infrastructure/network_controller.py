"""
网络控制器模块

管理沙箱容器的网络隔离和访问控制。
包括隔离网络创建、容器网络连接/断开、网络策略配置等功能。

"""

import asyncio
import ipaddress
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import docker
from docker.errors import APIError, NotFound
from docker.models.networks import Network

from ..config import settings
from ..exceptions import (
    InternalError,
    NetworkAccessDeniedError,
    SandboxNotFoundError,
)

logger = logging.getLogger(__name__)


@dataclass
class NetworkPolicy:
    """
    网络策略配置数据类
    
    定义沙箱的网络访问策略，包括是否启用网络、是否允许出站流量、
    允许访问的主机列表和禁止访问的 CIDR 列表。
    
    Attributes:
        enabled: 是否启用网络（False 表示完全隔离）
        allow_outbound: 是否允许出站流量
        allowed_hosts: 允许访问的主机列表（域名或 IP）
        denied_cidrs: 禁止访问的 CIDR 列表
    """
    enabled: bool = True                           # 是否启用网络
    allow_outbound: bool = False                   # 是否允许出站流量
    allowed_hosts: List[str] = field(default_factory=list)  # 允许访问的主机列表
    denied_cidrs: List[str] = field(default_factory=list)   # 禁止访问的 CIDR 列表
    
    def __post_init__(self):
        """初始化后处理"""
        # 确保 allowed_hosts 和 denied_cidrs 是列表
        if self.allowed_hosts is None:
            self.allowed_hosts = []
        if self.denied_cidrs is None:
            self.denied_cidrs = []
    
    def is_host_allowed(self, host: str) -> bool:
        """
        检查主机是否在白名单中
        
        Args:
            host: 要检查的主机名或 IP
            
        Returns:
            True 如果主机在白名单中或白名单为空（允许所有）
        """
        # 如果不允许出站流量，直接返回 False
        if not self.allow_outbound:
            return False
        
        # 如果白名单为空，允许所有（当 allow_outbound=True 时）
        if not self.allowed_hosts:
            return True
        
        # 检查是否在白名单中
        return host in self.allowed_hosts
    
    def is_cidr_denied(self, ip_address: str) -> bool:
        """
        检查 IP 地址是否在禁止的 CIDR 范围内
        
        Args:
            ip_address: 要检查的 IP 地址
            
        Returns:
            True 如果 IP 在禁止的 CIDR 范围内
        """
        try:
            ip = ipaddress.ip_address(ip_address)
            for cidr in self.denied_cidrs:
                network = ipaddress.ip_network(cidr, strict=False)
                if ip in network:
                    return True
            return False
        except ValueError:
            # 无效的 IP 地址，默认拒绝
            return True
    
    def __str__(self) -> str:
        """返回网络策略的字符串表示"""
        return (
            f"NetworkPolicy(enabled={self.enabled}, "
            f"allow_outbound={self.allow_outbound}, "
            f"allowed_hosts={len(self.allowed_hosts)} hosts, "
            f"denied_cidrs={len(self.denied_cidrs)} cidrs)"
        )


class NetworkController:
    """
    网络控制器
    
    管理沙箱的网络隔离和访问控制。
    使用 Docker 网络实现容器间隔离，通过 iptables 规则实现访问控制。
    
    
    网络隔离策略:
    - 每个沙箱使用独立的网络命名空间
    - 默认禁止所有出站流量
    - 通过 ICC (Inter-Container Communication) 禁止容器间通信
    - 使用 internal 网络模式阻止外部访问
    """
    
    # 默认禁止访问的内部网络 CIDR
    BLOCKED_CIDRS = [
        "10.0.0.0/8",       # 私有网络 A 类
        "172.16.0.0/12",    # 私有网络 B 类（包含 Docker 默认网络 172.17.0.0/16）
        "192.168.0.0/16",   # 私有网络 C 类
        "169.254.0.0/16",   # 链路本地地址
        "127.0.0.0/8"       # 回环地址
    ]
    
    def __init__(
        self,
        network_name: str = "sandbox_network",
        default_allow_outbound: bool = False,
        docker_socket: Optional[str] = None
    ):
        """
        初始化网络控制器
        
        Args:
            network_name: 沙箱网络名称（默认 "sandbox_network"）
            default_allow_outbound: 默认是否允许出站流量（默认 False）
            docker_socket: Docker socket 路径（可选，默认使用 settings 配置）
        """
        self._network_name = network_name
        self._default_allow_outbound = default_allow_outbound
        self._docker_socket = docker_socket or settings.docker_socket
        self._client: Optional[docker.DockerClient] = None
        self._network_id: Optional[str] = None
        self._lock = asyncio.Lock()
        
        logger.info(
            f"网络控制器初始化，网络名称: {self._network_name}, "
            f"默认允许出站: {self._default_allow_outbound}"
        )
    
    @classmethod
    def from_settings(cls) -> "NetworkController":
        """
        从 settings 配置创建网络控制器
        
        Returns:
            NetworkController 实例
        """
        return cls(
            network_name=settings.network_name,
            default_allow_outbound=settings.network.default_allow_outbound,
            docker_socket=settings.docker_socket
        )
    
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
                    base_url=self._docker_socket,
                    timeout=max(5, min(settings.timeout.sandbox_startup_timeout, 10)),
                )
                probe_client.ping()
                probe_client.close()
                self._client = docker.DockerClient(base_url=self._docker_socket)
                logger.debug("网络控制器 Docker 客户端连接成功")
            except Exception as e:
                logger.error(f"无法连接到 Docker daemon: {e}")
                raise InternalError(
                    message=f"无法连接到 Docker daemon: {self._docker_socket}",
                    original_error=e
                )
        return self._client
    
    @property
    def network_name(self) -> str:
        """获取网络名称"""
        return self._network_name
    
    @property
    def network_id(self) -> Optional[str]:
        """获取网络 ID"""
        return self._network_id
    
    @property
    def default_allow_outbound(self) -> bool:
        """获取默认出站流量策略"""
        return self._default_allow_outbound
    
    async def _run_in_executor(self, func, *args, **kwargs) -> Any:
        """
        在线程池中运行同步函数
        
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
    
    async def create_network(self) -> str:
        """
        创建隔离网络
        
        创建一个用于沙箱容器的隔离 Docker 网络。
        网络配置:
        - driver: bridge（桥接模式）
        - internal: True（禁止外部访问，Requirements 3.1）
        - enable_ipv6: False（禁用 IPv6）
        - options: com.docker.network.bridge.enable_icc=false（禁止容器间通信，Requirements 3.4）
        
        Returns:
            网络 ID
            
        Raises:
            InternalError: 网络创建失败
        """
        async with self._lock:
            try:
                # 检查网络是否已存在
                existing_network = await self._get_network_by_name(self._network_name)
                if existing_network:
                    self._network_id = existing_network.id
                    logger.info(f"使用已存在的网络: {self._network_name} ({self._network_id[:12]})")
                    return self._network_id
                
                logger.info(f"创建隔离网络: {self._network_name}")
                
                # 创建网络配置
                # - 3.1: internal=True 禁止外部访问
                # - 3.4: enable_icc=false 禁止容器间通信
                ipam_config = docker.types.IPAMConfig(
                    pool_configs=[
                        docker.types.IPAMPool(
                            subnet="172.28.0.0/16",
                            gateway="172.28.0.1"
                        )
                    ]
                )
                
                network = await self._run_in_executor(
                    self.client.networks.create,
                    name=self._network_name,
                    driver="bridge",
                    internal=True,
                    enable_ipv6=False,
                    ipam=ipam_config,
                    options={
                        "com.docker.network.bridge.enable_icc": "false",
                        "com.docker.network.bridge.enable_ip_masquerade": "false",
                    },
                    labels={
                        "sandbox.network": "true",
                        "sandbox.isolated": "true"
                    }
                )
                
                self._network_id = network.id
                logger.info(f"隔离网络创建成功: {self._network_name} ({self._network_id[:12]})")
                return self._network_id
                
            except APIError as e:
                logger.error(f"创建网络失败: {e}")
                raise InternalError(
                    message=f"创建网络失败: {e.explanation}",
                    original_error=e
                )
            except Exception as e:
                logger.error(f"创建网络时发生错误: {e}")
                raise InternalError(
                    message=f"创建网络失败: {str(e)}",
                    original_error=e
                )
    
    async def delete_network(self) -> bool:
        """
        删除隔离网络
        
        删除由此控制器创建的隔离网络。
        
        Returns:
            True 如果删除成功，False 如果网络不存在
            
        Raises:
            InternalError: 删除失败（如网络仍有容器连接）
        """
        async with self._lock:
            try:
                network = await self._get_network_by_name(self._network_name)
                if not network:
                    logger.debug(f"网络不存在: {self._network_name}")
                    return False
                
                logger.info(f"删除网络: {self._network_name}")
                await self._run_in_executor(network.remove)
                self._network_id = None
                logger.info(f"网络删除成功: {self._network_name}")
                return True
                
            except APIError as e:
                if "has active endpoints" in str(e):
                    logger.warning(f"网络仍有活跃连接，无法删除: {self._network_name}")
                    raise InternalError(
                        message="网络仍有活跃连接，无法删除",
                        original_error=e
                    )
                logger.error(f"删除网络失败: {e}")
                raise InternalError(
                    message=f"删除网络失败: {e.explanation}",
                    original_error=e
                )
    
    async def connect_container(
        self,
        container_id: str,
        policy: Optional[NetworkPolicy] = None
    ) -> None:
        """
        将容器连接到隔离网络
        
        根据网络策略将容器连接到沙箱网络。
        如果策略禁用网络，则不进行连接。
        
        Args:
            container_id: 容器 ID 或名称
            policy: 网络策略配置（可选，默认使用默认策略）
            
        Raises:
            SandboxNotFoundError: 容器不存在
            InternalError: 连接失败
        """
        # 使用默认策略
        if policy is None:
            policy = self.get_default_policy()
        
        # 如果网络被禁用，不进行连接
        if not policy.enabled:
            logger.info(f"容器 {container_id[:12]} 网络已禁用，跳过连接")
            return
        
        try:
            # 确保网络存在
            if not self._network_id:
                await self.create_network()
            
            network = await self._get_network_by_id(self._network_id)
            if not network:
                raise InternalError(message=f"网络不存在: {self._network_id}")
            
            logger.info(f"将容器 {container_id[:12]} 连接到网络 {self._network_name}")
            
            # 连接容器到网络
            await self._run_in_executor(
                network.connect,
                container_id
            )
            
            logger.info(f"容器 {container_id[:12]} 已连接到网络 {self._network_name}")
            
            # 记录网络策略信息
            logger.debug(f"容器 {container_id[:12]} 网络策略: {policy}")
            
        except NotFound:
            raise SandboxNotFoundError(sandbox_id=container_id)
        except APIError as e:
            if "already exists" in str(e).lower():
                logger.debug(f"容器 {container_id[:12]} 已连接到网络")
                return
            logger.error(f"连接容器到网络失败: {e}")
            raise InternalError(
                message=f"连接容器到网络失败: {e.explanation}",
                original_error=e
            )
        except Exception as e:
            logger.error(f"连接容器时发生错误: {e}")
            raise InternalError(
                message=f"连接容器失败: {str(e)}",
                original_error=e
            )
    
    async def disconnect_container(self, container_id: str) -> None:
        """
        断开容器的网络连接
        
        将容器从沙箱网络断开。
        
        Args:
            container_id: 容器 ID 或名称
            
        Raises:
            SandboxNotFoundError: 容器不存在
        """
        try:
            network = await self._get_network_by_name(self._network_name)
            if not network:
                logger.debug(f"网络不存在: {self._network_name}")
                return
            
            logger.info(f"断开容器 {container_id[:12]} 的网络连接")
            
            await self._run_in_executor(
                network.disconnect,
                container_id,
                force=True
            )
            
            logger.info(f"容器 {container_id[:12]} 已断开网络连接")
            
        except NotFound:
            # 容器可能已经被删除或未连接
            logger.debug(f"容器 {container_id[:12]} 未连接到网络或不存在")
        except APIError as e:
            if "is not connected" in str(e).lower():
                logger.debug(f"容器 {container_id[:12]} 未连接到网络")
                return
            logger.warning(f"断开容器网络连接时出错: {e}")
        except Exception as e:
            logger.warning(f"断开容器网络连接时发生错误: {e}")
    
    def get_default_policy(self) -> NetworkPolicy:
        """
        获取默认网络策略
        
        基于 settings 配置创建默认网络策略。
        
        Returns:
            NetworkPolicy 对象
        """
        return NetworkPolicy(
            enabled=True,
            allow_outbound=self._default_allow_outbound,
            allowed_hosts=list(settings.network.allowed_hosts),
            denied_cidrs=list(settings.network.blocked_cidrs)
        )
    
    def get_network_config(self, policy: Optional[NetworkPolicy] = None) -> Dict[str, Any]:
        """
        获取 Docker 网络配置
        
        根据网络策略生成 Docker 容器创建时使用的网络配置。
        
        Args:
            policy: 网络策略配置（可选，默认使用默认策略）
            
        Returns:
            Docker 网络配置字典
        """
        if policy is None:
            policy = self.get_default_policy()
        
        # 如果网络被禁用，使用 none 网络模式
        if not policy.enabled:
            return {
                "network_mode": "none"
            }
        
        # 如果不允许出站流量，使用 internal 网络
        if not policy.allow_outbound:
            return {
                "network_mode": "none"  # 完全隔离
            }
        
        # 允许出站流量时，使用自定义网络
        # 但仍然需要通过 iptables 规则限制访问
        return {
            "network_mode": self._network_name if self._network_id else "bridge"
        }
    
    def get_network_mode_for_container(
        self, 
        policy: Optional[NetworkPolicy] = None
    ) -> str:
        """
        获取容器的网络模式
        
        根据网络策略返回容器应使用的网络模式。
        
        Args:
            policy: 网络策略配置（可选）
            
        Returns:
            网络模式字符串（"none", "bridge", 或网络名称）
        """
        if policy is None:
            policy = self.get_default_policy()
        
        # 网络禁用 -> none
        if not policy.enabled:
            return "none"
        
        # 不允许出站 -> none（完全隔离）
        if not policy.allow_outbound:
            return "none"
        
        # 允许出站但有白名单限制 -> 使用自定义网络
        return self._network_name
    
    def validate_network_access(
        self,
        target: str,
        policy: Optional[NetworkPolicy] = None
    ) -> bool:
        """
        验证网络访问是否被允许
        
        检查目标地址是否符合网络策略。
        
        Args:
            target: 目标地址（IP 或域名）
            policy: 网络策略配置（可选）
            
        Returns:
            True 如果访问被允许，False 如果被拒绝
        """
        if policy is None:
            policy = self.get_default_policy()
        
        # 网络禁用
        if not policy.enabled:
            logger.debug(f"网络访问被拒绝（网络禁用）: {target}")
            return False
        
        # 不允许出站流量
        if not policy.allow_outbound:
            logger.debug(f"网络访问被拒绝（出站禁用）: {target}")
            return False
        
        # 检查是否是 IP 地址
        try:
            ip = ipaddress.ip_address(target)
            # 检查是否在禁止的 CIDR 范围内
            if policy.is_cidr_denied(str(ip)):
                logger.debug(f"网络访问被拒绝（CIDR 黑名单）: {target}")
                return False
        except ValueError:
            # 不是 IP 地址，是域名
            pass
        
        # 检查白名单
        if policy.allowed_hosts and target not in policy.allowed_hosts:
            logger.debug(f"网络访问被拒绝（不在白名单）: {target}")
            return False
        
        return True
    
    def log_access_attempt(
        self,
        container_id: str,
        target: str,
        allowed: bool,
        reason: Optional[str] = None
    ) -> None:
        """
        记录网络访问尝试
        
        Requirements: 3.5 (记录访问尝试到日志)
        
        Args:
            container_id: 容器 ID
            target: 目标地址
            allowed: 是否允许
            reason: 原因（可选）
        """
        if allowed:
            logger.info(
                f"网络访问允许: 容器={container_id[:12]}, 目标={target}"
            )
        else:
            logger.warning(
                f"网络访问被拒绝: 容器={container_id[:12]}, 目标={target}, "
                f"原因={reason or '策略限制'}"
            )
    
    async def _get_network_by_name(self, name: str) -> Optional[Network]:
        """
        通过名称获取网络
        
        Args:
            name: 网络名称
            
        Returns:
            Network 对象，如果不存在则返回 None
        """
        try:
            networks = await self._run_in_executor(
                self.client.networks.list,
                names=[name]
            )
            return networks[0] if networks else None
        except Exception as e:
            logger.warning(f"获取网络失败: {e}")
            return None
    
    async def _get_network_by_id(self, network_id: str) -> Optional[Network]:
        """
        通过 ID 获取网络
        
        Args:
            network_id: 网络 ID
            
        Returns:
            Network 对象，如果不存在则返回 None
        """
        try:
            return await self._run_in_executor(
                self.client.networks.get,
                network_id
            )
        except NotFound:
            return None
        except Exception as e:
            logger.warning(f"获取网络失败: {e}")
            return None
    
    async def network_exists(self) -> bool:
        """
        检查网络是否存在
        
        Returns:
            True 如果网络存在
        """
        network = await self._get_network_by_name(self._network_name)
        return network is not None
    
    async def get_connected_containers(self) -> List[str]:
        """
        获取连接到网络的容器列表
        
        Returns:
            容器 ID 列表
        """
        try:
            network = await self._get_network_by_name(self._network_name)
            if not network:
                return []
            
            # 刷新网络信息
            await self._run_in_executor(network.reload)
            
            # 获取连接的容器
            containers = network.attrs.get("Containers", {})
            return list(containers.keys())
            
        except Exception as e:
            logger.warning(f"获取连接容器列表失败: {e}")
            return []
    
    async def close(self) -> None:
        """
        关闭网络控制器
        
        关闭 Docker 客户端连接。
        """
        if self._client:
            try:
                self._client.close()
                logger.debug("网络控制器 Docker 客户端连接已关闭")
            except Exception as e:
                logger.warning(f"关闭 Docker 客户端时出错: {e}")
            finally:
                self._client = None
    
    async def __aenter__(self) -> "NetworkController":
        """异步上下文管理器入口"""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """异步上下文管理器出口"""
        await self.close()


def create_network_policy(
    enabled: bool = True,
    allow_outbound: bool = False,
    allowed_hosts: Optional[List[str]] = None,
    denied_cidrs: Optional[List[str]] = None
) -> NetworkPolicy:
    """
    创建网络策略的便捷函数
    
    Args:
        enabled: 是否启用网络
        allow_outbound: 是否允许出站流量
        allowed_hosts: 允许访问的主机列表
        denied_cidrs: 禁止访问的 CIDR 列表（默认使用 BLOCKED_CIDRS）
        
    Returns:
        NetworkPolicy 对象
    """
    if denied_cidrs is None:
        denied_cidrs = list(NetworkController.BLOCKED_CIDRS)
    
    return NetworkPolicy(
        enabled=enabled,
        allow_outbound=allow_outbound,
        allowed_hosts=allowed_hosts or [],
        denied_cidrs=denied_cidrs
    )
