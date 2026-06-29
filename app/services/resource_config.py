"""
资源配置管理服务

职责：
- ResourceConfigStore：把运行时修改的资源配置（默认值 + 软上限）持久化为 JSON 文件，
  重启自动加载，使变更跨重启保留；
- build_view / validate_and_clamp / apply_config：查看、校验收敛、应用到运行时
  ``settings.resource`` 并热刷新资源限制器。

存储位置：默认 ``$WORKSPACE_DIR/resource_config.json``（compose 中是持久化卷）；
WORKSPACE_DIR 未设置或目录不存在时持久化自动禁用（变更仅进程内生效）。

收敛策略与 per-沙箱分配保持一致——超出「绝对护栏」一律自动收敛(clamp)，不抛错；
同时保证默认值不超过软上限。
"""

import json
import logging
import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from ..config import settings
from ..infrastructure.resource_limiter import (
    MAX_CPU,
    MAX_DISK_MB,
    MAX_MEMORY_MB,
    MAX_PIDS,
    MIN_CPU,
    MIN_DISK_MB,
    MIN_MEMORY_MB,
    MIN_PIDS,
)

logger = logging.getLogger(__name__)

# 允许持久化 / 管理的资源配置字段（config 暂无 max_pids，进程数上限固定为绝对护栏）
RESOURCE_CONFIG_FIELDS = (
    "default_cpu",
    "default_memory_mb",
    "default_disk_mb",
    "default_pids",
    "max_cpu",
    "max_memory_mb",
    "max_disk_mb",
)

RESOURCE_CONFIG_FILENAME = "resource_config.json"

# 字段 -> (绝对下限, 绝对上限, 类型转换)
_GUARDRAILS: Dict[str, Tuple[float, float, type]] = {
    "default_cpu": (MIN_CPU, MAX_CPU, float),
    "default_memory_mb": (MIN_MEMORY_MB, MAX_MEMORY_MB, int),
    "default_disk_mb": (MIN_DISK_MB, MAX_DISK_MB, int),
    "default_pids": (MIN_PIDS, MAX_PIDS, int),
    "max_cpu": (MIN_CPU, MAX_CPU, float),
    "max_memory_mb": (MIN_MEMORY_MB, MAX_MEMORY_MB, int),
    "max_disk_mb": (MIN_DISK_MB, MAX_DISK_MB, int),
}


class ResourceConfigStore:
    """资源配置的 JSON 文件持久化存储。"""

    def __init__(self, path: Optional[str] = None):
        self._path = path if path is not None else self._resolve_default_path()

    @staticmethod
    def _resolve_default_path() -> str:
        workspace_dir = os.environ.get("WORKSPACE_DIR", "")
        if workspace_dir and os.path.isdir(workspace_dir):
            return os.path.join(workspace_dir, RESOURCE_CONFIG_FILENAME)
        return ""

    @property
    def path(self) -> str:
        return self._path

    @property
    def enabled(self) -> bool:
        """持久化是否可用（解析到有效路径）。"""
        return bool(self._path)

    def load(self) -> Optional[Dict[str, Any]]:
        """读取持久化的资源配置；文件不存在 / 持久化禁用 / 解析失败时返回 None。"""
        if not self._path or not os.path.exists(self._path):
            return None
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"读取资源配置持久化文件失败({self._path}): {e}")
            return None
        if not isinstance(data, dict):
            logger.warning(f"资源配置持久化文件格式异常(非对象): {self._path}")
            return None
        return {k: data[k] for k in RESOURCE_CONFIG_FIELDS if k in data}

    def save(self, config: Dict[str, Any]) -> bool:
        """原子写入资源配置；持久化禁用时跳过并返回 False。"""
        if not self._path:
            logger.warning(
                "资源配置持久化未启用（WORKSPACE_DIR 未设置或目录不存在），变更仅进程内生效"
            )
            return False
        payload = {k: config[k] for k in RESOURCE_CONFIG_FIELDS if k in config}
        try:
            directory = os.path.dirname(self._path) or "."
            os.makedirs(directory, exist_ok=True)
            # 原子写：临时文件 + 替换，避免写一半被读到
            fd, tmp_path = tempfile.mkstemp(
                prefix=".resource_config_", suffix=".tmp", dir=directory
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self._path)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            logger.info(f"资源配置已持久化到 {self._path}: {payload}")
            return True
        except OSError as e:
            logger.warning(f"写入资源配置持久化文件失败({self._path}): {e}")
            return False


_store: Optional[ResourceConfigStore] = None


def get_resource_config_store() -> ResourceConfigStore:
    """获取资源配置存储单例。"""
    global _store
    if _store is None:
        _store = ResourceConfigStore()
    return _store


def _active_limiter():
    """返回当前运行的资源限制器实例（无沙箱模式时为 None）。"""
    from .. import runtime

    if runtime.sandbox_manager is not None:
        return getattr(runtime.sandbox_manager, "_resource_limiter", None)
    return None


