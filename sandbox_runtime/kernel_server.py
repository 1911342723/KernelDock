"""
Kernel Server — 容器内常驻的轻量级代码执行进程

架构：
    容器启动时运行此服务，监听 TCP 端口 9999。
    宿主机的 CodeExecutor 通过 TCP（或 docker exec 中继）发送代码。

    两种执行模式：
    - 有状态（isolated=False，默认）：代码在常驻命名空间执行，
      DataFrame 等变量跨请求保留，适合 session 场景。
    - 无状态（isolated=True）：fork 出子进程执行（zygote 模式）。
      子进程通过 CoW 与父进程物理共享全部已预热库的内存页，
      每个实例新增内存仅几 MB；超时直接 SIGKILL 子进程，
      父进程命名空间零污染，无需 reset、无需销毁容器。

协议（4 字节长度头 + JSON over TCP）：
    请求: {"action": "execute", "code": "...", "timeout": 300, "isolated": true}
    响应: {"success": true, "stdout": "...", "charts": [...], "tables": [...]}

    流式请求 execute_stream 的响应为 NDJSON 行（无长度头），
    由 kernel_relay_stream 原样转发给宿主机逐行解析。

    其他 action: ping / reset / create_context / list_contexts / delete_context

并发模型：
    每个连接由独立线程处理（ping 永不被长执行阻塞）。
    - 有状态执行：全局锁串行化（保证命名空间一致性）。
    - fork 隔离执行：信号量限流（KERNEL_MAX_FORKS，默认 4），可并行。
"""

import base64
import ctypes
import io
import json
import os
import re
import signal
import socket
import struct
import sys
import threading
import time
import traceback
from datetime import datetime
from typing import Any, Dict, Optional


# ========== KSM（Kernel Samepage Merging）支持 ==========

def _enable_ksm_merge() -> None:
    """
    尽力开启进程级 KSM 合并（Linux 6.1+ 的 prctl PR_SET_MEMORY_MERGE=67）。

    宿主机开启 ksm/ksmtuned 后，多个同构沙箱容器中内容相同的匿名内存页
    会被内核合并为 CoW 共享页，实测同构 Python 堆可省 20%~50% 内存。
    fork 出的子进程自动继承该标志。内核不支持时静默忽略。
    """
    PR_SET_MEMORY_MERGE = 67
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        res = libc.prctl(PR_SET_MEMORY_MERGE, 1, 0, 0, 0)
        if res == 0:
            print("[Kernel] KSM memory merge enabled (PR_SET_MEMORY_MERGE)", flush=True)
    except Exception:
        pass


# ========== 全局命名空间 ==========

DEFAULT_CONTEXT_ID = "default"

_context_namespaces: Dict[str, Dict[str, Any]] = {}
_context_metadata: Dict[str, Dict[str, Any]] = {}
_initialized = False

# 有状态执行 / 命名空间变更操作的全局锁
_exec_lock = threading.Lock()
# fork 隔离执行的并发上限
_MAX_FORKS = int(os.environ.get("KERNEL_MAX_FORKS", "4"))
_fork_sema = threading.BoundedSemaphore(_MAX_FORKS)

# 预热初始化代码。
# 注意 seaborn 改为懒加载代理：import seaborn 约占 60~100MB 且多数请求
# 用不到；首次访问 sns.xxx 时才真正 import 并替换全局名。
_INIT_CODE = """
import os
os.environ.setdefault('DATA_DIR', '/data')
os.environ.setdefault('OUTPUT_DIR', '/output')

# BLAS 单线程（必须在 numpy import 前设置）：
# 单沙箱 1C 限额下多线程 BLAS 无收益，且并发 fork 会撞 pids_limit
for _v in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
           'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS'):
    os.environ.setdefault(_v, '1')

from sandbox_runtime import setup
setup()

import functools
print = functools.partial(print, flush=True)

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# seaborn 默认懒加载（省 ~80MB 常驻内存）；
# 但 fork 隔离模式下子进程内首次 import 要付 1~2s 代价，
# seaborn 重度场景可设 KERNEL_PRELOAD_SEABORN=1 在父进程预热（CoW 共享）。
if os.environ.get('KERNEL_PRELOAD_SEABORN', '0') == '1':
    import seaborn as sns
else:
    class _LazySeaborn:
        \"\"\"seaborn 懒加载代理：首次属性访问时才真正 import。\"\"\"
        def __getattr__(self, name):
            import seaborn as _seaborn
            globals()['sns'] = _seaborn
            return getattr(_seaborn, name)

    sns = _LazySeaborn()

from sandbox_runtime.charts import save_figure, capture_current_figures
from sandbox_runtime.tables import display_table, save_table

# 多表分析：由 bootstrap_source 按 TableRef 读 parquet 并注入。
# 这里给一个空 DataFrame 兜底，防止无 bootstrap 的场景直接 NameError。
df = pd.DataFrame()
_loaded_tables = {}
TABLE_REFS = []
FOCUS_REF = None

DATA_DIR = os.environ.get('DATA_DIR', '/data')
OUTPUT_DIR = os.environ.get('OUTPUT_DIR', '/output')
"""

