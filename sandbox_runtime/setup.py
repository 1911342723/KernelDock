"""
沙箱环境初始化模块

负责配置 Python 运行环境，包括：
- 标准输出/错误编码设置
- Matplotlib 后端和字体配置
- 数据目录和输出目录配置
- 预加载常用数据分析库
"""

import io
import os
import sys
import warnings
from typing import Optional

# 环境配置
_data_dir: str = os.environ.get('DATA_DIR', '/data')
_output_dir: str = os.environ.get('OUTPUT_DIR', '/output')
_initialized: bool = False
_selected_font: Optional[str] = None


def get_data_dir() -> str:
    """获取数据目录路径"""
    return _data_dir


def get_output_dir() -> str:
    """获取输出目录路径"""
    return _output_dir


def get_font_info() -> dict:
    """获取当前 matplotlib 字体配置信息"""
    return {
        'selected_font': _selected_font,
        'font_sans_serif': os.environ.get('MPL_FONT_SANS_SERIF', ''),
    }


def setup(
    data_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
    suppress_warnings: bool = True,
) -> None:
    """
    初始化沙箱运行环境
    
    Args:
        data_dir: 数据目录路径（可选，默认使用环境变量）
        output_dir: 输出目录路径（可选，默认使用环境变量）
        suppress_warnings: 是否抑制警告（默认 True）
    """
    global _data_dir, _output_dir, _initialized
    
    if _initialized:
        return
    
    # 更新目录配置
    if data_dir:
        _data_dir = data_dir
    if output_dir:
        _output_dir = output_dir
    
    # 设置标准输出编码
    _setup_encoding()
    
    # 抑制警告
    if suppress_warnings:
        warnings.filterwarnings('ignore')
    
    # 配置 matplotlib
    _setup_matplotlib()
    
    # 配置 seaborn
    _setup_seaborn()
    
    _initialized = True
    print("[OK] Sandbox runtime initialized")


def _setup_encoding() -> None:
    """配置 UTF-8 编码"""
    try:
        if hasattr(sys.stdout, 'buffer'):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding='utf-8', errors='replace'
            )
        if hasattr(sys.stderr, 'buffer'):
            sys.stderr = io.TextIOWrapper(
                sys.stderr.buffer, encoding='utf-8', errors='replace'
            )
    except Exception:
        pass  # 在某些环境中可能失败，忽略


def _setup_matplotlib() -> None:
    """配置 Matplotlib"""
    global _selected_font
    try:
        import matplotlib
        matplotlib.use('Agg')
        
        import matplotlib.pyplot as plt
        import matplotlib.font_manager as fm
        
        # 重建字体缓存
        fm._load_fontmanager(try_read_cache=False)
        
        # 手动添加字体目录（Debian/Ubuntu 字体路径）
        font_dirs = [
            '/usr/share/fonts/truetype/wqy',
            '/usr/share/fonts/opentype/noto',
            '/usr/share/fonts/truetype/noto',
            '/usr/share/fonts/truetype/dejavu',
            '/usr/share/fonts/truetype/liberation',
            '/usr/share/fonts/opentype',
            '/usr/share/fonts/truetype',
        ]
        for font_dir in font_dirs:
            if os.path.exists(font_dir):
                for root, dirs, files in os.walk(font_dir):
                    for font_file in files:
                        if font_file.endswith(('.ttf', '.otf', '.ttc')):
                            font_path = os.path.join(root, font_file)
                            try:
                                fm.fontManager.addfont(font_path)
                            except Exception:
                                pass
        
        # 查找可用的中文字体（科研规范优先）
        available_fonts = set(f.name for f in fm.fontManager.ttflist)
        print(f"[Font] Available CJK fonts: {[f for f in available_fonts if 'CJK' in f or 'Hei' in f or 'Song' in f or 'Noto' in f][:10]}")
        
        chinese_fonts = [
            'Noto Sans CJK SC',     # Debian 安装的 Noto CJK
            'Noto Sans CJK JP',
            'Noto Sans CJK TC',
            'WenQuanYi Micro Hei',  # 文泉驿微米黑
            'WenQuanYi Zen Hei',
            'SimHei',               # 科研图表标准字体
            'Microsoft YaHei',
            'Noto Serif CJK SC',
            'PingFang SC',
        ]
        selected_font = next(
            (f for f in chinese_fonts if f in available_fonts),
            'DejaVu Sans'
        )
        _selected_font = selected_font
        os.environ['MPL_FONT_SANS_SERIF'] = ','.join([selected_font, 'DejaVu Sans', 'Arial'])
        print(f"[Font] Selected: {selected_font}")
        
        # 应用字体配置
        plt.rcParams['font.sans-serif'] = [selected_font, 'DejaVu Sans', 'Arial']
        plt.rcParams['font.family'] = 'sans-serif'
        plt.rcParams['axes.unicode_minus'] = False
        plt.rcParams['figure.dpi'] = 150
        plt.rcParams['savefig.dpi'] = 300
        plt.rcParams['figure.facecolor'] = 'white'
        plt.rcParams['savefig.facecolor'] = 'white'
        
        # 注册图表捕获钩子
        from . import charts
        charts._register_capture_hooks()
        
    except ImportError:
        pass  # matplotlib 未安装


def _setup_seaborn() -> None:
    """配置 Seaborn"""
    try:
        import seaborn as sns
        
        # 科学配色方案
        scientific_colors = [
            '#2E86AB', '#A23B72', '#F18F01', '#C73E1D',
            '#3B1F2B', '#95C623', '#7768AE', '#E84855'
        ]
        sns.set_palette(scientific_colors)
        
    except ImportError:
        pass  # seaborn 未安装
