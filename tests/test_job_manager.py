"""JobManager 单元测试：生命周期、失败、取消、保留清理。"""

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.job_manager import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_SUCCEEDED,
    JobManager,
)


@pytest.mark.asyncio
async def test_job_succeeds_and_keeps_result():
    async def runner(record):
        return {"success": True, "stdout": "done", "execution_time_ms": 5}

    manager = JobManager(runner=runner, max_concurrent=2)
    record = manager.submit(code="print(1)", timeout=30)

    assert record.status in ("queued", "running")
    await asyncio.sleep(0.05)

    fetched = manager.get(record.job_id)
    assert fetched is not None
    assert fetched.status == JOB_STATUS_SUCCEEDED
    assert fetched.result["stdout"] == "done"
    assert fetched.finished_at is not None


@pytest.mark.asyncio
async def test_job_failure_records_error():
    async def runner(record):
        raise RuntimeError("boom")

    manager = JobManager(runner=runner)
    record = manager.submit(code="x", timeout=30)
    await asyncio.sleep(0.05)

    fetched = manager.get(record.job_id)
    assert fetched.status == JOB_STATUS_FAILED
    assert "boom" in fetched.error


@pytest.mark.asyncio
async def test_job_unsuccessful_result_maps_to_failed():
    async def runner(record):
        return {"success": False, "error": "ZeroDivisionError"}

    manager = JobManager(runner=runner)
    record = manager.submit(code="1/0", timeout=30)
    await asyncio.sleep(0.05)

    fetched = manager.get(record.job_id)
    assert fetched.status == JOB_STATUS_FAILED
    assert "ZeroDivisionError" in fetched.error


@pytest.mark.asyncio
async def test_cancel_running_job():
    started = asyncio.Event()

    async def runner(record):
        started.set()
        await asyncio.sleep(60)
        return {"success": True}

    manager = JobManager(runner=runner)
    record = manager.submit(code="long", timeout=120)
    await asyncio.wait_for(started.wait(), timeout=2)

    cancelled = manager.cancel(record.job_id)
    assert cancelled.status == JOB_STATUS_CANCELLED
    await asyncio.sleep(0.05)
    assert manager.get(record.job_id).status == JOB_STATUS_CANCELLED


@pytest.mark.asyncio
async def test_timeout_clamped_to_max():
    async def runner(record):
        return {"success": True}

    manager = JobManager(runner=runner, max_timeout=100)
    record = manager.submit(code="x", timeout=99999)
    assert record.timeout == 100
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_prune_removes_expired_finished_jobs():
    async def runner(record):
        return {"success": True}

    manager = JobManager(runner=runner, retention_seconds=60)
    record = manager.submit(code="x", timeout=10)
    await asyncio.sleep(0.05)

    # 手动把完成时间拨到过期
    manager._jobs[record.job_id].finished_at = time.time() - 120
    manager.prune()
    assert manager.get(record.job_id) is None


@pytest.mark.asyncio
async def test_shutdown_cancels_running():
    async def runner(record):
        await asyncio.sleep(60)
        return {"success": True}

    manager = JobManager(runner=runner)
    record = manager.submit(code="x", timeout=120)
    await asyncio.sleep(0.05)
    await manager.shutdown()
    assert manager.get(record.job_id).status == JOB_STATUS_CANCELLED
