import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import observability, runtime
from app.config import settings
from app.routes.sandboxes import admin_list_sandboxes
from app.services.health_monitor import SandboxMetrics
from app.services.sandbox_manager import SandboxInfo, SandboxState


def test_log_execution_event_emits_json(monkeypatch):
    messages = []
    monkeypatch.setattr(observability.logger, "info", messages.append)

    observability._log_execution_event(
        "execute_end",
        session_id="sess-1",
        context_id="ctx-1",
        container_id="container-1",
        code="print('hi')",
        duration_ms=123,
        success=True,
        chart_count=2,
        table_count=1,
    )

    payload = json.loads(messages[0])
    assert payload["event"] == "execute_end"
    assert payload["session_id"] == "sess-1"
    assert payload["context_id"] == "ctx-1"
    assert payload["container_id"] == "container-1"
    assert payload["duration_ms"] == 123
    assert payload["success"] is True
    assert payload["chart_count"] == 2
    assert payload["table_count"] == 1
    assert payload["code_hash"]
    assert payload["timestamp"]


class _FakeScope:
    def __init__(self):
        self.tags = {}
        self.contexts = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def set_tag(self, key, value):
        self.tags[key] = value

    def set_context(self, key, value):
        self.contexts[key] = value


class _FakeSentry:
    def __init__(self):
        self.scope = _FakeScope()
        self.captured = []

    def push_scope(self):
        return self.scope

    def capture_exception(self, exc):
        self.captured.append(exc)


@pytest.mark.asyncio
async def test_report_execution_failure_to_sentry_includes_logs(monkeypatch):
    fake_sentry = _FakeSentry()
    fake_docker_client = SimpleNamespace(
        get_container_logs_tail=AsyncMock(return_value={"stdout": "stdout-tail", "stderr": "stderr-tail"})
    )
    fake_manager = SimpleNamespace(_docker_client=fake_docker_client)

    monkeypatch.setattr(observability, "sentry_sdk", fake_sentry)
    monkeypatch.setattr(settings, "sentry_dsn", "https://example.com/1")
    monkeypatch.setattr(runtime, "sandbox_manager", fake_manager)

    await observability._report_execution_failure_to_sentry(
        session_id="sess-1",
        container_id="container-1",
        result={
            "success": False,
            "error": "execution timed out",
            "stdout": "stdout-body",
            "stderr": "stderr-body",
        },
    )

    assert len(fake_sentry.captured) == 1
    assert fake_sentry.scope.tags["session_id"] == "sess-1"
    assert fake_sentry.scope.tags["container_id"] == "container-1"
    assert fake_sentry.scope.contexts["container_logs"]["stdout"] == "stdout-tail"
    assert fake_sentry.scope.contexts["container_logs"]["stderr"] == "stderr-tail"
    assert fake_sentry.scope.contexts["execution_result"]["error"] == "execution timed out"


@pytest.mark.asyncio
async def test_admin_list_sandboxes_requires_token(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "secret-token")

    with pytest.raises(HTTPException) as exc_info:
        await admin_list_sandboxes(None)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_list_sandboxes_returns_expected_fields(monkeypatch):
    now = datetime.utcnow()
    sandbox = SandboxInfo(
        sandbox_id="sandbox-1",
        session_id="sess-1",
        container_id="container-1",
        state=SandboxState.RUNNING,
        created_at=now,
        last_activity=now,
        cpu_limit=1.0,
        memory_limit_mb=512,
        disk_limit_mb=1024,
        network_enabled=False,
        data_dir="/data/sess-1",
        output_dir="/output/sess-1",
    )
    metrics = SandboxMetrics(
        sandbox_id="sandbox-1",
        cpu_percent=12.345,
        memory_used_mb=45.678,
        memory_limit_mb=512,
        disk_used_mb=1.0,
        disk_limit_mb=1024,
        network_rx_bytes=10,
        network_tx_bytes=20,
        timestamp=now,
    )

    monkeypatch.setattr(settings, "admin_token", "secret-token")
    monkeypatch.setattr(
        runtime,
        "sandbox_manager",
        SimpleNamespace(list_sandboxes=AsyncMock(return_value=[sandbox])),
    )
    monkeypatch.setattr(
        runtime,
        "health_monitor",
        SimpleNamespace(get_sandbox_metrics=AsyncMock(return_value=metrics)),
    )
    monkeypatch.setattr(
        runtime,
        "context_manager",
        SimpleNamespace(list_contexts=lambda session_id: ["ctx-1", "ctx-2"]),
    )

    result = await admin_list_sandboxes("secret-token")

    assert result == [
        {
            "container_id": "container-1",
            "session_id": "sess-1",
            "context_count": 2,
            "created_at": now.isoformat(),
            "last_execution_at": now.isoformat(),
            "cpu_usage": 12.35,
            "memory_mb": 45.68,
        }
    ]
