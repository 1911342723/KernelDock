"""
Router 分布式双节点端到端验证。

前置：
    node1: docker compose up -d                                  (9527)
    node2: docker compose -p kd-node2 -f docker-compose.node2.yml up -d  (9528)
    router: ROUTER_NODES="n1=http://localhost:9527,n2=http://localhost:9528" \
            python router/kerneldock_router.py                   (9500)

运行：python -X utf8 tests/router_e2e.py
退出码 0=全过。
"""

import asyncio
import json
import sys

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


async def main() -> int:
    async with httpx.AsyncClient(timeout=180) as c:
        # ---- 0. 等待两个节点池就绪（避免刚重启时的偶发回退） ----
        h = {}
        for _ in range(30):
            try:
                h = (await c.get(f"{ROUTER}/health")).json()
                if h.get("nodes_healthy") == 2 and h.get("pool_available", 0) >= 2:
                    break
            except Exception:
                pass
            await asyncio.sleep(2)

        # ---- 1. 聚合健康 ----
        check(
            "聚合健康 2 节点",
            h.get("nodes_healthy") == 2 and h.get("status") == "healthy",
            f"pool={h.get('pool_available')}/{h.get('pool_total')}",
        )

        # ---- 2. 无状态执行经 router 分发 ----
        results = await asyncio.gather(
            *(c.post(f"{ROUTER}/execute", json={"code": f"print({i}*7)", "timeout": 30}) for i in range(6))
        )
        ok = all(r.status_code == 200 and r.json().get("success") for r in results)
        outs = [r.json().get("stdout", "") for r in results]
        check(
            "无状态执行 x6 全成功",
            ok and all(str(i * 7) in outs[i] for i in range(6)),
            f"status={[r.status_code for r in results]}",
        )

        # ---- 3. 会话创建落到两个节点（ID 带前缀） ----
        sessions = []
        for _ in range(4):
            r = await c.post(f"{ROUTER}/sessions", json={})
            sessions.append(r.json()["session_id"])
        prefixes = {s.split(":", 1)[0] for s in sessions}
        check(
            "4 个会话 ID 均带节点前缀",
            all(":" in s and s.split(":", 1)[0] in ("n1", "n2") for s in sessions),
            f"sessions={sessions}",
        )
        check("会话分布到 2 个节点", prefixes == {"n1", "n2"}, f"prefixes={prefixes}")

        # ---- 4. 会话有状态执行（路由粘性：变量必须回到同一节点） ----
        sticky_ok = True
        for i, sid in enumerate(sessions):
            await c.post(f"{ROUTER}/sessions/{sid}/execute", json={"code": f"v = {i * 100}", "timeout": 60})
            r = await c.post(f"{ROUTER}/sessions/{sid}/execute", json={"code": "print(v + 1)", "timeout": 60})
            body = r.json()
            if not (body.get("success") and str(i * 100 + 1) in body.get("stdout", "")):
                sticky_ok = False
                print(f"    !! 会话 {sid} 粘性失败: {json.dumps(body, ensure_ascii=False)[:200]}")
        check("会话粘性路由（变量跨调用保留 x4）", sticky_ok)

        # ---- 5. 会话 shell + fs 经前缀路由 ----
        sid = sessions[0]
        r = await c.post(f"{ROUTER}/sessions/{sid}/shell", json={"command": "echo routed_shell", "timeout": 30})
        check("会话 shell 经前缀路由", r.status_code == 200 and "routed_shell" in r.json().get("stdout", ""))

        import base64
        content_b64 = base64.b64encode("router e2e".encode()).decode()
        r = await c.put(f"{ROUTER}/sessions/{sid}/fs/write", json={"path": "/data/r.txt", "content_base64": content_b64})
        r2 = await c.get(f"{ROUTER}/sessions/{sid}/fs/read", params={"path": "/data/r.txt"})
        check(
            "会话 fs 写读经前缀路由",
            r.status_code == 200 and base64.b64decode(r2.json().get("content_base64", "")).decode() == "router e2e",
        )

        # ---- 6. 流式执行（SSE 经 router 透传） ----
        async with c.stream(
            "POST", f"{ROUTER}/v2/sessions/{sid}/execute", json={"code": "print('stream_hi')", "timeout": 60}
        ) as resp:
            stream_body = (await resp.aread()).decode("utf-8", "replace")
        check(
            "v2 SSE 流式执行透传",
            resp.status_code == 200 and "stream_hi" in stream_body,
            f"len={len(stream_body)}",
        )

        # ---- 7. 任务：无 session 调度 + 带 session 跟随 ----
        r = await c.post(f"{ROUTER}/jobs", json={"code": "print('job_free')", "timeout": 60})
        job_free = r.json()["job_id"]
        r = await c.post(f"{ROUTER}/jobs", json={"code": "print(v)", "timeout": 60, "session_id": sessions[1]})
        job_pinned = r.json()
        check(
            "带 session 的任务跟随节点",
            job_pinned["job_id"].split(":", 1)[0] == sessions[1].split(":", 1)[0],
            f"job={job_pinned['job_id']} session={sessions[1]}",
        )

        async def wait_job(jid: str) -> dict:
            for _ in range(30):
                jr = (await c.get(f"{ROUTER}/jobs/{jid}")).json()
                if jr.get("status") in ("succeeded", "failed", "cancelled"):
                    return jr
                await asyncio.sleep(1)
            return {}

        j1, j2 = await asyncio.gather(wait_job(job_free), wait_job(job_pinned["job_id"]))
        check(
            "任务轮询经前缀路由完成",
            j1.get("status") == "succeeded" and "job_free" in (j1.get("result") or {}).get("stdout", "")
            and j2.get("status") == "succeeded" and "100" in (j2.get("result") or {}).get("stdout", ""),
            f"free={j1.get('status')} pinned={j2.get('status')}",
        )

        # ---- 8. 列表聚合 ----
        r = await c.get(f"{ROUTER}/jobs")
        jobs = r.json()
        check(
            "/jobs 聚合且 ID 带前缀",
            isinstance(jobs, list) and len(jobs) >= 2 and all(":" in j["job_id"] for j in jobs),
            f"count={len(jobs)}",
        )
        r = await c.get(f"{ROUTER}/sandboxes")
        sb = r.json()
        check(
            "/sandboxes 聚合",
            isinstance(sb.get("sandboxes"), list) and sb.get("total", 0) >= 4,
            f"total={sb.get('total')}",
        )

        # ---- 9. /metrics 聚合带 node label ----
        r = await c.get(f"{ROUTER}/metrics")
        check(
            "/metrics 聚合带 node label",
            'node="n1"' in r.text and 'node="n2"' in r.text,
            f"bytes={len(r.text)}",
        )

        # ---- 10. 无效前缀 404 ----
        r = await c.post(f"{ROUTER}/sessions/no-prefix-id/execute", json={"code": "1", "timeout": 10})
        check("无前缀 ID 返回 404", r.status_code == 404, f"status={r.status_code}")

        # ---- 11. E2B 风格路由 ----
        r = await c.post(f"{ROUTER}/e2b/sandboxes", json={})
        e2b_id = r.json().get("sandboxID", "")
        r2 = await c.post(f"{ROUTER}/e2b/sandboxes/{e2b_id}/code", json={"code": "print('e2b_routed')"})
        e2b_ok = r2.status_code == 200 and "e2b_routed" in json.dumps(r2.json())
        r3 = await c.delete(f"{ROUTER}/e2b/sandboxes/{e2b_id}")
        check(
            "E2B 创建/执行/销毁经路由",
            ":" in e2b_id and e2b_ok and r3.status_code in (200, 204),
            f"id={e2b_id} exec={r2.status_code} del={r3.status_code}",
        )

        # ---- 12. 清理会话 ----
        del_ok = True
        for sid in sessions:
            r = await c.delete(f"{ROUTER}/sessions/{sid}")
            if r.status_code not in (200, 204):
                del_ok = False
        check("会话删除经前缀路由 x4", del_ok)

    print(f"\n===== Router e2e: {len(PASS)} 过 / {len(FAIL)} 败 =====", flush=True)
    if FAIL:
        print("失败项: " + ", ".join(FAIL), flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
