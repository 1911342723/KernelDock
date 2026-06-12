"""SandboxAgentOpsMixin 单元测试：路径白名单、包规格校验、shell 执行、pip 前置条件。"""

import asyncio
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.services.sandbox_agent_ops import (
    SandboxAgentOpsMixin,
    validate_container_path,
    validate_package_specs,
)


# ===== 路径白名单 =====

def test_validate_container_path_accepts_whitelisted():
    assert validate_container_path("/data/foo.csv") == "/data/foo.csv"
    assert validate_container_path("/output/sub/dir/x.png") == "/output/sub/dir/x.png"
    assert validate_container_path("/tmp") == "/tmp"
    assert validate_container_path("/home/sandbox/.local") == "/home/sandbox/.local"


def test_validate_container_path_rejects_escape_and_outside():
    with pytest.raises(ValueError):
        validate_container_path("/etc/passwd")
    with pytest.raises(ValueError):
        validate_container_path("/data/../etc/passwd")
    with pytest.raises(ValueError):
        validate_container_path("relative/path")
    with pytest.raises(ValueError):
        validate_container_path("")
    with pytest.raises(ValueError):
        validate_container_path("/datax/evil")  # 前缀相似但不是白名单目录
    with pytest.raises(ValueError):
        validate_container_path("/data/\x00bad")


# ===== pip 包规格 =====

def test_validate_package_specs_accepts_normal():
    assert validate_package_specs(["pandas", "polars==1.0.0", "uvicorn[standard]>=0.30"]) == [
        "pandas", "polars==1.0.0", "uvicorn[standard]>=0.30"
    ]


def test_validate_package_specs_rejects_option_injection():
    with pytest.raises(ValueError):
        validate_package_specs(["--index-url http://evil"])
    with pytest.raises(ValueError):
        validate_package_specs(["pandas; rm -rf /"])
    with pytest.raises(ValueError):
        validate_package_specs([])
    with pytest.raises(ValueError):
        validate_package_specs(["git+https://github.com/x/y"])


# ===== 测试用假对象 =====

class _FakeDockerClient:
    def __init__(self, exit_code=0, stdout="", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.calls = []

    async def exec_command(self, container_id, command, **kwargs):
        self.calls.append((container_id, command, kwargs))
        return SimpleNamespace(exit_code=self.exit_code, stdout=self.stdout, stderr=self.stderr)


class _FakeManager(SandboxAgentOpsMixin):
    def __init__(self, docker_client):
        self._docker_client = docker_client
        self._lock = asyncio.Lock()
        info = SimpleNamespace(container_id="container-abc", last_activity=datetime.now())
        self._sandboxes = {"sb-1": SimpleNamespace(info=info)}
        self._container_pool = None


# ===== shell 执行 =====

@pytest.mark.asyncio
async def test_execute_shell_wraps_with_timeout_and_succeeds():
    client = _FakeDockerClient(exit_code=0, stdout="hello\n")
    manager = _FakeManager(client)

    result = await manager.execute_shell("sb-1", "echo hello", timeout=30)

    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["stdout"] == "hello\n"
    assert result["timed_out"] is False
    # 命令被 coreutils timeout 包裹
    _, command, kwargs = client.calls[0]
    assert command.startswith("timeout -k 2 30 /bin/sh -c ")
    assert kwargs["timeout"] == 40  # 外层兜底 = timeout + 10


@pytest.mark.asyncio
async def test_execute_shell_maps_exit_124_to_timed_out():
    client = _FakeDockerClient(exit_code=124, stdout="", stderr="")
    manager = _FakeManager(client)

    result = await manager.execute_shell("sb-1", "sleep 999", timeout=1)

    assert result["success"] is False
    assert result["timed_out"] is True


@pytest.mark.asyncio
async def test_execute_shell_rejects_bad_workdir():
    manager = _FakeManager(_FakeDockerClient())
    with pytest.raises(ValueError):
        await manager.execute_shell("sb-1", "ls", timeout=10, workdir="/etc")


# ===== fs 操作 =====

@pytest.mark.asyncio
async def test_fs_delete_refuses_whitelist_root():
    manager = _FakeManager(_FakeDockerClient())
    with pytest.raises(ValueError):
        await manager.fs_delete("sb-1", "/data")


@pytest.mark.asyncio
async def test_fs_write_refuses_root_and_oversize():
    manager = _FakeManager(_FakeDockerClient())
    with pytest.raises(ValueError):
        await manager.fs_write("sb-1", "/output", b"x")


# ===== pip 装包前置条件 =====

@pytest.mark.asyncio
async def test_install_packages_requires_egress_proxy(monkeypatch):
    manager = _FakeManager(_FakeDockerClient())
    monkeypatch.setattr(settings.network, "egress_mode", "none")

    with pytest.raises(ValueError) as exc_info:
        await manager.install_packages("sb-1", ["pandas"])

    assert "egress" in str(exc_info.value).lower() or "EGRESS" in str(exc_info.value)