BACKUP_CAPTURE_CODE = """
from sandbox_runtime.charts import capture_current_figures
capture_current_figures()
"""

SVG_PATTERN = re.compile(r'SVG_BASE64_START:(.+?):SVG_BASE64_END', re.DOTALL)
TABLE_PATTERN = re.compile(r'TABLE_DATA_START:(.+?):TABLE_DATA_END', re.DOTALL)

MAX_OUTPUT_LENGTH = 100000


def _new_namespace() -> Dict[str, Any]:
    """创建并预热一个新的 context 命名空间（只做库导入，不做数据加载）。"""
    namespace: Dict[str, Any] = {}
    try:
        exec(_INIT_CODE, namespace)
    except Exception as e:
        print(f"[Kernel] 初始化命名空间失败: {e}", file=sys.stderr, flush=True)
    return namespace


def _init_namespace() -> None:
    """初始化默认 context 命名空间。"""
    global _initialized
    if _initialized:
        return

    # 冷启动耗时可观测（production-hardening #9）：预热库导入是 kernel
    # 就绪前的主要开销，打点出来才能量化优化收益、定位回退根因。
    _t0 = time.monotonic()
    now = datetime.utcnow().isoformat()
    _context_namespaces[DEFAULT_CONTEXT_ID] = _new_namespace()
    _context_metadata[DEFAULT_CONTEXT_ID] = {
        "created_at": now,
        "last_used_at": now,
        "bootstrap_initialized": False,
    }

    _initialized = True
    _warmup_ms = int((time.monotonic() - _t0) * 1000)
    print(f"[Kernel] 命名空间初始化完成 (预热 {_warmup_ms}ms)", flush=True)


def _get_or_create_namespace(context_id: Optional[str]) -> "tuple[str, Dict[str, Any]]":
    resolved_context_id = context_id or DEFAULT_CONTEXT_ID
    namespace = _context_namespaces.get(resolved_context_id)
    if namespace is None:
        namespace = _new_namespace()
        now = datetime.utcnow().isoformat()
        _context_namespaces[resolved_context_id] = namespace
        _context_metadata[resolved_context_id] = {
            "created_at": now,
            "last_used_at": now,
            "bootstrap_initialized": False,
        }
    else:
        _context_metadata[resolved_context_id]["last_used_at"] = datetime.utcnow().isoformat()
    return resolved_context_id, namespace


def reset_namespace(context_id: Optional[str] = None) -> None:
    """重置指定 context；未指定时重置默认 context。"""
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


# ========== 公共执行体 ==========

