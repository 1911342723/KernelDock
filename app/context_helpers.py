"""
代码上下文（CodeContext）解析与 bootstrap 渲染辅助

会话路由与执行路由共用的上下文工具函数。
"""

import json
from typing import Optional

from fastapi import HTTPException

from .models.code_context import CodeContext
from .schemas import ContextResponse
from .services.context_manager import ContextManager


def _require_context_manager() -> ContextManager:
    from . import runtime

    if runtime.context_manager is None:
        raise HTTPException(status_code=503, detail="上下文管理器未初始化")
    return runtime.context_manager


def _serialize_context(context: CodeContext) -> ContextResponse:
    return ContextResponse(
        context_id=context.context_id,
        session_id=context.session_id,
        language=context.language,
        created_at=context.created_at,
        last_used_at=context.last_used_at,
        parent_context_id=context.parent_context_id,
    )


def _ensure_default_context(session_id: str) -> CodeContext:
    context_manager = _require_context_manager()
    existing = context_manager.list_contexts(session_id)
    if existing:
        return existing[0]
    return context_manager.create_context(session_id=session_id)


def _resolve_execute_context_id(session_id: str, requested_context_id: Optional[str]) -> str:
    context_manager = _require_context_manager()
    if requested_context_id:
        context = context_manager.get_context(requested_context_id)
        if context is None or context.session_id != session_id:
            raise HTTPException(status_code=404, detail="Context not found")
        context.touch()
        return context.context_id
    return _ensure_default_context(session_id).context_id


def _render_context_bootstrap(
    context: CodeContext,
    *,
    data_dir: str,
    output_dir: str,
) -> Optional[str]:
    if not context.data_refs:
        return None
    refs_json = json.dumps(list(context.data_refs), ensure_ascii=False)
    focus_json = json.dumps(context.focus_ref, ensure_ascii=False)
    return f"""
import json as _ctx_json
import pandas as pd

DATA_DIR = r'{data_dir}'
OUTPUT_DIR = r'{output_dir}'
TABLE_REFS = _ctx_json.loads({refs_json!r})
FOCUS_REF = _ctx_json.loads({focus_json!r})
_loaded_tables = {{}}

for _ref in TABLE_REFS:
    _loaded_tables[_ref] = pd.read_parquet(f"{{DATA_DIR}}/{{_ref}}.parquet", engine="pyarrow")
    globals()[_ref] = _loaded_tables[_ref]

if FOCUS_REF and FOCUS_REF in _loaded_tables:
    df = _loaded_tables[FOCUS_REF]
elif TABLE_REFS:
    df = _loaded_tables[min(TABLE_REFS)]
else:
    df = pd.DataFrame()
""".strip()
