"""E2B 风格适配层端到端测试（对运行中的服务执行）。

用法: python tests/e2b_e2e.py [--url http://localhost:9527]
"""

import argparse
import json
import sys
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:9527")
    base = parser.parse_args().url.rstrip("/")

    def post(path: str, payload: dict | None = None) -> dict:
        req = urllib.request.Request(
            base + path,
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"},
        )
        return json.load(urllib.request.urlopen(req, timeout=120))

    failures = 0

    sb = post("/e2b/sandboxes", {"metadata": {"purpose": "e2e-test"}})
    sid = sb["sandboxID"]
    print(f"1. create sandbox: {sid[:12]}  template={sb['templateID']}")

    r1 = post(f"/e2b/sandboxes/{sid}/code", {"code": "x = 21\nprint('init ok')"})
    ok1 = r1["error"] is None and any("init ok" in line for line in r1["logs"]["stdout"])
    print(f"2. run init: {'PASS' if ok1 else 'FAIL ' + str(r1)[:200]}")
    failures += 0 if ok1 else 1

    code2 = (
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1,2,3],[x,x*2,x*3])\n"
        "print('answer', x*2)"
    )
    r2 = post(f"/e2b/sandboxes/{sid}/code", {"code": code2})
    has_chart = any(res.get("png") or res.get("svg") for res in r2["results"])
    ok2 = r2["error"] is None and any("answer 42" in line for line in r2["logs"]["stdout"]) and has_chart
    print(f"3. stateful run + chart: {'PASS' if ok2 else 'FAIL ' + str(r2)[:300]}")
    failures += 0 if ok2 else 1

    r3 = post(f"/e2b/sandboxes/{sid}/code", {"code": "1/0"})
    ok3 = r3["error"] is not None and "ZeroDivisionError" in r3["error"]["value"]
    print(f"4. error mapping: {'PASS' if ok3 else 'FAIL ' + str(r3)[:200]}  "
          f"name={r3['error']['name'] if r3['error'] else None}")
    failures += 0 if ok3 else 1

    lst = json.load(urllib.request.urlopen(base + "/e2b/sandboxes"))
    print(f"5. list: {len(lst)} sandbox(es)")

    req = urllib.request.Request(base + f"/e2b/sandboxes/{sid}", method="DELETE")
    urllib.request.urlopen(req)
    print("6. delete: ok")

    print(f"\n结果: {'全部通过' if failures == 0 else str(failures) + ' 项失败'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