def build_view() -> Dict[str, Any]:
    """构建资源配置视图：当前生效的默认值 / 软上限 / 绝对护栏 / 说明。"""
    limiter = _active_limiter()
    if limiter is not None:
        eff = limiter.get_effective_config()
    else:
        r = settings.resource
        eff = {
            "default_cpu": r.default_cpu,
            "default_memory_mb": r.default_memory_mb,
            "default_disk_mb": r.default_disk_mb,
            "default_pids": r.default_pids,
            "max_cpu": r.max_cpu,
            "max_memory_mb": r.max_memory_mb,
            "max_disk_mb": r.max_disk_mb,
            "max_pids": MAX_PIDS,
        }
    return {
        "default": {
            "cpu": eff["default_cpu"],
            "memory_mb": eff["default_memory_mb"],
            "disk_mb": eff["default_disk_mb"],
            "pids": eff["default_pids"],
        },
        "max": {
            "cpu": eff["max_cpu"],
            "memory_mb": eff["max_memory_mb"],
            "disk_mb": eff["max_disk_mb"],
            "pids": eff["max_pids"],
        },
        "guardrails": {
            "cpu": [MIN_CPU, MAX_CPU],
            "memory_mb": [MIN_MEMORY_MB, MAX_MEMORY_MB],
            "disk_mb": [MIN_DISK_MB, MAX_DISK_MB],
            "pids": [MIN_PIDS, MAX_PIDS],
        },
        "persistence": {
            "enabled": get_resource_config_store().enabled,
            "path": get_resource_config_store().path,
        },
        "sandbox_manager_active": limiter is not None,
        "notes": [
            "default：创建沙箱/池容器时未显式指定资源时使用的默认值",
            "max：单沙箱可分配的软上限，创建会话超限时自动收敛(clamp)到上限",
            "guardrails：绝对护栏，软上限不可超过该区间",
            "磁盘限制依赖存储驱动，当前仅作为元数据记录与展示",
            "运行时修改默认值影响新建容器；已有预热池容器随老化轮替后生效",
        ],
    }


def validate_and_clamp(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """
    在当前生效配置基础上合并 payload，收敛到绝对护栏，并保证默认值 <= 软上限。

    Returns:
        (merged, warnings)：merged 为含全部 7 个字段的最终生效配置；
        warnings 记录发生的收敛/降级，供调用方回显。
    """
    warnings: List[str] = []
    r = settings.resource
    merged: Dict[str, Any] = {
        "default_cpu": r.default_cpu,
        "default_memory_mb": r.default_memory_mb,
        "default_disk_mb": r.default_disk_mb,
        "default_pids": r.default_pids,
        "max_cpu": r.max_cpu,
        "max_memory_mb": r.max_memory_mb,
        "max_disk_mb": r.max_disk_mb,
    }

    # 合并请求中提供的字段（忽略 None / 未知字段）
    for key in RESOURCE_CONFIG_FIELDS:
        if key in payload and payload[key] is not None:
            merged[key] = payload[key]

    # 类型转换 + 绝对护栏收敛
    for key, (lo, hi, caster) in _GUARDRAILS.items():
        raw = merged[key]
        try:
            value = caster(raw)
        except (TypeError, ValueError):
            fallback = caster(getattr(r, key))
            warnings.append(f"{key} 取值非法({raw!r})，保持原值 {fallback}")
            merged[key] = fallback
            continue
        clamped = caster(min(max(value, lo), hi))
        if clamped != value:
            warnings.append(f"{key}={value} 超出绝对护栏 [{lo}, {hi}]，收敛为 {clamped}")
        merged[key] = clamped

    # 保证默认值不超过软上限（按维度下调默认值）
    for label, default_key, max_key in (
        ("CPU", "default_cpu", "max_cpu"),
        ("内存", "default_memory_mb", "max_memory_mb"),
        ("磁盘", "default_disk_mb", "max_disk_mb"),
    ):
        if merged[default_key] > merged[max_key]:
            warnings.append(
                f"{label}默认值 {merged[default_key]} 大于上限 {merged[max_key]}，"
                f"已下调默认值到上限"
            )
            merged[default_key] = merged[max_key]

    return merged, warnings


def apply_config(merged: Dict[str, Any]) -> None:
    """把已收敛的配置写入运行时 settings.resource，并热刷新资源限制器。"""
    r = settings.resource
    r.default_cpu = merged["default_cpu"]
    r.default_memory_mb = merged["default_memory_mb"]
    r.default_disk_mb = merged["default_disk_mb"]
    r.default_pids = merged["default_pids"]
    r.max_cpu = merged["max_cpu"]
    r.max_memory_mb = merged["max_memory_mb"]
    r.max_disk_mb = merged["max_disk_mb"]

    limiter = _active_limiter()
    if limiter is not None:
        limiter.reload_from_settings()


def load_persisted_into_settings() -> Optional[Dict[str, Any]]:
    """
    启动时调用：把持久化的资源配置加载并应用到 settings.resource。

    在沙箱管理器初始化「之前」调用，这样 ResourceLimiter.from_settings 直接读到
    持久化后的值；返回应用后的生效配置（无持久化时返回 None）。
    """
    persisted = get_resource_config_store().load()
    if not persisted:
        return None
    merged, warnings = validate_and_clamp(persisted)
    apply_config(merged)
    for w in warnings:
        logger.warning(f"加载持久化资源配置时收敛: {w}")
    logger.info(f"已从持久化文件加载资源配置: {merged}")
    return merged
