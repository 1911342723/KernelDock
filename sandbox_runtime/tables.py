"""
表格输出模块

提供 DataFrame 表格数据的捕获和保存功能：
- display_table(): 在输出中捕获表格数据
- save_table(): 保存表格到文件并捕获
"""

import io
import json
import os
from typing import Any, Dict, List, Optional

# 捕获的表格列表
_captured_tables: List[Dict[str, Any]] = []


def get_captured_tables() -> List[Dict[str, Any]]:
    """获取已捕获的表格列表"""
    return _captured_tables.copy()


def clear_captured_tables() -> None:
    """清空已捕获的表格"""
    global _captured_tables
    _captured_tables = []


def display_table(
    df,
    name: str = "数据表",
    max_rows: int = 100,
    description: Optional[str] = None,
) -> None:
    """
    显示并捕获 DataFrame 表格
    
    将 DataFrame 转换为 JSON 格式输出，供前端解析显示。
    
    Args:
        df: pandas DataFrame 对象
        name: 表格名称
        max_rows: 最大显示行数
        description: 表格描述
    """
    global _captured_tables
    
    try:
        import pandas as pd
        
        if not isinstance(df, pd.DataFrame):
            print(f"[Warning] display_table: 参数不是 DataFrame 类型")
            return
        
        # 截取显示行数
        display_df = df.head(max_rows)
        
        # 构建表格数据
        table_id = f"table_{len(_captured_tables) + 1}"
        table_data = {
            "id": table_id,
            "name": name,
            "columns": list(df.columns),
            "data": display_df.to_dict(orient='records'),
            "totalRows": len(df),
            "displayedRows": len(display_df),
            "dtypes": {col: str(dtype) for col, dtype in df.dtypes.items()},
            "description": description,
        }
        
        # 生成 CSV 数据
        csv_buffer = io.StringIO()
        df.to_csv(csv_buffer, index=False, encoding='utf-8')
        table_data["csvData"] = csv_buffer.getvalue()
        
        # 添加到捕获列表
        _captured_tables.append(table_data)
        
        # 输出标记供解析
        json_str = json.dumps(table_data, ensure_ascii=False, default=str)
        print(f"TABLE_DATA_START:{json_str}:TABLE_DATA_END")
        
    except ImportError:
        print("[Error] pandas 未安装，无法使用 display_table")
    except Exception as e:
        print(f"[Error] display_table 失败: {e}")


def save_table(
    df,
    filename: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> Optional[str]:
    """
    保存 DataFrame 到文件并捕获
    
    Args:
        df: pandas DataFrame 对象
        filename: 文件名（支持 .csv、.xlsx）
        name: 表格名称（可选，默认使用文件名）
        description: 表格描述
        output_dir: 输出目录（可选）
        
    Returns:
        保存的文件路径，失败返回 None
    """
    try:
        import pandas as pd
        from .setup import get_output_dir
        
        if not isinstance(df, pd.DataFrame):
            print(f"[Warning] save_table: 参数不是 DataFrame 类型")
            return None
        
        # 确保文件扩展名
        if not filename.endswith(('.csv', '.xlsx', '.xls')):
            filename += '.csv'
        
        # 确定输出路径
        output_dir = output_dir or get_output_dir()
        file_path = os.path.join(output_dir, filename)
        
        # 保存文件
        if filename.endswith('.csv'):
            df.to_csv(file_path, index=False, encoding='utf-8-sig')
        else:
            df.to_excel(file_path, index=False)
        
        print(f"TABLE_SAVED: {file_path}")
        
        # 显示表格
        display_table(df, name=name or filename, description=description)
        
        return file_path
        
    except ImportError:
        print("[Error] pandas 未安装，无法使用 save_table")
        return None
    except Exception as e:
        print(f"[Error] save_table 失败: {e}")
        return None
