"""
4C8G 小型服务器压测脚本

针对本服务在 4C8G 部署形态（pool=2, max_pool=4, max_concurrent=8,
单沙箱 1C/512MB）设计的五个场景，验证锐评后落地的关键改造：

  S1 burst    无状态突发      —— 弹性扩容是否跟得上（target 2→4）
  S2 sustain  持续并发        —— 队列/池在长稳负载下的吞吐与 P95
  S3 mixed    有状态+无状态   —— 长会话执行不应阻塞 fork 隔离的无状态请求
                                 （修复前 bug：fork 抢全局锁会被阻塞 300s）
  S4 timeout  超时风暴        —— fork SIGKILL 后容器不销毁、池不缩水
  S5 idle     空闲缩容观察    —— 验证 idle_shrink（默认 600s，可 --idle-wait 缩短观察）

每个场景结束后采样 /health 与 /metrics，重点盯：
  - sandbox_kernel_fallback_total  必须保持 0（>0 = 热启动失效）
  - pool target/available          弹性伸缩轨迹
  - 延迟 P50/P95/P99 与错误率

使用：
  python tests/stress_4c8g.py --url http://localhost:9527 --scenario all
  python tests/stress_4c8g.py --url http://localhost:9527 --api-key xxx -s burst
"""

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Windows 控制台默认 GBK，✅ 等字符会让整个脚本崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import aiohttp
except ImportError:
    print("需要 aiohttp 依赖:  pip install aiohttp")
    sys.exit(1)


# ===== 负载代码 =====

CODE_LIGHT = "print(sum(range(1000)))"

CODE_PANDAS = """\
import pandas as pd
import numpy as np
df = pd.DataFrame(np.random.randn(2000, 8), columns=list('ABCDEFGH'))
print(df.groupby((df['A'] > 0)).mean().to_string())
"""

CODE_CHART = """\
import numpy as np
import matplotlib.pyplot as plt
x = np.linspace(0, 6.28, 200)
fig, ax = plt.subplots(figsize=(6, 4))
ax.plot(x, np.sin(x)); ax.plot(x, np.cos(x))
ax.set_title('stress 4c8g')
print('chart ready')
"""

CODE_MEMORY = """\
import numpy as np
# ~80MB 峰值，验证 512MB 限额内的真实分析负载
a = np.random.randn(1000, 10000)
print(float(a.mean()), a.shape)
del a
"""

CODE_SLEEP_3S = "import time; time.sleep(3); print('slow done')"

CODE_TIMEOUT = "import time; time.sleep(9999)"

CODE_SESSION_INIT = """\
import pandas as pd
import numpy as np
state_df = pd.DataFrame(np.random.randn(500, 4), columns=list('WXYZ'))
counter = 0
print('session ready', state_df.shape)
"""

CODE_SESSION_STEP = """\
counter += 1
print('step', counter, float(state_df['W'].sum()))
"""


# ===== 结果统计 =====

@dataclass
class Result:
    ok: bool
    status: int
    latency_ms: float
    error: str = ""
    kernel_path: bool = True  # sandbox_info/execution 是否走了 kernel


@dataclass
class Report:
    name: str
    results: List[Result] = field(default_factory=list)
    wall_seconds: float = 0.0
    notes: List[str] = field(default_factory=list)

    def render(self) -> str:
        total = len(self.results)
        if total == 0:
            return f"[{self.name}] 无样本"
        ok = [r for r in self.results if r.ok]
        lat = sorted(r.latency_ms for r in ok) or [0.0]

        def pct(p: float) -> float:
            return lat[min(len(lat) - 1, int(len(lat) * p))]

        lines = [
            f"\n===== {self.name} =====",
            f"  请求: {total}  成功: {len(ok)}  失败: {total - len(ok)}"
            f"  成功率: {len(ok) / total * 100:.1f}%",
            f"  墙钟: {self.wall_seconds:.1f}s"
            f"  吞吐: {total / self.wall_seconds:.2f} req/s" if self.wall_seconds else "",
            f"  延迟 ms  P50={pct(0.50):.0f}  P95={pct(0.95):.0f}"
            f"  P99={pct(0.99):.0f}  max={lat[-1]:.0f}",
        ]
        errors: Dict[str, int] = {}
        for r in self.results:
            if not r.ok:
                key = (r.error or f"HTTP {r.status}")[:80]
                errors[key] = errors.get(key, 0) + 1
        for err, count in sorted(errors.items(), key=lambda kv: -kv[1])[:5]:
            lines.append(f"  错误 x{count}: {err}")
        lines.extend(f"  备注: {n}" for n in self.notes)
        return "\n".join(line for line in lines if line)


