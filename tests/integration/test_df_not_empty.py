import base64
import io
import os
import sys

import pandas as pd
import pytest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_ROOT = os.path.dirname(os.path.dirname(TEST_DIR))
WORKSPACE_ROOT = os.path.dirname(SERVICE_ROOT)
BACKEND_ROOT = os.path.join(WORKSPACE_ROOT, "backend")

sys.path.insert(0, SERVICE_ROOT)
sys.path.insert(0, BACKEND_ROOT)

import httpx

from infrastructure.execution.stateless_executor import render_bootstrap


SANDBOX_E2E_URL = os.environ.get("SANDBOX_E2E_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(
    not SANDBOX_E2E_URL,
    reason="Set SANDBOX_E2E_URL=http://host:port to run live sandbox integration tests.",
)


def _make_three_sheet_xlsx() -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame({"city": ["A", "B", "C"], "sales": [12, 18, 25]}).to_excel(
            writer,
            sheet_name="Sales",
            index=False,
        )
        pd.DataFrame({"city": ["A", "B", "C"], "profit": [3.2, 4.5, 6.1]}).to_excel(
            writer,
            sheet_name="Profit",
            index=False,
        )
        pd.DataFrame({"city": ["A", "B", "C"], "cost": [8.8, 13.5, 18.9]}).to_excel(
            writer,
            sheet_name="Cost",
            index=False,
        )
    return buf.getvalue()


def _build_multitable_payload() -> dict:
    workbook_bytes = _make_three_sheet_xlsx()
    sheets = pd.read_excel(io.BytesIO(workbook_bytes), sheet_name=None)

    refs = [f"e2e_{sheet_name}" for sheet_name in sheets.keys()]
    focus_ref = refs[1]
    pre_load_parquet = {}

    for ref, (_, dataframe) in zip(refs, sheets.items()):
        parquet_buf = io.BytesIO()
        dataframe.to_parquet(parquet_buf, engine="pyarrow", compression="snappy", index=False)
        pre_load_parquet[ref] = base64.b64encode(parquet_buf.getvalue()).decode("ascii")

    code = (
        "print(f'SHAPE={df.shape[0]},{df.shape[1]}')\n"
        "print('KEYS=' + ','.join(sorted(_loaded_tables.keys())))\n"
        "print('FOCUS=' + str(FOCUS_REF))\n"
    )

    return {
        "code": code,
        "timeout": 30,
        "pre_load_parquet": pre_load_parquet,
        "bootstrap_source": render_bootstrap(
            refs=refs,
            focus=focus_ref,
            data_dir="/data",
            output_dir="/output",
        ),
        "expected_refs": refs,
        "expected_focus": focus_ref,
    }


async def _post_execute(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{SANDBOX_E2E_URL}/execute",
            json={
                "code": payload["code"],
                "timeout": payload["timeout"],
                "pre_load_parquet": payload["pre_load_parquet"],
                "bootstrap_source": payload["bootstrap_source"],
            },
        )
    response.raise_for_status()
    return response.json()


def _assert_df_loaded(result: dict, expected_refs: list[str], expected_focus: str) -> None:
    assert result["success"] is True, result.get("error") or result.get("stderr")

    output = result.get("output", "")
    shape_line = next(line for line in output.splitlines() if line.startswith("SHAPE="))
    keys_line = next(line for line in output.splitlines() if line.startswith("KEYS="))
    focus_line = next(line for line in output.splitlines() if line.startswith("FOCUS="))

    rows, cols = [int(part) for part in shape_line.split("=", 1)[1].split(",", 1)]
    loaded_refs = keys_line.split("=", 1)[1].split(",")
    loaded_focus = focus_line.split("=", 1)[1]

    assert rows > 0
    assert cols > 0
    assert loaded_refs == sorted(expected_refs)
    assert loaded_focus == expected_focus


@pytest.mark.asyncio
async def test_df_not_empty_across_three_repeated_executes():
    payload = _build_multitable_payload()

    for _ in range(3):
        result = await _post_execute(payload)
        _assert_df_loaded(result, payload["expected_refs"], payload["expected_focus"])


@pytest.mark.asyncio
async def test_df_not_empty_under_concurrent_executes():
    payload = _build_multitable_payload()

    results = await pytest.importorskip("asyncio").gather(
        *[_post_execute(payload) for _ in range(3)]
    )

    for result in results:
        _assert_df_loaded(result, payload["expected_refs"], payload["expected_focus"])