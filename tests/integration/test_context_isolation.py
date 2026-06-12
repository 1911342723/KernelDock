import base64
import io
import os

import httpx
import pandas as pd
import pytest


SANDBOX_E2E_URL = os.environ.get("SANDBOX_E2E_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(
    not SANDBOX_E2E_URL,
    reason="SANDBOX_E2E_URL not set; skipping live context isolation integration test",
)


def _build_preload_payload() -> tuple[dict, str]:
    dataframe = pd.DataFrame({"value": [1, 2, 3]})
    buffer = io.BytesIO()
    dataframe.to_parquet(buffer, index=False, engine="pyarrow")
    pre_load_parquet = {
        "tbl_main": base64.b64encode(buffer.getvalue()).decode("ascii"),
    }
    bootstrap_source = "\n".join(
        [
            "import pandas as pd",
            "TABLE_REFS = ['tbl_main']",
            "FOCUS_REF = 'tbl_main'",
            "_loaded_tables = {'tbl_main': pd.read_parquet('/data/tbl_main.parquet', engine='pyarrow')}",
            "tbl_main = _loaded_tables['tbl_main']",
            "df = tbl_main",
        ]
    )
    return pre_load_parquet, bootstrap_source


async def _post_json(path: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(f"{SANDBOX_E2E_URL}{path}", json=payload)
        response.raise_for_status()
        if response.content:
            return response.json()
        return {}


@pytest.mark.asyncio
async def test_context_isolation_and_fork_rehydration_live():
    pre_load_parquet, bootstrap_source = _build_preload_payload()

    session = await _post_json("/sessions", {})
    session_id = session["session_id"]

    parent = await _post_json(f"/sessions/{session_id}/contexts", {"language": "python"})
    sibling = await _post_json(f"/sessions/{session_id}/contexts", {"language": "python"})

    parent_result = await _post_json(
        f"/sessions/{session_id}/execute",
        {
            "context_id": parent["context_id"],
            "code": "x = 1\nprint(int(df['value'].sum()))",
            "timeout": 60,
            "pre_load_parquet": pre_load_parquet,
            "bootstrap_source": bootstrap_source,
        },
    )
    assert parent_result["success"] is True
    assert parent_result["stdout"].splitlines()[-1] == "6"

    sibling_result = await _post_json(
        f"/sessions/{session_id}/execute",
        {
            "context_id": sibling["context_id"],
            "code": "print(x)",
            "timeout": 60,
        },
    )
    assert sibling_result["success"] is False
    assert "NameError" in ((sibling_result.get("stderr") or "") + (sibling_result.get("error") or ""))

    forked = await _post_json(
        f"/sessions/{session_id}/contexts",
        {"language": "python", "fork_from": parent["context_id"]},
    )
    forked_result = await _post_json(
        f"/sessions/{session_id}/execute",
        {
            "context_id": forked["context_id"],
            "code": "print(int(df['value'].sum()))\nprint('x' in globals())",
            "timeout": 60,
        },
    )
    assert forked_result["success"] is True
    assert forked_result["stdout"].splitlines()[-2:] == ["6", "False"]
