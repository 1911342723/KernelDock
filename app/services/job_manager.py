"""
后台任务管理器：长时执行的异步提交 + 轮询

设计取舍：
- 任务不占用 HTTP 执行队列（避免 1h 长任务饿死同步请求），
  走独立 asyncio.Semaphore 限流（settings.jobs.max_concurrent）。
- 纯内存存储（单副本现实），已完成结果保留 retention_seconds 供轮询，
  总量超 max_entries 时淘汰最旧的已完成记录。
- 取消语义：cancel() 取消 asyncio 任务；kernel 侧 fork 子进程由
  执行超时 SIGKILL 兜底，不会泄漏。
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"

_TERMINAL_STATUSES = {JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED}


@dataclass
class JobRecord:
    job_id: str
    kind: str                       # "stateless" | "session"
    code: str
    timeout: int
    session_id: Optional[str] = None
    status: str = JOB_STATUS_QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self, include_result: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "job_id": self.job_id,
            "kind": self.kind,
            "status": self.status,
            "session_id": self.session_id,
            "timeout": self.timeout,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }
        if include_result:
            payload["result"] = self.result
        return payload


class JobManager:
    """后台任务管理器。"""

    def __init__(
        self,
        runner: Callable[[JobRecord], Awaitable[Dict[str, Any]]],
        max_concurrent: int = 2,
        max_timeout: int = 3600,
        retention_seconds: int = 3600,
        max_entries: int = 500,
    ):
        """
        Args:
            runner: 实际执行函数，输入 JobRecord 返回结果字典
                    （由装配层注入，通常调用 SandboxManager 的执行方法）
            max_concurrent: 并发上限
            max_timeout: 单任务最大超时（秒）
            retention_seconds: 已完成结果保留时长
            max_entries: 记录总数上限
        """
        self._runner = runner
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_timeout = max_timeout
        self._retention_seconds = retention_seconds
        self._max_entries = max_entries
        self._jobs: Dict[str, JobRecord] = {}
        self._tasks: Dict[str, asyncio.Task] = {}

    @property
    def max_timeout(self) -> int:
        return self._max_timeout

    def submit(
        self,
        code: str,
        timeout: int,
        session_id: Optional[str] = None,
    ) -> JobRecord:
        """提交任务，立即返回 queued 记录。"""
        self.prune()

        timeout = max(1, min(int(timeout), self._max_timeout))
        record = JobRecord(
            job_id=f"job-{uuid.uuid4().hex[:12]}",
            kind="session" if session_id else "stateless",
            code=code,
            timeout=timeout,
            session_id=session_id,
        )
        self._jobs[record.job_id] = record
        task = asyncio.create_task(self._run(record), name=record.job_id)
        self._tasks[record.job_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(record.job_id, None))
        logger.info(f"后台任务已提交: {record.job_id} kind={record.kind} timeout={timeout}s")
        return record

    async def _run(self, record: JobRecord) -> None:
        try:
            async with self._semaphore:
                if record.status == JOB_STATUS_CANCELLED:
                    return
                record.status = JOB_STATUS_RUNNING
                record.started_at = time.time()
                result = await self._runner(record)
                record.result = result
                record.status = (
                    JOB_STATUS_SUCCEEDED
                    if isinstance(result, dict) and result.get("success", False)
                    else JOB_STATUS_FAILED
                )
                if record.status == JOB_STATUS_FAILED and isinstance(result, dict):
                    record.error = str(result.get("error") or "execution failed")
        except asyncio.CancelledError:
            record.status = JOB_STATUS_CANCELLED
            record.error = "cancelled"
            raise
        except Exception as e:
            record.status = JOB_STATUS_FAILED
            record.error = str(e)
            logger.error(f"后台任务失败: {record.job_id}, {e}")
        finally:
            if record.finished_at is None:
                record.finished_at = time.time()

    def get(self, job_id: str) -> Optional[JobRecord]:
        self.prune()
        return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 50) -> List[JobRecord]:
        self.prune()
        records = sorted(self._jobs.values(), key=lambda r: r.created_at, reverse=True)
        return records[:limit]

    def cancel(self, job_id: str) -> Optional[JobRecord]:
        """取消任务；已终态的直接返回现状。"""
        record = self._jobs.get(job_id)
        if record is None:
            return None
        if record.status in _TERMINAL_STATUSES:
            return record
        task = self._tasks.get(job_id)
        record.status = JOB_STATUS_CANCELLED
        record.error = "cancelled"
        record.finished_at = time.time()
        if task and not task.done():
            task.cancel()
        return record

    def prune(self) -> None:
        """清理过期的已完成记录 + 总量超限时淘汰最旧的已完成记录。"""
        now = time.time()
        expired = [
            job_id
            for job_id, r in self._jobs.items()
            if r.status in _TERMINAL_STATUSES
            and r.finished_at is not None
            and now - r.finished_at > self._retention_seconds
        ]
        for job_id in expired:
            self._jobs.pop(job_id, None)

        if len(self._jobs) > self._max_entries:
            finished = sorted(
                (r for r in self._jobs.values() if r.status in _TERMINAL_STATUSES),
                key=lambda r: r.finished_at or r.created_at,
            )
            overflow = len(self._jobs) - self._max_entries
            for r in finished[:overflow]:
                self._jobs.pop(r.job_id, None)

    async def shutdown(self) -> None:
        """取消全部未完成任务。"""
        for job_id, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
