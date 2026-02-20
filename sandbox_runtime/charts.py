"""
图表捕获模块

使用更可靠的机制捕获 matplotlib 图表：
- 注册 plt.show()、plt.savefig() 和 plt.close() 钩子
- 生成 SVG base64 通过 stdout 传输给调用方
- 不写磁盘文件，避免 output 目录无限膨胀
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
) -> Optional[str]:
    """
    捕获图表为 base64 并通过 stdout 输出
    
    仅在显式指定 output_dir 时才写磁盘文件。
    
    Args:
        fig: matplotlib Figure 对象
        name: 图表名称（不含扩展名）
        format: 图表格式（svg、png、pdf）
        output_dir: 输出目录（可选，指定时才写文件）
        
    Returns:
        保存的文件路径（如果写了文件），否则 None
    """
    file_path = None
    
    # 仅在显式指定 output_dir 时写磁盘
    if output_dir:
        file_path = os.path.join(output_dir, f"{name}.{format}")
        fig.savefig(file_path, format=format, bbox_inches='tight', facecolor='white')
    
    # 始终生成 base64 通过 stdout 传输
    if format == 'svg':
        _capture_figure_to_base64(fig, file_path)
    
    return file_path


def capture_current_figures() -> List[Dict[str, Any]]:
    """
    捕获当前所有未关闭的 matplotlib 图表
    
    仅生成 base64 通过 stdout 传输，不写磁盘文件，避免 output 目录膨胀。
    
    Returns:
        捕获的图表数据列表
    """
    global _captured_figure_ids
    
    try:
        import matplotlib.pyplot as plt
        
        captured = []
        
        for fig_num in plt.get_fignums():
            # 跳过已捕获的图表
            if fig_num in _captured_figure_ids:
                continue
                
            fig = plt.figure(fig_num)
            _captured_figure_ids.add(fig_num)
            
            # 仅生成内存中的 base64，不写磁盘
            buf = io.BytesIO()
            fig.savefig(buf, format='svg', bbox_inches='tight', facecolor='white')
            buf.seek(0)
            svg_b64 = base64.b64encode(buf.read()).decode('utf-8')
            
            chart_data = {
                "path": None,
                "base64": svg_b64,
                "format": "svg"
            }
            captured.append(chart_data)
            
            # 输出标记供解析
            print(f"SVG_BASE64_START:{svg_b64}:SVG_BASE64_END")
        
        _captured_charts.extend(captured)
        return captured
        
    except ImportError:
        return []


def _capture_figure_to_base64(fig, file_path: Optional[str] = None) -> None:
    """将图表捕获为 base64 并添加到列表，仅通过 stdout 输出"""
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
    
    拦截 plt.show()、plt.savefig() 和 plt.close() 调用，自动捕获图表。
    """
    global _hooks_registered
    
    if _hooks_registered:
        return
    
    try:
        import matplotlib.pyplot as plt
        
        # 保存原始函数
        _original_show = plt.show
        _original_savefig = plt.savefig
        _original_close = plt.close
        
        def _wrapped_show(*args, **kwargs):
            """包装的 show 函数，自动捕获图表"""
            capture_current_figures()
            # 不调用原始 show（Agg 后端不需要）
        
        def _wrapped_savefig(*args, **kwargs):
            """包装的 savefig 函数，重定向相对路径到 OUTPUT_DIR 并捕获 base64"""
            # 拦截文件路径：将相对路径重定向到 OUTPUT_DIR
            # 防止 LLM 生成的代码在只读 rootfs 上写文件导致 OSError
            output_dir = os.environ.get('OUTPUT_DIR', '/output')
            if args:
                fname = args[0]
                if isinstance(fname, str) and not os.path.isabs(fname) and not hasattr(fname, 'write'):
                    # 相对路径 → 重定向到 OUTPUT_DIR
                    redirected = os.path.join(output_dir, os.path.basename(fname))
                    args = (redirected,) + args[1:]
            elif 'fname' in kwargs:
                fname = kwargs['fname']
                if isinstance(fname, str) and not os.path.isabs(fname):
                    kwargs['fname'] = os.path.join(output_dir, os.path.basename(fname))
            
            result = _original_savefig(*args, **kwargs)
            
            # 捕获当前 figure 的 base64（不额外写磁盘）
            if plt.get_fignums():
                fig = plt.gcf()
                _capture_figure_to_base64(fig)
            
            return result
        
        def _wrapped_close(*args, **kwargs):
            """包装的 close 函数，在关闭前捕获未捕获的图表"""
            # 在关闭前先捕获所有未捕获的图表
            capture_current_figures()
            # 调用原始 close
            _original_close(*args, **kwargs)
        
        # 替换函数
        plt.show = _wrapped_show
        plt.savefig = _wrapped_savefig
        plt.close = _wrapped_close
        
        _hooks_registered = True
        
    except ImportError:
        pass
