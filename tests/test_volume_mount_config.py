import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import sandbox_manager as sandbox_manager_module
from app.services.sandbox_manager import SandboxManager


@pytest.mark.asyncio
async def test_create_workspace_dirs_prefers_shared_data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_manager_module.settings, "shared_data_root", str(tmp_path / "shared"))

    manager = SandboxManager(
        docker_client=AsyncMock(),
        resource_limiter=SimpleNamespace(),
        network_controller=AsyncMock(),
        container_pool=AsyncMock(),
        security_policy=SimpleNamespace(
            to_docker_config=lambda: {},
            get_tmpfs_config=lambda: {},
        ),
        workspace_base=str(tmp_path / "workspaces"),
    )

    data_dir, output_dir, data_dir_readonly = manager._create_workspace_dirs("sess_1")

    assert data_dir == str(tmp_path / "shared" / "sess_1")
    assert output_dir == str(tmp_path / "workspaces" / "sess_1" / "output")
    assert data_dir_readonly is True


@pytest.mark.asyncio
async def test_create_container_mounts_shared_data_read_only(tmp_path):
    docker_client = AsyncMock()
    docker_client.create_container = AsyncMock(return_value=SimpleNamespace(container_id="container-123"))
    docker_client.start_container = AsyncMock()
    network_controller = AsyncMock()
    network_controller.get_network_mode_for_container.return_value = "none"

    manager = SandboxManager(
        docker_client=docker_client,
        resource_limiter=SimpleNamespace(),
        network_controller=network_controller,
        container_pool=AsyncMock(),
        security_policy=SimpleNamespace(
            to_docker_config=lambda: {},
            get_tmpfs_config=lambda: {},
        ),
        workspace_base=str(tmp_path / "workspaces"),
    )

    resource_limits = SimpleNamespace(to_container_create_kwargs=lambda: {})
    network_policy = SimpleNamespace(enabled=False, allow_outbound=False)

    container_id = await manager._create_container(
        sandbox_id="sandbox_1",
        resource_limits=resource_limits,
        network_policy=network_policy,
        data_dir=str(tmp_path / "shared" / "sess_1"),
        output_dir=str(tmp_path / "workspaces" / "sess_1" / "output"),
        data_dir_readonly=True,
    )

    assert container_id == "container-123"
    kwargs = docker_client.create_container.await_args.kwargs
    assert kwargs["volumes"][str(tmp_path / "shared" / "sess_1")]["mode"] == "ro"
    assert kwargs["volumes"][str(tmp_path / "workspaces" / "sess_1" / "output")]["mode"] == "rw"
