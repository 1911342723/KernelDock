import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import CodeContext
from app.services.context_manager import ContextManager


def test_code_context_touch_and_idle_detection():
    context = CodeContext(
        context_id="ctx_1",
        session_id="sess_1",
        last_used_at=datetime.now() - timedelta(seconds=10),
    )

    assert context.is_idle(datetime.now(), timeout_seconds=5) is True

    context.touch()

    assert context.is_idle(datetime.now(), timeout_seconds=5) is False


def test_context_manager_create_and_list_contexts():
    manager = ContextManager()

    first = manager.create_context("sess_1")
    second = manager.create_context("sess_1")

    contexts = manager.list_contexts("sess_1")

    assert [ctx.context_id for ctx in contexts] == [first.context_id, second.context_id]


def test_context_manager_fork_copies_data_refs_and_focus():
    manager = ContextManager()
    parent = manager.create_context("sess_1")
    parent.data_refs = ("t_a", "t_b")
    parent.focus_ref = "t_b"

    forked = manager.create_context("sess_1", fork_from=parent.context_id)

    assert forked.context_id != parent.context_id
    assert forked.data_refs == ("t_a", "t_b")
    assert forked.focus_ref == "t_b"


def test_context_manager_delete_and_cleanup_idle():
    manager = ContextManager()
    active = manager.create_context("sess_1")
    idle = manager.create_context("sess_1")
    idle.last_used_at = datetime.now() - timedelta(seconds=3600)

    deleted = manager.cleanup_idle(timeout=60)

    assert deleted == [idle.context_id]
    assert manager.get_context(idle.context_id) is None
    assert manager.get_context(active.context_id) is not None

    assert manager.delete_context(active.context_id) is True
    assert manager.delete_context(active.context_id) is False