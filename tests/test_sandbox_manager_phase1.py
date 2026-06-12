import os
import sys
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.sandbox_manager import SandboxManager
from app.infrastructure.docker_client import ExecResult


@pytest.mark.asyncio
async def test_execute_stateless_materializes_parquet_when_data_dir_empty():
    manager = SandboxManager(docker_client=AsyncMock(), container_pool=AsyncMock())
    manager._container_pool.acquire_for_execution = AsyncMock(
        return_value=SimpleNamespace(container_id="container-1234567890")
    )
    manager._container_pool.release = AsyncMock()
    manager._container_pool.replenish = AsyncMock()
    manager._data_dir_has_materialized_parquet = AsyncMock(return_value=False)
    manager._materialize_pre_load_parquet = AsyncMock()
    manager._cleanup_and_reset_for_reuse = AsyncMock(return_value=True)
    manager._release_or_destroy_stateless_container = AsyncMock()

    with patch(
        "app.executor.CodeExecutor.execute_in_container",
        new=AsyncMock(return_value={"success": True, "kernel_unhealthy": False}),
    ):
        result = await manager.execute_stateless(
            code="print(1)",
            timeout=30,
            pre_load_parquet={"t_a": "Zm9v"},
            bootstrap_source="print('bootstrap')",
        )

    assert result["success"] is True
    manager._data_dir_has_materialized_parquet.assert_awaited_once_with(
        "container-1234567890"
    )
    manager._materialize_pre_load_parquet.assert_awaited_once_with(
        "container-1234567890",
        {"t_a": "Zm9v"},
    )


@pytest.mark.asyncio
async def test_execute_stateless_skips_materialization_when_parquet_already_present():
    manager = SandboxManager(docker_client=AsyncMock(), container_pool=AsyncMock())
    manager._container_pool.acquire_for_execution = AsyncMock(
        return_value=SimpleNamespace(container_id="container-1234567890")
    )
    manager._container_pool.release = AsyncMock()
    manager._container_pool.replenish = AsyncMock()
    manager._data_dir_has_materialized_parquet = AsyncMock(return_value=True)
    manager._materialize_pre_load_parquet = AsyncMock()
    manager._cleanup_and_reset_for_reuse = AsyncMock(return_value=True)
    manager._release_or_destroy_stateless_container = AsyncMock()

    with patch(
        "app.executor.CodeExecutor.execute_in_container",
        new=AsyncMock(return_value={"success": True, "kernel_unhealthy": False}),
    ):
        result = await manager.execute_stateless(
            code="print(1)",
            timeout=30,
            pre_load_parquet={"t_a": "Zm9v"},
            bootstrap_source="print('bootstrap')",
        )

    assert result["success"] is True
    manager._data_dir_has_materialized_parquet.assert_awaited_once_with(
        "container-1234567890"
    )
    manager._materialize_pre_load_parquet.assert_not_awaited()


@pytest.mark.asyncio
async def test_materialize_pre_load_parquet_falls_back_to_smaller_exec_chunks():
    docker_client = AsyncMock()
    docker_client.put_archive = AsyncMock(side_effect=RuntimeError("pipe closed"))
    docker_client.exec_command = AsyncMock(
        side_effect=[
            ExecResult(exit_code=0, stdout="", stderr=""),
            ExecResult(exit_code=0, stdout="", stderr=""),
            ExecResult(exit_code=0, stdout="", stderr=""),
            ExecResult(exit_code=0, stdout="", stderr=""),
            ExecResult(exit_code=0, stdout="t_big.parquet", stderr=""),
        ]
    )
    manager = SandboxManager(docker_client=docker_client, container_pool=AsyncMock())

    raw = b"a" * 9000
    payload = {"t_big": base64.b64encode(raw).decode("ascii")}

    await manager._materialize_pre_load_parquet("container-1234567890", payload)

    docker_client.put_archive.assert_awaited_once()
    assert docker_client.exec_command.await_count == 4

    mkdir_call = docker_client.exec_command.await_args_list[0]
    first_chunk_call = docker_client.exec_command.await_args_list[1]
    second_chunk_call = docker_client.exec_command.await_args_list[2]

    assert mkdir_call.args == ("container-1234567890", "mkdir -p /data")
    assert "> /data/t_big.parquet" in first_chunk_call.args[1]
    assert ">> /data/t_big.parquet" in second_chunk_call.args[1]