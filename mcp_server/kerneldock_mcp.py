"""
KernelDock MCP Server

把 KernelDock REST API 包装为 MCP (Model Context Protocol) 工具，
任何 MCP 客户端（Claude Desktop / Cursor / Cline 等）零成本接入沙箱执行能力。

运行方式（stdio transport，由 MCP 客户端拉起）：

    pip install -r requirements-mcp.txt
    KERNELDOCK_URL=http://localhost:9527 python mcp_server/kerneldock_mcp.py

客户端配置示例（Claude Desktop / Cursor mcp.json）：

    {
      "mcpServers": {
        "kerneldock": {
          "command": "python",
          "args": ["/path/to/mcp_server/kerneldock_mcp.py"],
          "env": {
            "KERNELDOCK_URL": "http://localhost:9527",
            "KERNELDOCK_API_KEY": "your-api-key"
          }
        }
      }
    }

环境变量：
    KERNELDOCK_URL       服务地址，默认 http://localhost:9527
    KERNELDOCK_API_KEY   API Key（服务端配置了 SANDBOX_API_KEYS 时必填）
    KERNELDOCK_TIMEOUT   HTTP 客户端超时秒数，默认 660
"""

import json
import os
from typing import Any, Dict, List, Optional

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("KERNELDOCK_URL", "http://localhost:9527").rstrip("/")
API_KEY = os.environ.get("KERNELDOCK_API_KEY", "")
HTTP_TIMEOUT = float(os.environ.get("KERNELDOCK_TIMEOUT", "660"))

mcp = FastMCP(
    "kerneldock",
    instructions=(
        "KernelDock Python 沙箱执行服务。"
        "无状态执行用 execute_python；多轮分析先 create_session 再 execute_in_session"
        "（变量跨调用保留）；需要装包用 install_packages；"
        "长任务（>5 分钟）用 submit_job + get_job 轮询。"
    ),
)


def _headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return headers


async def _request(method: str, path: str, json_body: Optional[dict] = None, params: Optional[dict] = None) -> Any:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.request(
            method, f"{BASE_URL}{path}", json=json_body, params=params, headers=_headers()
        )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise RuntimeError(f"KernelDock API {resp.status_code}: {detail}")
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()


def _summarize_execution(result: Dict[str, Any]) -> str:
    """把执行结果压缩成 LLM 友好的 JSON 文本（图表只给元信息不给 base64 正文）。"""
    charts = result.get("charts") or []
    summary = {
        "success": result.get("success"),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "error": result.get("error"),
        "execution_time_ms": (result.get("execution_info") or {}).get("execution_time_ms")
        or result.get("execution_time_ms", 0),
        "charts": [
            {"index": i, "format": c.get("format"), "size_base64": len(c.get("base64") or "")}
            for i, c in enumerate(charts)
        ],
        "tables": result.get("tables") or [],
    }
    return json.dumps(summary, ensure_ascii=False, default=str)


# ===== 代码执行 =====

@mcp.tool()
async def execute_python(code: str, timeout: int = 60) -> str:
    """无状态执行 Python 代码（pandas/numpy/matplotlib 可用）。返回 stdout/stderr/图表元信息的 JSON。"""
    result = await _request("POST", "/execute", {"code": code, "timeout": timeout})
    return _summarize_execution(result)


@mcp.tool()
async def create_session() -> str:
    """创建有状态会话沙箱（变量跨调用保留）。返回 session_id 等信息的 JSON。"""
    result = await _request("POST", "/sessions", {})
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def execute_in_session(session_id: str, code: str, timeout: int = 120) -> str:
    """在会话沙箱内执行 Python 代码，变量、import、数据跨调用保留。"""
    result = await _request(
        "POST", f"/sessions/{session_id}/execute", {"code": code, "timeout": timeout}
    )
    return _summarize_execution(result)


@mcp.tool()
async def delete_session(session_id: str) -> str:
    """销毁会话沙箱及其全部数据。"""
    await _request("DELETE", f"/sessions/{session_id}")
    return json.dumps({"deleted": session_id}, ensure_ascii=False)


