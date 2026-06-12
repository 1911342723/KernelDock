import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sandbox_runtime import kernel_server


def setup_function():
    kernel_server._context_namespaces.clear()
    kernel_server._context_metadata.clear()
    kernel_server._initialized = False
    kernel_server.BACKUP_CAPTURE_CODE = ""
    kernel_server._new_namespace = lambda: {"__builtins__": __builtins__}


def test_execute_code_isolated_by_context_id():
    first = kernel_server.execute_code("value = 41", context_id="ctx_a")
    second = kernel_server.execute_code("print(value + 1)", context_id="ctx_a")
    third = kernel_server.execute_code("print('value' in globals())", context_id="ctx_b")

    assert first["success"] is True
    assert second["success"] is True
    assert second["context_id"] == "ctx_a"
    assert second["stdout"].splitlines()[-1] == "42"
    assert third["success"] is True
    assert third["context_id"] == "ctx_b"
    assert third["stdout"].splitlines()[-1] == "False"


def test_bootstrap_runs_once_per_context():
    first = kernel_server.execute_code(
        "print(seed)",
        bootstrap_source="seed = 7",
        context_id="ctx_seed",
    )
    second = kernel_server.execute_code(
        "print(seed)",
        bootstrap_source="seed = 99",
        context_id="ctx_seed",
    )

    assert first["success"] is True
    assert second["success"] is True
    assert first["stdout"].splitlines()[-1] == "7"
    assert second["stdout"].splitlines()[-1] == "7"


def test_execute_code_stream_emits_structured_events():
    events = []

    kernel_server.execute_code_stream(
        'print("hello")\nprint("SVG_BASE64_START:abc:SVG_BASE64_END")\nprint("TABLE_DATA_START:{\\"id\\":\\"t1\\"}:TABLE_DATA_END")',
        context_id="ctx_stream",
        send_event=events.append,
    )

    event_types = [event["type"] for event in events]
    assert "stdout" in event_types
    assert "chart" in event_types
    assert "table" in event_types
    assert event_types[-1] == "done"
    assert events[-1]["success"] is True