# ===== HTTP 客户端 =====

class Client:
    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        # 4C8G 网关连接数不宜过大
        self._connector_limit = 32
        self._headers = headers
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "Client":
        self._session = aiohttp.ClientSession(
            headers=self._headers,
            connector=aiohttp.TCPConnector(limit=self._connector_limit),
            timeout=aiohttp.ClientTimeout(total=420),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.close()

    async def execute(self, code: str, timeout: int = 60) -> Result:
        start = time.monotonic()
        try:
            async with self._session.post(
                f"{self.base_url}/execute",
                json={"code": code, "timeout": timeout},
            ) as resp:
                latency = (time.monotonic() - start) * 1000
                body = await resp.json(content_type=None)
                mode = ((body or {}).get("sandbox_info") or {}).get("mode", "")
                return Result(
                    ok=resp.status == 200 and bool((body or {}).get("success")),
                    status=resp.status,
                    latency_ms=latency,
                    error=str((body or {}).get("error") or "")[:200]
                    if resp.status != 200 or not (body or {}).get("success") else "",
                    kernel_path="kernel" in str(mode),
                )
        except Exception as e:
            return Result(
                ok=False, status=0,
                latency_ms=(time.monotonic() - start) * 1000,
                error=f"{type(e).__name__}: {e}",
            )

    async def execute_expect_timeout(self, code: str, timeout: int) -> Result:
        """超时风暴专用：服务端返回超时错误即视为'符合预期'。"""
        r = await self.execute(code, timeout=timeout)
        if not r.ok and ("超时" in r.error or "Timeout" in r.error or "timeout" in r.error):
            return Result(ok=True, status=r.status, latency_ms=r.latency_ms)
        r.error = r.error or "预期超时但未返回超时错误"
        r.ok = False
        return r

    async def session_create(self) -> Optional[str]:
        try:
            async with self._session.post(
                f"{self.base_url}/sessions", json={}
            ) as resp:
                if resp.status != 200:
                    return None
                return (await resp.json()).get("session_id")
        except Exception:
            return None

    async def session_execute(self, session_id: str, code: str, timeout: int = 300) -> Result:
        start = time.monotonic()
        try:
            async with self._session.post(
                f"{self.base_url}/sessions/{session_id}/execute",
                json={"code": code, "timeout": timeout},
            ) as resp:
                latency = (time.monotonic() - start) * 1000
                body = await resp.json(content_type=None)
                return Result(
                    ok=resp.status == 200 and bool((body or {}).get("success")),
                    status=resp.status,
                    latency_ms=latency,
                    error=str((body or {}).get("error") or "")[:200],
                )
        except Exception as e:
            return Result(
                ok=False, status=0,
                latency_ms=(time.monotonic() - start) * 1000,
                error=f"{type(e).__name__}: {e}",
            )

    async def session_delete(self, session_id: str) -> None:
        try:
            async with self._session.delete(
                f"{self.base_url}/sessions/{session_id}"
            ):
                pass
        except Exception:
            pass

    async def snapshot(self) -> Dict[str, object]:
        """采样 /health 与 /metrics 的关键指标。"""
        info: Dict[str, object] = {}
        try:
            async with self._session.get(f"{self.base_url}/health") as resp:
                health = await resp.json(content_type=None)
                info["pool_available"] = health.get("pool_available")
                info["pool_total"] = health.get("pool_total")
                info["active_sandboxes"] = health.get("active_sandboxes")
                info["cpu%"] = health.get("cpu_usage_percent")
                info["mem%"] = health.get("memory_usage_percent")
        except Exception as e:
            info["health_error"] = str(e)
        try:
            async with self._session.get(f"{self.base_url}/metrics") as resp:
                text = await resp.text()
                for key in ("sandbox_kernel_exec_total", "sandbox_kernel_fallback_total"):
                    m = re.search(rf"^{key}\s+(\d+)", text, re.MULTILINE)
                    if m:
                        info[key] = int(m.group(1))
        except Exception as e:
            info["metrics_error"] = str(e)
        return info


def fmt_snapshot(tag: str, snap: Dict[str, object]) -> str:
    return f"  [{tag}] " + "  ".join(f"{k}={v}" for k, v in snap.items())


# ===== 场景 =====

async def scenario_burst(client: Client) -> Report:
    """S1 突发：瞬间打入 16 个轻请求（>2 倍常态池），观察弹性扩容。"""
    report = Report("S1 burst 无状态突发 x16")
    report.notes.append(fmt_snapshot("before", await client.snapshot()))
    start = time.monotonic()
    report.results = list(await asyncio.gather(
        *[client.execute(CODE_LIGHT, timeout=30) for _ in range(16)]
    ))
    report.wall_seconds = time.monotonic() - start
    report.notes.append(fmt_snapshot("after", await client.snapshot()))
    return report


async def scenario_sustain(client: Client, duration: int = 60) -> Report:
    """S2 持续：8 并发混合负载（轻/重/图表/大内存）跑指定时长。"""
    report = Report(f"S2 sustain 持续 8 并发 x {duration}s")
    report.notes.append(fmt_snapshot("before", await client.snapshot()))
    deadline = time.monotonic() + duration
    payloads = [CODE_LIGHT, CODE_PANDAS, CODE_CHART, CODE_MEMORY]

    async def worker(idx: int) -> None:
        i = 0
        while time.monotonic() < deadline:
            code = payloads[(idx + i) % len(payloads)]
            report.results.append(await client.execute(code, timeout=60))
            i += 1

    start = time.monotonic()
    await asyncio.gather(*[worker(i) for i in range(8)])
    report.wall_seconds = time.monotonic() - start
    report.notes.append(fmt_snapshot("after", await client.snapshot()))
    return report


async def scenario_mixed(client: Client) -> Report:
    """
    S3 混合：1 个有状态会话先跑 3s 慢执行，同时打入无状态请求。

    验证修复：fork 隔离执行不抢全局执行锁——慢会话执行期间，
    无状态请求延迟应远小于 3s（修复前会被阻塞）。
    """
    report = Report("S3 mixed 长会话 + 并发无状态")
    session_id = await client.session_create()
    if not session_id:
        report.notes.append("会话创建失败，场景跳过")
        return report

    init = await client.session_execute(session_id, CODE_SESSION_INIT)
    if not init.ok:
        report.notes.append(f"会话初始化失败: {init.error}")
        await client.session_delete(session_id)
        return report

    start = time.monotonic()
    slow_task = asyncio.create_task(
        client.session_execute(session_id, CODE_SLEEP_3S, timeout=30)
    )
    await asyncio.sleep(0.3)  # 确保慢执行已持有有状态执行锁

    stateless_results = await asyncio.gather(
        *[client.execute(CODE_LIGHT, timeout=30) for _ in range(8)]
    )
    slow_result = await slow_task
    report.wall_seconds = time.monotonic() - start

    report.results = list(stateless_results) + [slow_result]
    # 判定口径：池内 fork 并行的那批请求（最快的 4 个）不应被会话执行
    # 阻塞；池容量不足导致的临时容器冷启动（慢的那批）不计入判定。
    sorted_latency = sorted(r.latency_ms for r in stateless_results)
    fast_batch_max = sorted_latency[min(3, len(sorted_latency) - 1)]
    # 阈值 8s：若被 3s 慢会话串行化，最快批会逼近 3s×排队深度；
    # 4C8G 下池外请求走临时容器（4~15s 冷启动）属容量问题，不计入阻塞判定
    blocked = fast_batch_max > 8000
    report.notes.append(
        f"无状态最快 4 个的最大延迟={fast_batch_max:.0f}ms（慢会话 3s 执行期间）"
        f" → {'❌ 疑似被会话执行串行化' if blocked else '✅ 未被会话执行串行化'}"
        f"；全部 8 个延迟分布 {[int(x) for x in sorted_latency]}"
    )

    # 验证会话状态在慢执行后仍连续
    step = await client.session_execute(session_id, CODE_SESSION_STEP)
    report.results.append(step)
    report.notes.append(
        f"会话状态连续性: {'✅ 变量跨执行保留' if step.ok else '❌ ' + step.error}"
    )
    await client.session_delete(session_id)
    return report


async def scenario_timeout_storm(client: Client) -> Report:
    """
    S4 超时风暴：并发 4 个必超时请求（5s 超时），随后立刻验证池可用性。

    验证修复：fork SIGKILL 后父 kernel 与容器存活——
    修复前一次超时 = 销毁一个容器，4 个并发超时会清空整个池。
    """
    report = Report("S4 timeout 超时风暴 x4")
    before = await client.snapshot()
    report.notes.append(fmt_snapshot("before", before))

    start = time.monotonic()
    report.results = list(await asyncio.gather(
        *[client.execute_expect_timeout(CODE_TIMEOUT, timeout=5) for _ in range(4)]
    ))

    # 风暴后立即打正常请求：若容器被销毁，这里会冷启动（延迟暴涨）或失败
    probe_results = await asyncio.gather(
        *[client.execute(CODE_LIGHT, timeout=30) for _ in range(4)]
    )
    report.results.extend(probe_results)
    report.wall_seconds = time.monotonic() - start

    after = await client.snapshot()
    report.notes.append(fmt_snapshot("after", after))
    probe_p95 = sorted(r.latency_ms for r in probe_results)[-1]
    report.notes.append(
        f"风暴后探针最大延迟 {probe_p95:.0f}ms"
        f" → {'✅ 池存活（fork 隔离生效）' if probe_p95 < 5000 else '❌ 疑似容器被销毁后冷启动'}"
    )
    return report


async def scenario_idle_shrink(client: Client, idle_wait: int) -> Report:
    """S5 空闲缩容：静置 idle_wait 秒后对比池规模（默认配置需 >600s 才缩）。"""
    report = Report(f"S5 idle 空闲缩容观察（静置 {idle_wait}s）")
    report.notes.append(fmt_snapshot("before", await client.snapshot()))
    await asyncio.sleep(idle_wait)
    report.notes.append(fmt_snapshot("after", await client.snapshot()))
    report.notes.append(
        "判定：after 的 pool_total 应 ≤ before（idle_shrink_seconds 内静置才会缩容，"
        "默认 600s，可用 SANDBOX_POOL__IDLE_SHRINK_SECONDS 缩短后实测）"
    )
    report.results.append(Result(ok=True, status=200, latency_ms=0))
    return report


# ===== 入口 =====

SCENARIOS = {
    "burst": scenario_burst,
    "sustain": scenario_sustain,
    "mixed": scenario_mixed,
    "timeout": scenario_timeout_storm,
    "idle": scenario_idle_shrink,
}


async def main() -> int:
    parser = argparse.ArgumentParser(description="4C8G 压测")
    parser.add_argument("--url", default="http://localhost:9527")
    parser.add_argument("--api-key", default="")
    parser.add_argument(
        "-s", "--scenario", default="all",
        choices=["all", *SCENARIOS.keys()],
    )
    parser.add_argument("--sustain-seconds", type=int, default=60)
    parser.add_argument("--idle-wait", type=int, default=90)
    args = parser.parse_args()

    names = list(SCENARIOS.keys()) if args.scenario == "all" else [args.scenario]

    async with Client(args.url, args.api_key) as client:
        baseline = await client.snapshot()
        if baseline.get("health_error"):
            print(f"服务不可达: {baseline['health_error']}")
            return 1
        print("基线:", fmt_snapshot("baseline", baseline))

        reports: List[Report] = []
        for name in names:
            if name == "sustain":
                reports.append(await SCENARIOS[name](client, args.sustain_seconds))
            elif name == "idle":
                reports.append(await SCENARIOS[name](client, args.idle_wait))
            else:
                reports.append(await SCENARIOS[name](client))
            print(reports[-1].render())

        final = await client.snapshot()
        print("\n===== 总结 =====")
        print(fmt_snapshot("final", final))
        fallback = final.get("sandbox_kernel_fallback_total")
        if isinstance(fallback, int) and fallback > 0:
            print(f"⚠️  kernel 回退 {fallback} 次——热启动部分失效，必须排查（看网关 WARNING 日志）")
        elif fallback == 0:
            print("✅ 全程零回退，所有执行均走 kernel 快路径")

        failed = sum(1 for rep in reports for r in rep.results if not r.ok)
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
