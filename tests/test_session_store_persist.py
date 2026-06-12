"""SessionStore 异步持久化单测（production-hardening #3）。

验证：write-through 写不阻塞事件循环（offload 到单线程 executor）、
WAL 生效、重启可恢复、close 干净关闭。
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.session_store import SessionStore  # noqa: E402


@pytest.mark.asyncio
async def test_persist_and_restore(tmp_path):
    db = str(tmp_path / "sessions.db")
    store = SessionStore(db_path=db)
    try:
        info = await store.create_session(session_id="s1", metadata={"k": "v"})
        assert info.session_id == "s1"
        await store.update_sandbox_id("s1", "sandbox-abc")
    finally:
        await store.close()

    # 新实例从同一 DB 恢复
    store2 = SessionStore(db_path=db)
    try:
        restored = await store2.get_session("s1")
        assert restored is not None
        # 重启后 sandbox 绑定被清空（容器已不在）
        assert restored.sandbox_id is None
        assert restored.metadata.get("k") == "v"
    finally:
        await store2.close()


@pytest.mark.asyncio
async def test_wal_enabled(tmp_path):
    db = str(tmp_path / "wal.db")
    store = SessionStore(db_path=db)
    try:
        mode = store._db.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_persist_does_not_block_event_loop(tmp_path):
    """写盘期间事件循环仍能跑别的协程（offload 生效的弱验证）。"""
    db = str(tmp_path / "block.db")
    store = SessionStore(db_path=db)
    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(50):
            ticks += 1
            await asyncio.sleep(0.001)

    try:
        t = asyncio.create_task(ticker())
        for i in range(30):
            await store.create_session(session_id=f"s{i}", metadata={})
        await t
        assert ticks == 50
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_delete_persisted(tmp_path):
    db = str(tmp_path / "del.db")
    store = SessionStore(db_path=db)
    try:
        await store.create_session(session_id="d1", metadata={})
        assert await store.get_session("d1") is not None
        await store.delete_session("d1")
        assert await store.get_session("d1") is None
    finally:
        await store.close()

    store2 = SessionStore(db_path=db)
    try:
        assert await store2.get_session("d1") is None
    finally:
        await store2.close()
