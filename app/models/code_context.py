from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CodeContext:
    context_id: str
    session_id: str
    language: str = "python"
    created_at: datetime = field(default_factory=datetime.now)
    last_used_at: datetime = field(default_factory=datetime.now)
    data_refs: tuple[str, ...] = field(default_factory=tuple)
    focus_ref: str | None = None

    def touch(self) -> None:
        self.last_used_at = datetime.now()

    def is_idle(self, now: datetime, timeout_seconds: int) -> bool:
        return (now - self.last_used_at).total_seconds() > timeout_seconds
