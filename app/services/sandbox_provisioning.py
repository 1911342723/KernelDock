"""
沙箱供给 Mixin：容器创建、kernel 直连网络准备、kernel 就绪等待、
出站网络策略与容器池配置。

由 SandboxManager 组合使用；方法内通过 self 访问管理器持有的组件。
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict

from ..config import resolve_instance_id, settings

if TYPE_CHECKING:
    from ..infrastructure.docker_client import DockerClient
    from ..infrastructure.network_controller import NetworkController, NetworkPolicy
    from ..infrastructure.resource_limiter import ResourceLimiter, ResourceLimits
    from ..infrastructure.security_policy import SecurityPolicy

logger = logging.getLogger(__name__)


class SandboxProvisioningMixin:
    """SandboxManager 的容器供给职责。"""

    if TYPE_CHECKING:
        SANDBOX_LABEL_KEY: str
        SANDBOX_LABEL_VALUE: str
        _docker_client: "DockerClient"
        _network_controller: "NetworkController"
        _resource_limiter: "ResourceLimiter"
        _security_policy: "SecurityPolicy"
        _docker_image: str

    async def _create_container(
        self,
        sandbox_id: str,
        resource_limits: "ResourceLimits",
        network_policy: "NetworkPolicy",
        data_dir: str,
        output_dir: str,
        data_dir_readonly: bool = False,
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
        # DockerClient.create_container 不接受 privileged 参数
        security_kwargs.pop("privileged", None)

        # 获取 tmpfs 配置
        tmpfs_config = self._security_policy.get_tmpfs_config()

        # 获取网络模式
        network_mode = self._network_controller.get_network_mode_for_container(network_policy)
        # 原本完全禁网（none）的容器按出站策略调整：
        # - egress_mode=proxy：接沙箱网络 + 注入代理环境变量（与池容器一致，
        #   session 内 pip install 依赖此通路）
        # - kernel_transport=direct：接 internal 网络（无外网路由）
        extra_environment: Dict[str, Any] = {}
        if network_mode == "none":
            egress_kwargs = dict(self._get_egress_kwargs())
            network_mode = egress_kwargs.pop("network_mode", "none")
            extra_environment = egress_kwargs.pop("environment", {}) or {}

        # 卷挂载配置
        volumes = {
            data_dir: {"bind": "/data", "mode": "ro" if data_dir_readonly else "rw"},
            output_dir: {"bind": "/output", "mode": "rw"},
        }

        # 容器标签（含实例 ID：多实例共享 daemon 时清理只清本实例，防误删）
        labels = {
            self.SANDBOX_LABEL_KEY: self.SANDBOX_LABEL_VALUE,
            self.INSTANCE_LABEL_KEY: resolve_instance_id(),
            "sandbox.id": sandbox_id,
            "sandbox.created_at": datetime.now().isoformat(),
        }

        # gVisor 运行时配置
        runtime = None
        if settings.security.use_gvisor:
            runtime = settings.security.gvisor_runtime
            logger.info(f"使用 gVisor 运行时: {runtime}")

        # 创建容器
        create_kwargs: Dict[str, Any] = {}
        if extra_environment:
            create_kwargs["environment"] = extra_environment
        # sandbox_id 本身即 "sandbox-{hex12}"，直接作容器名。
        # 切忌再做 [:8] 之类截断——截出来全是 "sandbox-" 常量，
        # 同一 daemon 上第二个会话沙箱必然撞名 409。
        container_info = await self._docker_client.create_container(
            image=self._docker_image,
            name=sandbox_id,
            command=None,  # 使用 Dockerfile CMD（kernel_server）
            labels=labels,
            volumes=volumes,
            network_mode=network_mode,
            tmpfs=tmpfs_config,
            working_dir="/home/sandbox",
            runtime=runtime,  # gVisor 支持
            **create_kwargs,
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

        # 等待 kernel 就绪：否则会话首次执行（往往是注入初始变量的 init）
        # 会静默回退到一次性 docker exec 进程，变量根本没进 kernel
        # 命名空间，后续执行全部 NameError。
        await self._wait_kernel_ready(container_info.container_id, timeout_seconds=45)

        return container_info.container_id

    @property
    def _kernel_network_name(self) -> str:
        """沙箱互联 internal 网络名（kernel 直连 + egress 代理共用）。"""
        return f"{settings.network_name}-kernel"

    async def _setup_sandbox_networks(self) -> None:
        """
        沙箱互联网络准备（direct 直连与 egress proxy 两个场景共用）：

        1. 创建 internal bridge 网络（无外网路由，沙箱仍然出不了网）；
        2. direct 模式：把网关自身容器接入（容器内 hostname 即容器 ID），
           网关不在容器里跑（本地开发）时失败——届时直连不可达，
           executor 自动回退 relay，功能不受影响；
        3. egress proxy 模式：把白名单代理容器也接入该网络。
           注意不能用 settings.network_name 那张网（icc=false 容器间互断，
           沙箱根本连不到代理）——必须用本网络（icc 互通，internal 无外路由，
           代理靠它在 compose 网络上的另一条腿出网）。
        """
        use_direct = settings.kernel_transport == "direct"
        use_proxy = getattr(settings.network, "egress_mode", "none") == "proxy"
        if not (use_direct or use_proxy):
            return

        ok = await self._docker_client.ensure_internal_network(self._kernel_network_name)
        if not ok:
            logger.warning("沙箱互联网络创建失败：direct 将回退 relay，egress 代理不可达")
            return

        if use_direct:
            import socket as _socket
            self_id = _socket.gethostname()
            connected = await self._docker_client.connect_container_to_network(
                self_id, self._kernel_network_name
            )
            if connected:
                logger.info(f"网关已接入 kernel 直连网络: {self._kernel_network_name}")
            else:
                logger.warning(
                    "网关自身接入 kernel 直连网络失败（本地开发模式？），"
                    "执行将自动回退 relay"
                )

        if use_proxy:
            from urllib.parse import urlsplit

            proxy_host = urlsplit(settings.network.egress_proxy_url).hostname or ""
            if proxy_host and proxy_host not in ("localhost", "127.0.0.1"):
                connected = await self._docker_client.connect_container_to_network(
                    proxy_host, self._kernel_network_name
                )
                if connected:
                    logger.info(
                        f"egress 代理 {proxy_host} 已接入沙箱网络: {self._kernel_network_name}"
                    )
                else:
                    logger.warning(
                        f"egress 代理 {proxy_host} 接入沙箱网络失败"
                        "（代理容器未运行？外部代理可忽略），pip 装包等出站功能将不可用"
                    )

    async def _wait_kernel_ready(
        self, container_id: str, timeout_seconds: int = 45
    ) -> bool:
        """轮询容器内 kernel TCP 端口直到可 ping 通（与容器池就绪检查一致）。"""
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
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                result = await self._docker_client.exec_command(
                    container_id, ping_cmd, timeout=5
                )
                if result.exit_code == 0:
                    return True
            except Exception:
                pass
            await asyncio.sleep(1.0)
        logger.warning(f"等待 kernel 就绪超时: {container_id[:12]}")
        return False

    def _get_egress_kwargs(self) -> Dict[str, Any]:
        """
        出站网络策略参数。

        egress_mode=none（默认）：network_mode=none，完全禁网
        （direct 传输时接 kernel 互联网络，internal 无外网路由）。
        egress_mode=proxy：接入 kernel 互联网络（icc 互通才能到达代理；
        settings.network_name 那张网 icc=false 不可用于此场景），
        HTTP(S)_PROXY 指向白名单代理（compose profile 'egress' 提供
        tinyproxy，白名单在代理侧维护），实现"可控放行 pip 源/对象存储"
        而非一刀切禁网。网络本身 internal=True，绕过代理无法直接出网。
        """
        network_cfg = settings.network
        if getattr(network_cfg, "egress_mode", "none") != "proxy":
            if settings.kernel_transport == "direct":
                # direct 模式：接 internal 网络（无外网路由），网关可 TCP 直连 kernel
                return {"network_mode": self._kernel_network_name}
            return {"network_mode": "none"}

        proxy_url = network_cfg.egress_proxy_url
        return {
            "network_mode": self._kernel_network_name,
            "environment": {
                "HTTP_PROXY": proxy_url,
                "HTTPS_PROXY": proxy_url,
                "http_proxy": proxy_url,
                "https_proxy": proxy_url,
                "NO_PROXY": "localhost,127.0.0.1",
                "PIP_PROXY": proxy_url,
            },
        }

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
        # DockerClient.create_container 不接受 privileged 参数
        security_kwargs.pop("privileged", None)

        # 池容器用于无状态执行，没有 volume 挂载
        # 需要将 /data 和 /output 也加入 tmpfs 使其可写
        tmpfs_config = self._security_policy.get_tmpfs_config()
        tmpfs_config["/data"] = "size=500M,mode=1777,uid=1000,gid=1000"
        tmpfs_config["/output"] = "size=500M,mode=1777,uid=1000,gid=1000"
        tmpfs_config["/var/cache/fontconfig"] = "size=10M,mode=1777,uid=1000,gid=1000"

        egress_kwargs = self._get_egress_kwargs()
        # 容器内 fork 并发上限与共享租约配置联动
        environment = {
            **egress_kwargs.pop("environment", {}),
            "KERNEL_MAX_FORKS": str(settings.pool.shared_max_per_container),
        }

        config: Dict[str, Any] = {
            **resource_kwargs,
            **security_kwargs,
            "tmpfs": tmpfs_config,
            **egress_kwargs,  # 默认 network_mode=none
            "environment": environment,
        }
        # gVisor：池容器与 session 容器保持一致的运行时隔离
        if settings.security.use_gvisor:
            config["runtime"] = settings.security.gvisor_runtime
        return config
