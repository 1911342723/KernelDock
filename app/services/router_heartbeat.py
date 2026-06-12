"""
节点 → Router 自注册心跳（分布式 Phase 2）

网关配置了 ROUTER_URL 后启动本任务：周期 POST {ROUTER_URL}/admin/nodes
把自己注册进集群；心跳断超过 router 侧 ROUTER_NODE_TTL 后被自动摘除。

环境变量（部署拓扑配置，独立于 SANDBOX_* 业务配置）：
    ROUTER_URL                router 地址（如 http://10.0.0.100:9500）；未设置则不启动心跳。
                              **支持逗号分隔多个**——多副本 router（各自独立维护节点表）
                              时必须把每个副本都列上（如 K8s 下两个 router pod 的稳定 DNS），
                              否则只有收到心跳的那个副本认识本节点
    NODE_ADVERTISE_URL        router 访问本节点用的地址（如 http://10.0.0.3:9527）。
                              必须是 router 可达的地址，容器内 hostname 通常不可达，
                              所以跨机部署必填；未设置时回退 http://{hostname}:8080
    NODE_NAME                 节点名（成为资源 ID 前缀），默认容器 hostname（小写化处理）
    ROUTER_ADMIN_TOKEN        router 管理令牌（router 设置了 ROUTER_ADMIN_TOKEN 时必填）
    ROUTER_HEARTBEAT_INTERVAL 心跳间隔秒数，默认 10（应小于 router 的 ROUTER_NODE_TTL/2）
"""

import asyncio
import logging
import os
import re
import socket
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_NAME_SANITIZE_RE = re.compile(r"[^a-z0-9_-]")


def _node_name() -> str:
    raw = os.environ.get("NODE_NAME") or socket.gethostname()
    name = _NAME_SANITIZE_RE.sub("-", raw.lower())
    return name or "node"


def parse_router_urls(raw: str) -> List[str]:
    """解析逗号分隔的 router 地址列表（去尾斜杠、去重、保持顺序）。"""
    urls: List[str] = []
    for part in (raw or "").split(","):
        url = part.strip().rstrip("/")
        if url and url not in urls:
            urls.append(url)
    return urls


class RouterHeartbeat:
    """周期向全部 router 副本注册自身；router 不可达只告警不影响节点本身服务。"""

    def __init__(self) -> None:
        self.router_urls = parse_router_urls(os.environ.get("ROUTER_URL", ""))
        self.node_name = _node_name()
        self.advertise_url = (
            os.environ.get("NODE_ADVERTISE_URL")
            or f"http://{socket.gethostname()}:8080"
        ).strip().rstrip("/")
        self.admin_token = (os.environ.get("ROUTER_ADMIN_TOKEN") or "").strip()
        self.interval = float(os.environ.get("ROUTER_HEARTBEAT_INTERVAL", "10"))
        self._task: Optional[asyncio.Task] = None
        # 每个 router 独立计失败次数（多副本场景某个副本短暂不可达很常见）
        self._failures: Dict[str, int] = {url: 0 for url in self.router_urls}

    @property
    def enabled(self) -> bool:
        return bool(self.router_urls)

    def start(self) -> None:
        if not self.enabled:
            return
        self._task = asyncio.create_task(self._loop(), name="router-heartbeat")
        logger.info(
            f"router 心跳已启动: {self.router_urls} <- {self.node_name}={self.advertise_url} "
            f"(每 {self.interval}s)"
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _collect_load(self) -> Dict[str, int]:
        """采集本节点实时负载，随心跳上报（router 据此调度，免去反向轮询）。"""
        load = {"active_sandboxes": 0, "pool_available": 0, "pool_total": 0, "queue_load": 0}
        try:
            from .. import runtime

            mgr = runtime.sandbox_manager
            if mgr is not None:
                try:
                    load["active_sandboxes"] = int(mgr.active_count)
                except Exception:
                    pass
                pool = getattr(mgr, "_container_pool", None)
                if pool is not None:
                    try:
                        status = await pool.get_pool_status()
                        load["pool_available"] = int(status.get("available_count", status.get("available", 0)) or 0)
                        load["pool_total"] = int(status.get("total_count", status.get("total", 0)) or 0)
                    except Exception:
                        pass
            q = runtime.execution_queue
            if q is not None:
                try:
                    gs = q.get_global_status()
                    load["queue_load"] = int(gs.get("queued_count", 0)) + int(gs.get("executing_count", 0))
                except Exception:
                    pass
        except Exception:
            pass
        return load

    async def _beat_one(self, client, router_url: str, payload: dict, headers: dict) -> bool:
        try:
            resp = await client.post(
                f"{router_url}/admin/nodes", json=payload, headers=headers
            )
            if resp.status_code == 200:
                if self._failures.get(router_url, 0) > 0:
                    logger.info(f"router 心跳恢复: {router_url}")
                self._failures[router_url] = 0
                return True
            self._failures[router_url] = self._failures.get(router_url, 0) + 1
            logger.warning(
                f"router 心跳被拒（{router_url} -> {resp.status_code}）: {resp.text[:200]}"
            )
            return False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            count = self._failures.get(router_url, 0) + 1
            self._failures[router_url] = count
            # 限频告警：连续失败只在 1/6/12... 次时记日志，避免刷屏
            if count == 1 or count % 6 == 0:
                logger.warning(
                    f"router 心跳失败 x{count}（{router_url}，节点服务不受影响）: {e}"
                )
            return False

    async def _loop(self) -> None:
        import httpx

        headers = {}
        if self.admin_token:
            headers["X-Admin-Token"] = self.admin_token
        # router 短暂不可达（如重启）时缩短重试间隔，尽快重注册关闭"无节点空窗"
        retry_interval = min(2.0, self.interval)

        async with httpx.AsyncClient(timeout=5.0) as client:
            while True:
                load = await self._collect_load()
                payload = {"name": self.node_name, "url": self.advertise_url, "load": load}
                results = await asyncio.gather(
                    *(self._beat_one(client, url, payload, headers) for url in self.router_urls)
                )
                # 有任一 router 失败 → 用短间隔尽快重试（缩短重注册空窗）
                sleep_s = retry_interval if not all(results) else self.interval
                await asyncio.sleep(sleep_s)
