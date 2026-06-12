"""
并发压力测试 - Code Executor Service

测试场景:
  1. 无状态执行 (POST /execute) 并发压测
  2. 有状态会话 (POST /sessions + execute) 并发压测
  3. 混合负载 (无状态 + 有状态混合)
  4. 递增负载 (逐步增加并发数，找到吞吐瓶颈)

使用方式:
  pip install aiohttp   (如果尚未安装)
  python tests/stress_test.py --url http://localhost:8080 --scenario all
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

try:
    import aiohttp
except ImportError:
    print("需要 aiohttp 依赖:  pip install aiohttp")
    sys.exit(1)


BASE_URL = "http://localhost:8080"

SIMPLE_CODE = "print(1 + 1)"

COMPUTE_CODE = """\
import pandas as pd
import numpy as np

df = pd.DataFrame(np.random.randn(500, 5), columns=list('ABCDE'))
summary = df.describe()
print(summary.to_string())
"""

CHART_CODE = """\
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

x = np.linspace(0, 2 * np.pi, 100)
fig, ax = plt.subplots()
ax.plot(x, np.sin(x))
ax.set_title('Stress Test Chart')
plt.savefig('/output/chart.png', dpi=72)
plt.close()
print('chart done')
"""

SLEEP_CODE = """\
import time
time.sleep(2)
print('slept 2s')
"""


@dataclass
class RequestResult:
    success: bool
    status_code: int
    latency_ms: float
    error: Optional[str] = None
    queue_waited: float = 0.0
    exec_output: str = ""


@dataclass
class ScenarioReport:
    name: str
    concurrency: int
    total_requests: int
    results: list = field(default_factory=list)

    @property
    def successes(self) -> int:
        return sum(1 for r in self.results if r.success)

    @property
    def failures(self) -> int:
        return self.total_requests - self.successes

    @property
    def latencies(self) -> list:
        return [r.latency_ms for r in self.results if r.success]

    def summary(self) -> str:
        lat = self.latencies
        lines = [
            "",
            f"{'=' * 60}",
            f"  场景: {self.name}",
            f"  并发数: {self.concurrency}  |  总请求: {self.total_requests}",
            f"{'=' * 60}",
            f"  成功: {self.successes}  |  失败: {self.failures}  "
            f"| 成功率: {self.successes / max(self.total_requests, 1) * 100:.1f}%",
        ]
        if lat:
            lines += [
                f"  延迟 (ms):",
                f"    min    = {min(lat):8.1f}",
                f"    p50    = {statistics.median(lat):8.1f}",
                f"    p90    = {sorted(lat)[int(len(lat) * 0.9)]:8.1f}",
                f"    p99    = {sorted(lat)[int(len(lat) * 0.99)]:8.1f}",
                f"    max    = {max(lat):8.1f}",
                f"    avg    = {statistics.mean(lat):8.1f}",
            ]
            total_sec = max(lat) / 1000 if lat else 1
            lines.append(
                f"  吞吐 (QPS): ~{self.successes / total_sec:.1f}"
            )
        queue_waits = [r.queue_waited for r in self.results if r.queue_waited > 0]
        if queue_waits:
            lines.append(
                f"  排队等待 (s): avg={statistics.mean(queue_waits):.2f}  "
                f"max={max(queue_waits):.2f}"
            )
        if self.failures:
            errors = {}
            for r in self.results:
                if not r.success:
                    key = r.error or f"HTTP {r.status_code}"
                    errors[key] = errors.get(key, 0) + 1
            lines.append("  错误分布:")
            for k, v in sorted(errors.items(), key=lambda x: -x[1]):
                lines.append(f"    {k}: {v}")
        lines.append(f"{'=' * 60}")
        return "\n".join(lines)


async def execute_stateless(session: aiohttp.ClientSession,
                            code: str, timeout: int = 30) -> RequestResult:
    t0 = time.monotonic()
    try:
        async with session.post(
            f"{BASE_URL}/execute",
            json={"code": code, "timeout": timeout},
            timeout=aiohttp.ClientTimeout(total=timeout + 30),
        ) as resp:
            body = await resp.json()
            latency = (time.monotonic() - t0) * 1000
            queue_waited = 0.0
            if body.get("queue_info"):
                queue_waited = body["queue_info"].get("waited_seconds", 0)
            return RequestResult(
                success=body.get("success", False) and resp.status == 200,
                status_code=resp.status,
                latency_ms=latency,
                error=body.get("error"),
                queue_waited=queue_waited,
                exec_output=body.get("output", "")[:200],
            )
    except Exception as e:
        latency = (time.monotonic() - t0) * 1000
        return RequestResult(
            success=False, status_code=0, latency_ms=latency, error=str(e)
        )


async def execute_stateful(session: aiohttp.ClientSession,
                           code: str, timeout: int = 30) -> RequestResult:
    """Create session -> execute -> delete session"""
    sid = f"stress-{uuid.uuid4().hex[:8]}"
    t0 = time.monotonic()
    try:
        async with session.post(
            f"{BASE_URL}/sessions", json={"session_id": sid},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                return RequestResult(
                    success=False,
                    status_code=resp.status,
                    latency_ms=(time.monotonic() - t0) * 1000,
                    error=f"create session failed: {text[:200]}",
                )

        async with session.post(
            f"{BASE_URL}/sessions/{sid}/execute",
            json={"code": code, "timeout": timeout},
            timeout=aiohttp.ClientTimeout(total=timeout + 30),
        ) as resp:
            body = await resp.json()
            latency = (time.monotonic() - t0) * 1000
            queue_waited = 0.0
            if body.get("queue_info"):
                queue_waited = body["queue_info"].get("waited_seconds", 0)
            result = RequestResult(
                success=body.get("success", False) and resp.status == 200,
                status_code=resp.status,
                latency_ms=latency,
                error=body.get("error"),
                queue_waited=queue_waited,
                exec_output=body.get("output", "")[:200],
            )

        try:
            await session.delete(
                f"{BASE_URL}/sessions/{sid}",
                timeout=aiohttp.ClientTimeout(total=15),
            )
        except Exception:
            pass

        return result
    except Exception as e:
        latency = (time.monotonic() - t0) * 1000
        return RequestResult(
            success=False, status_code=0, latency_ms=latency, error=str(e)
        )


async def run_scenario(name: str, concurrency: int, total: int,
                       coro_factory, http_session: aiohttp.ClientSession) -> ScenarioReport:
    report = ScenarioReport(name=name, concurrency=concurrency, total_requests=total)
    semaphore = asyncio.Semaphore(concurrency)
    completed = 0

    async def bounded_task():
        nonlocal completed
        async with semaphore:
            result = await coro_factory(http_session)
            report.results.append(result)
            completed += 1
            if completed % 10 == 0 or completed == total:
                print(f"  [{name}] {completed}/{total} 完成", flush=True)
            return result

    print(f"\n>> 启动场景: {name}  (并发={concurrency}, 总数={total})")
    tasks = [asyncio.create_task(bounded_task()) for _ in range(total)]
    await asyncio.gather(*tasks)
    return report


async def scenario_stateless_simple(concurrency: int, total: int) -> ScenarioReport:
    conn = aiohttp.TCPConnector(limit=concurrency + 5, force_close=False)
    async with aiohttp.ClientSession(connector=conn) as session:
        return await run_scenario(
            "无状态-简单计算", concurrency, total,
            lambda s: execute_stateless(s, SIMPLE_CODE), session,
        )


async def scenario_stateless_compute(concurrency: int, total: int) -> ScenarioReport:
    conn = aiohttp.TCPConnector(limit=concurrency + 5, force_close=False)
    async with aiohttp.ClientSession(connector=conn) as session:
        return await run_scenario(
            "无状态-DataFrame计算", concurrency, total,
            lambda s: execute_stateless(s, COMPUTE_CODE), session,
        )


async def scenario_stateless_chart(concurrency: int, total: int) -> ScenarioReport:
    conn = aiohttp.TCPConnector(limit=concurrency + 5, force_close=False)
    async with aiohttp.ClientSession(connector=conn) as session:
        return await run_scenario(
            "无状态-图表生成", concurrency, total,
            lambda s: execute_stateless(s, CHART_CODE, timeout=60), session,
        )


async def scenario_stateful(concurrency: int, total: int) -> ScenarioReport:
    conn = aiohttp.TCPConnector(limit=concurrency + 5, force_close=False)
    async with aiohttp.ClientSession(connector=conn) as session:
        return await run_scenario(
            "有状态-会话完整流程", concurrency, total,
            lambda s: execute_stateful(s, COMPUTE_CODE), session,
        )


async def scenario_mixed(concurrency: int, total: int) -> ScenarioReport:
    conn = aiohttp.TCPConnector(limit=concurrency + 5, force_close=False)
    async with aiohttp.ClientSession(connector=conn) as session:
        counter = {"i": 0}

        async def mixed_factory(s):
            counter["i"] += 1
            if counter["i"] % 3 == 0:
                return await execute_stateful(s, COMPUTE_CODE)
            elif counter["i"] % 3 == 1:
                return await execute_stateless(s, SIMPLE_CODE)
            else:
                return await execute_stateless(s, COMPUTE_CODE)

        return await run_scenario(
            "混合负载", concurrency, total, mixed_factory, session,
        )


async def scenario_ramp_up(max_concurrency: int, requests_per_level: int) -> list:
    """Incrementally increase concurrency to find throughput ceiling."""
    reports = []
    for c in [1, 2, 4, 6, 8, 10, 12, 16, max_concurrency]:
        if c > max_concurrency:
            continue
        conn = aiohttp.TCPConnector(limit=c + 5, force_close=False)
        async with aiohttp.ClientSession(connector=conn) as session:
            report = await run_scenario(
                f"递增负载 C={c}", c, requests_per_level,
                lambda s: execute_stateless(s, SIMPLE_CODE), session,
            )
            reports.append(report)
            print(report.summary())
    return reports


async def check_health() -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BASE_URL}/health", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    print(f"服务状态: {json.dumps(data, indent=2, ensure_ascii=False)}")
                    return True
                print(f"健康检查失败: HTTP {resp.status}")
                return False
    except Exception as e:
        print(f"无法连接到服务: {e}")
        return False


async def main():
    parser = argparse.ArgumentParser(description="Code Executor 并发压力测试")
    parser.add_argument("--url", default="http://localhost:8080", help="服务地址")
    parser.add_argument("--scenario", default="all",
                        choices=["stateless", "compute", "chart", "stateful",
                                 "mixed", "ramp", "all"],
                        help="测试场景")
    parser.add_argument("-c", "--concurrency", type=int, default=6,
                        help="并发数 (默认6)")
    parser.add_argument("-n", "--total", type=int, default=30,
                        help="总请求数 (默认30)")
    args = parser.parse_args()

    global BASE_URL
    BASE_URL = args.url.rstrip("/")

    print(f"目标: {BASE_URL}")
    print(f"并发: {args.concurrency}  |  总请求: {args.total}")
    print("-" * 60)

    if not await check_health():
        print("\n服务不可用，请先启动服务")
        sys.exit(1)

    reports = []

    if args.scenario in ("stateless", "all"):
        reports.append(await scenario_stateless_simple(args.concurrency, args.total))

    if args.scenario in ("compute", "all"):
        reports.append(await scenario_stateless_compute(args.concurrency, args.total))

    if args.scenario in ("chart", "all"):
        reports.append(await scenario_stateless_chart(
            min(args.concurrency, 4), min(args.total, 12)
        ))

    if args.scenario in ("stateful", "all"):
        reports.append(await scenario_stateful(args.concurrency, args.total))

    if args.scenario in ("mixed", "all"):
        reports.append(await scenario_mixed(args.concurrency, args.total))

    if args.scenario in ("ramp", "all"):
        ramp_reports = await scenario_ramp_up(args.concurrency, max(args.total // 3, 6))
        reports.extend(ramp_reports)

    print("\n\n" + "#" * 60)
    print("  压力测试结果汇总")
    print("#" * 60)
    for r in reports:
        print(r.summary())


if __name__ == "__main__":
    asyncio.run(main())