def _run_user_code(
    code: str,
    namespace: Dict[str, Any],
    bootstrap_source: Optional[str],
    run_bootstrap: bool,
    context_label: str,
) -> "tuple[bool, Optional[str]]":
    """
    在给定命名空间中执行 bootstrap + 用户代码 + 兜底图表捕获。

    调用方负责 stdout/stderr 重定向。返回 (success, error_msg)。
    """
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
                pre_data_listing = os.listdir('/data')
            except (FileNotFoundError, PermissionError, OSError):
                pre_data_listing = "<missing>"
            print(f"[Kernel] pre-bootstrap /data contents={pre_data_listing}", flush=True)
            print(f"[Kernel] bootstrap_source_len={len(bootstrap_source)}", flush=True)

            if run_bootstrap:
                exec(bootstrap_source, namespace)

            try:
                post_data_listing = os.listdir('/data')
            except (FileNotFoundError, PermissionError, OSError):
                post_data_listing = "<missing>"
            df_obj = namespace.get('df')
            if df_obj is None:
                df_shape_repr = "<no df>"
            elif hasattr(df_obj, 'shape'):
                df_shape_repr = str(df_obj.shape)
            else:
                df_shape_repr = f"<{type(df_obj).__name__}>"
            print(f"[Kernel] post-bootstrap /data contents={post_data_listing}", flush=True)
            print(
                f"[Kernel] bootstrap done, context_id={context_label}, df.shape={df_shape_repr}, "
                f"_loaded_tables={list(namespace.get('_loaded_tables', {}).keys())}",
                flush=True,
            )
        else:
            print(f"[Kernel] no bootstrap_source provided, context_id={context_label}", flush=True)

        exec(code, namespace)
        exec(BACKUP_CAPTURE_CODE, namespace)
        return True, None
    except SystemExit:
        return True, None
    except Exception:
        return False, traceback.format_exc()


def _clear_capture_buffers() -> None:
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


def _build_result(
    stdout_text: str,
    stderr_text: str,
    success: bool,
    error_msg: Optional[str],
    elapsed: float,
    context_id: Optional[str] = None,
    isolated: bool = False,
) -> Dict[str, Any]:
    """Parse stdout for charts/tables and build the execution result dict."""
    charts = []
    for match in SVG_PATTERN.finditer(stdout_text):
        svg_b64 = match.group(1).strip()
        if svg_b64:
            charts.append({"path": None, "base64": svg_b64, "format": "svg"})

    clean_stdout = SVG_PATTERN.sub('[图表已生成]', stdout_text)

    tables = []
    for match in TABLE_PATTERN.finditer(stdout_text):
        try:
            table_data = json.loads(match.group(1).strip())
            tables.append(table_data)
        except json.JSONDecodeError:
            pass

    clean_stdout = TABLE_PATTERN.sub('[表格数据已捕获]', clean_stdout)

    output = clean_stdout
    if stderr_text:
        output += f"\n[stderr]:\n{stderr_text}"

    if len(output) > MAX_OUTPUT_LENGTH:
        output = output[:MAX_OUTPUT_LENGTH] + "\n... (输出已截断)"

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
        "isolated": isolated,
    }


# ========== fork 隔离执行（zygote 模式） ==========

