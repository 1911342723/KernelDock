"""
KernelDock Router — 多节点分布式部署的无状态路由网关（Phase 1 MVP）

设计（详见 deploy/distributed-design.md）：
- 会话 = 节点本地容器（不可迁移），分布式 = 把请求路由到拥有该资源的节点；
- 资源 ID 前缀路由：创建类响应里的 session_id/job_id/sandbox_id 改写为
  "{node}:{id}"，后续请求按前缀直达节点（代理时剥掉前缀）。ID 对客户端
  是不透明字符串，改写完全透明；
- router 自身零状态：不存映射表、不依赖外部存储，多开实例 + DNS/VIP 即高可用；
- 无状态执行（/execute、/execute/shell、无 session 的 /jobs）按队列水位
  分发到最闲的健康节点——这是并发扩容的主要收益来源。

配置（环境变量）：
    ROUTER_NODES            静态节点表，"n1=http://10.0.0.1:9527,n2=http://10.0.0.2:9527"；
                            节点名只能含 [a-z0-9_-]（成为 ID 前缀）。
                            可留空——节点带 ROUTER_URL 启动后自注册（Phase 2）
    ROUTER_PORT             监听端口，默认 9500
    ROUTER_HEALTH_INTERVAL  节点健康轮询秒数，默认 2.0
    ROUTER_UPSTREAM_TIMEOUT 转发上游读超时秒数，默认 660
    ROUTER_NODE_TTL         动态注册节点的心跳过期秒数，默认 30（超时自动摘除）
    ROUTER_ADMIN_TOKEN      /admin/* 写操作令牌（X-Admin-Token）。安全默认：未设置时
                            写操作（注册/摘除）被拒绝；读（查询）放行
    ROUTER_ALLOW_INSECURE_ADMIN  =true 时允许无令牌写（本地开发/可信内网）

节点自注册（Phase 2）：
    节点网关配置 ROUTER_URL=http://router:9500 + NODE_ADVERTISE_URL=http://本机IP:9527
    （可选 NODE_NAME，默认容器 hostname）后启动即自动入集群、周期心跳；
    心跳断 ROUTER_NODE_TTL 秒后自动摘除，其上会话按"节点宕机"语义处理。

运行：
    pip install -r requirements-router.txt
    ROUTER_NODES=... python router/kerneldock_router.py
"""

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

logger = logging.getLogger("kerneldock.router")

NODE_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
# sandboxID 是 E2B 风格适配层的 camelCase 字段
ID_KEYS = ("session_id", "job_id", "sandbox_id", "sandboxID")
# 客户端可见 ID："{node}:{原始id}"
PREFIX_SEP = ":"

# 不应转发的 hop-by-hop 头（小写）
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length", "content-encoding",
}


def _parse_nodes(raw: str) -> Dict[str, str]:
    """解析静态节点表；允许为空（节点可通过 /admin/nodes 自注册加入）。"""
    nodes: Dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"ROUTER_NODES 条目格式应为 name=url: {part!r}")
        name, url = part.split("=", 1)
        name = name.strip()
        if not NODE_NAME_RE.match(name):
            raise ValueError(f"节点名只能含 [a-z0-9_-]: {name!r}")
        nodes[name] = url.strip().rstrip("/")
    return nodes


def split_prefixed_id(value: str, known_nodes: Dict[str, str]) -> Tuple[Optional[str], str]:
    """"n1:xxx" → ("n1", "xxx")；前缀不是已知节点时原样返回 (None, value)。"""
    if PREFIX_SEP in value:
        node, rest = value.split(PREFIX_SEP, 1)
        if node in known_nodes and rest:
            return node, rest
    return None, value


