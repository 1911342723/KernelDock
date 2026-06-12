"""
MCP server 实连端到端验证（官方 mcp SDK 客户端，stdio transport）。

模拟 Claude Desktop / Cursor 的接入方式：
    stdio 拉起 mcp_server/kerneldock_mcp.py → initialize 握手 → list_tools → 逐个调用全部 12 个工具。

前置条件：
    1. KernelDock 网关已运行（默认 http://localhost:9527，可用 KERNELDOCK_URL 覆盖）
    2. pip install -r requirements-mcp.txt（mcp>=1.2.0, httpx）

运行：
    python -X utf8 tests/mcp_e2e.py

不依赖 pytest（独立脚本，面向运行中的服务），退出码 0=全过，1=有失败。
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = REPO_ROOT / "mcp_server" / "kerneldock_mcp.py"

EXPECTED_TOOLS = {
    "execute_python",
    "create_session",
    "execute_in_session",
    "delete_session",
    "run_shell",
    "list_files",
    "read_file",
    "write_file",
    "install_packages",
    "submit_job",
    "get_job",
    "get_chart",
}

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}  {detail}", flush=True)


def tool_text(result) -> str:
    """提取 CallToolResult 的首个 text content。"""
    for item in result.content:
        if getattr(item, "type", "") == "text":
            return item.text
    return ""


def parse(result) -> dict:
    """容错解析工具返回：非 JSON（如错误文本）时包成 {'_raw': ...}。"""
    txt = tool_text(result)
    try:
        body = json.loads(txt)
        return body if isinstance(body, dict) else {"_raw": body}
    except (json.JSONDecodeError, TypeError):
        return {"_raw": txt}


async def call(session: ClientSession, tool: str, args: dict, timeout: float = 120.0):
    return await asyncio.wait_for(session.call_tool(tool, args), timeout=timeout)


async def run_suite(session: ClientSession) -> None:
    # ---- 1. initialize 已在外层完成，这里从 list_tools 开始 ----
    tools_resp = await session.list_tools()
    tool_names = {t.name for t in tools_resp.tools}
    check(
        "list_tools 暴露 12 个工具",
        tool_names == EXPECTED_TOOLS,
        f"got {len(tool_names)}: missing={EXPECTED_TOOLS - tool_names or '{}'} extra={tool_names - EXPECTED_TOOLS or '{}'}",
    )
    no_schema = [t.name for t in tools_resp.tools if not t.inputSchema]
    check("全部工具带 inputSchema", not no_schema, f"missing schema: {no_schema}")

    # ---- 2. execute_python（无状态执行）----
    r = await call(session, "execute_python", {"code": "print(1 + 41)"})
    body = parse(r)
    check(
        "execute_python 无状态执行",
        (not r.isError) and body.get("success") and "42" in body.get("stdout", ""),
        f"time={body.get('execution_time_ms')}ms",
    )

    # ---- 3. execute_python 带图表（charts 只回元信息，不带 base64 正文）----
    chart_code = (
        "import matplotlib\nmatplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1,2,3],[4,5,6])\nplt.show()"
    )
    r = await call(session, "execute_python", {"code": chart_code})
    body = parse(r)
    charts = body.get("charts") or []
    check(
        "execute_python 图表只回元信息",
        body.get("success")
        and len(charts) >= 1
        and "base64" not in charts[0]
        and charts[0].get("size_base64", 0) > 0,
        f"charts={charts}",
    )

    # ---- 4. create_session ----
    r = await call(session, "create_session", {}, timeout=180)
    body = parse(r)
    session_id = body.get("session_id", "")
    check("create_session", (not r.isError) and bool(session_id), f"session_id={session_id}")
    if not session_id:
        print("!! 无 session_id，跳过后续会话类工具", flush=True)
        return

    try:
        # ---- 5. execute_in_session（有状态：变量跨调用保留）----
        await call(session, "execute_in_session", {"session_id": session_id, "code": "x = 99"})
        r = await call(
            session, "execute_in_session", {"session_id": session_id, "code": "print(x + 1)"}
        )
        body = parse(r)
        check(
            "execute_in_session 变量跨调用保留",
            body.get("success") and "100" in body.get("stdout", ""),
            f"stdout_tail={body.get('stdout', '')[-40:]!r}",
        )

        # ---- 6. run_shell（会话内）----
        r = await call(
            session, "run_shell", {"session_id": session_id, "command": "echo hello_mcp && uname -s"}
        )
        body = parse(r)
        check(
            "run_shell 会话内",
            body.get("exit_code") == 0 and "hello_mcp" in body.get("stdout", ""),
            f"resp={tool_text(r)[:160]}",
        )

        # ---- 7. run_shell（无状态）----
        r = await call(session, "run_shell", {"command": "echo stateless_shell"})
        body = parse(r)
        check(
            "run_shell 无状态",
            body.get("exit_code") == 0 and "stateless_shell" in body.get("stdout", ""),
            f"resp={tool_text(r)[:160]}",
        )

        # ---- 8. write_file ----
        content = "KernelDock MCP e2e 写入测试\nline2"
        r = await call(
            session,
            "write_file",
            {"session_id": session_id, "path": "/data/mcp_e2e.txt", "content": content},
        )
        check("write_file", not r.isError, f"resp={tool_text(r)[:160]}")

        # ---- 9. read_file（内容回读一致）----
        r = await call(session, "read_file", {"session_id": session_id, "path": "/data/mcp_e2e.txt"})
        body = parse(r)
        check("read_file 内容一致", body.get("content") == content, f"resp={tool_text(r)[:160]}")

        # ---- 10. list_files（能看到刚写的文件）----
        r = await call(session, "list_files", {"session_id": session_id, "path": "/data"})
        txt = tool_text(r)
        try:
            entries = json.loads(txt)
        except json.JSONDecodeError:
            entries = []
        if isinstance(entries, dict):
            entries = entries.get("entries") or entries.get("files") or []
        names = [e.get("name", "") if isinstance(e, dict) else str(e) for e in entries]
        check("list_files 包含新文件", any("mcp_e2e.txt" in n for n in names), f"names={names}")

        # ---- 11. 文件系统路径白名单（越权应报错而非成功）----
        r = await call(session, "read_file", {"session_id": session_id, "path": "/etc/passwd"})
        txt = tool_text(r)
        check(
            "read_file 白名单外路径被拒",
            r.isError and ("400" in txt or "403" in txt or "白名单" in txt),
            f"resp={txt[:120]}",
        )

        # ---- 12. install_packages ----
        # 注意：测试包必须选镜像里没有、kernel 也不会预加载的（six 之类
        # 已在 sys.modules 缓存的包，新装版本不会生效）。
        r = await call(
            session,
            "install_packages",
            {"session_id": session_id, "packages": ["shortuuid==1.0.13"]},
            timeout=320,
        )
        txt = tool_text(r)
        if r.isError and "409" in txt:
            check("install_packages 物理禁网 409 传播", True, "egress=none 预期行为")
        elif not r.isError:
            body = parse(r)
            ok = bool(body.get("success", False) or body.get("installed"))
            check("install_packages 安装成功（egress=proxy）", ok, f"resp={txt[:200]}")
            if ok:
                # 装完立即可 import（验证 sys.path 修补对后续执行生效）
                r2 = await call(
                    session,
                    "execute_in_session",
                    {
                        "session_id": session_id,
                        "code": "import shortuuid; print('shortuuid', shortuuid.uuid())",
                    },
                )
                b2 = parse(r2)
                check(
                    "install_packages 后新包立即可 import",
                    b2.get("success") and "shortuuid " in b2.get("stdout", ""),
                    f"stdout_tail={b2.get('stdout', '')[-40:]!r}",
                )
        else:
            check("install_packages", False, f"非预期错误: {txt[:200]}")

        # ---- 13. submit_job + get_job 轮询 ----
        r = await call(
            session,
            "submit_job",
            {"code": "import time\ntime.sleep(1)\nprint('job_done')", "timeout": 60},
        )
        body = parse(r)
        job_id = body.get("job_id", "")
        check("submit_job 返回 job_id", bool(job_id), f"job_id={job_id} status={body.get('status')}")

        if job_id:
            final = None
            for _ in range(30):
                r = await call(session, "get_job", {"job_id": job_id})
                jb = parse(r)
                if jb.get("status") in ("succeeded", "failed", "cancelled"):
                    final = jb
                    break
                await asyncio.sleep(1)
            stdout = ((final or {}).get("result") or {}).get("stdout", "")
            check(
                "get_job 轮询至完成",
                final is not None and final.get("status") == "succeeded" and "job_done" in stdout,
                f"status={(final or {}).get('status')} stdout_tail={stdout[-30:]!r}",
            )
        else:
            check("get_job 轮询至完成", False, "无 job_id 跳过")

        # ---- 14. get_chart（返回完整 base64 正文）----
        r = await call(
            session, "get_chart", {"session_id": session_id, "code": chart_code, "chart_index": 0}
        )
        body = parse(r)
        check(
            "get_chart 完整 base64",
            len(body.get("base64") or "") > 1000 and bool(body.get("format")),
            f"format={body.get('format')} len={len(body.get('base64') or '')}",
        )

    finally:
        # ---- 15. delete_session ----
        r = await call(session, "delete_session", {"session_id": session_id})
        body = parse(r)
        check("delete_session", body.get("deleted") == session_id, f"resp={tool_text(r)[:120]}")


async def main() -> int:
    env = dict(os.environ)
    env.setdefault("KERNELDOCK_URL", "http://localhost:9527")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_SCRIPT)],
        env=env,
    )

    t0 = time.perf_counter()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await asyncio.wait_for(session.initialize(), timeout=30)
            check(
                "initialize 握手",
                init.serverInfo.name == "kerneldock",
                f"server={init.serverInfo.name} protocol={init.protocolVersion}",
            )
            await run_suite(session)

    elapsed = time.perf_counter() - t0
    print(f"\n===== MCP 实连验证结果: {len(PASS)} 过 / {len(FAIL)} 败, 耗时 {elapsed:.1f}s =====", flush=True)
    if FAIL:
        print("失败项: " + ", ".join(FAIL), flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
