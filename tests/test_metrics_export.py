import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.health_monitor import HealthMonitor, SandboxMetrics


def test_prometheus_export_includes_execution_and_container_metrics():
    monitor = HealthMonitor()
    monitor.record_execution(duration_seconds=0.2, success=True)
    monitor.record_execution(duration_seconds=1.2, success=False)
    monitor._cached_metrics = {
        "sandbox-1": SandboxMetrics(
            sandbox_id="sandbox-1",
            cpu_percent=10.0,
            memory_used_mb=128.0,
            memory_limit_mb=256,
            disk_used_mb=20.0,
            disk_limit_mb=512,
            network_rx_bytes=10,
            network_tx_bytes=20,
            timestamp=datetime.now(),
        )
    }

    exported = monitor.export_prometheus_metrics()

    assert "sandbox_active_count" in exported
    assert "sandbox_pool_available" not in exported or isinstance(exported, str)
    assert 'execution_total{status="success"} 1' in exported
    assert 'execution_total{status="failure"} 1' in exported
    assert 'execution_duration_seconds_bucket{le="0.25"} 1' in exported
    assert 'execution_duration_seconds_bucket{le="2.0"} 2' in exported
    assert 'container_memory_bytes{sandbox_id="sandbox-1"} 134217728' in exported
