# Sandbox Runtime Library
# 预装在沙箱容器中的 Python 运行时库

"""
Sandbox Runtime - 代码执行沙箱的预置运行时库

包含：
- 数据加载和处理工具
- 图表捕获钩子（无需 Monkey Patching）
- 表格输出函数
- 中文字体和样式配置

使用方式：
    from sandbox_runtime import setup
    setup()  # 初始化运行环境
"""

from .setup import setup, get_data_dir, get_output_dir
from .charts import save_figure, capture_current_figures
from .tables import display_table, save_table
from .data_loader import load_parquet_tables, get_loaded_tables, get_default_dataframe

__all__ = [
    'setup',
    'get_data_dir',
    'get_output_dir',
    'save_figure',
    'capture_current_figures',
    'display_table',
    'save_table',
    'load_parquet_tables',
    'get_loaded_tables',
    'get_default_dataframe',
]

__version__ = '1.0.0'
