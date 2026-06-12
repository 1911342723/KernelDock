"""
kernel_server Linux 行为验证（在容器内运行，不依赖 pandas）

验证点：
  V1 fork 隔离：isolated 执行的变量不泄漏到父命名空间
  V2 超时 SIGKILL：isolated 超时后父 kernel 存活、后续执行正常
  V3 并发不阻塞：有状态慢执行期间，isolated 执行不被全局锁阻塞
  V4 上下文隔离 + bootstrap 一次性语义（有状态路径）
  V5 TCP 协议端到端（ping / execute / 长度头帧）

用法（容器内）：
  python tests/verify_kernel_linux.py
"""

import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def setup_module():
    from sandbox_runtime import kernel_server as ks
    # 不依赖 pandas：替换命名空间初始化为轻量版
    ks._new_namespace = lambda: {"__builtins__": __builtins__}
    ks.BACKUP_CAPTURE_CODE = ""
    ks._context_namespaces.clear()
    ks._context_metadata.clear()
    ks._initialized = False
    ks._init_namespace()
    return ks


def test_v1_fork_isolation(ks):
    print("V1 fork 隔离")
    if not hasattr(os, "fork"):
        print("  [SKIP] 非 Linux 环境")
        return
    r1 = ks.execute_code("leak_var = 123\nprint('isolated done')", isolated=True)
    check("isolated 执行成功", r1.get("success") is True, str(r1.get("error")))
    check("结果带 isolated 标志", r1.get("isolated") is True)
    r2 = ks.execute_code("print('leak_var' in globals())")
    check(
        "变量未泄漏到父命名空间",
        r2.get("stdout", "").strip().splitlines()[-1] == "False",
        r2.get("stdout", ""),
    )


def test_v2_timeout_sigkill(ks):
    print("V2 超时 SIGKILL 后父 kernel 存活")
    if not hasattr(os, "fork"):
        print("  [SKIP] 非 Linux 环境")
        return
    start = time.monotonic()
    r = ks.execute_code("import time\ntime.sleep(60)", timeout=2, isolated=True)
    elapsed = time.monotonic() - start
    check("超时返回错误", r.get("success") is False and "超时" in (r.get("error") or ""))
    check(f"耗时贴近超时阈值（{elapsed:.1f}s < 10s）", elapsed < 10)
    check("无 kernel_unhealthy（容器无需销毁）", not r.get("kernel_unhealthy"))
    r2 = ks.execute_code("print('still alive')", isolated=True)
    check("超时后父 kernel 仍可执行", r2.get("success") is True and "still alive" in r2.get("stdout", ""))


def test_v3_concurrency(ks):
    print("V3 有状态慢执行不阻塞 isolated 执行")
    if not hasattr(os, "fork"):
        print("  [SKIP] 非 Linux 环境")
        return
    results = {}

    def slow_stateful():
        results["slow"] = ks.execute_code("import time\ntime.sleep(3)\nprint('slow done')", timeout=30)

    t = threading.Thread(target=slow_stateful)
    t.start()
    time.sleep(0.5)  # 确保慢执行已持有 _exec_lock

    start = time.monotonic()
    fast = ks.execute_code("print('fast done')", timeout=10, isolated=True)
    fast_elapsed = time.monotonic() - start
    t.join()

    check("isolated 执行成功", fast.get("success") is True, str(fast.get("error")))
    check(
        f"isolated 未被慢执行阻塞（{fast_elapsed:.2f}s < 2s）",
        fast_elapsed < 2.0,
        f"耗时 {fast_elapsed:.2f}s（修复前会 >2.5s）",
    )
    check("慢执行自身正常", results["slow"].get("success") is True)


def test_v4_contexts(ks):
    print("V4 上下文隔离 + bootstrap 一次性")
    a1 = ks.execute_code("v = 41", context_id="ctx_a")
    a2 = ks.execute_code("print(v + 1)", context_id="ctx_a")
    b1 = ks.execute_code("print('v' in globals())", context_id="ctx_b")
    check("ctx_a 变量跨执行保留", a2.get("stdout", "").strip().splitlines()[-1] == "42")
    check("ctx_b 与 ctx_a 隔离", b1.get("stdout", "").strip().splitlines()[-1] == "False")

    s1 = ks.execute_code("print(seed)", bootstrap_source="seed = 7", context_id="ctx_s")
    s2 = ks.execute_code("print(seed)", bootstrap_source="seed = 99", context_id="ctx_s")
    check("bootstrap 仅首次执行", s1.get("stdout", "").strip().splitlines()[-1] == "7"
          and s2.get("stdout", "").strip().splitlines()[-1] == "7")

    # 超时重置应只影响对应 context（修复的 bug）
    ks.execute_code("keep = 1", context_id="ctx_keep")
    if hasattr(os, "fork"):
        ks.execute_code("import time\ntime.sleep(60)", timeout=1, isolated=True, context_id="ctx_other")
        k = ks.execute_code("print('keep' in globals())", context_id="ctx_keep")
        check("isolated 超时不影响其他 context", k.get("stdout", "").strip().splitlines()[-1] == "True")


def _rpc(port: int, payload: dict) -> dict:
    s = socket.socket()
    s.settimeout(30)
    s.connect(("127.0.0.1", port))
    raw = json.dumps(payload).encode()
    s.sendall(struct.pack(">I", len(raw)) + raw)
    header = b""
    while len(header) < 4:
        header += s.recv(4 - len(header))
    (n,) = struct.unpack(">I", header)
    body = b""
    while len(body) < n:
        body += s.recv(min(65536, n - len(body)))
    s.close()
    return json.loads(body.decode())


def test_v5_tcp_protocol():
    print("V5 TCP 协议端到端")
    env = dict(os.environ, KERNEL_PORT="19999", PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    proc = subprocess.Popen(
        [sys.executable, "-m", "sandbox_runtime.kernel_server"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        ready = False
        for _ in range(60):
            try:
                resp = _rpc(19999, {"action": "ping"})
                if resp.get("status") == "ok":
                    ready = True
                    break
            except OSError:
                time.sleep(1)
        check("kernel TCP 服务就绪（ping ok）", ready)
        if ready:
            r = _rpc(19999, {"action": "execute", "code": "print('tcp ok')", "timeout": 30, "isolated": True})
            check("TCP isolated 执行成功", r.get("success") is True and "tcp ok" in r.get("stdout", ""), str(r.get("error")))
            ctxs = _rpc(19999, {"action": "list_contexts"})
            check("list_contexts 正常", ctxs.get("status") == "ok")
    finally:
        proc.kill()


def main() -> int:
    ks = setup_module()
    test_v1_fork_isolation(ks)
    test_v2_timeout_sigkill(ks)
    test_v3_concurrency(ks)
    test_v4_contexts(ks)
    test_v5_tcp_protocol()
    print(f"\n结果: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
