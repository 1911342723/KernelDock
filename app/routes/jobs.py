"""
后台任务路由：长时执行的异步提交 + 轮询 + 取消

任务不占用 HTTP 执行队列（独立信号量限流，见 JobsConfig）。
"""

import logging
from typing import List

from fastapi import APIRouter, HTTPException

from .. import runtime
from ..schemas import JobResponse, SubmitJobRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Jobs"])


def _require_job_manager():
    if runtime.job_manager is None:
        raise HTTPException(status_code=503, detail="后台任务管理器未初始化")
    return runtime.job_manager


@router.post("/jobs", response_model=JobResponse, status_code=202)
async def submit_job(request: SubmitJobRequest):
    """
    提交后台任务（异步执行 Python 代码，立即返回 job_id）。

    - 不带 session_id：无状态执行（借池容器）
    - 带 session_id：在该会话沙箱内执行（要求会话已绑定沙箱）
    - timeout 上限由 SANDBOX_JOBS__MAX_TIMEOUT 控制（默认 3600s）
    """
    job_manager = _require_job_manager()
    if not runtime.sandbox_manager:
        raise HTTPException(status_code=503, detail="沙箱管理器未启用（后台任务需要 Docker 沙箱模式）")

    if request.session_id:
        sandbox_info = await runtime.sandbox_manager.get_sandbox_by_session(request.session_id)
        if not sandbox_info:
            raise HTTPException(status_code=404, detail="Session 不存在或未绑定沙箱")

    record = job_manager.submit(
        code=request.code,
        timeout=request.timeout,
        session_id=request.session_id,
    )
    return JobResponse(**record.to_dict())


@router.get("/jobs", response_model=List[JobResponse])
async def list_jobs(limit: int = 50):
    """列出最近的后台任务（不含 result 正文，避免响应过大）。"""
    job_manager = _require_job_manager()
    return [
        JobResponse(**record.to_dict(include_result=False))
        for record in job_manager.list_jobs(limit=max(1, min(limit, 200)))
    ]


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str):
    """查询任务状态；终态时附带完整执行结果。"""
    job_manager = _require_job_manager()
    record = job_manager.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期清理")
    return JobResponse(**record.to_dict())


@router.delete("/jobs/{job_id}", response_model=JobResponse)
async def cancel_job(job_id: str):
    """取消任务（已终态的返回现状）。"""
    job_manager = _require_job_manager()
    record = job_manager.cancel(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期清理")
    return JobResponse(**record.to_dict())
