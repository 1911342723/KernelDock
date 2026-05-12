"""
数据加载模块 — multi-table-analysis 版本 (design §5.3)

新契约：
- backend 侧已把每张 LogicalTable 序列化为 ``<data_dir>/<TableRef>.parquet``；
- execute 请求里会带 ``bootstrap_source``（backend 渲染的自包含 Python 源码），
  沙箱在用户代码之前 exec 该源码，负责读 parquet、注入全局、绑定 ``df``。
- 本模块仅提供 ``load_parquet_tables`` 作为可复用 helper，以及
  ``get_default_dataframe`` 用于 kernel 初始化时的兜底；旧的
  filename-indexed 扫描 (``load_data_files`` / ``generate_variable_name``)
  已删除。
"""

import os
from typing import Any, Dict, Iterable, Optional

# 最近一次 ``load_parquet_tables`` 的结果（供 kernel 调试 / ``df`` 回填使用）
_loaded_tables: Dict[str, Any] = {}


def get_loaded_tables() -> Dict[str, Any]:
    """获取最近一次加载的表格字典（按 TableRef 索引）。"""
    return dict(_loaded_tables)


def load_parquet_tables(
    data_dir: str,
    table_refs: Iterable[str],
    focus_ref: Optional[str] = None,
    globals_dict: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """按 TableRef 读取 ``<data_dir>/<ref>.parquet`` 并注入全局。

    Args:
        data_dir: parquet 所在目录（通常 ``/data``）。
        table_refs: 已由 backend ``WorkbookParser`` 固化的 TableRef 列表。
        focus_ref: 焦点 ref。决定 ``df`` 绑定：
            ``focus_ref`` in refs → ``df = _loaded_tables[focus_ref]``；
            否则 → ``df = _loaded_tables[min(refs)]``；
            refs 为空 → ``df = pd.DataFrame()``。
        globals_dict: 要注入的全局命名空间；每个 ref 以 Python identifier
            形式绑定到该 dict。

    Returns:
        按 TableRef 索引的 DataFrame 字典（也就是 ``_loaded_tables`` 的副本）。

    Raises:
        FileNotFoundError: 任一 ref 对应的 parquet 文件缺失。
    """
    import pandas as pd

    global _loaded_tables
    loaded: Dict[str, Any] = {}
    refs = list(table_refs)

    for ref in refs:
        path = os.path.join(data_dir, f"{ref}.parquet")
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"load_parquet_tables: missing parquet for table_ref={ref!r} at {path}"
            )
        df = pd.read_parquet(path, engine="pyarrow")
        loaded[ref] = df
        if globals_dict is not None:
            globals_dict[ref] = df

    # 绑定 df
    if focus_ref and focus_ref in loaded:
        focal = loaded[focus_ref]
    elif refs:
        focal = loaded[min(refs)]
    else:
        focal = pd.DataFrame()

    if globals_dict is not None:
        globals_dict["df"] = focal
        globals_dict["_loaded_tables"] = loaded
        globals_dict["TABLE_REFS"] = refs
        globals_dict["FOCUS_REF"] = focus_ref

    _loaded_tables = loaded
    return loaded


def get_default_dataframe():
    """获取默认 DataFrame（最近一次加载的第一张表；没有则返回空 DataFrame）。

    留作 kernel 初始化 / 老代码回退使用。新管线优先用 bootstrap_source 的
    焦点决议结果。
    """
    try:
        import pandas as pd

        if _loaded_tables:
            return next(iter(_loaded_tables.values()))
        return pd.DataFrame()
    except ImportError:
        return None
