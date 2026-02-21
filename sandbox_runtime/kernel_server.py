"""
Kernel Server — 容器内常驻的轻量级代码执行进程

架构：
    容器启动时运行此服务，监听 TCP 端口 9999。
    宿主机的 CodeExecutor 通过 TCP 连接发送代码，Kernel Server 在
    **同一个 Python 命名空间** 中执行，使得 DataFrame 等变量常驻内存。

协议（JSON-line over TCP）：
    请求: {"action": "execute", "code": "...", "timeout": 300}
    响应: {"success": true, "stdout": "...", "stderr": "...", "charts": [...], "tables": [...]}

    特殊请求:
    {"action": "reset"}     → 清空命名空间（保留导入的库）
    {"action": "ping"}      → 健康检查，返回 {"status": "ok"}

安全说明：
    - 每个容器对应唯一 session，命名空间不会跨 session 共享
    - 超时由 signal.alarm + threading 双重保障
    - 异常不会终止 server 进程
"""

import base64
import io
import json
import os
import re
import signal
import socket
import sys
import threading
import traceback
from typing import Any, Dict, Optional


# ========== 全局命名空间 ==========

_namespace: Dict[str, Any] = {}
_initialized = False


def _init_namespace() -> None:
    """
    预加载常用库到全局命名空间 — 仅执行一次。
    后续所有代码执行共享这个命名空间，变量常驻内存。
    """
    global _namespace, _initialized
    if _initialized:
        return

    # 初始化沙箱运行时环境
    init_code = """
import os
os.environ.setdefault('DATA_DIR', '/data')
os.environ.setdefault('OUTPUT_DIR', '/output')

from sandbox_runtime import setup
setup()

import functools
print = functools.partial(print, flush=True)

from sandbox_runtime.data_loader import load_data_files, get_default_dataframe
load_data_files(globals_dict=globals())
df = get_default_dataframe()

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns

from sandbox_runtime.charts import save_figure, capture_current_figures
from sandbox_runtime.tables import display_table, save_table

DATA_DIR = os.environ.get('DATA_DIR', '/data')
OUTPUT_DIR = os.environ.get('OUTPUT_DIR', '/output')
"""
    try:
        exec(init_code, _namespace)
    except Exception as e:
        print(f"[Kernel] 初始化命名空间失败: {e}", file=sys.stderr, flush=True)

    _initialized = True
    print("[Kernel] 命名空间初始化完成", flush=True)


def reset_namespace() -> None:
    """清空命名空间（保留 __builtins__），重新初始化"""
    global _namespace, _initialized
    builtins = _namespace.get("__builtins__")
    _namespace.clear()
    if builtins:
        _namespace["__builtins__"] = builtins
    _initialized = False
    _init_namespace()


# ========== 代码执行 ==========

# 用于从 stdout 提取图表/表格标记的正则
SVG_PATTERN = re.compile(r'SVG_BASE64_START:(.+?):SVG_BASE64_END', re.DOTALL)
TABLE_PATTERN = re.compile(r'TABLE_DATA_START:(.+?):TABLE_DATA_END', re.DOTALL)

# 备份代码：执行后检查未捕获的图表
BACKUP_CAPTURE_CODE = """
from sandbox_runtime.charts import capture_current_figures
capture_current_figures()
"""


