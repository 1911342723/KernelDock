"""
Code Executor Service Client
用于从后端调用 Code Executor 服务
"""

import httpx
import base64
from typing import Dict, Any, Optional, List
from dataclasses import dataclass


@dataclass
class ExecutionResult:
    """代码执行结果"""
    success: bool
    output: str
    stdout: str
    stderr: str
    charts: List[Dict]
    tables: List[Dict]
    images: List[str]
    error: Optional[str]
    queue_info: Optional[Dict] = None


class CodeExecutorClient:
    """Code Executor 服务客户端"""
    
    def __init__(self, base_url: str = "http://localhost:8080", timeout: float = 600):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)
    
    async def close(self):
        """关闭客户端"""
        await self._client.aclose()
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
    
    async def health_check(self) -> bool:
        """健康检查"""
        try:
            resp = await self._client.get(f"{self.base_url}/health")
            return resp.status_code == 200
        except Exception:
            return False
    
    async def create_session(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        """创建会话"""
        resp = await self._client.post(
            f"{self.base_url}/sessions",
            json={"session_id": session_id}
        )
        resp.raise_for_status()
        return resp.json()
    
    async def get_session(self, session_id: str) -> Dict[str, Any]:
        """获取会话信息"""
        resp = await self._client.get(f"{self.base_url}/sessions/{session_id}")
        resp.raise_for_status()
        return resp.json()
    
    async def delete_session(self, session_id: str) -> bool:
        """删除会话"""
        resp = await self._client.delete(f"{self.base_url}/sessions/{session_id}")
        return resp.status_code == 200
    
    async def execute_code(
        self, 
        session_id: str, 
        code: str, 
        timeout: int = 300
    ) -> ExecutionResult:
        """执行代码"""
        resp = await self._client.post(
            f"{self.base_url}/sessions/{session_id}/execute",
            json={"code": code, "timeout": timeout}
        )
        resp.raise_for_status()
        data = resp.json()
        return ExecutionResult(**data)
    
    async def load_data(
        self, 
        session_id: str, 
        data_json: str, 
        filename: str = "data.csv"
    ) -> Dict[str, Any]:
        """加载 JSON 数据"""
        resp = await self._client.post(
            f"{self.base_url}/sessions/{session_id}/load-data",
            json={"data_json": data_json, "filename": filename}
        )
        resp.raise_for_status()
        return resp.json()
    
    async def upload_file(
        self, 
        session_id: str, 
        file_content: bytes, 
        filename: str
    ) -> Dict[str, Any]:
        """上传文件"""
        files = {"file": (filename, file_content)}
        data = {"filename": filename}
        resp = await self._client.post(
            f"{self.base_url}/sessions/{session_id}/upload",
            files=files,
            data=data
        )
        resp.raise_for_status()
        return resp.json()
    
    async def get_table_schemas(self, session_id: str) -> List[Dict]:
        """获取表格模式"""
        resp = await self._client.get(
            f"{self.base_url}/sessions/{session_id}/schemas"
        )
        resp.raise_for_status()
        return resp.json()
    
    async def get_multi_table_context(self, session_id: str) -> Dict[str, Any]:
        """获取多表格上下文"""
        resp = await self._client.get(
            f"{self.base_url}/sessions/{session_id}/context"
        )
        resp.raise_for_status()
        return resp.json()
    
    async def list_files(self, session_id: str) -> Dict[str, List]:
        """列出文件"""
        resp = await self._client.get(
            f"{self.base_url}/sessions/{session_id}/files"
        )
        resp.raise_for_status()
        return resp.json()
    
    async def download_file(
        self, 
        session_id: str, 
        file_type: str, 
        filename: str
    ) -> bytes:
        """下载文件"""
        resp = await self._client.get(
            f"{self.base_url}/sessions/{session_id}/files/{file_type}/{filename}"
        )
        resp.raise_for_status()
        data = resp.json()
        return base64.b64decode(data["content_base64"])
    
    async def cleanup_old_sessions(self, max_age_hours: float = 12) -> int:
        """清理过期会话"""
        resp = await self._client.post(
            f"{self.base_url}/cleanup",
            params={"max_age_hours": max_age_hours}
        )
        resp.raise_for_status()
        return resp.json().get("cleaned", 0)

    async def execute_stateless(
        self,
        code: str,
        data_files: Dict[str, str] = None,
        timeout: int = 30,
    ) -> ExecutionResult:
        """
        无状态执行代码（即用即毁模式）。

        不需要创建 session，直接提交代码和数据文件执行。
        容器执行完立即归还池，适合一次性绘图/计算场景。

        Args:
            code: Python 代码
            data_files: 数据文件字典 {filename: base64_content}
            timeout: 执行超时（秒）

        Returns:
            ExecutionResult
        """
        payload = {"code": code, "timeout": timeout}
        if data_files:
            payload["data_files"] = data_files
        resp = await self._client.post(
            f"{self.base_url}/execute",
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return ExecutionResult(**data)


# 便捷函数
async def execute_code_simple(
    code: str,
    session_id: str = "default",
    base_url: str = "http://localhost:8080"
) -> ExecutionResult:
    """简单执行代码（一次性调用）"""
    async with CodeExecutorClient(base_url) as client:
        return await client.execute_code(session_id, code)
