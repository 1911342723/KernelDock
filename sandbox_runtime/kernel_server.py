"""
Kernel Server — 容器内常驻的轻量级代码执行进程。

支持两种执行协议：
- `action=execute`：一次性返回聚合结果。
- `action=execute_stream`：逐行返回 NDJSON 事件流。
"""

import ctypes
import io
import json
import os
import re
import socket
import sys
import threading
import traceback
from datetime import datetime
from typing import Any, Dict, Optional


DEFAULT_CONTEXT_ID = "default"
KERNEL_PORT = int(os.environ.get("KERNEL_PORT", "9999"))
MAX_MSG_SIZE = 100 * 1024 * 1024

SVG_PATTERN = re.compile(r"SVG_BASE64_START:(.+?):SVG_BASE64_END", re.DOTALL)
TABLE_PATTERN = re.compile(r"TABLE_DATA_START:(.+?):TABLE_DATA_END", re.DOTALL)

BACKUP_CAPTURE_CODE = """
from sandbox_runtime.charts import capture_current_figures
capture_current_figures()
"""

_context_namespaces: Dict[str, Dict[str, Any]] = {}
_context_metadata: Dict[str, Dict[str, Any]] = {}
_initialized = False


def _emit_stream_event(send_event, event_type: str, **payload) -> None:
    send_event({"type": event_type, **payload})


class _StreamingEventWriter:
    def __init__(self, stream_name: str, send_event):
        self._stream_name = stream_name
        self._send_event = send_event
        self._buffer = ""

    def write(self, text: str) -> int:
        if not text:
            return 0
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit_line(line + "\n")
        return len(text)

    def flush(self) -> None:
        if self._buffer:
            self._emit_line(self._buffer)
            self._buffer = ""

    def _emit_line(self, text: str) -> None:
        stripped = text.rstrip("\n")
        if self._stream_name == "stdout":
            svg_match = SVG_PATTERN.fullmatch(stripped)
            if svg_match:
                _emit_stream_event(
                    self._send_event,
                    "chart",
                    chart={"path": None, "base64": svg_match.group(1).strip(), "format": "svg"},
                )
                return

            table_match = TABLE_PATTERN.fullmatch(stripped)
            if table_match:
                try:
                    table = json.loads(table_match.group(1).strip())
                except json.JSONDecodeError:
                    table = None
                if table is not None:
                    _emit_stream_event(self._send_event, "table", table=table)
                    return

        _emit_stream_event(self._send_event, self._stream_name, text=text)


def _new_namespace() -> Dict[str, Any]:
    init_code = """
import os
os.environ.setdefault('DATA_DIR', '/data')
os.environ.setdefault('OUTPUT_DIR', '/output')

from sandbox_runtime import setup
setup()

import functools
print = functools.partial(print, flush=True)

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns

from sandbox_runtime.charts import save_figure, capture_current_figures
from sandbox_runtime.tables import display_table, save_table

df = pd.DataFrame()
_loaded_tables = {}
TABLE_REFS = []
FOCUS_REF = None

DATA_DIR = os.environ.get('DATA_DIR', '/data')
OUTPUT_DIR = os.environ.get('OUTPUT_DIR', '/output')
"""
    namespace: Dict[str, Any] = {}
    try:
        exec(init_code, namespace)
    except Exception as exc:
        print(f"[Kernel] 初始化命名空间失败: {exc}", file=sys.stderr, flush=True)
    return namespace


def _init_namespace() -> None:
    global _initialized
    if _initialized:
        return
    reset_namespace(DEFAULT_CONTEXT_ID)
    _initialized = True
    print("[Kernel] 命名空间初始化完成", flush=True)


def _get_or_create_namespace(context_id: Optional[str]) -> tuple[str, Dict[str, Any]]:
    resolved_context_id = context_id or DEFAULT_CONTEXT_ID
    namespace = _context_namespaces.get(resolved_context_id)
    if namespace is None:
        reset_namespace(resolved_context_id)
        namespace = _context_namespaces[resolved_context_id]
    else:
        _context_metadata[resolved_context_id]["last_used_at"] = datetime.utcnow().isoformat()
    return resolved_context_id, namespace


def reset_namespace(context_id: Optional[str] = None) -> None:
    resolved_context_id = context_id or DEFAULT_CONTEXT_ID
    now = datetime.utcnow().isoformat()
    _context_namespaces[resolved_context_id] = _new_namespace()
    _context_metadata[resolved_context_id] = {
        "created_at": now,
        "last_used_at": now,
        "bootstrap_initialized": False,
    }


def create_context(context_id: Optional[str]) -> Dict[str, Any]:
    resolved_context_id, _ = _get_or_create_namespace(context_id)
    metadata = _context_metadata[resolved_context_id]
    return {
        "status": "ok",
        "context_id": resolved_context_id,
        "created_at": metadata["created_at"],
    }


def list_contexts() -> Dict[str, Any]:
    return {
        "status": "ok",
        "contexts": [
            {
                "context_id": context_id,
                "created_at": metadata.get("created_at"),
                "last_used_at": metadata.get("last_used_at"),
                "bootstrap_initialized": metadata.get("bootstrap_initialized", False),
            }
            for context_id, metadata in sorted(_context_metadata.items())
        ],
    }


