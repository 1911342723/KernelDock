import os
import time
from pathlib import Path

import httpx
import pandas as pd
import pytest


SANDBOX_E2E_URL = os.environ.get("SANDBOX_E2E_URL", "").rstrip("/")
DEEPTHINK_DATA_ROOT = os.environ.get("DEEPTHINK_DATA_ROOT", "").strip()
SANDBOX_E2E_LOG_PATH = os.environ.get("SANDBOX_E2E_LOG_PATH", "").strip()
TARGET_MB = int(os.environ.get("VOLUME_MOUNT_E2E_TARGET_MB", "100"))
pytestmark = pytest.mark.skipif(
    not SANDBOX_E2E_URL or not DEEPTHINK_DATA_ROOT,
    reason="SANDBOX_E2E_URL or DEEPTHINK_DATA_ROOT not set; skipping live volume mount integration test",
)


async def _post_json(path: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.post(f"{SANDBOX_E2E_URL}{path}", json=payload)
        response.raise_for_status()
        return response.json() if response.content else {}


async def _delete(path: str) -> None:
    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.delete(f"{SANDBOX_E2E_URL}{path}")
        response.raise_for_status()


def _write_large_parquet(session_id: str, ref: str = "tbl_big") -> Path:
    session_dir = Path(DEEPTHINK_DATA_ROOT) / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = session_dir / f"{ref}.parquet"

    rows = 250_000
    while True:
        df = pd.DataFrame(
            {
                "idx": range(rows),
                "payload": [f"row-{i:07d}-" + ("x" * 96) for i in range(rows)],
            }
        )
        df.to_parquet(parquet_path, index=False, engine="pyarrow", compression="snappy")
        if parquet_path.stat().st_size >= TARGET_MB * 1024 * 1024:
            return parquet_path
        rows *= 2


@pytest.mark.asyncio
async def test_volume_mount_live_avoids_preload_and_keeps_startup_fast():
    session = await _post_json("/sessions", {})
    session_id = session["session_id"]
    parquet_path = _write_large_parquet(session_id)

    bootstrap_source = "\n".join(
        [
            "import pandas as pd",
            "TABLE_REFS = ['tbl_big']",
            "FOCUS_REF = 'tbl_big'",
            "_loaded_tables = {'tbl_big': pd.read_parquet('/data/tbl_big.parquet', engine='pyarrow')}",
            "tbl_big = _loaded_tables['tbl_big']",
            "df = tbl_big",
        ]
    )
    payload = {
        "code": "print(len(df))",
        "timeout": 120,
        "bootstrap_source": bootstrap_source,
    }

    start = time.perf_counter()
    result = await _post_json(f"/sessions/{session_id}/execute", payload)
    elapsed = time.perf_counter() - start

    assert "pre_load_parquet" not in payload
    assert parquet_path.exists()
    assert result["success"] is True
    assert result["stdout"].strip().isdigit()
    assert elapsed <= 2.0

    if SANDBOX_E2E_LOG_PATH:
        log_text = Path(SANDBOX_E2E_LOG_PATH).read_text(encoding="utf-8", errors="ignore")
        assert "pre_load_parquet requires bootstrap_source" not in log_text

    await _delete(f"/sessions/{session_id}")
