"""
Router Phase 2 端到端验证：节点自注册 / 心跳 / TTL 自动摘除。

前置（由调用方准备）：
    - router 运行在 9500，静态节点 n1（或完全无静态节点）；
      未设 ROUTER_ADMIN_TOKEN 时需 ROUTER_ALLOW_INSECURE_ADMIN=true（否则注册被拒）
    - node1 运行在 9527
    - node2 以自注册模式启动：NODE2_ROUTER_URL=http://host.docker.internal:9500

运行：python -X utf8 tests/router_phase2_e2e.py
"""

import asyncio
import subprocess
import sys
import time

import httpx

ROUTER = "http://localhost:9500"

PASS: list = []
FAIL: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}  {detail}", flush=True)


async def wait_for(c: httpx.AsyncClient, predicate, timeout_s: float, interval: float = 2.0):
    """轮询 /admin/nodes 直到 predicate(nodes_dict) 为真。"""
    deadline = time.monotonic() + timeout_s
    last = {}
    while time.monotonic() < deadline:
        try:
            last = (await c.get(f"{ROUTER}/admin/nodes")).json().get("nodes", {})
            if predicate(last):
                return True, last
        except Exception:
            pass
        await asyncio.sleep(interval)
    return False, last


async def main() -> int:
    async with httpx.AsyncClient(timeout=60) as c:
        # ---- 1. 静态节点 n1 在表中（n2 可能已抢先自注册，时序不做假设） ----
        nodes = (await c.get(f"{ROUTER}/admin/nodes")).json()["nodes"]
        check(
            "静态节点 n1 存在且 kind=static",
            nodes.get("n1", {}).get("kind") == "static",
            f"nodes={list(nodes)}",
        )

        # ---- 2. node2 自注册出现（等心跳） ----
        ok, nodes = await wait_for(
            c, lambda ns: "n2" in ns and ns["n2"]["kind"] == "dynamic" and ns["n2"]["healthy"], 60
        )
        check("node2 自注册为 dynamic 且健康", ok, f"nodes={ {k: v.get('kind') for k, v in nodes.items()} }")

        # ---- 3. 动态节点可被调度（创建会话直到落在 n2） ----
        landed_n2 = ""
        created = []
        for _ in range(4):
            r = await c.post(f"{ROUTER}/sessions", json={})
            sid = r.json().get("session_id", "")
            created.append(sid)
            if sid.startswith("n2:"):
                landed_n2 = sid
                break
        check("动态节点可被调度（会话落 n2）", bool(landed_n2), f"created={created}")

        if landed_n2:
            r = await c.post(
                f"{ROUTER}/sessions/{landed_n2}/execute",
                json={"code": "print('dyn_node_ok')", "timeout": 60},
            )
            body = r.json()
            check(
                "动态节点上的会话可执行",
                body.get("success") and "dyn_node_ok" in body.get("stdout", ""),
                f"success={body.get('success')}",
            )

        for sid in created:
            try:
                await c.delete(f"{ROUTER}/sessions/{sid}")
            except Exception:
                pass

        # ---- 4. 静态节点名不可被抢占 ----
        r = await c.post(f"{ROUTER}/admin/nodes", json={"name": "n1", "url": "http://evil:1"})
        check("静态节点名抢占被拒 409", r.status_code == 409, f"status={r.status_code}")

        # ---- 5. 停止 node2 → 心跳断 → TTL 自动摘除 ----
        print("  ... 停止 node2 容器，等待 TTL 摘除（约 30~45s）", flush=True)
        subprocess.run(
            ["docker", "stop", "kerneldock-2"], capture_output=True, timeout=60
        )
        ok, nodes = await wait_for(c, lambda ns: "n2" not in ns, 75, interval=3.0)
        check("心跳断后 n2 被自动摘除", ok, f"nodes={list(nodes)}")

        # ---- 6. 摘除后 n2 前缀请求明确报错（而非挂死） ----
        r = await c.post(
            f"{ROUTER}/sessions/n2:dead-session/execute", json={"code": "1", "timeout": 10}
        )
        check("被摘除节点的前缀请求返回 404", r.status_code == 404, f"status={r.status_code}")

        # ---- 7. node2 重新启动 → 自动回归集群 ----
        subprocess.run(["docker", "start", "kerneldock-2"], capture_output=True, timeout=60)
        ok, nodes = await wait_for(
            c, lambda ns: "n2" in ns and ns["n2"]["healthy"], 90, interval=3.0
        )
        check("node2 重启后自动回归集群", ok, f"nodes={list(nodes)}")

    print(f"\n===== Router Phase2 e2e: {len(PASS)} 过 / {len(FAIL)} 败 =====", flush=True)
    if FAIL:
        print("失败项: " + ", ".join(FAIL), flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
