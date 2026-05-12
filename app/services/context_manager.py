from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Dict, List
from uuid import uuid4

from ..models import CodeContext


class ContextManager:
    def __init__(self) -> None:
        self._contexts: Dict[str, CodeContext] = {}
        self._session_to_contexts: Dict[str, List[str]] = defaultdict(list)

    def create_context(
        self,
        session_id: str,
        fork_from: str | None = None,
        language: str = "python",
    ) -> CodeContext:
        parent = self.get_context(fork_from) if fork_from else None
        context = CodeContext(
            context_id=f"ctx_{uuid4().hex[:12]}",
            session_id=session_id,
            language=language,
            data_refs=parent.data_refs if parent else tuple(),
            focus_ref=parent.focus_ref if parent else None,
        )
        self._contexts[context.context_id] = context
        self._session_to_contexts[session_id].append(context.context_id)
        return context

    def get_context(self, context_id: str | None) -> CodeContext | None:
        if not context_id:
            return None
        return self._contexts.get(context_id)

    def list_contexts(self, session_id: str) -> list[CodeContext]:
        context_ids = self._session_to_contexts.get(session_id, [])
        return [self._contexts[context_id] for context_id in context_ids if context_id in self._contexts]

    def delete_context(self, context_id: str) -> bool:
        context = self._contexts.pop(context_id, None)
        if context is None:
            return False

        remaining = [ctx_id for ctx_id in self._session_to_contexts[context.session_id] if ctx_id != context_id]
        if remaining:
            self._session_to_contexts[context.session_id] = remaining
        else:
            self._session_to_contexts.pop(context.session_id, None)
        return True

    def cleanup_idle(self, timeout: int = 1800) -> list[str]:
        now = datetime.now()
        deleted_context_ids = []
        for context in list(self._contexts.values()):
            if context.is_idle(now, timeout):
                deleted_context_ids.append(context.context_id)
                self.delete_context(context.context_id)
        return deleted_context_ids