def reprefix_ids(obj: Any, node: str) -> Any:
    """递归把 JSON 里的 session_id/job_id/sandbox_id 字符串值加上节点前缀。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ID_KEYS and isinstance(v, str) and v and not v.startswith(f"{node}{PREFIX_SEP}"):
                out[k] = f"{node}{PREFIX_SEP}{v}"
            else:
                out[k] = reprefix_ids(v, node)
        return out
    if isinstance(obj, list):
        return [reprefix_ids(item, node) for item in obj]
    return obj


def strip_id_prefixes(obj: Any, known_nodes: Dict[str, str]) -> Tuple[Any, Optional[str]]:
    """
    递归剥掉请求体中 ID 字段的节点前缀。
    返回 (清洗后的对象, 第一个发现的节点名)——用于"跟随 session 的任务提交"。
    """
    found: List[str] = []

    def walk(o: Any) -> Any:
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if k in ID_KEYS and isinstance(v, str) and v:
                    node, raw = split_prefixed_id(v, known_nodes)
                    if node:
                        found.append(node)
                        out[k] = raw
                        continue
                out[k] = walk(v)
            return out
        if isinstance(o, list):
            return [walk(item) for item in o]
        return o

    cleaned = walk(obj)
    return cleaned, (found[0] if found else None)


_METRIC_LINE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(.+)$")


def inject_node_label(metrics_text: str, node: str, seen_meta: set) -> str:
    """把 Prometheus 文本里的每条样本注入 node label；HELP/TYPE 全局去重。"""
    out_lines: List[str] = []
    for line in metrics_text.splitlines():
        if not line.strip():
            continue
        if line.startswith("#"):
            if line not in seen_meta:
                seen_meta.add(line)
                out_lines.append(line)
            continue
        m = _METRIC_LINE_RE.match(line)
        if not m:
            out_lines.append(line)
            continue
        name, labels, value = m.group(1), m.group(2), m.group(3)
        inner = labels[1:-1] if labels else ""
        merged = f'node="{node}"' + (f",{inner}" if inner else "")
        out_lines.append(f"{name}{{{merged}}} {value}")
    return "\n".join(out_lines)


class RouterMetrics:
    """router 自身的轻量计数器（不引入 prometheus_client，零额外依赖）。"""

    def __init__(self) -> None:
        self.schedule_total: Dict[Tuple[str, str], int] = {}   # (node, kind) -> n
        self.no_node_total: Dict[str, int] = {}                # kind -> n
        self.node_expired_total = 0
        self.node_registered_total = 0

    def record_schedule(self, node: str, kind: str) -> None:
        key = (node, kind)
        self.schedule_total[key] = self.schedule_total.get(key, 0) + 1

    def record_no_node(self, kind: str) -> None:
        self.no_node_total[kind] = self.no_node_total.get(kind, 0) + 1

    def render(self, nodes: Dict[str, "NodeState"]) -> str:
        lines: List[str] = []
        lines.append("# HELP router_up router 是否运行")
        lines.append("# TYPE router_up gauge")
        lines.append("router_up 1")

        healthy = sum(1 for n in nodes.values() if n.healthy)
        dynamic = sum(1 for n in nodes.values() if n.dynamic)
        lines.append("# HELP router_nodes_total router 已知节点数")
        lines.append("# TYPE router_nodes_total gauge")
        lines.append(f"router_nodes_total {len(nodes)}")
        lines.append("# HELP router_nodes_healthy 健康节点数")
        lines.append("# TYPE router_nodes_healthy gauge")
        lines.append(f"router_nodes_healthy {healthy}")
        lines.append("# HELP router_nodes_dynamic 动态（自注册）节点数")
        lines.append("# TYPE router_nodes_dynamic gauge")
        lines.append(f"router_nodes_dynamic {dynamic}")

        lines.append("# HELP router_schedule_total 调度到各节点的请求数")
        lines.append("# TYPE router_schedule_total counter")
        for (node, kind), n in sorted(self.schedule_total.items()):
            lines.append(f'router_schedule_total{{node="{node}",kind="{kind}"}} {n}')

        lines.append("# HELP router_no_healthy_node_total 无健康节点导致拒绝的请求数")
        lines.append("# TYPE router_no_healthy_node_total counter")
        for kind, n in sorted(self.no_node_total.items()):
            lines.append(f'router_no_healthy_node_total{{kind="{kind}"}} {n}')

        lines.append("# HELP router_node_expired_total 心跳超时摘除的动态节点累计")
        lines.append("# TYPE router_node_expired_total counter")
        lines.append(f"router_node_expired_total {self.node_expired_total}")

        lines.append("# HELP router_node_registered_total 动态节点注册（新增）累计")
        lines.append("# TYPE router_node_registered_total counter")
        lines.append(f"router_node_registered_total {self.node_registered_total}")
        return "\n".join(lines)


class NodeState:
    def __init__(self, name: str, url: str, dynamic: bool = False):
        self.name = name
        self.url = url
        self.dynamic = dynamic            # True=自注册节点（心跳 TTL 管理）
        self.last_heartbeat: float = time.time()
        self.healthy = False
        self.consecutive_failures = 0
        self.active_sandboxes = 0
        self.pool_available = 0
        self.pool_total = 0
        self.queue_load = 0          # running + queued
        self.last_check: float = 0.0
        self.last_error: str = ""

    def summary(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "kind": "dynamic" if self.dynamic else "static",
            "healthy": self.healthy,
            "active_sandboxes": self.active_sandboxes,
            "pool_available": self.pool_available,
            "pool_total": self.pool_total,
            "queue_load": self.queue_load,
            "last_heartbeat_age": round(time.time() - self.last_heartbeat, 1) if self.dynamic else None,
            "last_error": self.last_error or None,
        }


class Router:
    def __init__(
        self,
        nodes: Dict[str, str],
        health_interval: float,
        upstream_timeout: float,
        node_ttl: float = 30.0,
    ):
        self.nodes: Dict[str, NodeState] = {
            name: NodeState(name, url) for name, url in nodes.items()
        }
        self.node_urls = dict(nodes)
        self.health_interval = health_interval
        self.node_ttl = node_ttl
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=upstream_timeout, write=30.0, pool=10.0),
            limits=httpx.Limits(max_connections=512, max_keepalive_connections=64),
        )
        self._rr_counter = 0
        self._monitor_task: Optional[asyncio.Task] = None
        # 优雅停机：置位后 /health 返回 503，让 LB/K8s 把本副本移出轮转，
        # 在途请求由 uvicorn graceful shutdown 收尾（production-hardening #5）
        self.draining = False
        self.metrics = RouterMetrics()

    # ---------- 动态节点注册 / 心跳 / 过期（Phase 2） ----------

    @staticmethod
    def _apply_load(node: "NodeState", load: Optional[Dict[str, Any]]) -> None:
        """把心跳上报的负载写进 NodeState（心跳本身即证明节点存活）。"""
        node.healthy = True
        node.consecutive_failures = 0
        node.last_error = ""
        node.last_check = time.time()
        if not isinstance(load, dict):
            return
        try:
            node.active_sandboxes = int(load.get("active_sandboxes", node.active_sandboxes) or 0)
            node.pool_available = int(load.get("pool_available", node.pool_available) or 0)
            node.pool_total = int(load.get("pool_total", node.pool_total) or 0)
            node.queue_load = int(load.get("queue_load", node.queue_load) or 0)
        except (TypeError, ValueError):
            pass

    def register_node(
        self, name: str, url: str, load: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        注册或心跳一个动态节点（幂等）。

        - 新节点：加入路由表（dynamic=True）
        - 已存在的动态节点：刷新心跳，URL 变化时更新
        - 与静态节点重名：409（静态表来自部署配置，不允许被抢占）
        - load：心跳携带的实时负载，直接写入并标记节点健康（push 模型，
          router 无需再反向轮询动态节点的 /health + /queue/status）。
        """
        if not NODE_NAME_RE.match(name):
            raise ValueError(f"节点名只能含 [a-z0-9_-]: {name!r}")
        url = url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"节点 URL 必须是 http(s)://: {url!r}")

        existing = self.nodes.get(name)
        if existing is not None and not existing.dynamic:
            raise PermissionError(f"节点名 {name!r} 已被静态配置占用")

        if existing is None:
            node = NodeState(name, url, dynamic=True)
            self.nodes[name] = node
            self.node_urls[name] = url
            self._apply_load(node, load)
            self.metrics.node_registered_total += 1
            logger.info(f"动态节点注册: {name} -> {url}")
            return {"registered": True, "renewed": False, "ttl": self.node_ttl}

        existing.last_heartbeat = time.time()
        if existing.url != url:
            logger.info(f"动态节点 {name} URL 更新: {existing.url} -> {url}")
            existing.url = url
            self.node_urls[name] = url
        self._apply_load(existing, load)
        return {"registered": True, "renewed": True, "ttl": self.node_ttl}

    def remove_node(self, name: str) -> bool:
        """手动移除动态节点；静态节点不可移除（改 ROUTER_NODES 重启）。"""
        node = self.nodes.get(name)
        if node is None:
            return False
        if not node.dynamic:
            raise PermissionError(f"静态节点 {name!r} 不可在线移除")
        self.nodes.pop(name, None)
        self.node_urls.pop(name, None)
        logger.info(f"动态节点已移除: {name}")
        return True

    def expire_dynamic_nodes(self, now: Optional[float] = None) -> List[str]:
        """摘除心跳超时的动态节点，返回被摘除的名字列表。"""
        now = now if now is not None else time.time()
        expired = [
            name
            for name, node in self.nodes.items()
            if node.dynamic and (now - node.last_heartbeat) > self.node_ttl
        ]
        for name in expired:
            self.nodes.pop(name, None)
            self.node_urls.pop(name, None)
            self.metrics.node_expired_total += 1
            logger.warning(f"动态节点心跳超时（>{self.node_ttl}s），已摘除: {name}")
        return expired

    # ---------- 健康轮询 ----------

    async def start(self) -> None:
        await self._probe_all()
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
        await self.client.aclose()

    async def _monitor_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.health_interval)
                self.expire_dynamic_nodes()
                await self._probe_all()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"健康轮询异常: {e}")

    async def _probe_all(self) -> None:
        # 仅轮询静态节点；动态节点的存活与负载由心跳 push（去掉反向轮询，
        # 大集群下省掉 N×2 req/周期 的探活放大）。
        static_nodes = [n for n in self.nodes.values() if not n.dynamic]
        if static_nodes:
            await asyncio.gather(*(self._probe(node) for node in static_nodes))

    async def _probe(self, node: NodeState) -> None:
        try:
            health = await self.client.get(f"{node.url}/health", timeout=3.0)
            health.raise_for_status()
            body = health.json()
            node.active_sandboxes = int(body.get("active_sandboxes") or 0)
            node.pool_available = int(body.get("pool_available") or 0)
            node.pool_total = int(body.get("pool_total") or 0)
            try:
                queue = await self.client.get(f"{node.url}/queue/status", timeout=3.0)
                if queue.status_code == 200:
                    qb = queue.json()
                    # /queue/status 字段是 queued_count/executing_count（此前误用
                    # running/queued 恒为 0，static 节点调度从未拿到真实队列深度）
                    node.queue_load = int(qb.get("queued_count") or 0) + int(
                        qb.get("executing_count") or 0
                    )
            except Exception:
                pass  # 队列状态拿不到不影响健康判定
            node.healthy = True
            node.consecutive_failures = 0
            node.last_error = ""
        except Exception as e:
            node.consecutive_failures += 1
            node.last_error = str(e)
            if node.consecutive_failures >= 2:
                node.healthy = False
        node.last_check = time.time()

    # ---------- 调度 ----------

    def healthy_nodes(self) -> List[NodeState]:
        return [n for n in self.nodes.values() if n.healthy]

    def pick_for_stateless(self) -> Optional[NodeState]:
        candidates = self.healthy_nodes()
        if not candidates:
            self.metrics.record_no_node("stateless")
            return None
        self._rr_counter += 1
        chosen = min(
            candidates,
            key=lambda n: (n.queue_load, (hash(n.name) + self._rr_counter) % len(candidates)),
        )
        self.metrics.record_schedule(chosen.name, "stateless")
        return chosen

    def pick_for_session(self) -> Optional[NodeState]:
        candidates = self.healthy_nodes()
        if not candidates:
            self.metrics.record_no_node("session")
            return None
        self._rr_counter += 1
        chosen = min(
            candidates,
            key=lambda n: (n.active_sandboxes, (hash(n.name) + self._rr_counter) % len(candidates)),
        )
        self.metrics.record_schedule(chosen.name, "session")
        return chosen


