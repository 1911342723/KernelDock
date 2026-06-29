"""
资源配置管理与 per-沙箱资源分配的单元测试（无需 Docker）。

覆盖：
- 资源限制器使用「可配置软上限」而非旧的硬编码常量（核心 bug 修复回归）
- 软上限不可超过绝对护栏
- apply_config 调小上限时默认值随之下调
- ResourceConfigStore 持久化 round-trip 与禁用态
- validate_and_clamp 的护栏收敛与 default<=max 约束
- GET /resource-config 视图、PUT /admin/resource-config 鉴权与热生效+持久化
- create_session 透传 per-沙箱资源并回显实际分配
"""

import os
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import runtime
from app.config import settings
from app.infrastructure.resource_limiter import (
    MAX_CPU,
    MAX_DISK_MB,
    MAX_MEMORY_MB,
    ResourceLimiter,
)


@pytest.fixture
def restore_resource_settings():
    """快照并恢复 settings.resource，避免 apply_config 污染其它测试。"""
    snapshot = settings.resource.model_copy(deep=True)
    yield
    for field in (
        "default_cpu",
        "default_memory_mb",
        "default_disk_mb",
        "default_pids",
        "max_cpu",
        "max_memory_mb",
        "max_disk_mb",
    ):
        setattr(settings.resource, field, getattr(snapshot, field))


# ---------------------------------------------------------------------------
# ResourceLimiter：软上限打通（核心修复）
# ---------------------------------------------------------------------------

def test_resource_limiter_clamps_to_configurable_soft_max():
    """请求超过软上限时收敛到软上限（旧实现会错误地放行到硬编码 4.0/4096）。"""
    limiter = ResourceLimiter(
        default_cpu=1.0,
        default_memory_mb=512,
        default_disk_mb=1024,
        default_pids=100,
        max_cpu=2.0,
        max_memory_mb=2048,
        max_disk_mb=4096,
        max_pids=200,
    )
    limits = limiter.get_limits(cpu=4.0, memory_mb=8192, disk_mb=10240, pids=500)
    assert limits.cpu_count == 2.0
    assert limits.memory_mb == 2048
    assert limits.disk_mb == 4096
    assert limits.pids_limit == 200


def test_soft_max_cannot_exceed_absolute_guardrail():
    limiter = ResourceLimiter(max_cpu=9999, max_memory_mb=10**9, max_disk_mb=10**9)
    assert limiter.max_cpu == MAX_CPU
    assert limiter.max_memory_mb == MAX_MEMORY_MB
    assert limiter.max_disk_mb == MAX_DISK_MB


def test_default_within_soft_max_is_preserved():
    limiter = ResourceLimiter(default_cpu=1.5, max_cpu=2.0)
    limits = limiter.get_limits()  # 不传 = 用默认
    assert limits.cpu_count == 1.5


def test_apply_config_lowers_default_when_max_reduced():
    limiter = ResourceLimiter(default_cpu=2.0, max_cpu=4.0)
    assert limiter.default_cpu == 2.0
    limiter.apply_config(max_cpu=1.0)
    assert limiter.max_cpu == 1.0
    # 上限调小后，原本 2.0 的默认值被收敛到新上限
    assert limiter.default_cpu == 1.0


def test_reload_from_settings(monkeypatch, restore_resource_settings):
    limiter = ResourceLimiter.from_settings()
    settings.resource.max_cpu = 3.0
    settings.resource.default_cpu = 2.5
    limiter.reload_from_settings()
    assert limiter.max_cpu == 3.0
    assert limiter.default_cpu == 2.5


# ---------------------------------------------------------------------------
# ResourceConfigStore：持久化
# ---------------------------------------------------------------------------

def test_resource_config_store_roundtrip(tmp_path):
    from app.services.resource_config import ResourceConfigStore

    path = str(tmp_path / "resource_config.json")
    store = ResourceConfigStore(path=path)
    assert store.enabled
    assert store.load() is None  # 文件尚不存在

    store.save({"default_cpu": 2.0, "max_cpu": 4.0, "unknown_field": 123})
    loaded = store.load()
    assert loaded["default_cpu"] == 2.0
    assert loaded["max_cpu"] == 4.0
    assert "unknown_field" not in loaded  # 未知字段被过滤


def test_resource_config_store_disabled_without_path():
    from app.services.resource_config import ResourceConfigStore

    store = ResourceConfigStore(path="")
    assert not store.enabled
    assert store.load() is None
    assert store.save({"default_cpu": 1.0}) is False


# ---------------------------------------------------------------------------
# validate_and_clamp
# ---------------------------------------------------------------------------

def test_validate_and_clamp_guardrail(restore_resource_settings):
    from app.services.resource_config import validate_and_clamp

    merged, warnings = validate_and_clamp({"max_cpu": 9999})
    assert merged["max_cpu"] == MAX_CPU
    assert any("绝对护栏" in w for w in warnings)


