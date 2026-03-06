"""
执行队列模块 - 基于令牌桶的并发控制

使用 asyncio.Semaphore 限制真实并发执行数，匹配 CPU 核心数，
避免高并发场景下 CPU 上下文切换导致的性能退化。

提供排队凭证（QueueTicket）用于前端展示排队状态。
"""

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncGenerator, Callable, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class QueueTicket:
    """排队凭证"""
    ticket_id: str
    session_id: str
    position: int = 0
    estimated_wait_seconds: float = 0.0
    status: str = "queued"  # queued | executing | completed
    enqueued_at: float = field(default_factory=time.monotonic)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None


class ExecutionQueue:
    """
    执行队列 - 基于 asyncio.Semaphore 的令牌桶

    限制同时执行的代码数量，超出的请求排队等待。
    提供排队位置和预估等待时间，供前端展示。

    Args:
        max_concurrent: 最大并发执行数（建议 = CPU 核心数）
        avg_execution_time: 初始平均执行时间估算（秒）
        queue_timeout: 排队超时时间（秒）
    """

    def __init__(
        self,
        max_concurrent: int = 4,
        avg_execution_time: float = 5.0,
        queue_timeout: int = 300,
    ):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._queue_timeout = queue_timeout

        # 等待队列（有序）和正在执行的票据
        self._queue: OrderedDict[str, QueueTicket] = OrderedDict()
        self._executing: Dict[str, QueueTicket] = {}
        self._lock = asyncio.Lock()

        # 滑动平均执行时间
        self._avg_execution_time = avg_execution_time
        self._alpha = 0.3  # 指数移动平均系数

        # 统计
        self._total_enqueued = 0
        self._total_executed = 0
        self._total_timed_out = 0

        # 回调函数：当排队位置变化时通知
        self._position_change_callbacks: Dict[str, Callable] = {}

        logger.info(
            f"ExecutionQueue 初始化: max_concurrent={max_concurrent}, "
            f"avg_execution_time={avg_execution_time}s, "
            f"queue_timeout={queue_timeout}s"
        )

    @asynccontextmanager
    async def acquire(self, session_id: str) -> AsyncGenerator[QueueTicket, None]:
        """
        上下文管理器：排队 -> 获取令牌 -> 执行 -> 释放

        Usage:
            async with queue.acquire(session_id) as ticket:
                # ticket.status == 'executing'
                result = await do_work()
        """
        ticket = QueueTicket(
            ticket_id=str(uuid.uuid4()),
            session_id=session_id,
        )

        # 加入等待队列
        async with self._lock:
            self._queue[ticket.ticket_id] = ticket
            self._total_enqueued += 1
            self._refresh_positions()

        logger.info(
            f"[Queue] 入队: ticket={ticket.ticket_id[:8]}, "
            f"session={session_id}, position={ticket.position}, "
            f"est_wait={ticket.estimated_wait_seconds:.1f}s"
        )

        try:
            # 等待信号量（带超时）
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(),
                    timeout=self._queue_timeout,
                )
            except asyncio.TimeoutError:
                # 排队超时
                async with self._lock:
                    self._queue.pop(ticket.ticket_id, None)
                    self._total_timed_out += 1
                    self._refresh_positions()
                logger.warning(
                    f"[Queue] 排队超时: ticket={ticket.ticket_id[:8]}, "
                    f"timeout={self._queue_timeout}s"
                )
                raise asyncio.TimeoutError(
                    f"排队等待超时（{self._queue_timeout}秒），服务器繁忙请稍后重试"
                )

            # 获得令牌，从等待队列移入执行中
            async with self._lock:
                self._queue.pop(ticket.ticket_id, None)
                ticket.status = "executing"
                ticket.started_at = time.monotonic()
                self._executing[ticket.ticket_id] = ticket
                self._refresh_positions()

            logger.info(
                f"[Queue] 开始执行: ticket={ticket.ticket_id[:8]}, "
                f"waited={ticket.started_at - ticket.enqueued_at:.2f}s"
            )

            yield ticket

        finally:
            # 释放信号量，更新统计
            execution_time = 0.0
            async with self._lock:
                ticket.status = "completed"
                ticket.completed_at = time.monotonic()
                self._executing.pop(ticket.ticket_id, None)

                if ticket.started_at:
                    execution_time = ticket.completed_at - ticket.started_at
                    self._update_avg_time(execution_time)

                self._total_executed += 1
                self._refresh_positions()

            self._semaphore.release()

            logger.info(
                f"[Queue] 执行完成: ticket={ticket.ticket_id[:8]}, "
                f"exec_time={execution_time:.2f}s, "
                f"avg_time={self._avg_execution_time:.2f}s"
            )

    def create_ticket(self, session_id: str) -> QueueTicket:
        """创建排队凭证（用于 WebSocket 模式，手动管理生命周期）"""
        ticket = QueueTicket(
            ticket_id=str(uuid.uuid4()),
            session_id=session_id,
        )
        return ticket

    @asynccontextmanager
    async def acquire_with_ticket(
        self, ticket: QueueTicket
    ) -> AsyncGenerator[QueueTicket, None]:
        """使用已创建的 ticket 进行排队（用于 WebSocket 模式）"""
        async with self._lock:
            self._queue[ticket.ticket_id] = ticket
            self._total_enqueued += 1
            self._refresh_positions()

        try:
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(),
                    timeout=self._queue_timeout,
                )
            except asyncio.TimeoutError:
                async with self._lock:
                    self._queue.pop(ticket.ticket_id, None)
                    self._total_timed_out += 1
                    self._refresh_positions()
                raise asyncio.TimeoutError(
                    f"排队等待超时（{self._queue_timeout}秒），服务器繁忙请稍后重试"
                )

            async with self._lock:
                self._queue.pop(ticket.ticket_id, None)
                ticket.status = "executing"
                ticket.started_at = time.monotonic()
                self._executing[ticket.ticket_id] = ticket
                self._refresh_positions()

            yield ticket

        finally:
            execution_time = 0.0
            async with self._lock:
                ticket.status = "completed"
                ticket.completed_at = time.monotonic()
                self._executing.pop(ticket.ticket_id, None)

                if ticket.started_at:
                    execution_time = ticket.completed_at - ticket.started_at
                    self._update_avg_time(execution_time)

                self._total_executed += 1
                self._refresh_positions()

            self._semaphore.release()

    def get_queue_status(self, ticket_id: str) -> Optional[QueueTicket]:
        """查询排队状态"""
        if ticket_id in self._queue:
            return self._queue[ticket_id]
        if ticket_id in self._executing:
            return self._executing[ticket_id]
        return None

    def get_global_status(self) -> dict:
        """全局队列状态"""
        return {
            "queued_count": len(self._queue),
            "executing_count": len(self._executing),
            "max_concurrent": self._max_concurrent,
            "avg_execution_time": round(self._avg_execution_time, 2),
            "total_enqueued": self._total_enqueued,
            "total_executed": self._total_executed,
            "total_timed_out": self._total_timed_out,
        }

    def register_position_callback(
        self, session_id: str, callback: Callable[[QueueTicket], None]
    ) -> None:
        """
        注册排队位置变化回调
        
        当该 session 的排队位置发生变化时，会调用 callback(ticket)
        
        Args:
            session_id: 会话 ID
            callback: 回调函数，接收 QueueTicket 参数
        """
        self._position_change_callbacks[session_id] = callback
        logger.debug(f"Registered position callback for session {session_id}")

    def unregister_position_callback(self, session_id: str) -> None:
        """
        取消注册排队位置变化回调
        
        Args:
            session_id: 会话 ID
        """
        if session_id in self._position_change_callbacks:
            del self._position_change_callbacks[session_id]
            logger.debug(f"Unregistered position callback for session {session_id}")

    def _refresh_positions(self) -> None:
        """刷新所有等待中 ticket 的位置和预估时间，并触发回调"""
        for idx, ticket in enumerate(self._queue.values()):
            old_position = ticket.position
            ticket.position = idx + 1
            ticket.estimated_wait_seconds = self._estimate_wait(ticket.position)
            
            # 如果位置发生变化，触发回调
            if old_position != ticket.position:
                self._notify_position_change(ticket)

    def _notify_position_change(self, ticket: QueueTicket) -> None:
        """
        通知排队位置变化
        
        Args:
            ticket: 排队凭证
        """
        callback = self._position_change_callbacks.get(ticket.session_id)
        if callback:
            try:
                # 如果是异步回调，创建任务执行
                if asyncio.iscoroutinefunction(callback):
                    asyncio.create_task(callback(ticket))
                else:
                    callback(ticket)
            except Exception as e:
                logger.error(
                    f"Error in position change callback for session {ticket.session_id}: {e}"
                )

    async def resize(self, new_max_concurrent: int) -> None:
        """
        Dynamically adjust the concurrency limit to match available resources
        (e.g. container pool size changes).

        Increasing the limit releases extra permits immediately; decreasing
        it takes effect as existing permits are returned.
        """
        if new_max_concurrent < 1:
            new_max_concurrent = 1

        async with self._lock:
            old = self._max_concurrent
            if new_max_concurrent == old:
                return

            delta = new_max_concurrent - old
            self._max_concurrent = new_max_concurrent

            if delta > 0:
                for _ in range(delta):
                    self._semaphore.release()
            else:
                for _ in range(-delta):
                    try:
                        self._semaphore.acquire_nowait()
                    except Exception:
                        break

            self._refresh_positions()

        logger.info(f"[Queue] 并发限制调整: {old} -> {new_max_concurrent}")

    def _estimate_wait(self, position: int) -> float:
        """预估等待时间 = ceil(position / max_concurrent) * avg_time"""
        import math
        batches = math.ceil(position / self._max_concurrent)
        return batches * self._avg_execution_time

    def _update_avg_time(self, execution_time: float) -> None:
        """指数移动平均更新执行时间"""
        self._avg_execution_time = (
            self._alpha * execution_time
            + (1 - self._alpha) * self._avg_execution_time
        )
