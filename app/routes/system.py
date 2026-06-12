"""
系统路由：健康检查、Prometheus 指标、统计信息、队列状态、过期会话清理
"""

import logging

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from .. import runtime
from ..executor import session_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["System"])


@router.get("/health")
async def health_check():
    """
    健康检查端点

    Requirements 9.1: 提供服务级别的健康检查端点
    Requirements 9.2: 报告当前活跃沙箱数量、容器池状态和系统资源使用

    Returns:
        服务健康状态信息
    """
    if runtime.health_monitor:
        try:
            health = await runtime.health_monitor.get_service_health()
            return {
                "status": health.status,
                "service": "code-executor",
                "active_sandboxes": health.active_sandboxes,
                "pool_available": health.pool_available,
                "pool_total": health.pool_total,
                "cpu_usage_percent": round(health.cpu_usage_percent, 2),
                "memory_usage_percent": round(health.memory_usage_percent, 2),
                "uptime_seconds": health.uptime_seconds,
                "last_check": health.last_check.isoformat(),
                "details": health.details,
            }
        except Exception as e:
            logger.warning(f"获取健康状态失败: {e}")

    # 回退到基本健康检查
    return {"status": "healthy", "service": "code-executor"}


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    """
    Prometheus 指标端点

    Requirements 9.5: 支持 Prometheus 格式的指标导出

    Returns:
        Prometheus 格式的指标文本
    """
    from ..executor import EXECUTION_PATH_COUNTERS

    extra_lines = [
        "# HELP sandbox_kernel_exec_total 通过 Kernel Server 快路径执行的次数",
        "# TYPE sandbox_kernel_exec_total counter",
        f"sandbox_kernel_exec_total {EXECUTION_PATH_COUNTERS['kernel_exec_total']}",
        "",
        "# HELP sandbox_kernel_fallback_total 回退到 docker exec 慢路径的次数（>0 说明热启动失效，需要排查）",
        "# TYPE sandbox_kernel_fallback_total counter",
        f"sandbox_kernel_fallback_total {EXECUTION_PATH_COUNTERS['kernel_fallback_total']}",
        "",
    ]
    extra = "\n".join(extra_lines)

    if runtime.health_monitor:
        try:
            return runtime.health_monitor.export_prometheus_metrics() + "\n" + extra
        except Exception as e:
            logger.warning(f"导出 Prometheus 指标失败: {e}")

    # 回退到基本指标
    return extra


@router.post("/cleanup")
async def cleanup_old_sessions(max_age_hours: float = 12):
    """
    清理过期会话

    Requirements 10.1: 保持现有 API 接口格式不变
    """
    # 清理会话存储中的过期会话
    expired_count = 0
    if runtime.session_store:
        expired_count = await runtime.session_store.cleanup_expired()

    # 清理兼容的 session_manager 中的过期会话
    count = await session_manager.cleanup_old_sessions(max_age_hours)

    return {"cleaned": count + expired_count}


@router.get("/statistics")
async def get_statistics():
    """
    获取服务统计信息

    Requirements 10.6: 新增功能通过新的 API 端点提供
    """
    stats = {
        "service": "code-executor",
        "version": "2.0.0",
        "sandbox_manager_enabled": runtime.sandbox_manager is not None,
        "session_count": len(session_manager.sessions),
    }

    if runtime.sandbox_manager:
        sandbox_stats = await runtime.sandbox_manager.get_statistics()
        stats["sandbox"] = sandbox_stats

    if runtime.session_store:
        stats["session_store"] = {
            "total_sessions": await runtime.session_store.get_session_count(),
            "active_sessions": await runtime.session_store.get_active_session_count(),
        }

    if runtime.execution_queue:
        stats["execution_queue"] = runtime.execution_queue.get_global_status()

    return stats


@router.get("/queue/status")
async def get_queue_status():
    """
    获取执行队列状态

    返回当前排队数、执行数、平均耗时等信息。
    """
    if not runtime.execution_queue:
        return {"enabled": False, "message": "执行队列未启用"}
    return {"enabled": True, **runtime.execution_queue.get_global_status()}