def _execute_isolated(
    code: str,
    timeout: int,
    bootstrap_source: Optional[str],
    context_id: Optional[str],
) -> Dict[str, Any]:
    """
    fork 子进程执行用户代码（无状态请求的最优路径）。

    - 子进程通过 CoW 与父进程共享全部已预热库的物理内存页；
    - 子进程在父进程命名空间的浅拷贝上执行，写时复制，父进程零污染；
    - 超时 SIGKILL 子进程即可，父 kernel 不死、容器无需销毁重建；
    - bootstrap 每次在子进程内重新执行（子进程退出即销毁，无残留）。
    """
    import time
    start = time.monotonic()
    resolved_context_id = context_id or DEFAULT_CONTEXT_ID

    read_fd, write_fd = os.pipe()
    # 注意：这里刻意不抢 _exec_lock——有状态执行持锁可达数百秒，
    # 持锁 fork 会让全部无状态请求被一个长会话执行阻塞。
    # 快照一致性依赖：子进程内 dict(parent_ns) 浅拷贝在 CPython 中
    # 持 GIL 原子完成，不会读到结构损坏的字典。
    # 已知低概率风险：fork 瞬间其他线程恰好持有 import lock 时，
    # 子进程内首次 import 可能死锁——由父进程的超时 SIGKILL 兜底。
    pid = os.fork()

    if pid == 0:
        # ---------- 子进程 ----------
        exit_code = 0
        try:
            os.close(read_fd)
            _clear_capture_buffers()

            # 基于预热命名空间做浅拷贝：库引用共享（CoW），新变量隔离
            _, parent_ns = _get_or_create_namespace(resolved_context_id)
            namespace = dict(parent_ns)

            captured_out = io.StringIO()
            captured_err = io.StringIO()
            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = captured_out, captured_err
            try:
                success, error_msg = _run_user_code(
                    code, namespace, bootstrap_source,
                    run_bootstrap=bool(bootstrap_source),
                    context_label=resolved_context_id,
                )
            finally:
                sys.stdout, sys.stderr = old_stdout, old_stderr

            result = _build_result(
                captured_out.getvalue(),
                captured_err.getvalue(),
                success,
                error_msg,
                time.monotonic() - start,
                context_id=resolved_context_id,
                isolated=True,
            )
            payload = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
            os.write(write_fd, struct.pack(">I", len(payload)))
            written = 0
            while written < len(payload):
                written += os.write(write_fd, payload[written:written + 65536])
        except BaseException:
            exit_code = 1
        finally:
            try:
                os.close(write_fd)
            except OSError:
                pass
            os._exit(exit_code)

    # ---------- 父进程 ----------
    os.close(write_fd)
    # 执行超时与传输宽限分离：
    # - 子进程还没开始回传（chunks 为空）→ 严格按 timeout 判超时
    # - 已开始回传（执行已完成，正在传大结果）→ 额外给 15s 传输宽限
    exec_deadline = start + timeout
    transfer_deadline = start + timeout + 15

    chunks: "list[bytes]" = []
    timed_out = False
    try:
        import select
        while True:
            now = time.monotonic()
            deadline = transfer_deadline if chunks else exec_deadline
            remaining = deadline - now
            if remaining <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([read_fd], [], [], min(remaining, 0.5))
            if not ready:
                continue
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break  # EOF：子进程写完退出
            chunks.append(chunk)
    finally:
        os.close(read_fd)
        if timed_out:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        # 无论哪条路径都收割子进程，避免僵尸
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    if timed_out:
        elapsed = time.monotonic() - start
        return {
            "success": False,
            "stdout": "",
            "stderr": "",
            "output": f"[Timeout]: 执行超过 {timeout} 秒限制",
            "charts": [],
            "tables": [],
            "images": [],
            "error": f"执行超时（{timeout}秒）",
            "execution_time_ms": int(elapsed * 1000),
            "context_id": resolved_context_id,
            "isolated": True,
            "timed_out": True,
        }

    raw = b"".join(chunks)
    elapsed = time.monotonic() - start
    if len(raw) >= 4:
        (length,) = struct.unpack(">I", raw[:4])
        body = raw[4:4 + length]
        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    return {
        "success": False,
        "stdout": "",
        "stderr": "",
        "output": "",
        "charts": [],
        "tables": [],
        "images": [],
        "error": "隔离子进程异常退出（可能 OOM 被杀）",
        "execution_time_ms": int(elapsed * 1000),
        "context_id": resolved_context_id,
        "isolated": True,
    }


# ========== 有状态执行（常驻命名空间） ==========

def _force_terminate_thread(thread: threading.Thread) -> bool:
    """
    Force-terminate a thread by raising SystemExit in it via ctypes.
    Best-effort：阻塞在 C 扩展中的线程可能无响应。
    """
    if not thread.is_alive():
        return True
    tid = thread.ident
    if tid is None:
        return False
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(tid), ctypes.py_object(SystemExit)
    )
    return res == 1


