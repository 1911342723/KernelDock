"""
执行可观测性：结构化执行事件日志、Sentry 失败上报、执行指标记录
"""

import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Optional

try:
    import sentry_sdk
except ImportError:  # pragma: no cover - optional dependency
    sentry_sdk = None

from .config import settings

logger = logging.getLogger(__name__)


def _execution_code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]


def _log_execution_event(
    event: str,
    *,
    session_id: str,
    context_id: Optional[str],
    container_id: Optional[str],
    code: str,
    duration_ms: Optional[int] = None,
    success: Optional[bool] = None,
    chart_count: Optional[int] = None,
    table_count: Optional[int] = None,
) -> None:
    payload = {
        "event": event,
        "session_id": session_id,
        "context_id": context_id,
        "container_id": container_id,
        "code_hash": _execution_code_hash(code),
        "timestamp": datetime.utcnow().isoformat(),
    }
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if success is not None:
        payload["success"] = success
    if chart_count is not None:
        payload["chart_count"] = chart_count
    if table_count is not None:
        payload["table_count"] = table_count
    logger.info(json.dumps(payload, ensure_ascii=False, default=str))


def _should_report_failure_to_sentry(result: dict[str, Any]) -> bool:
    error_text = str(result.get("error") or result.get("stderr") or "").lower()
    return any(token in error_text for token in ("oom", "out of memory", "timeout", "timed out", "killed", "crash", "segfault", "超时"))


async def _report_execution_failure_to_sentry(
    *,
    session_id: str,
    container_id: Optional[str],
    result: dict[str, Any],
) -> None:
    from . import runtime

    if not sentry_sdk or not settings.sentry_dsn or not container_id or not runtime.sandbox_manager:
        return
    if not _should_report_failure_to_sentry(result):
        return

    logs_tail = {"stdout": "", "stderr": ""}
    try:
        logs_tail = await runtime.sandbox_manager._docker_client.get_container_logs_tail(container_id)
    except Exception as exc:  # pragma: no cover - best effort diagnostics
        logger.warning(f"读取容器日志尾部失败: {exc}")

    with sentry_sdk.push_scope() as scope:
        scope.set_tag("session_id", session_id)
        scope.set_tag("container_id", container_id)
        scope.set_context("container_logs", logs_tail)
        scope.set_context(
            "execution_result",
            {
                "success": result.get("success", False),
                "error": result.get("error"),
                "stderr": (result.get("stderr") or "")[-1024:],
                "stdout": (result.get("stdout") or "")[-1024:],
            },
        )
        sentry_sdk.capture_exception(RuntimeError(result.get("error") or "sandbox execution failed"))


def _record_execution_metrics(result: dict[str, Any]) -> None:
    from . import runtime

    if not runtime.health_monitor or not isinstance(result, dict):
        return
    runtime.health_monitor.record_execution(
        duration_seconds=max(0.0, float(result.get("execution_time_ms", 0) or 0) / 1000.0),
        success=bool(result.get("success", False)),
    )