def test_validate_and_clamp_default_not_exceed_max(restore_resource_settings):
    from app.services.resource_config import validate_and_clamp

    merged, warnings = validate_and_clamp({"max_cpu": 1.0, "default_cpu": 4.0})
    assert merged["max_cpu"] == 1.0
    assert merged["default_cpu"] == 1.0
    assert any("默认值" in w for w in warnings)


# ---------------------------------------------------------------------------
# 路由：GET /resource-config 与 PUT /admin/resource-config
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_resource_config_view(monkeypatch):
    from app.routes import resource_config as rc_route

    monkeypatch.setattr(runtime, "sandbox_manager", None)
    view = await rc_route.get_resource_config()
    assert {"default", "max", "guardrails", "persistence", "notes"} <= set(view.keys())
    assert view["guardrails"]["cpu"][1] == MAX_CPU


@pytest.mark.asyncio
async def test_update_resource_config_requires_admin(monkeypatch):
    from app.routes import resource_config as rc_route
    from app.schemas import ResourceConfigUpdateRequest

    monkeypatch.setattr(settings, "admin_token", "secret-token")
    with pytest.raises(HTTPException) as exc_info:
        await rc_route.update_resource_config(
            ResourceConfigUpdateRequest(max_cpu=2.0), x_admin_token=None
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_update_resource_config_applies_and_persists(
    tmp_path, monkeypatch, restore_resource_settings
):
    from app.routes import resource_config as rc_route
    from app.services import resource_config as rc_service
    from app.schemas import ResourceConfigUpdateRequest

    monkeypatch.setattr(settings, "admin_token", "secret-token")
    store = rc_service.ResourceConfigStore(path=str(tmp_path / "rc.json"))
    monkeypatch.setattr(rc_service, "_store", store)

    limiter = ResourceLimiter.from_settings()
    monkeypatch.setattr(
        runtime, "sandbox_manager", SimpleNamespace(_resource_limiter=limiter)
    )

    result = await rc_route.update_resource_config(
        ResourceConfigUpdateRequest(max_cpu=3.0, default_cpu=2.0),
        x_admin_token="secret-token",
    )

    assert result["success"] is True
    assert result["persisted"] is True
    # settings 已更新
    assert settings.resource.max_cpu == 3.0
    assert settings.resource.default_cpu == 2.0
    # 限制器已热刷新
    assert limiter.max_cpu == 3.0
    assert limiter.default_cpu == 2.0
    # 已持久化
    assert store.load()["max_cpu"] == 3.0


@pytest.mark.asyncio
async def test_update_resource_config_clamps_over_guardrail(
    tmp_path, monkeypatch, restore_resource_settings
):
    from app.routes import resource_config as rc_route
    from app.services import resource_config as rc_service
    from app.schemas import ResourceConfigUpdateRequest

    monkeypatch.setattr(settings, "admin_token", "secret-token")
    monkeypatch.setattr(
        rc_service, "_store", rc_service.ResourceConfigStore(path=str(tmp_path / "rc.json"))
    )
    monkeypatch.setattr(runtime, "sandbox_manager", None)

    result = await rc_route.update_resource_config(
        ResourceConfigUpdateRequest(max_cpu=9999),
        x_admin_token="secret-token",
    )
    assert result["applied"]["max_cpu"] == MAX_CPU
    assert any("绝对护栏" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# create_session：per-沙箱资源分配透传与回显
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_session_passes_and_echoes_resource_limits(monkeypatch):
    from app.routes import sessions as sessions_route
    from app.schemas import CreateSessionRequest

    captured = {}

    async def fake_create_sandbox(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            sandbox_id="sandbox-1",
            cpu_limit=2.0,
            memory_limit_mb=2048,
            disk_limit_mb=4096,
            pids_limit=200,
        )

    monkeypatch.setattr(
        runtime, "sandbox_manager", SimpleNamespace(create_sandbox=fake_create_sandbox)
    )
    monkeypatch.setattr(runtime, "session_store", None)
    monkeypatch.setattr(
        sessions_route,
        "session_manager",
        SimpleNamespace(
            create_session=lambda sid: SimpleNamespace(
                session_id=sid or "s1",
                workspace_dir="/w",
                data_dir="/d",
                output_dir="/o",
            )
        ),
    )

    req = CreateSessionRequest(
        session_id="s1",
        cpu_limit=99.0,
        memory_limit_mb=8192,
        disk_limit_mb=4096,
        pids_limit=200,
    )
    resp = await sessions_route.create_session(req)

    # 原始请求值透传给 create_sandbox（收敛在限制器内部完成）
    assert captured["cpu_limit"] == 99.0
    assert captured["memory_limit_mb"] == 8192
    assert captured["disk_limit_mb"] == 4096
    assert captured["pids_limit"] == 200
    # 响应回显实际分配（收敛后）的真实资源
    assert resp.cpu_limit == 2.0
    assert resp.memory_limit_mb == 2048
    assert resp.disk_limit_mb == 4096
    assert resp.pids_limit == 200
