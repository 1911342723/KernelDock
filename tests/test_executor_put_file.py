"""
测试 CodeExecutor._put_file_to_container 分块写入逻辑

验证：
- 短代码单块写入
- 长代码多块写入（模拟超过 ARG_MAX 的场景）
- 写入失败时抛出 InternalError
- 分块边界正确，内容完整
- execute_in_container 集成写入+验证+执行的完整流程
"""

import asyncio
import base64
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.executor import CodeExecutor, DATA_LOADER_TEMPLATE, BACKUP_CHART_CAPTURE_CODE
from app.exceptions import InternalError


@dataclass
class FakeExecResult:
    """模拟 ExecResult"""
    exit_code: int
    stdout: str
    stderr: str


class TestPutFileToContainer:
    """测试 _put_file_to_container 分块写入"""

    def _make_executor(self):
        mock_client = MagicMock()
        mock_client.exec_command = AsyncMock()
        executor = CodeExecutor.__new__(CodeExecutor)
        executor._docker_client = mock_client
        return executor, mock_client

    @pytest.mark.asyncio
    async def test_short_content_single_chunk(self):
        """短内容应只调用一次 exec_command（单块）"""
        executor, mock_client = self._make_executor()
        mock_client.exec_command.return_value = FakeExecResult(exit_code=0, stdout="", stderr="")

        content = b"print('hello world')"
        await executor._put_file_to_container("cid123", "/tmp/test.py", content)

        assert mock_client.exec_command.call_count == 1
        call_args = mock_client.exec_command.call_args
        cmd = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("command", call_args[0][1])
        # 第一块用 > 覆盖写
        assert " > " in cmd
        assert ">> " not in cmd

    @pytest.mark.asyncio
    async def test_long_content_multiple_chunks(self):
        """超过 24KB 的内容应分多块写入"""
        executor, mock_client = self._make_executor()
        mock_client.exec_command.return_value = FakeExecResult(exit_code=0, stdout="", stderr="")

        # 50KB 内容，应分 3 块 (24KB + 24KB + 2KB)
        content = b"x" * (50 * 1024)
        await executor._put_file_to_container("cid123", "/tmp/test.py", content)

        assert mock_client.exec_command.call_count == 3

        # 第一块用 >，后续用 >>
        cmds = [call[0][1] for call in mock_client.exec_command.call_args_list]
        assert " > " in cmds[0]
        assert " >> " in cmds[1]
        assert " >> " in cmds[2]

    @pytest.mark.asyncio
    async def test_content_integrity(self):
        """验证分块写入后内容可正确还原"""
        executor, mock_client = self._make_executor()
        mock_client.exec_command.return_value = FakeExecResult(exit_code=0, stdout="", stderr="")

        # 使用包含中文的真实代码
        content = ("import pandas as pd\nprint('你好世界')\n" * 1000).encode("utf-8")
        await executor._put_file_to_container("cid123", "/tmp/test.py", content)

        # 从每次调用中提取 base64 块并还原
        reassembled = b""
        for call in mock_client.exec_command.call_args_list:
            cmd = call[0][1]
            # 提取 echo '...' 中的 base64 内容
            b64_start = cmd.index("'") + 1
            b64_end = cmd.index("'", b64_start)
            b64_chunk = cmd[b64_start:b64_end]
            reassembled += base64.b64decode(b64_chunk)

        assert reassembled == content

    @pytest.mark.asyncio
    async def test_chunk_failure_raises_error(self):
        """某块写入失败时应抛出 InternalError"""
        executor, mock_client = self._make_executor()

        # 第一块成功，第二块失败
        mock_client.exec_command.side_effect = [
            FakeExecResult(exit_code=0, stdout="", stderr=""),
            FakeExecResult(exit_code=1, stdout="", stderr="No space left on device"),
        ]

        content = b"x" * (30 * 1024)  # 需要 2 块
        with pytest.raises(InternalError):
            await executor._put_file_to_container("cid123", "/tmp/test.py", content)

    @pytest.mark.asyncio
    async def test_exact_chunk_boundary(self):
        """内容恰好等于 chunk_size 时应只有 1 块"""
        executor, mock_client = self._make_executor()
        mock_client.exec_command.return_value = FakeExecResult(exit_code=0, stdout="", stderr="")

        content = b"a" * 24576  # 恰好 24KB
        await executor._put_file_to_container("cid123", "/tmp/test.py", content)
        assert mock_client.exec_command.call_count == 1

    @pytest.mark.asyncio
    async def test_one_byte_over_boundary(self):
        """内容比 chunk_size 多 1 字节时应有 2 块"""
        executor, mock_client = self._make_executor()
        mock_client.exec_command.return_value = FakeExecResult(exit_code=0, stdout="", stderr="")

        content = b"a" * 24577  # 24KB + 1
        await executor._put_file_to_container("cid123", "/tmp/test.py", content)
        assert mock_client.exec_command.call_count == 2

    @pytest.mark.asyncio
    async def test_empty_content(self):
        """空内容不应调用 exec_command"""
        executor, mock_client = self._make_executor()
        mock_client.exec_command.return_value = FakeExecResult(exit_code=0, stdout="", stderr="")

        await executor._put_file_to_container("cid123", "/tmp/test.py", b"")
        assert mock_client.exec_command.call_count == 0

    @pytest.mark.asyncio
    async def test_user_passed_to_exec(self):
        """user 参数应传递给 exec_command"""
        executor, mock_client = self._make_executor()
        mock_client.exec_command.return_value = FakeExecResult(exit_code=0, stdout="", stderr="")

        await executor._put_file_to_container(
            "cid123", "/tmp/test.py", b"hello", user="sandbox"
        )

        call_kwargs = mock_client.exec_command.call_args
        assert call_kwargs[1].get("user") == "sandbox" or call_kwargs.kwargs.get("user") == "sandbox"