ROUTER: Optional[Router] = None


def _forward_headers(request: Request) -> Dict[str, str]:
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS
    }
    # 分布式链路标识（production-hardening #11）：没有就生成，确保 router→node
    # 同一请求共享一个 X-Request-Id，跨进程日志可串联
    if "x-request-id" not in {k.lower() for k in headers}:
        import uuid as _uuid

        headers["X-Request-Id"] = f"req-{_uuid.uuid4().hex[:12]}"
    return headers


def _response_headers(upstream: httpx.Response) -> Dict[str, str]:
    return {
        k: v for k, v in upstream.headers.items() if k.lower() not in HOP_HEADERS
    }


async def _proxy_buffered(
    request: Request,
    node: NodeState,
    path: str,
    body: Optional[bytes] = None,
) -> Tuple[httpx.Response, bytes]:
    """转发并完整读取响应（用于需要 JSON 改写的端点）。"""
    upstream = await ROUTER.client.request(
        request.method,
        f"{node.url}{path}",
        params=request.query_params,
        content=body if body is not None else await request.body(),
        headers=_forward_headers(request),
    )
    return upstream, upstream.content


async def _proxy_streaming(
    request: Request,
    node: NodeState,
    path: str,
) -> StreamingResponse:
    """流式转发（SSE / 文件下载等），不缓冲。"""
    upstream_request = ROUTER.client.build_request(
        request.method,
        f"{node.url}{path}",
        params=request.query_params,
        content=await request.body(),
        headers=_forward_headers(request),
    )
    upstream = await ROUTER.client.send(upstream_request, stream=True)

    async def body_iter():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
        media_type=upstream.headers.get("content-type"),
    )