def delete_context(context_id: Optional[str]) -> Dict[str, Any]:
    resolved_context_id = context_id or DEFAULT_CONTEXT_ID
    if resolved_context_id == DEFAULT_CONTEXT_ID:
        reset_namespace(DEFAULT_CONTEXT_ID)
        return {
            "status": "ok",
            "context_id": DEFAULT_CONTEXT_ID,
            "message": "默认上下文已重置",
        }
    namespace = _context_namespaces.pop(resolved_context_id, None)
    _context_metadata.pop(resolved_context_id, None)
    if namespace is None:
        return {"status": "not_found", "context_id": resolved_context_id}
    return {"status": "ok", "context_id": resolved_context_id}


def _force_terminate_thread(thread: threading.Thread) -> bool:
    if not thread.is_alive():
        return True
    tid = thread.ident
    if tid is None:
        return False
    result = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(tid), ctypes.py_object(SystemExit)
    )
    return result == 1


def _execute_common(
    code: str,
    timeout: int,
    bootstrap_source: Optional[str],
    context_id: Optional[str],
    *,
    stdout_writer,
    stderr_writer,
) -> tuple[bool, Optional[str], str]:
    resolved_context_id, namespace = _get_or_create_namespace(context_id)
    success = True
    error_msg = None

    try:
        from sandbox_runtime.charts import clear_captured_charts

        clear_captured_charts()
    except Exception:
        pass
    try:
        from sandbox_runtime.tables import clear_captured_tables

        clear_captured_tables()
    except Exception:
        pass

    def _run() -> None:
        nonlocal success, error_msg
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = stdout_writer
        sys.stderr = stderr_writer
        try:
            try:
                from sandbox_runtime.setup import get_font_info

                font_info = get_font_info()
                print(f"[Font] Selected: {font_info.get('selected_font')}")
                print(f"[Font] Sans-serif: {font_info.get('font_sans_serif')}")
            except Exception:
                pass

            if bootstrap_source:
                try:
                    pre_data_listing = os.listdir("/data")
                except (FileNotFoundError, PermissionError, OSError):
                    pre_data_listing = "<missing>"
                print(f"[Kernel] pre-bootstrap /data contents={pre_data_listing}")
                print(f"[Kernel] bootstrap_source_len={len(bootstrap_source)}")

                if not _context_metadata[resolved_context_id].get("bootstrap_initialized", False):
                    exec(bootstrap_source, namespace)
                    _context_metadata[resolved_context_id]["bootstrap_initialized"] = True

                try:
                    post_data_listing = os.listdir("/data")
                except (FileNotFoundError, PermissionError, OSError):
                    post_data_listing = "<missing>"
                df_obj = namespace.get("df")
                if df_obj is None:
                    df_shape_repr = "<no df>"
                elif hasattr(df_obj, "shape"):
                    df_shape_repr = str(df_obj.shape)
                else:
                    df_shape_repr = f"<{type(df_obj).__name__}>"
                print(f"[Kernel] post-bootstrap /data contents={post_data_listing}")
                print(
                    f"[Kernel] bootstrap done, context_id={resolved_context_id}, df.shape={df_shape_repr}, "
                    f"_loaded_tables={list(namespace.get('_loaded_tables', {}).keys())}"
                )
            else:
                print(f"[Kernel] no bootstrap_source provided, context_id={resolved_context_id}")

            exec(code, namespace)
            exec(BACKUP_CAPTURE_CODE, namespace)
        except SystemExit:
            pass
        except Exception:
            success = False
            error_msg = traceback.format_exc()
            stderr_writer.write(error_msg)
        finally:
            stdout_writer.flush()
            stderr_writer.flush()
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        _force_terminate_thread(thread)
        thread.join(timeout=2)
        reset_namespace(resolved_context_id)
        return False, f"执行超时（{timeout}秒）", resolved_context_id

    return success, error_msg, resolved_context_id


def _build_result(
    stdout_text: str,
    stderr_text: str,
    success: bool,
    error_msg: Optional[str],
    elapsed: float,
    context_id: Optional[str] = None,
) -> Dict[str, Any]:
    charts = []
    for match in SVG_PATTERN.finditer(stdout_text):
        svg_b64 = match.group(1).strip()
        if svg_b64:
            charts.append({"path": None, "base64": svg_b64, "format": "svg"})

    clean_stdout = SVG_PATTERN.sub("[图表已生成]", stdout_text)

    tables = []
    for match in TABLE_PATTERN.finditer(stdout_text):
        try:
            table_data = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            continue
        tables.append(table_data)

    clean_stdout = TABLE_PATTERN.sub("[表格数据已捕获]", clean_stdout)

    output = clean_stdout
    if stderr_text:
        output += f"\n[stderr]:\n{stderr_text}"

    if len(output) > 100000:
        output = output[:100000] + "\n... (输出已截断)"

    return {
        "success": success,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "output": output,
        "charts": charts,
        "tables": tables,
        "images": [c["path"] for c in charts if c.get("path")],
        "error": error_msg,
        "execution_time_ms": int(elapsed * 1000),
        "context_id": context_id or DEFAULT_CONTEXT_ID,
    }


