import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import context_helpers, runtime
from app.services.context_manager import ContextManager


@pytest.fixture(autouse=True)
def reset_context_manager():
    original = runtime.context_manager
    runtime.context_manager = ContextManager()
    try:
        yield
    finally:
        runtime.context_manager = original


def test_resolve_execute_context_id_creates_and_reuses_default_context():
    first = context_helpers._resolve_execute_context_id("sess_1", None)
    second = context_helpers._resolve_execute_context_id("sess_1", None)

    assert first == second
    assert len(runtime.context_manager.list_contexts("sess_1")) == 1


def test_resolve_execute_context_id_rejects_context_from_other_session():
    foreign = runtime.context_manager.create_context("sess_foreign")

    with pytest.raises(HTTPException) as exc_info:
        context_helpers._resolve_execute_context_id("sess_local", foreign.context_id)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Context not found"


def test_render_context_bootstrap_uses_saved_table_refs_and_focus():
    context = runtime.context_manager.create_context("sess_1")
    context.data_refs = ("tbl_a", "tbl_b")
    context.focus_ref = "tbl_b"

    bootstrap = context_helpers._render_context_bootstrap(
        context,
        data_dir="/data",
        output_dir="/output",
    )

    assert bootstrap is not None
    assert "tbl_a" in bootstrap
    assert "tbl_b" in bootstrap
    assert "pd.read_parquet" in bootstrap
    compile(bootstrap, "<context_bootstrap>", "exec")
