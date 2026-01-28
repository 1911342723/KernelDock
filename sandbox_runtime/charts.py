"""
图表捕获模块

使用更可靠的机制捕获 matplotlib 图表：
- 注册 plt.show() 和 plt.savefig() 钩子
- 自动保存图表到输出目录
- 生成 SVG base64 用于前端显示
"""

import base64
import io
import os
from typing import List, Optional, Dict, Any, Set

# 捕获的图表列表
_captured_charts: List[Dict[str, Any]] = []
_captured_figure_ids: Set[int] = set()  # 已捕获的 figure id，防止重复
_hooks_registered: bool = False


def get_captured_charts() -> List[Dict[str, Any]]:
    """获取已捕获的图表列表"""
    return _captured_charts.copy()


def clear_captured_charts() -> None:
    """清空已捕获的图表"""
    global _captured_charts, _captured_figure_ids
    _captured_charts = []
    _captured_figure_ids = set()


def save_figure(
    fig,
    name: str = "chart",
    format: str = "svg",
    output_dir: Optional[str] = None,
) -> str:
    """
    保存图表并捕获
    
    Args:
        fig: matplotlib Figure 对象
        name: 图表名称（不含扩展名）
        format: 图表格式（svg、png、pdf）
        output_dir: 输出目录（可选）
        
    Returns:
        保存的文件路径
    """
    from .setup import get_output_dir
    
    output_dir = output_dir or get_output_dir()
    file_path = os.path.join(output_dir, f"{name}.{format}")
    
    # 保存到文件
    fig.savefig(file_path, format=format, bbox_inches='tight', facecolor='white')
    
    # 生成 base64（仅 SVG 格式）
    if format == 'svg':
        _capture_figure_to_base64(fig, file_path)
    
    print(f"CHART_SAVED: {file_path}")
    return file_path


def capture_current_figures() -> List[Dict[str, Any]]:
    """
    捕获当前所有未关闭的 matplotlib 图表
    
    Returns:
        捕获的图表数据列表
    """
    global _captured_figure_ids
    
    try:
        import matplotlib.pyplot as plt
        from .setup import get_output_dir
        
        output_dir = get_output_dir()
        captured = []
        
        for fig_num in plt.get_fignums():
            # 跳过已捕获的图表
            if fig_num in _captured_figure_ids:
                continue
                
            fig = plt.figure(fig_num)
            _captured_figure_ids.add(fig_num)
            
            # 保存 SVG 文件
            chart_idx = len(_captured_charts) + len(captured) + 1
            svg_path = os.path.join(output_dir, f"chart_{chart_idx}.svg")
            fig.savefig(svg_path, format='svg', bbox_inches='tight', facecolor='white')
            
            # 生成 base64
            buf = io.BytesIO()
            fig.savefig(buf, format='svg', bbox_inches='tight', facecolor='white')
            buf.seek(0)
            svg_b64 = base64.b64encode(buf.read()).decode('utf-8')
            
            chart_data = {
                "path": svg_path,
                "base64": svg_b64,
                "format": "svg"
            }
            captured.append(chart_data)
            
            # 输出标记供解析
            print(f"SVG_BASE64_START:{svg_b64}:SVG_BASE64_END")
            print(f"CHART_SAVED: {svg_path}")
        
        _captured_charts.extend(captured)
        return captured
        
    except ImportError:
        return []


def _capture_figure_to_base64(fig, file_path: str) -> None:
    """将图表捕获为 base64 并添加到列表"""
    global _captured_charts, _captured_figure_ids
    
    # 检查是否已捕获（通过 figure number）
    fig_num = fig.number
    if fig_num in _captured_figure_ids:
        return  # 已捕获，跳过
    
    _captured_figure_ids.add(fig_num)
    
    buf = io.BytesIO()
    fig.savefig(buf, format='svg', bbox_inches='tight', facecolor='white')
    buf.seek(0)
    svg_b64 = base64.b64encode(buf.read()).decode('utf-8')
    
    chart_data = {
        "path": file_path,
        "base64": svg_b64,
        "format": "svg"
    }
    _captured_charts.append(chart_data)
    
    # 输出标记供解析
    print(f"SVG_BASE64_START:{svg_b64}:SVG_BASE64_END")


def _register_capture_hooks() -> None:
    """
    注册图表捕获钩子
    
    拦截 plt.show() 和 plt.savefig() 调用，自动捕获图表。
    """
    global _hooks_registered
    
    if _hooks_registered:
        return
    
    try:
        import matplotlib.pyplot as plt
        from .setup import get_output_dir
        
        # 保存原始函数
        _original_show = plt.show
        _original_savefig = plt.savefig
        
        def _wrapped_show(*args, **kwargs):
            """包装的 show 函数，自动捕获图表"""
            capture_current_figures()
            # 不调用原始 show（Agg 后端不需要）
        
        def _wrapped_savefig(*args, **kwargs):
            """包装的 savefig 函数，额外捕获 base64"""
            result = _original_savefig(*args, **kwargs)
            
            # 检查是否需要捕获
            if plt.get_fignums():
                fig = plt.gcf()
                
                # 获取保存路径
                if args:
                    file_path = args[0]
                else:
                    file_path = kwargs.get('fname', '')
                
                # 如果不是 SVG 格式，额外生成 SVG
                if not str(file_path).endswith('.svg'):
                    output_dir = get_output_dir()
                    chart_idx = len(_captured_charts) + 1
                    svg_path = os.path.join(output_dir, f"chart_{chart_idx}.svg")
                    fig.savefig(svg_path, format='svg', bbox_inches='tight', facecolor='white')
                    file_path = svg_path
                
                _capture_figure_to_base64(fig, str(file_path))
                print(f"CHART_SAVED: {file_path}")
            
            return result
        
        # 替换函数
        plt.show = _wrapped_show
        plt.savefig = _wrapped_savefig
        
        _hooks_registered = True
        
    except ImportError:
        pass