def execute_code(
    code: str,
    timeout: int = 300,
    bootstrap_source: Optional[str] = None,
    context_id: Optional[str] = None,
) -> Dict[str, Any]:
    import time

    start = time.monotonic()
    captured_out = io.StringIO()
    captured_err = io.StringIO()
    success, error_msg, resolved_context_id = _execute_common(
        code,
        timeout,
        bootstrap_source,
        context_id,
        stdout_writer=captured_out,
        stderr_writer=captured_err,
    )
    elapsed = time.monotonic() - start

    if not success and error_msg and error_msg.startswith("执行超时"):
        return {
            "success": False,
            "stdout": captured_out.getvalue(),
            "stderr": captured_err.getvalue(),
            "output": f"[Timeout]: 执行超过 {timeout} 秒限制",
            "charts": [],
            "tables": [],
            "images": [],
            "error": error_msg,
            "execution_time_ms": int(elapsed * 1000),
            "kernel_unhealthy": True,
            "context_id": resolved_context_id,
        }

    return _build_result(
        captured_out.getvalue(),
        captured_err.getvalue(),
        success,
        error_msg,
        elapsed,
        context_id=resolved_context_id,
    )


def execute_code_stream(
    code: str,
    timeout: int = 300,
    bootstrap_source: Optional[str] = None,
    context_id: Optional[str] = None,
    send_event=None,
) -> None:
    import time

    if send_event is None:
        raise ValueError("send_event is required for execute_code_stream")

    start = time.monotonic()
    stdout_writer = _StreamingEventWriter("stdout", send_event)
    stderr_writer = _StreamingEventWriter("stderr", send_event)
    success, error_msg, resolved_context_id = _execute_common(
        code,
        timeout,
        bootstrap_source,
        context_id,
        stdout_writer=stdout_writer,
        stderr_writer=stderr_writer,
    )
    elapsed = time.monotonic() - start

    if not success and error_msg:
        _emit_stream_event(
            send_event,
            "error",
            error=error_msg,
            context_id=resolved_context_id,
        )

    _emit_stream_event(
        send_event,
        "done",
        success=success,
        error=error_msg,
        execution_time_ms=int(elapsed * 1000),
        context_id=resolved_context_id,
        timed_out=bool(error_msg and error_msg.startswith("执行超时")),
    )


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def _send_response(conn: socket.socket, response: dict) -> None:
    payload = json.dumps(response, ensure_ascii=False, default=str).encode("utf-8")
    conn.sendall(len(payload).to_bytes(4, "big") + payload)


def _send_stream_response(conn: socket.socket, event: dict) -> None:
    payload = (json.dumps(event, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    conn.sendall(payload)


def handle_client(conn: socket.socket, addr: tuple) -> None:
    try:
        header = _recv_exact(conn, 4)
        if not header:
            return
        msg_len = int.from_bytes(header, "big")
        if msg_len > MAX_MSG_SIZE:
            _send_response(conn, {"success": False, "error": "消息过大"})
            return

        raw = _recv_exact(conn, msg_len)
        if not raw:
            return

        request = json.loads(raw.decode("utf-8"))
        action = request.get("action", "execute")

        if action == "ping":
            response = {"status": "ok"}
        elif action == "reset":
            resolved_context_id = request.get("context_id") or DEFAULT_CONTEXT_ID
            reset_namespace(resolved_context_id)
            response = {"status": "ok", "context_id": resolved_context_id, "message": "命名空间已重置"}
        elif action == "create_context":
            response = create_context(request.get("context_id"))
        elif action == "list_contexts":
            response = list_contexts()
        elif action == "delete_context":
            response = delete_context(request.get("context_id"))
        elif action == "execute_stream":
            execute_code_stream(
                request.get("code", ""),
                request.get("timeout", 300),
                bootstrap_source=request.get("bootstrap_source") or None,
                context_id=request.get("context_id"),
                send_event=lambda event: _send_stream_response(conn, event),
            )
            return
        elif action == "execute":
            response = execute_code(
                request.get("code", ""),
                request.get("timeout", 300),
                bootstrap_source=request.get("bootstrap_source") or None,
                context_id=request.get("context_id"),
            )
        else:
            response = {"success": False, "error": f"未知命令: {action}"}

        _send_response(conn, response)
    except Exception as exc:
        try:
            _send_response(conn, {"success": False, "error": str(exc)})
        except Exception:
            pass
    finally:
        conn.close()


def serve_forever() -> None:
    _init_namespace()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", KERNEL_PORT))
    server.listen(32)

    print(f"[Kernel] 监听 0.0.0.0:{KERNEL_PORT}", flush=True)

    while True:
        try:
            conn, addr = server.accept()
            handle_client(conn, addr)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[Kernel] 连接处理异常: {exc}", file=sys.stderr, flush=True)

    server.close()
    print("[Kernel] 服务器已关闭", flush=True)


if __name__ == "__main__":
    serve_forever()