def execute_code(
    code: str,
    timeout: int = 300,
    bootstrap_source: Optional[str] = None,
    context_id: Optional[str] = None,
    isolated: bool = False,
) -> Dict[str, Any]:
    """
    执行代码。

    isolated=True 且平台支持 fork 时走子进程隔离路径（推荐给无状态请求）；
    否则在常驻命名空间执行（session 场景，变量跨请求保留）。
    """
    if isolated and hasattr(os, "fork"):
        with _fork_sema:
            return _execute_isolated(code, timeout, bootstrap_source, context_id)

    import time
    start = time.monotonic()

    with _exec_lock:
        resolved_context_id, namespace = _get_or_create_namespace(context_id)

        captured_out = io.StringIO()
        captured_err = io.StringIO()
        _clear_capture_buffers()

        success = True
        error_msg = None

        run_bootstrap = bool(bootstrap_source) and not _context_metadata[
            resolved_context_id
        ].get("bootstrap_initialized", False)

        def _run():
            nonlocal success, error_msg
            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = captured_out
            sys.stderr = captured_err
            try:
                success, error_msg = _run_user_code(
                    code, namespace, bootstrap_source,
                    run_bootstrap=run_bootstrap,
                    context_label=resolved_context_id,
                )
                if not success and error_msg:
                    captured_err.write(error_msg)
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=timeout)

        if thread.is_alive():
            elapsed = time.monotonic() - start
            _force_terminate_thread(thread)
            thread.join(timeout=2)
            kernel_unhealthy = True
            if thread.is_alive():
                print(
                    "[Kernel] WARNING: thread still alive after forced termination",
                    file=sys.stderr, flush=True,
                )
            try:
                reset_namespace(resolved_context_id)
            except Exception as e:
                print(
                    f"[Kernel] WARNING: reset after timeout failed: {e}",
                    file=sys.stderr, flush=True,
                )
            # 最后兜底：宿主收到 kernel_unhealthy 后会销毁容器；
            # 若宿主未处理，1 秒后自杀以避免僵尸 kernel 占池。
            try:
                threading.Timer(1.0, lambda: os._exit(124)).start()
            except Exception:
                pass
            return {
                "success": False,
                "stdout": captured_out.getvalue(),
                "stderr": captured_err.getvalue(),
                "output": f"[Timeout]: 执行超过 {timeout} 秒限制",
                "charts": [],
                "tables": [],
                "images": [],
                "error": f"执行超时（{timeout}秒）",
                "execution_time_ms": int(elapsed * 1000),
                "kernel_unhealthy": kernel_unhealthy,
                "context_id": resolved_context_id,
            }

        if run_bootstrap and success:
            _context_metadata[resolved_context_id]["bootstrap_initialized"] = True

        elapsed = time.monotonic() - start
        return _build_result(
            captured_out.getvalue(),
            captured_err.getvalue(),
            success,
            error_msg,
            elapsed,
            context_id=resolved_context_id,
        )


# ========== 流式执行 ==========

def _emit_stream_event(send_event, event_type: str, **payload) -> None:
    event = {"type": event_type, **payload}
    send_event(event)


class _StreamingEventWriter:
    """把 write() 的文本按行切分为流式事件的 file-like 对象。"""

    def __init__(self, stream_name: str, send_event):
        self._stream_name = stream_name
        self._send_event = send_event
        self._buffer = ""
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            text = str(text)
        with self._lock:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                self._emit_line(line + "\n")
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._buffer:
                self._emit_line(self._buffer)
                self._buffer = ""

    def _emit_line(self, text: str) -> None:
        # charts.py / tables.py 的标记均为单行打印，
        # 在流式路径下转换为结构化 chart / table 事件
        if self._stream_name == "stdout":
            svg_match = SVG_PATTERN.search(text)
            if svg_match:
                _emit_stream_event(
                    self._send_event, "chart",
                    chart={"path": None, "base64": svg_match.group(1).strip(), "format": "svg"},
                )
                return
            table_match = TABLE_PATTERN.search(text)
            if table_match:
                try:
                    table_data = json.loads(table_match.group(1).strip())
                except json.JSONDecodeError:
                    table_data = None
                if table_data is not None:
                    _emit_stream_event(self._send_event, "table", table=table_data)
                    return
        _emit_stream_event(self._send_event, self._stream_name, text=text)

    def isatty(self) -> bool:
        return False


def execute_code_stream(
    code: str,
    timeout: int = 300,
    bootstrap_source: Optional[str] = None,
    context_id: Optional[str] = None,
    send_event=None,
) -> None:
    """执行代码并通过 send_event 增量发出 NDJSON 事件（有状态路径）。"""
    import time

    if send_event is None:
        raise ValueError("send_event is required for execute_code_stream")

    start = time.monotonic()

    with _exec_lock:
        resolved_context_id, namespace = _get_or_create_namespace(context_id)
        _clear_capture_buffers()

        success = True
        error_msg = None

        run_bootstrap = bool(bootstrap_source) and not _context_metadata[
            resolved_context_id
        ].get("bootstrap_initialized", False)

        def _run():
            nonlocal success, error_msg
            old_stdout, old_stderr = sys.stdout, sys.stderr
            stdout_writer = _StreamingEventWriter("stdout", send_event)
            stderr_writer = _StreamingEventWriter("stderr", send_event)
            sys.stdout = stdout_writer
            sys.stderr = stderr_writer
            try:
                success, error_msg = _run_user_code(
                    code, namespace, bootstrap_source,
                    run_bootstrap=run_bootstrap,
                    context_label=resolved_context_id,
                )
                if not success and error_msg:
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
            elapsed = time.monotonic() - start
            _force_terminate_thread(thread)
            thread.join(timeout=2)
            try:
                reset_namespace(resolved_context_id)
            except Exception as e:
                _emit_stream_event(
                    send_event, "stderr",
                    text=f"[Kernel] WARNING: reset after timeout failed: {e}\n",
                )
            _emit_stream_event(
                send_event, "error",
                error=f"执行超时（{timeout}秒）",
                context_id=resolved_context_id,
            )
            _emit_stream_event(
                send_event, "done",
                success=False,
                error=f"执行超时（{timeout}秒）",
                execution_time_ms=int(elapsed * 1000),
                context_id=resolved_context_id,
                timed_out=True,
            )
            return

        if run_bootstrap and success:
            _context_metadata[resolved_context_id]["bootstrap_initialized"] = True

        elapsed = time.monotonic() - start
        if not success and error_msg:
            _emit_stream_event(
                send_event, "error",
                error=error_msg,
                context_id=resolved_context_id,
            )
        _emit_stream_event(
            send_event, "done",
            success=success,
            error=error_msg,
            execution_time_ms=int(elapsed * 1000),
            context_id=resolved_context_id,
            timed_out=False,
        )