class TestExecuteInContainerIntegration:
    """测试 execute_in_container 中写入+验证+执行的完整流程"""

    def _make_executor(self):
        mock_client = MagicMock()
        mock_client.exec_command = AsyncMock()
        executor = CodeExecutor.__new__(CodeExecutor)
        executor._docker_client = mock_client
        return executor, mock_client

    @pytest.mark.asyncio
    async def test_verify_failure_returns_error(self):
        """文件验证失败时应返回 InternalError"""
        executor, mock_client = self._make_executor()

        # kernel-first 路径直接返回 None（kernel 不可达），聚焦测试 docker exec 回退
        executor._execute_via_kernel = AsyncMock(return_value=None)

        # _put_file_to_container 的调用成功
        # verify 返回 MISSING
        mock_client.exec_command.side_effect = [
            FakeExecResult(exit_code=0, stdout="", stderr=""),   # put_file chunk
            FakeExecResult(exit_code=0, stdout="MISSING", stderr=""),  # verify
        ]

        result = await executor.execute_in_container(
            container_id="abc123def456",
            code="print('hi')",
            data_dir="/data",
            output_dir="/output",
            timeout=30,
        )

        assert result["success"] is False
        assert "写入容器失败" in result["error"]

    @pytest.mark.asyncio
    async def test_successful_execution_flow(self):
        """完整成功流程：写入 → 验证 → 执行 → 清理"""
        executor, mock_client = self._make_executor()

        # kernel-first 路径直接返回 None（kernel 不可达），聚焦测试 docker exec 回退
        executor._execute_via_kernel = AsyncMock(return_value=None)

        mock_client.exec_command.side_effect = [
            FakeExecResult(exit_code=0, stdout="", stderr=""),       # put_file chunk
            FakeExecResult(exit_code=0, stdout="OK\n", stderr=""),   # verify
            FakeExecResult(exit_code=0, stdout="hello\n", stderr=""),  # python exec
            FakeExecResult(exit_code=0, stdout="", stderr=""),       # rm cleanup
        ]

        # Mock _parse_execution_result
        with patch.object(executor, '_parse_execution_result') as mock_parse:
            mock_parse.return_value = {
                "success": True,
                "output": "hello",
                "stdout": "hello\n",
                "stderr": "",
                "charts": [],
                "tables": [],
                "images": [],
                "error": None,
                "execution_time_ms": 100,
            }

            result = await executor.execute_in_container(
                container_id="abc123def456",
                code="print('hello')",
                data_dir="/data",
                output_dir="/output",
                timeout=30,
            )

        assert result["success"] is True
        # 应有 4 次调用：写入、验证、执行、清理
        assert mock_client.exec_command.call_count == 4
