"""单请求端到端延迟探针（对运行中的服务）。"""

import json
import sys
import time
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9527"

CODE = 'import pandas as pd\nprint(len(pd.DataFrame({"a": [1, 2, 3]})))'


def execute(code: str) -> dict:
    req = urllib.request.Request(
        BASE + "/execute",
        data=json.dumps({"code": code, "timeout": 30}).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=90))


execute("print('warmup')")

latencies = []
for i in range(8):
    t0 = time.monotonic()
    d = execute(CODE)
    e2e = int((time.monotonic() - t0) * 1000)
    latencies.append(e2e)
    print(
        f"round {i+1}: e2e={e2e}ms  "
        f"exec={d['execution_info']['execution_time_ms']}ms  "
        f"mode={d['sandbox_info']['mode']}  success={d['success']}"
    )

latencies.sort()
print(f"\ne2e P50={latencies[len(latencies)//2]}ms  min={latencies[0]}ms  max={latencies[-1]}ms")
