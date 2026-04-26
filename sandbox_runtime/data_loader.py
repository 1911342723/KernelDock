"""
数据加载模块

自动加载数据目录中的 CSV/Excel 文件：
- 支持多种编码格式
- 自动生成变量名
- 提供加载后的表格信息查询
"""

import os
import re
from typing import Dict, List, Optional, Any

# 已加载的表格
_loaded_tables: Dict[str, Any] = {}


def get_loaded_tables() -> Dict[str, Any]:
    """获取已加载的表格字典"""
    return _loaded_tables.copy()


def generate_variable_name(filename: str) -> str:
    """
    根据文件名生成有效的 Python 变量名
    
    Args:
        filename: 文件名
        
    Returns:
        有效的 Python 变量名
    """
    # 移除扩展名
    name_without_ext = filename.rsplit('.', 1)[0] if '.' in filename else filename
    
    # 替换非法字符
    var_name = re.sub(r'[^a-zA-Z0-9_]', '_', name_without_ext)
    var_name = re.sub(r'_+', '_', var_name).strip('_')
    
    # 处理空名称
    if not var_name:
        var_name = 'df_data'
    
    # 处理数字开头
    if var_name[0].isdigit():
        var_name = 'df_' + var_name
    
    return var_name


def _load_single_file(file_path: str, file_name: str):
    """
    加载单个数据文件
    
    Args:
        file_path: 文件完整路径
        file_name: 文件名
        
    Returns:
        pandas DataFrame 或 None
    """
    try:
        import pandas as pd
        
        if file_name.endswith('.csv'):
            # 尝试不同编码
            for encoding in ['utf-8', 'gbk', 'gb2312', 'latin1']:
                try:
                    return pd.read_csv(file_path, encoding=encoding)
                except (UnicodeDecodeError, UnicodeError):
                    continue
            # 最后使用 errors='ignore'
            return pd.read_csv(file_path, encoding='utf-8', errors='ignore')
        
        elif file_name.endswith(('.xlsx', '.xls')):
            return pd.read_excel(file_path)
        
        else:
            return None
            
    except Exception as e:
        print(f"[Warning] 加载文件 {file_name} 失败: {e}")
        return None


def load_data_files(
    data_dir: Optional[str] = None,
    globals_dict: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    加载数据目录中的所有数据文件
    
    自动扫描数据目录，加载 CSV 和 Excel 文件，
    并将它们注入到指定的全局命名空间。
    
    Args:
        data_dir: 数据目录路径（可选，默认使用配置）
        globals_dict: 全局变量字典（用于注入变量）
        
    Returns:
        加载的表格字典 {文件名: DataFrame}
    """
    global _loaded_tables
    
    try:
        import pandas as pd
        from .setup import get_data_dir
        
        data_dir = data_dir or get_data_dir()
        
        # 检查目录是否存在
        if not os.path.exists(data_dir):
            print(f"[Info] 数据目录不存在: {data_dir}")
            return {}
        
        _loaded_tables = {}
        
        # 扫描数据文件
        data_files = sorted([
            f for f in os.listdir(data_dir)
            if f.endswith(('.csv', '.xlsx', '.xls'))
        ])
        
        if not data_files:
            print("[Info] 未找到数据文件")
            return {}
        
        print(f"[OK] Found {len(data_files)} data file(s)")
        
        # 加载文件
        for idx, file_name in enumerate(data_files):
            file_path = os.path.join(data_dir, file_name)
            df = _load_single_file(file_path, file_name)
            
            if df is not None:
                var_name = generate_variable_name(file_name)
                _loaded_tables[file_name] = df
                
                # 注入到全局命名空间
                if globals_dict is not None:
                    globals_dict[var_name] = df
                
                print(f"  [{idx + 1}] {file_name} -> {var_name}")
        
        return _loaded_tables
        
    except ImportError:
        print("[Error] pandas 未安装，无法加载数据文件")
        return {}
    except Exception as e:
        print(f"[Error] 加载数据文件失败: {e}")
        return {}


def get_default_dataframe():
    """
    获取默认的 DataFrame
    
    如果有加载的表格，返回第一个；否则返回空 DataFrame。
    
    Returns:
        pandas DataFrame
    """
    try:
        import pandas as pd
        
        if _loaded_tables:
            return list(_loaded_tables.values())[0]
        else:
            return pd.DataFrame()
            
    except ImportError:
        return None
