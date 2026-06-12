import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import runtime
from app.routes import execution as execution_routes
from app.schemas import ExecuteCodeRequest
from app.services.context_manager import ContextManager


class _FakeSandboxInfo:
    sandbox_id = "sb_1"
    container_id = "container_1"
    state = None
    cpu_limit = None
    memory_limit_mb = None
    network_enabled = False


class _FakeSandboxManager:
    async def get_sandbox_by_session(self, session_id):
        return _FakeSandboxInfo()

    async def stream_execute_code(self, **kwargs):
        yield {"type": "stdout", "text": "line 1\n"}
        yield {"type": "chart", "chart": {"base64": "abc", "format": "svg", "path": None}}
        yield {"type": "done", "success": True, "error": None, "execution_time_ms": 12, "context_id": kwargs.get("context_id"), "timed_out": False}


@pytest.fixture(autouse=True)
def reset_streaming_globals():
    original_context_manager = runtime.context_manager
    original_sandbox_manager = runtime.sandbox_manager
    original_execution_queue = runtime.execution_queue
    original_session_store = runtime.session_store
    runtime.context_manager = ContextManager()
    runtime.sandbox_manager = _FakeSandboxManager()
    runtime.execution_queue = None
    runtime.session_store = None
    try:
        yield
    finally:
        runtime.context_manager = original_context_manager
        runtime.sandbox_manager = original_sandbox_manager
        runtime.execution_queue = original_execution_queue
        runtime.session_store = original_session_store


@pytest.mark.asyncio
async def test_execute_streaming_encodes_sse_events():
    request = ExecuteCodeRequest(code="print('hello')")

    chunks = []
    async for chunk in execution_routes._execute_streaming("sess_stream", request):
        chunks.append(chunk)

    body = "".join(chunks)
    assert "event: stdout" in body
    assert "event: chart" in body
    assert "event: done" in body