# ===== Shell =====

@mcp.tool()
async def run_shell(command: str, session_id: str = "", timeout: int = 60) -> str:
    """执行 shell 命令。提供 session_id 时在该会话沙箱内执行（推荐），否则走无状态执行。"""
    if session_id:
        result = await _request(
            "POST", f"/sessions/{session_id}/shell", {"command": command, "timeout": timeout}
        )
    else:
        result = await _request("POST", "/execute/shell", {"command": command, "timeout": timeout})
    return json.dumps(result, ensure_ascii=False)


# ===== 文件系统 =====

@mcp.tool()
async def list_files(session_id: str, path: str = "/data") -> str:
    """列出会话沙箱内目录（允许 /data /output /tmp /home/sandbox）。"""
    result = await _request("GET", f"/sessions/{session_id}/fs/list", params={"path": path})
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def read_file(session_id: str, path: str, as_text: bool = True) -> str:
    """读取会话沙箱内文件。as_text=True 时解码为文本返回，否则返回 base64。"""
    result = await _request("GET", f"/sessions/{session_id}/fs/read", params={"path": path})
    if as_text:
        import base64 as _b64

        try:
            content = _b64.b64decode(result.get("content_base64", "")).decode("utf-8")
            return json.dumps(
                {"path": result.get("path"), "size": result.get("size"), "content": content},
                ensure_ascii=False,
            )
        except UnicodeDecodeError:
            result["note"] = "二进制文件，已返回 base64"
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def write_file(session_id: str, path: str, content: str) -> str:
    """把文本内容写入会话沙箱内文件（自动创建父目录）。"""
    import base64 as _b64

    payload = {
        "path": path,
        "content_base64": _b64.b64encode(content.encode("utf-8")).decode("ascii"),
    }
    result = await _request("PUT", f"/sessions/{session_id}/fs/write", payload)
    return json.dumps(result, ensure_ascii=False)


# ===== pip 装包 =====

@mcp.tool()
async def install_packages(session_id: str, packages: List[str], timeout: int = 300) -> str:
    """在会话沙箱内 pip install（如 ["polars==1.0.0"]）。需要服务端开启 egress proxy 模式。"""
    result = await _request(
        "POST", f"/sessions/{session_id}/packages", {"packages": packages, "timeout": timeout}
    )
    return json.dumps(result, ensure_ascii=False)


# ===== 后台任务 =====

@mcp.tool()
async def submit_job(code: str, timeout: int = 600, session_id: str = "") -> str:
    """提交长时 Python 任务（异步执行，立即返回 job_id，用 get_job 轮询结果）。"""
    payload: Dict[str, Any] = {"code": code, "timeout": timeout}
    if session_id:
        payload["session_id"] = session_id
    result = await _request("POST", "/jobs", payload)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def get_job(job_id: str) -> str:
    """查询后台任务状态；status 为 succeeded/failed/cancelled 时附带执行结果。"""
    result = await _request("GET", f"/jobs/{job_id}")
    # 压缩 result 里的图表 base64，避免撑爆上下文
    if isinstance(result.get("result"), dict):
        inner = result["result"]
        charts = inner.get("charts") or []
        inner["charts"] = [
            {"index": i, "format": c.get("format"), "size_base64": len(c.get("base64") or "")}
            for i, c in enumerate(charts)
        ]
    return json.dumps(result, ensure_ascii=False, default=str)


@mcp.tool()
async def get_chart(session_id: str, code: str = "", chart_index: int = 0) -> str:
    """重新执行绘图代码并返回指定图表的完整 base64（需要图表正文时使用）。"""
    if not code:
        return json.dumps({"error": "请提供绘图代码"}, ensure_ascii=False)
    result = await _request(
        "POST", f"/sessions/{session_id}/execute", {"code": code, "timeout": 120}
    )
    charts = result.get("charts") or []
    if chart_index >= len(charts):
        return json.dumps(
            {"error": f"图表索引越界: {chart_index}, 共 {len(charts)} 张"}, ensure_ascii=False
        )
    return json.dumps(charts[chart_index], ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
