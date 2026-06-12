"""
集成测试 - 验证代码执行接口返回的丰富信息

针对 ExecuteCodeResponse 新增的三个结构化字段进行全面验证:
  - queue_info:     排队信息（位置、等待、并发状态、全局统计）
  - sandbox_info:   沙箱信息（模式、容器池、资源限制）
  - execution_info: 执行细节（耗时、路径、代码大小、超时检测）

使用方式:
  python tests/test_enriched_response.py [--url http://localhost:8080]
"""

import argparse
import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Optional

try:
    import aiohttp
except ImportError:
    print("需要 aiohttp:  pip install aiohttp")
    sys.exit(1)

BASE_URL = "http://localhost:8080"

PASS = "\033[92m PASS \033[0m"
FAIL = "\033[91m FAIL \033[0m"

total_pass = 0
total_fail = 0


def check(name: str, condition: bool, detail: str = ""):
    global total_pass, total_fail
    if condition:
        total_pass += 1
        print(f"  [{PASS}] {name}")
    else:
        total_fail += 1
        msg = f"  [{FAIL}] {name}"
        if detail:
            msg += f"  ({detail})"
        print(msg)


async def api_post(session: aiohttp.ClientSession, path: str, body: dict,
                   timeout: int = 60) -> dict:
    async with session.post(
        f"{BASE_URL}{path}",
        json=body,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as resp:
        return await resp.json()


async def api_get(session: aiohttp.ClientSession, path: str) -> dict:
    async with session.get(
        f"{BASE_URL}{path}",
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        return await resp.json()


async def api_delete(session: aiohttp.ClientSession, path: str):
    async with session.delete(
        f"{BASE_URL}{path}",
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        return await resp.json()


# ================================================================
# Test 1: 无状态执行 - 基础字段完整性
# ================================================================
async def test_stateless_basic(session: aiohttp.ClientSession):
    print("\n=== Test 1: 无状态执行 - 基础字段完整性 ===")
    r = await api_post(session, "/execute", {"code": "print(42)", "timeout": 30})

    check("success == True", r["success"] is True)
    check("output 包含 42", "42" in r["output"])
    check("error 为 None", r["error"] is None)
    check("charts 是 list", isinstance(r["charts"], list))
    check("tables 是 list", isinstance(r["tables"], list))
    check("images 是 list", isinstance(r["images"], list))

    check("queue_info 存在", r.get("queue_info") is not None)
    check("sandbox_info 存在", r.get("sandbox_info") is not None)
    check("execution_info 存在", r.get("execution_info") is not None)


# ================================================================
# Test 2: queue_info 字段验证
# ================================================================
async def test_queue_info(session: aiohttp.ClientSession):
    print("\n=== Test 2: queue_info 字段验证 ===")
    r = await api_post(session, "/execute", {"code": "print(1)", "timeout": 30})
    qi = r.get("queue_info", {})

    check("position_on_entry >= 1", qi.get("position_on_entry", 0) >= 1)
    check("waited_seconds >= 0", qi.get("waited_seconds", -1) >= 0)
    check("estimated_wait_seconds >= 0", qi.get("estimated_wait_seconds", -1) >= 0)
    check("queue_depth >= 0", qi.get("queue_depth", -1) >= 0)
    check("executing_count >= 0", qi.get("executing_count", -1) >= 0)
    check("max_concurrent > 0", qi.get("max_concurrent", 0) > 0,
          f"got {qi.get('max_concurrent')}")
    check("avg_execution_time > 0", qi.get("avg_execution_time", 0) > 0)
    check("total_enqueued > 0", qi.get("total_enqueued", 0) > 0)
    check("total_executed >= 0", qi.get("total_executed", -1) >= 0)


# ================================================================
# Test 3: sandbox_info 字段验证（无状态模式）
# ================================================================
async def test_sandbox_info_stateless(session: aiohttp.ClientSession):
    print("\n=== Test 3: sandbox_info - 无状态模式 ===")
    r = await api_post(session, "/execute", {"code": "print('hi')", "timeout": 30})
    si = r.get("sandbox_info", {})

    check("mode == stateless_pool_kernel",
          si.get("mode") == "stateless_pool_kernel",
          f"got {si.get('mode')}")
    check("pool_available 是整数", isinstance(si.get("pool_available"), int))
    check("pool_total 是整数", isinstance(si.get("pool_total"), int))
    check("pool_total > 0", (si.get("pool_total") or 0) > 0,
          f"got {si.get('pool_total')}")


# ================================================================
# Test 4: execution_info 字段验证
# ================================================================
async def test_execution_info(session: aiohttp.ClientSession):
    print("\n=== Test 4: execution_info 字段验证 ===")
    code = "x = sum(range(1000)); print(x)"
    r = await api_post(session, "/execute", {"code": code, "timeout": 30})
    ei = r.get("execution_info", {})

    check("execution_time_ms > 0", ei.get("execution_time_ms", 0) > 0,
          f"got {ei.get('execution_time_ms')}")
    check("execution_path 非 unknown",
          ei.get("execution_path", "unknown") != "unknown",
          f"got {ei.get('execution_path')}")
    check("code_size_bytes 正确",
          ei.get("code_size_bytes", 0) == len(code.encode("utf-8")),
          f"expected {len(code.encode('utf-8'))}, got {ei.get('code_size_bytes')}")
    check("timeout_configured == 30", ei.get("timeout_configured") == 30)
    check("timed_out == False", ei.get("timed_out") is False)
    check("chart_count == 0", ei.get("chart_count") == 0)
    check("table_count == 0", ei.get("table_count") == 0)
    check("output_truncated == False", ei.get("output_truncated") is False)
    check("output_size_bytes > 0", ei.get("output_size_bytes", 0) > 0)


# ================================================================
# Test 5: 错误执行 - execution_info 仍然填充
# ================================================================
async def test_error_execution(session: aiohttp.ClientSession):
    print("\n=== Test 5: 错误执行 - 字段仍然完整 ===")
    r = await api_post(session, "/execute", {"code": "1/0", "timeout": 30})

    check("success == False", r["success"] is False)
    check("error 非空", r.get("error") is not None and len(r["error"]) > 0)
    check("queue_info 仍存在", r.get("queue_info") is not None)
    check("sandbox_info 仍存在", r.get("sandbox_info") is not None)
    check("execution_info 仍存在", r.get("execution_info") is not None)

    ei = r.get("execution_info", {})
    check("timed_out == False (非超时错误)", ei.get("timed_out") is False)
    check("execution_time_ms >= 0", ei.get("execution_time_ms", -1) >= 0)


# ================================================================
# Test 6: 有状态会话 - 完整流程
# ================================================================
async def test_stateful_session(session: aiohttp.ClientSession):
    print("\n=== Test 6: 有状态会话 - 完整流程 ===")
    sid = f"test-{uuid.uuid4().hex[:8]}"

    cr = await api_post(session, "/sessions", {"session_id": sid})
    check("会话创建成功", cr.get("session_id") == sid)

    r = await api_post(session, f"/sessions/{sid}/execute",
                       {"code": "a = 100; print(a)", "timeout": 30})
    check("执行成功", r["success"] is True)
    check("output 包含 100", "100" in r["output"])

    check("queue_info 存在", r.get("queue_info") is not None)
    check("sandbox_info 存在", r.get("sandbox_info") is not None)
    check("execution_info 存在", r.get("execution_info") is not None)

    si = r.get("sandbox_info", {})
    check("mode 非 unknown", si.get("mode", "unknown") != "unknown",
          f"got {si.get('mode')}")
    check("pool_total 有值", si.get("pool_total") is not None)

    ei = r.get("execution_info", {})
    check("execution_path 非 unknown", ei.get("execution_path", "unknown") != "unknown",
          f"got {ei.get('execution_path')}")

    await api_delete(session, f"/sessions/{sid}")


# ================================================================
# Test 7: 并发请求 - queue_info 反映排队状态
# ================================================================
async def test_concurrent_queue(session: aiohttp.ClientSession):
    print("\n=== Test 7: 并发请求 - queue_info 反映排队状态 ===")

    slow_code = "import time; time.sleep(1); print('done')"
    tasks = [
        api_post(session, "/execute", {"code": slow_code, "timeout": 30})
        for _ in range(8)
    ]
    results = await asyncio.gather(*tasks)

    all_success = all(r["success"] for r in results)
    check("8 个并发请求全部成功", all_success,
          f"成功: {sum(1 for r in results if r['success'])}/8")

    positions = [r.get("queue_info", {}).get("position_on_entry", 0) for r in results]
    check("所有 position_on_entry >= 1",
          all(p >= 1 for p in positions),
          f"positions: {positions}")

    waits = [r.get("queue_info", {}).get("waited_seconds", 0) for r in results]
    check("部分请求 waited_seconds > 0 (超过 pool 大小有排队)",
          any(w > 0 for w in waits),
          f"waits: {[round(w, 2) for w in waits]}")

    max_concs = [r.get("queue_info", {}).get("max_concurrent", 0) for r in results]
    check("max_concurrent 一致且 > 0",
          len(set(max_concs)) == 1 and max_concs[0] > 0,
          f"values: {set(max_concs)}")

    total_enqueued = [r.get("queue_info", {}).get("total_enqueued", 0) for r in results]
    check("total_enqueued 合理 (>= 并发请求数)",
          max(total_enqueued) >= 8,
          f"max total_enqueued: {max(total_enqueued)}")


# ================================================================
# Test 8: 图表生成 - chart_count 正确
# ================================================================
async def test_chart_count(session: aiohttp.ClientSession):
    print("\n=== Test 8: 图表生成 - chart_count 正确 ===")
    code = """\
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

fig, ax = plt.subplots()
ax.plot([1, 2, 3], [1, 4, 9])
plt.savefig('/output/test.png', dpi=72)
plt.close()
print('chart done')
"""
    r = await api_post(session, "/execute", {"code": code, "timeout": 60})
    check("执行成功", r["success"] is True)

    ei = r.get("execution_info", {})
    charts = r.get("charts", [])
    check(f"charts 列表有元素", len(charts) > 0, f"got {len(charts)}")
    check("chart_count 与 charts 长度一致",
          ei.get("chart_count", -1) == len(charts),
          f"chart_count={ei.get('chart_count')}, len(charts)={len(charts)}")


# ================================================================
# Test 9: 大输出 - output_truncated 检测
# ================================================================
async def test_output_truncated(session: aiohttp.ClientSession):
    print("\n=== Test 9: 大输出 - output_truncated 检测 ===")
    code = "print('A' * 200000)"
    r = await api_post(session, "/execute", {"code": code, "timeout": 30})
    check("执行成功", r["success"] is True)

    ei = r.get("execution_info", {})
    check("output_truncated == True", ei.get("output_truncated") is True,
          f"got {ei.get('output_truncated')}, output_size={ei.get('output_size_bytes')}")


# ================================================================
# Test 10: /queue/status 端点一致性
# ================================================================
async def test_queue_status_endpoint(session: aiohttp.ClientSession):
    print("\n=== Test 10: /queue/status 端点一致性 ===")
    qs = await api_get(session, "/queue/status")

    check("enabled == True", qs.get("enabled") is True)
    check("max_concurrent > 0", qs.get("max_concurrent", 0) > 0)
    check("total_executed >= 0", qs.get("total_executed", -1) >= 0)
    check("avg_execution_time > 0", qs.get("avg_execution_time", 0) > 0)

    r = await api_post(session, "/execute", {"code": "print(1)", "timeout": 30})
    qi = r.get("queue_info", {})
    check("queue_info.max_concurrent == /queue/status.max_concurrent",
          qi.get("max_concurrent") == qs.get("max_concurrent"),
          f"{qi.get('max_concurrent')} vs {qs.get('max_concurrent')}")


# ================================================================
# Test 11: /health 端点池信息与 sandbox_info 一致
# ================================================================
async def test_health_consistency(session: aiohttp.ClientSession):
    print("\n=== Test 11: /health 与 sandbox_info 池信息一致 ===")
    health = await api_get(session, "/health")
    r = await api_post(session, "/execute", {"code": "print(1)", "timeout": 30})
    si = r.get("sandbox_info", {})

    check("pool_total 一致",
          si.get("pool_total") == health.get("pool_total"),
          f"sandbox_info={si.get('pool_total')} health={health.get('pool_total')}")


# ================================================================
# Main
# ================================================================
async def main():
    parser = argparse.ArgumentParser(description="Enriched Response 集成测试")
    parser.add_argument("--url", default="http://localhost:8080")
    args = parser.parse_args()

    global BASE_URL
    BASE_URL = args.url.rstrip("/")

    print(f"目标: {BASE_URL}")
    print("=" * 60)

    async with aiohttp.ClientSession() as session:
        try:
            health = await api_get(session, "/health")
            print(f"服务状态: {health.get('status')}  "
                  f"pool: {health.get('pool_available')}/{health.get('pool_total')}")
        except Exception as e:
            print(f"无法连接到服务: {e}")
            sys.exit(1)

        await test_stateless_basic(session)
        await test_queue_info(session)
        await test_sandbox_info_stateless(session)
        await test_execution_info(session)
        await test_error_execution(session)
        await test_stateful_session(session)
        await test_concurrent_queue(session)
        await test_chart_count(session)
        await test_output_truncated(session)
        await test_queue_status_endpoint(session)
        await test_health_consistency(session)

    print("\n" + "=" * 60)
    print(f"  总计: {total_pass + total_fail}  "
          f"通过: {total_pass}  失败: {total_fail}")
    if total_fail > 0:
        print(f"  {FAIL} 有 {total_fail} 个测试失败")
    else:
        print(f"  {PASS} 全部通过!")
    print("=" * 60)
    sys.exit(1 if total_fail > 0 else 0)


if __name__ == "__main__":
    asyncio.run(main())