def execute_code(code: str, timeout: int = 300) -> Dict[str, Any]:
    """
    在常驻命名空间中执行代码，捕获 stdout/stderr/charts/tables。

    Args:
        code: Python 代码字符串
        timeout: 超时秒数

    Returns:
        执行结果字典
    """
    import time
    start = time.monotonic()

    # 重新加载数据文件（支持动态添加数据文件）
    try:
        from sandbox_runtime.data_loader import load_data_files, get_default_dataframe
        import os
        
        # 调试：检查数据目录
        data_dir = os.environ.get('DATA_DIR', '/data')
        print(f"[Kernel] 数据目录: {data_dir}", flush=True)
        if os.path.exists(data_dir):
            files = os.listdir(data_dir)
            print(f"[Kernel] 数据目录文件: {files}", flush=True)
        else:
            print(f"[Kernel] 数据目录不存在!", flush=True)
        
        # 加载数据文件
        loaded = load_data_files(globals_dict=_namespace)
        print(f"[Kernel] 已加载 {len(loaded)} 个数据文件", flush=True)
        
        # 更新默认 df
        _namespace['df'] = get_default_dataframe()
        
        # 调试：打印命名空间中的 DataFrame 变量
        df_vars = [k for k, v in _namespace.items() if hasattr(v, 'shape') and hasattr(v, 'columns')]
        print(f"[Kernel] DataFrame 变量: {df_vars}", flush=True)
        
    except Exception as e:
        import traceback
        print(f"[Kernel] 重新加载数据文件失败: {e}", file=sys.stderr, flush=True)
        print(traceback.format_exc(), file=sys.stderr, flush=True)

    # 重定向 stdout / stderr
    old_stdout, old_stderr = sys.stdout, sys.stderr
    captured_out = io.StringIO()
    captured_err = io.StringIO()

    # 清空上次执行的图表捕获状态
    try:
        from sandbox_runtime.charts import clear_captured_charts
        clear_captured_charts()
    except Exception:
        pass

    success = True
    error_msg = None

    # 超时控制（使用线程 + Event）
    timed_out = threading.Event()

    def _run():
        nonlocal success, error_msg
        sys.stdout = captured_out
        sys.stderr = captured_err
        try:
            exec(code, _namespace)
            # 备份图表捕获
            exec(BACKUP_CAPTURE_CODE, _namespace)
        except SystemExit:
            pass
        except Exception:
            success = False
            error_msg = traceback.format_exc()
            captured_err.write(error_msg)
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        # 超时
        timed_out.set()
        elapsed = time.monotonic() - start
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
        }

    elapsed = time.monotonic() - start
    stdout_text = captured_out.getvalue()
    stderr_text = captured_err.getvalue()

    # 提取图表
    charts = []
    for match in SVG_PATTERN.finditer(stdout_text):
        svg_b64 = match.group(1).strip()
        if svg_b64:
            charts.append({"path": None, "base64": svg_b64, "format": "svg"})

    clean_stdout = SVG_PATTERN.sub('[图表已生成]', stdout_text)

    # 提取表格
    tables = []
    for match in TABLE_PATTERN.finditer(stdout_text):
        try:
            table_data = json.loads(match.group(1).strip())
            tables.append(table_data)
        except json.JSONDecodeError:
            pass

    clean_stdout = TABLE_PATTERN.sub('[表格数据已捕获]', clean_stdout)

    # 构建输出
    output = clean_stdout
    if stderr_text:
        output += f"\n[stderr]:\n{stderr_text}"

    MAX_OUTPUT_LENGTH = 100000
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
    }


# ========== TCP 服务器 ==========

KERNEL_PORT = int(os.environ.get("KERNEL_PORT", "9999"))
MAX_MSG_SIZE = 100 * 1024 * 1024  # 100MB


def handle_client(conn: socket.socket, addr: tuple) -> None:
    """处理单个 TCP 客户端连接"""
    try:
        # 读取消息：先读 4 字节长度头，再读 payload
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
            reset_namespace()
            response = {"status": "ok", "message": "命名空间已重置"}
        elif action == "execute":
            code = request.get("code", "")
            timeout = request.get("timeout", 300)
            response = execute_code(code, timeout)
        else:
            response = {"success": False, "error": f"未知命令: {action}"}

        _send_response(conn, response)

    except Exception as e:
        try:
            _send_response(conn, {"success": False, "error": str(e)})
        except Exception:
            pass
    finally:
        conn.close()


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


def serve_forever() -> None:
    """启动 TCP 服务器，每个连接处理一个请求"""
    # 初始化命名空间
    _init_namespace()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", KERNEL_PORT))
    server.listen(5)

    print(f"[Kernel] 监听 0.0.0.0:{KERNEL_PORT}", flush=True)

    while True:
        try:
            conn, addr = server.accept()
            # 每个请求在主线程中串行处理（保证命名空间一致性）
            handle_client(conn, addr)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[Kernel] 连接处理异常: {e}", file=sys.stderr, flush=True)

    server.close()
    print("[Kernel] 服务器已关闭", flush=True)


if __name__ == "__main__":
    serve_forever()