def _json_or_none(content: bytes, upstream: httpx.Response) -> Optional[Any]:
    ctype = upstream.headers.get("content-type", "")
    if "application/json" not in ctype:
        return None
    try:
        import json

        return json.loads(content)
    except Exception:
        return None


def _rewritten_response(upstream: httpx.Response, content: bytes, node: str) -> Response:
    """JSON 响应做 ID 前缀改写；非 JSON 原样返回。"""
    body = _json_or_none(content, upstream)
    if body is None:
        return Response(
            content=content,
            status_code=upstream.status_code,
            headers=_response_headers(upstream),
        )
    return JSONResponse(
        reprefix_ids(body, node),
        status_code=upstream.status_code,
        headers={
            k: v
            for k, v in _response_headers(upstream).items()
            if k.lower() != "content-type"
        },
    )


def _no_node_response() -> JSONResponse:
    return JSONResponse(
        {"detail": "没有健康的 KernelDock 节点可用"}, status_code=503
    )


def _unknown_prefix_response(resource_id: str) -> JSONResponse:
    return JSONResponse(
        {
            "detail": (
                f"资源 ID 缺少有效节点前缀: {resource_id!r}"
                "（分布式部署下请使用创建接口返回的完整 ID）"
            )
        },
        status_code=404,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ROUTER
    nodes = _parse_nodes(os.environ.get("ROUTER_NODES", ""))
    ROUTER = Router(
        nodes,
        health_interval=float(os.environ.get("ROUTER_HEALTH_INTERVAL", "2.0")),
        upstream_timeout=float(os.environ.get("ROUTER_UPSTREAM_TIMEOUT", "660")),
        node_ttl=float(os.environ.get("ROUTER_NODE_TTL", "30")),
    )
    await ROUTER.start()
    if nodes:
        logger.info(f"router 启动，静态节点: {ROUTER.node_urls}")
    else:
        logger.info("router 启动，无静态节点（等待节点自注册）")
    yield
    await ROUTER.stop()


app = FastAPI(title="KernelDock Router", lifespan=lifespan)


# ===================== 节点管理（Phase 2：自注册/心跳/摘除） =====================

def _check_admin_token(request: Request, *, write: bool = False) -> Optional[JSONResponse]:
    """
    校验管理端点令牌。

    安全默认（production-hardening #2）：写操作（注册/摘除节点）在未配置
    ROUTER_ADMIN_TOKEN 时**默认拒绝**——否则任何能访问 router 的人都能注册
    自己控制的 URL 劫持用户流量。仅当显式 ROUTER_ALLOW_INSECURE_ADMIN=true
    才放行无令牌写（本地开发/可信内网）。读操作（查询节点表）不强制令牌。
    """
    expected = (os.environ.get("ROUTER_ADMIN_TOKEN") or "").strip()
    provided = request.headers.get("X-Admin-Token", "")

    if not expected:
        if write and (os.environ.get("ROUTER_ALLOW_INSECURE_ADMIN", "").lower() != "true"):
            return JSONResponse(
                {
                    "detail": (
                        "管理写操作被拒：未配置 ROUTER_ADMIN_TOKEN。"
                        "生产请设置令牌；本地开发可设 ROUTER_ALLOW_INSECURE_ADMIN=true 放行。"
                    )
                },
                status_code=403,
            )
        return None

    import secrets

    if not secrets.compare_digest(provided, expected):
        return JSONResponse({"detail": "Invalid admin token"}, status_code=403)
    return None


@app.post("/admin/nodes")
async def register_node(request: Request):
    """节点注册 / 心跳（幂等）。body: {"name": "n3", "url": "http://10.0.0.3:9527"}"""
    denied = _check_admin_token(request, write=True)
    if denied:
        return denied
    import json

    try:
        body = json.loads(await request.body())
        name = str(body["name"]).strip()
        url = str(body["url"]).strip()
        load = body.get("load")
    except Exception:
        return JSONResponse({"detail": "body 需为 {\"name\": ..., \"url\": ...}"}, status_code=400)

    try:
        # 心跳携带 load 时直接标记健康+写负载，无需反向探活
        result = ROUTER.register_node(name, url, load=load)
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    except PermissionError as e:
        return JSONResponse({"detail": str(e)}, status_code=409)

    if not result["renewed"] and not isinstance(load, dict):
        # 老式心跳（无 load）的新节点：立即探活一次尽快进调度池
        await ROUTER._probe(ROUTER.nodes[name])
    return result


@app.get("/admin/nodes")
async def list_nodes(request: Request):
    denied = _check_admin_token(request)
    if denied:
        return denied
    return {
        "node_ttl": ROUTER.node_ttl,
        "nodes": {name: n.summary() for name, n in ROUTER.nodes.items()},
    }


@app.delete("/admin/nodes/{name}")
async def delete_node(request: Request, name: str):
    denied = _check_admin_token(request, write=True)
    if denied:
        return denied
    try:
        removed = ROUTER.remove_node(name)
    except PermissionError as e:
        return JSONResponse({"detail": str(e)}, status_code=409)
    if not removed:
        return JSONResponse({"detail": f"节点不存在: {name}"}, status_code=404)
    return {"removed": name}


@app.post("/admin/drain")
async def drain(request: Request):
    """
    优雅停机预备：置 draining → /health 503 → LB/K8s 摘除本副本，
    在途请求由 uvicorn graceful shutdown 收尾。K8s preStop 调用本端点后
    sleep 几秒再让进程收 SIGTERM。
    """
    denied = _check_admin_token(request, write=True)
    if denied:
        return denied
    ROUTER.draining = True
    logger.info("router 进入 draining 状态（/health 将返回 503）")
    return {"draining": True}


# ===================== 聚合端点 =====================

@app.get("/health")
async def aggregated_health():
    nodes = {name: n.summary() for name, n in ROUTER.nodes.items()}
    healthy = [n for n in ROUTER.nodes.values() if n.healthy]
    if ROUTER.draining:
        status = "draining"
    elif len(healthy) == len(ROUTER.nodes):
        status = "healthy"
    elif healthy:
        status = "degraded"
    else:
        status = "unhealthy"
    return JSONResponse(
        {
            "status": status,
            "service": "kerneldock-router",
            "draining": ROUTER.draining,
            "nodes_total": len(ROUTER.nodes),
            "nodes_healthy": len(healthy),
            "active_sandboxes": sum(n.active_sandboxes for n in healthy),
            "pool_available": sum(n.pool_available for n in healthy),
            "pool_total": sum(n.pool_total for n in healthy),
            "nodes": nodes,
        },
        # 排空中或无健康节点都返回 503（让 LB 摘除本副本）
        status_code=503 if (ROUTER.draining or not healthy) else 200,
    )


@app.get("/metrics")
async def aggregated_metrics(request: Request):
    seen_meta: set = set()
    # router 自身指标（转发调度/节点摘除/无节点拒绝等）置于最前
    parts: List[str] = [ROUTER.metrics.render(ROUTER.nodes)]
    for node in ROUTER.healthy_nodes():
        try:
            resp = await ROUTER.client.get(
                f"{node.url}/metrics", headers=_forward_headers(request), timeout=10.0
            )
            if resp.status_code == 200:
                parts.append(inject_node_label(resp.text, node.name, seen_meta))
        except Exception as e:
            logger.warning(f"拉取节点 {node.name} metrics 失败: {e}")
    return PlainTextResponse("\n".join(parts) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/queue/status")
@app.get("/statistics")
async def aggregated_stats(request: Request):
    path = request.url.path
    per_node: Dict[str, Any] = {}
    totals: Dict[str, Any] = {}
    for node in ROUTER.healthy_nodes():
        try:
            resp = await ROUTER.client.get(
                f"{node.url}{path}", headers=_forward_headers(request), timeout=10.0
            )
            body = resp.json()
            per_node[node.name] = body
            if isinstance(body, dict):
                for k, v in body.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        totals[k] = totals.get(k, 0) + v
        except Exception as e:
            per_node[node.name] = {"error": str(e)}
    return {"aggregate": totals, "nodes": per_node}


async def _aggregate_list(request: Request, path: str, list_key: Optional[str] = None):
    """fan-out 到全部健康节点，合并列表并加 ID 前缀。"""
    merged: List[Any] = []
    extra_totals: Dict[str, Any] = {}
    for node in ROUTER.healthy_nodes():
        try:
            resp = await ROUTER.client.get(
                f"{node.url}{path}",
                params=request.query_params,
                headers=_forward_headers(request),
                timeout=15.0,
            )
            if resp.status_code != 200:
                continue
            body = reprefix_ids(resp.json(), node.name)
            if list_key is None:
                if isinstance(body, list):
                    merged.extend(body)
            else:
                if isinstance(body, dict):
                    merged.extend(body.get(list_key) or [])
                    for k, v in body.items():
                        if isinstance(v, (int, float)) and not isinstance(v, bool):
                            extra_totals[k] = extra_totals.get(k, 0) + v
        except Exception as e:
            logger.warning(f"聚合 {path} 失败 node={node.name}: {e}")
    if list_key is None:
        return merged
    extra_totals[list_key] = merged
    extra_totals["total"] = len(merged)
    return extra_totals


@app.get("/jobs")
async def list_jobs(request: Request):
    return await _aggregate_list(request, "/jobs")


@app.get("/sandboxes")
async def list_sandboxes(request: Request):
    return await _aggregate_list(request, "/sandboxes", list_key="sandboxes")


@app.get("/e2b/sandboxes")
async def list_e2b_sandboxes(request: Request):
    return await _aggregate_list(request, "/e2b/sandboxes")


@app.get("/admin/sandboxes")
async def admin_list_sandboxes(request: Request):
    return await _aggregate_list(request, "/admin/sandboxes")


@app.post("/cleanup")
async def broadcast_cleanup(request: Request):
    results: Dict[str, Any] = {}
    body = await request.body()
    for node in ROUTER.healthy_nodes():
        try:
            resp = await ROUTER.client.post(
                f"{node.url}/cleanup",
                content=body,
                headers=_forward_headers(request),
                timeout=60.0,
            )
            results[node.name] = resp.json() if resp.status_code == 200 else {
                "status_code": resp.status_code
            }
        except Exception as e:
            results[node.name] = {"error": str(e)}
    return {"nodes": results}


# ===================== 创建类端点（选节点 + 响应 ID 改写） =====================

@app.post("/sessions")
async def create_session(request: Request):
    import json

    raw = await request.body()
    target: Optional[NodeState] = None
    body_bytes = raw
    if raw:
        try:
            cleaned, pinned = strip_id_prefixes(json.loads(raw), ROUTER.node_urls)
            if pinned:
                target = ROUTER.nodes.get(pinned)
                if target is None or not target.healthy:
                    return _no_node_response()
            body_bytes = json.dumps(cleaned, ensure_ascii=False).encode("utf-8")
        except json.JSONDecodeError:
            pass
    if target is None:
        target = ROUTER.pick_for_session()
    if target is None:
        return _no_node_response()
    upstream, content = await _proxy_buffered(request, target, "/sessions", body=body_bytes)
    return _rewritten_response(upstream, content, target.name)


@app.post("/e2b/sandboxes")
async def e2b_create_sandbox(request: Request):
    target = ROUTER.pick_for_session()
    if target is None:
        return _no_node_response()
    upstream, content = await _proxy_buffered(request, target, "/e2b/sandboxes")
    return _rewritten_response(upstream, content, target.name)


@app.post("/jobs")
async def submit_job(request: Request):
    import json

    raw = await request.body()
    target: Optional[NodeState] = None
    body_bytes = raw
    if raw:
        try:
            cleaned, pinned = strip_id_prefixes(json.loads(raw), ROUTER.node_urls)
            body_bytes = json.dumps(cleaned, ensure_ascii=False).encode("utf-8")
            if pinned:
                target = ROUTER.nodes.get(pinned)
                if target is None or not target.healthy:
                    return _no_node_response()
        except json.JSONDecodeError:
            pass
    if target is None:
        target = ROUTER.pick_for_stateless()
    if target is None:
        return _no_node_response()
    upstream, content = await _proxy_buffered(request, target, "/jobs", body=body_bytes)
    return _rewritten_response(upstream, content, target.name)


# ===================== 无状态执行（按队列水位调度） =====================

@app.post("/execute")
@app.post("/execute/shell")
async def stateless_execute(request: Request):
    target = ROUTER.pick_for_stateless()
    if target is None:
        return _no_node_response()
    upstream, content = await _proxy_buffered(request, target, request.url.path)
    return _rewritten_response(upstream, content, target.name)


# ===================== 带 ID 前缀的资源路由 =====================

def _route_by_prefix(resource_id: str) -> Tuple[Optional[NodeState], str]:
    node_name, raw_id = split_prefixed_id(resource_id, ROUTER.node_urls)
    if node_name is None:
        return None, raw_id
    return ROUTER.nodes.get(node_name), raw_id


# 子路径前缀命中即流式透传（大响应体、且响应不含需改写的 ID 字段）
_STREAMABLE_SUBPATHS = ("files/", "fs/read", "download")


def _is_streamable(request: Request, rest: str) -> bool:
    if request.headers.get("accept", "").startswith("text/event-stream"):
        return True
    return any(rest.startswith(p) for p in _STREAMABLE_SUBPATHS)


@app.api_route(
    "/sessions/{session_id:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
)
async def session_scoped(request: Request, session_id: str):
    # session_id 可能带子路径（/sessions/{id}/execute 等），先拆首段
    first, _, rest = session_id.partition("/")
    node, raw_id = _route_by_prefix(first)
    if node is None:
        return _unknown_prefix_response(first)
    if not node.healthy:
        return _no_node_response()
    path = f"/sessions/{raw_id}" + (f"/{rest}" if rest else "")
    # 大响应体端点（文件下载 / fs 读 / SSE）直接流式透传，不缓冲进内存
    # （production-hardening #4：fs/read 上限 64MB，并发缓冲会撑爆 router）。
    # 这些端点的响应体不含需要改写的 ID 字段，流式安全。
    if _is_streamable(request, rest):
        return await _proxy_streaming(request, node, path)
    upstream, content = await _proxy_buffered(request, node, path)
    return _rewritten_response(upstream, content, node.name)


@app.post("/v2/sessions/{session_id}/execute")
async def session_stream_execute(request: Request, session_id: str):
    node, raw_id = _route_by_prefix(session_id)
    if node is None:
        return _unknown_prefix_response(session_id)
    if not node.healthy:
        return _no_node_response()
    return await _proxy_streaming(request, node, f"/v2/sessions/{raw_id}/execute")


@app.api_route("/jobs/{job_id}", methods=["GET", "DELETE"])
async def job_scoped(request: Request, job_id: str):
    node, raw_id = _route_by_prefix(job_id)
    if node is None:
        return _unknown_prefix_response(job_id)
    if not node.healthy:
        return _no_node_response()
    upstream, content = await _proxy_buffered(request, node, f"/jobs/{raw_id}")
    return _rewritten_response(upstream, content, node.name)


@app.api_route("/sandboxes/{sandbox_id:path}", methods=["GET", "DELETE"])
async def sandbox_scoped(request: Request, sandbox_id: str):
    first, _, rest = sandbox_id.partition("/")
    node, raw_id = _route_by_prefix(first)
    if node is None:
        return _unknown_prefix_response(first)
    if not node.healthy:
        return _no_node_response()
    path = f"/sandboxes/{raw_id}" + (f"/{rest}" if rest else "")
    upstream, content = await _proxy_buffered(request, node, path)
    return _rewritten_response(upstream, content, node.name)


@app.api_route("/e2b/sandboxes/{sandbox_id:path}", methods=["GET", "POST", "DELETE"])
async def e2b_scoped(request: Request, sandbox_id: str):
    first, _, rest = sandbox_id.partition("/")
    node, raw_id = _route_by_prefix(first)
    if node is None:
        return _unknown_prefix_response(first)
    if not node.healthy:
        return _no_node_response()
    path = f"/e2b/sandboxes/{raw_id}" + (f"/{rest}" if rest else "")
    upstream, content = await _proxy_buffered(request, node, path)
    return _rewritten_response(upstream, content, node.name)


# ===================== WebSocket 桥接 =====================

async def _bridge_websocket(client_ws: WebSocket, upstream_url: str) -> None:
    """双向桥接客户端 WS 与上游节点 WS。"""
    import websockets

    extra_headers = {}
    for key in ("authorization", "x-api-key"):
        if key in client_ws.headers:
            extra_headers[key] = client_ws.headers[key]

    try:
        upstream = await websockets.connect(
            upstream_url,
            additional_headers=extra_headers,
            max_size=16 * 1024 * 1024,
            open_timeout=10,
        )
    except Exception as e:
        await client_ws.close(code=1014, reason=f"上游节点不可达: {e}"[:120])
        return

    await client_ws.accept()

    async def client_to_upstream():
        while True:
            message = await client_ws.receive()
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code") or 1000)
            if message.get("text") is not None:
                await upstream.send(message["text"])
            elif message.get("bytes") is not None:
                await upstream.send(message["bytes"])

    async def upstream_to_client():
        async for message in upstream:
            if isinstance(message, bytes):
                await client_ws.send_bytes(message)
            else:
                await client_ws.send_text(message)

    tasks = [
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in pending:
            task.cancel()
    finally:
        try:
            await upstream.close()
        except Exception:
            pass
        try:
            await client_ws.close()
        except Exception:
            pass


def _ws_url(node: NodeState, path: str, query: str) -> str:
    scheme = "wss" if node.url.startswith("https") else "ws"
    base = node.url.split("://", 1)[1]
    return f"{scheme}://{base}{path}" + (f"?{query}" if query else "")


@app.websocket("/ws")
async def ws_stateless(websocket: WebSocket):
    node = ROUTER.pick_for_stateless()
    if node is None:
        await websocket.close(code=1013, reason="没有健康节点")
        return
    await _bridge_websocket(
        websocket, _ws_url(node, "/ws", websocket.url.query)
    )


@app.websocket("/ws/{session_id}")
async def ws_session(websocket: WebSocket, session_id: str):
    node, raw_id = _route_by_prefix(session_id)
    if node is None or not node.healthy:
        await websocket.close(code=1014, reason="无效的会话节点前缀或节点不可用")
        return
    await _bridge_websocket(
        websocket, _ws_url(node, f"/ws/{raw_id}", websocket.url.query)
    )


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("ROUTER_PORT", "9500")),
        # SIGTERM 后给在途转发 25s 排空（配合 preStop drain + K8s grace period）
        timeout_graceful_shutdown=int(os.environ.get("ROUTER_GRACEFUL_TIMEOUT", "25")),
    )