# ========== TCP 服务器 ==========

KERNEL_PORT = int(os.environ.get("KERNEL_PORT", "9999"))
MAX_MSG_SIZE = 100 * 1024 * 1024  # 100MB


def handle_client(conn: socket.socket, addr: tuple) -> None:
    """处理单个 TCP 客户端连接（每连接一个线程）。"""
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
            with _exec_lock:
                reset_namespace(resolved_context_id)
            response = {
                "status": "ok",
                "context_id": resolved_context_id,
                "message": "命名空间已重置",
            }
        elif action == "create_context":
            with _exec_lock:
                response = create_context(request.get("context_id"))
        elif action == "list_contexts":
            response = list_contexts()
        elif action == "delete_context":
            with _exec_lock:
                response = delete_context(request.get("context_id"))
        elif action == "execute":
            response = execute_code(
                request.get("code", ""),
                request.get("timeout", 300),
                bootstrap_source=request.get("bootstrap_source") or None,
                context_id=request.get("context_id"),
                isolated=bool(request.get("isolated", False)),
            )
        elif action == "execute_stream":
            execute_code_stream(
                request.get("code", ""),
                request.get("timeout", 300),
                bootstrap_source=request.get("bootstrap_source") or None,
                context_id=request.get("context_id"),
                send_event=lambda event: _send_stream_response(conn, event),
            )
            return
        else:
            response = {"success": False, "error": f"未知命令: {action}"}

        _send_response(conn, response)

    except Exception as e:
        try:
            _send_response(conn, {"success": False, "error": str(e)})
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    """精确读取 n 个字节"""
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def _send_response(conn: socket.socket, response: dict) -> None:
    """发送 JSON 响应（4 字节长度头 + payload）"""
    payload = json.dumps(response, ensure_ascii=False, default=str).encode("utf-8")
    conn.sendall(len(payload).to_bytes(4, "big") + payload)


def _send_stream_response(conn: socket.socket, event: dict) -> None:
    """发送流式 NDJSON 事件（无长度头，按行分隔，由中继原样转发）。"""
    line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
    conn.sendall(line.encode("utf-8"))


def serve_forever() -> None:
    """启动 TCP 服务。每连接一个线程，ping 不被长执行阻塞。"""
    _enable_ksm_merge()
    _init_namespace()

    # 回收 fork 子进程，避免僵尸（_execute_isolated 内有 waitpid，
    # 这里兜底处理异常路径遗留的子进程）
    if hasattr(signal, "SIGCHLD"):
        try:
            signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        except (ValueError, OSError):
            pass

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", KERNEL_PORT))
    server.listen(32)

    print(f"[Kernel] 监听 0.0.0.0:{KERNEL_PORT} (max_forks={_MAX_FORKS})", flush=True)

    while True:
        try:
            conn, addr = server.accept()
            threading.Thread(
                target=handle_client, args=(conn, addr), daemon=True
            ).start()
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[Kernel] 连接处理异常: {e}", file=sys.stderr, flush=True)

    server.close()
    print("[Kernel] 服务器已关闭", flush=True)


if __name__ == "__main__":
    serve_forever()
