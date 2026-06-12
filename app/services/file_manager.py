"""
文件管理器模块

负责沙箱文件的上传、下载、列表和清理功能。
使用 Docker 卷挂载实现文件在宿主机和容器之间的共享。

"""

import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import settings
from ..exceptions import FileNotFoundError as SandboxFileNotFoundError
from ..exceptions import InternalError, SandboxNotFoundError

logger = logging.getLogger(__name__)


# 支持的数据文件扩展名
SUPPORTED_DATA_EXTENSIONS = {".csv", ".xlsx", ".xls"}

# 所有支持的文件扩展名（包括输出文件）
SUPPORTED_EXTENSIONS = SUPPORTED_DATA_EXTENSIONS | {".svg", ".png", ".jpg", ".jpeg", ".json", ".txt"}


@dataclass
class FileInfo:
    """
    文件信息数据类
    
    包含文件的基本信息。
    
    Attributes:
        filename: 文件名
        filepath: 完整文件路径
        size_bytes: 文件大小（字节）
        created_at: 创建时间
        modified_at: 修改时间
        is_data_file: 是否为数据文件（CSV/Excel）
        variable_name: 自动生成的变量名（仅数据文件）
    """
    filename: str
    filepath: str
    size_bytes: int
    created_at: datetime
    modified_at: datetime
    is_data_file: bool = False
    variable_name: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "filename": self.filename,
            "filepath": self.filepath,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at.isoformat(),
            "modified_at": self.modified_at.isoformat(),
            "is_data_file": self.is_data_file,
            "variable_name": self.variable_name,
        }


@dataclass
class UploadResult:
    """
    文件上传结果数据类
    
    Attributes:
        success: 是否成功
        filename: 文件名
        filepath: 文件路径
        size_bytes: 文件大小
        variable_name: 自动生成的变量名（仅数据文件）
        rows: 数据行数（仅数据文件）
        columns: 数据列数（仅数据文件）
        column_names: 列名列表（仅数据文件）
        error: 错误信息（如果失败）
    """
    success: bool
    filename: str
    filepath: Optional[str] = None
    size_bytes: int = 0
    variable_name: Optional[str] = None
    rows: Optional[int] = None
    columns: Optional[int] = None
    column_names: Optional[List[str]] = None
    error: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        result = {
            "success": self.success,
            "filename": self.filename,
        }
        if self.success:
            result.update({
                "filepath": self.filepath,
                "size_bytes": self.size_bytes,
                "variable_name": self.variable_name,
            })
            if self.rows is not None:
                result["rows"] = self.rows
            if self.columns is not None:
                result["columns"] = self.columns
            if self.column_names is not None:
                result["column_names"] = self.column_names
        else:
            result["error"] = self.error
        return result


def generate_variable_name(filename: str) -> str:
    """
    根据文件名生成有效的 Python 变量名
    
    Requirements 5.3: 自动将数据文件加载为 pandas DataFrame 变量
    
    Args:
        filename: 文件名
        
    Returns:
        有效的 Python 变量名
    """
    # 移除扩展名
    name_without_ext = filename.rsplit('.', 1)[0] if '.' in filename else filename
    
    # 替换非法字符为下划线
    var_name = re.sub(r'[^a-zA-Z0-9_]', '_', name_without_ext)
    
    # 合并连续下划线
    var_name = re.sub(r'_+', '_', var_name).strip('_')
    
    # 确保不为空
    if not var_name:
        var_name = 'df_data'
    
    # 如果以数字开头，添加前缀
    if var_name[0].isdigit():
        var_name = 'df_' + var_name
    
    return var_name


def is_data_file(filename: str) -> bool:
    """
    检查文件是否为数据文件
    
    Requirements 5.2: 支持 CSV、Excel（xlsx、xls）格式的数据文件
    
    Args:
        filename: 文件名
        
    Returns:
        是否为数据文件
    """
    ext = Path(filename).suffix.lower()
    return ext in SUPPORTED_DATA_EXTENSIONS


class FileManager:
    """
    文件管理器
    
    负责沙箱文件的上传、下载、列表和清理功能。
    使用 Docker 卷挂载实现文件在宿主机和容器之间的共享。
    
    
    使用方式:
    ```python
    file_manager = FileManager()
    
    # 上传文件
    result = await file_manager.upload_file(
        sandbox_id="sandbox-123",
        filename="data.csv",
        content=file_bytes
    )
    
    # 下载文件
    content = await file_manager.download_file(
        sandbox_id="sandbox-123",
        filename="data.csv"
    )
    
    # 列出文件
    files = await file_manager.list_files(sandbox_id="sandbox-123")
    
    # 清理文件
    await file_manager.cleanup_sandbox_files(sandbox_id="sandbox-123")
    ```
    """
    
    def __init__(self, workspace_base: Optional[str] = None):
        """
        初始化文件管理器
        
        Args:
            workspace_base: 工作空间基础目录（可选，默认使用 settings）
        """
        self._workspace_base = workspace_base or settings.workspace_base
        logger.info(f"文件管理器初始化，工作空间: {self._workspace_base}")
    
    @classmethod
    def from_settings(cls) -> "FileManager":
        """
        从 settings 配置创建文件管理器
        
        Returns:
            FileManager 实例
        """
        return cls()
    
    def _get_sandbox_dirs(self, sandbox_id: str) -> Tuple[str, str, str]:
        """
        获取沙箱的目录路径
        
        Requirements 5.4: 提供独立的数据目录（/data）和输出目录（/output）
        
        Args:
            sandbox_id: 沙箱 ID
            
        Returns:
            (workspace_dir, data_dir, output_dir) 元组
        """
        workspace_dir = os.path.join(self._workspace_base, sandbox_id)
        data_dir = os.path.join(workspace_dir, "data")
        output_dir = os.path.join(workspace_dir, "output")
        return workspace_dir, data_dir, output_dir
    
    def _ensure_dirs_exist(self, sandbox_id: str) -> Tuple[str, str, str]:
        """
        确保沙箱目录存在
        
        Args:
            sandbox_id: 沙箱 ID
            
        Returns:
            (workspace_dir, data_dir, output_dir) 元组
        """
        workspace_dir, data_dir, output_dir = self._get_sandbox_dirs(sandbox_id)
        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)
        return workspace_dir, data_dir, output_dir
    
    def _validate_filename(self, filename: str) -> str:
        """
        验证并清理文件名
        
        防止路径遍历攻击。
        
        Args:
            filename: 原始文件名
            
        Returns:
            清理后的安全文件名
            
        Raises:
            ValueError: 文件名无效
        """
        # 移除路径分隔符，防止路径遍历
        safe_name = os.path.basename(filename)
        
        # 移除危险字符
        safe_name = re.sub(r'[<>:"|?*]', '_', safe_name)
        
        # 确保不为空
        if not safe_name or safe_name in ('.', '..'):
            raise ValueError(f"无效的文件名: {filename}")
        
        return safe_name
    
    async def upload_file(
        self,
        sandbox_id: str,
        filename: str,
        content: bytes,
        directory: str = "data"
    ) -> UploadResult:
        """
        上传文件到沙箱
        
        
        Args:
            sandbox_id: 沙箱 ID
            filename: 文件名
            content: 文件内容（字节）
            directory: 目标目录（"data" 或 "output"）
            
        Returns:
            UploadResult 对象
        """
        logger.debug(f"上传文件: {filename} -> 沙箱 {sandbox_id}")
        
        try:
            # 验证文件名
            safe_filename = self._validate_filename(filename)
            
            # 确保目录存在
            workspace_dir, data_dir, output_dir = self._ensure_dirs_exist(sandbox_id)
            
            # 确定目标目录
            if directory == "output":
                target_dir = output_dir
            else:
                target_dir = data_dir
            
            # 写入文件
            filepath = os.path.join(target_dir, safe_filename)
            with open(filepath, 'wb') as f:
                f.write(content)
            
            file_size = len(content)
            logger.info(f"文件上传成功: {safe_filename}, 大小: {file_size} 字节")
            
            # 如果是数据文件，尝试解析获取元信息
            if is_data_file(safe_filename):
                variable_name = generate_variable_name(safe_filename)
                
                # 尝试读取数据文件获取行列信息
                try:
                    rows, columns, column_names = await self._get_datafile_info(filepath, safe_filename)
                    return UploadResult(
                        success=True,
                        filename=safe_filename,
                        filepath=filepath,
                        size_bytes=file_size,
                        variable_name=variable_name,
                        rows=rows,
                        columns=columns,
                        column_names=column_names
                    )
                except Exception as e:
                    logger.warning(f"解析数据文件失败: {e}")
                    # 即使解析失败，文件上传仍然成功
                    return UploadResult(
                        success=True,
                        filename=safe_filename,
                        filepath=filepath,
                        size_bytes=file_size,
                        variable_name=variable_name
                    )
            else:
                return UploadResult(
                    success=True,
                    filename=safe_filename,
                    filepath=filepath,
                    size_bytes=file_size
                )
                
        except ValueError as e:
            logger.warning(f"文件名无效: {e}")
            return UploadResult(
                success=False,
                filename=filename,
                error=str(e)
            )
        except Exception as e:
            logger.error(f"文件上传失败: {e}")
            return UploadResult(
                success=False,
                filename=filename,
                error=f"上传失败: {str(e)}"
            )
    
    async def _get_datafile_info(
        self,
        filepath: str,
        filename: str
    ) -> Tuple[int, int, List[str]]:
        """
        获取数据文件的行列信息
        
        Args:
            filepath: 文件路径
            filename: 文件名
            
        Returns:
            (rows, columns, column_names) 元组
        """
        import pandas as pd
        
        ext = Path(filename).suffix.lower()
        
        if ext == '.csv':
            # 尝试多种编码
            for encoding in ['utf-8', 'gbk', 'gb2312', 'latin1']:
                try:
                    df = pd.read_csv(filepath, encoding=encoding)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                df = pd.read_csv(filepath, encoding='utf-8', errors='ignore')
        else:
            # Excel 文件
            df = pd.read_excel(filepath)
        
        return len(df), len(df.columns), list(df.columns)
    
    async def download_file(
        self,
        sandbox_id: str,
        filename: str,
        directory: Optional[str] = None
    ) -> bytes:
        """
        从沙箱下载文件
        
        Requirements 5.5: 请求下载文件时返回指定文件的内容
        
        Args:
            sandbox_id: 沙箱 ID
            filename: 文件名
            directory: 目录（"data" 或 "output"，为空则自动搜索）
            
        Returns:
            文件内容（字节）
            
        Raises:
            SandboxFileNotFoundError: 文件不存在
        """
        logger.debug(f"下载文件: {filename} <- 沙箱 {sandbox_id}")
        
        # 验证文件名
        safe_filename = self._validate_filename(filename)
        
        # 获取目录路径
        workspace_dir, data_dir, output_dir = self._get_sandbox_dirs(sandbox_id)
        
        # 确定文件路径
        filepath = None
        if directory == "data":
            filepath = os.path.join(data_dir, safe_filename)
        elif directory == "output":
            filepath = os.path.join(output_dir, safe_filename)
        else:
            # 自动搜索：先在 data 目录，再在 output 目录
            data_path = os.path.join(data_dir, safe_filename)
            output_path = os.path.join(output_dir, safe_filename)
            
            if os.path.exists(data_path):
                filepath = data_path
            elif os.path.exists(output_path):
                filepath = output_path
        
        # 检查文件是否存在
        if filepath is None or not os.path.exists(filepath):
            raise SandboxFileNotFoundError(
                filename=safe_filename,
                sandbox_id=sandbox_id,
                directory=directory
            )
        
        # 读取文件内容
        with open(filepath, 'rb') as f:
            content = f.read()
        
        logger.info(f"文件下载成功: {safe_filename}, 大小: {len(content)} 字节")
        return content
    
    async def list_files(
        self,
        sandbox_id: str,
        directory: Optional[str] = None,
        data_files_only: bool = False
    ) -> List[FileInfo]:
        """
        列出沙箱中的文件
        
        Args:
            sandbox_id: 沙箱 ID
            directory: 目录（"data" 或 "output"，为空则列出所有）
            data_files_only: 是否只列出数据文件
            
        Returns:
            FileInfo 列表
        """
        logger.debug(f"列出文件: 沙箱 {sandbox_id}, 目录: {directory}")
        
        workspace_dir, data_dir, output_dir = self._get_sandbox_dirs(sandbox_id)
        
        files: List[FileInfo] = []
        
        # 确定要扫描的目录
        dirs_to_scan = []
        if directory == "data":
            dirs_to_scan = [(data_dir, "data")]
        elif directory == "output":
            dirs_to_scan = [(output_dir, "output")]
        else:
            dirs_to_scan = [(data_dir, "data"), (output_dir, "output")]
        
        for dir_path, dir_name in dirs_to_scan:
            if not os.path.exists(dir_path):
                continue
            
            for filename in os.listdir(dir_path):
                filepath = os.path.join(dir_path, filename)
                
                # 跳过目录
                if os.path.isdir(filepath):
                    continue
                
                # 检查是否为数据文件
                is_data = is_data_file(filename)
                
                # 如果只要数据文件，跳过非数据文件
                if data_files_only and not is_data:
                    continue
                
                # 获取文件信息
                stat = os.stat(filepath)
                
                file_info = FileInfo(
                    filename=filename,
                    filepath=filepath,
                    size_bytes=stat.st_size,
                    created_at=datetime.fromtimestamp(stat.st_ctime),
                    modified_at=datetime.fromtimestamp(stat.st_mtime),
                    is_data_file=is_data,
                    variable_name=generate_variable_name(filename) if is_data else None
                )
                files.append(file_info)
        
        logger.debug(f"找到 {len(files)} 个文件")
        return files
    
    async def delete_file(
        self,
        sandbox_id: str,
        filename: str,
        directory: Optional[str] = None
    ) -> bool:
        """
        删除沙箱中的文件
        
        Args:
            sandbox_id: 沙箱 ID
            filename: 文件名
            directory: 目录（"data" 或 "output"，为空则自动搜索）
            
        Returns:
            是否成功删除
            
        Raises:
            SandboxFileNotFoundError: 文件不存在
        """
        logger.debug(f"删除文件: {filename} <- 沙箱 {sandbox_id}")
        
        # 验证文件名
        safe_filename = self._validate_filename(filename)
        
        # 获取目录路径
        workspace_dir, data_dir, output_dir = self._get_sandbox_dirs(sandbox_id)
        
        # 确定文件路径
        filepath = None
        if directory == "data":
            filepath = os.path.join(data_dir, safe_filename)
        elif directory == "output":
            filepath = os.path.join(output_dir, safe_filename)
        else:
            # 自动搜索
            data_path = os.path.join(data_dir, safe_filename)
            output_path = os.path.join(output_dir, safe_filename)
            
            if os.path.exists(data_path):
                filepath = data_path
            elif os.path.exists(output_path):
                filepath = output_path
        
        # 检查文件是否存在
        if filepath is None or not os.path.exists(filepath):
            raise SandboxFileNotFoundError(
                filename=safe_filename,
                sandbox_id=sandbox_id,
                directory=directory
            )
        
        # 删除文件
        os.remove(filepath)
        logger.info(f"文件删除成功: {safe_filename}")
        return True
    
    async def cleanup_sandbox_files(self, sandbox_id: str) -> bool:
        """
        清理沙箱的所有文件
        
        Requirements 5.6: 沙箱销毁时清理所有关联的文件
        
        Args:
            sandbox_id: 沙箱 ID
            
        Returns:
            是否成功清理
        """
        logger.info(f"清理沙箱文件: {sandbox_id}")
        
        workspace_dir, _, _ = self._get_sandbox_dirs(sandbox_id)
        
        if not os.path.exists(workspace_dir):
            logger.debug(f"工作空间目录不存在: {workspace_dir}")
            return True
        
        try:
            shutil.rmtree(workspace_dir)
            logger.info(f"沙箱文件清理完成: {sandbox_id}")
            return True
        except Exception as e:
            logger.error(f"清理沙箱文件失败: {sandbox_id}, 错误: {e}")
            return False
    
    async def get_file_info(
        self,
        sandbox_id: str,
        filename: str,
        directory: Optional[str] = None
    ) -> FileInfo:
        """
        获取文件信息
        
        Args:
            sandbox_id: 沙箱 ID
            filename: 文件名
            directory: 目录（"data" 或 "output"，为空则自动搜索）
            
        Returns:
            FileInfo 对象
            
        Raises:
            SandboxFileNotFoundError: 文件不存在
        """
        # 验证文件名
        safe_filename = self._validate_filename(filename)
        
        # 获取目录路径
        workspace_dir, data_dir, output_dir = self._get_sandbox_dirs(sandbox_id)
        
        # 确定文件路径
        filepath = None
        if directory == "data":
            filepath = os.path.join(data_dir, safe_filename)
        elif directory == "output":
            filepath = os.path.join(output_dir, safe_filename)
        else:
            # 自动搜索
            data_path = os.path.join(data_dir, safe_filename)
            output_path = os.path.join(output_dir, safe_filename)
            
            if os.path.exists(data_path):
                filepath = data_path
            elif os.path.exists(output_path):
                filepath = output_path
        
        # 检查文件是否存在
        if filepath is None or not os.path.exists(filepath):
            raise SandboxFileNotFoundError(
                filename=safe_filename,
                sandbox_id=sandbox_id,
                directory=directory
            )
        
        # 获取文件信息
        stat = os.stat(filepath)
        is_data = is_data_file(safe_filename)
        
        return FileInfo(
            filename=safe_filename,
            filepath=filepath,
            size_bytes=stat.st_size,
            created_at=datetime.fromtimestamp(stat.st_ctime),
            modified_at=datetime.fromtimestamp(stat.st_mtime),
            is_data_file=is_data,
            variable_name=generate_variable_name(safe_filename) if is_data else None
        )
    
    async def copy_file_to_sandbox(
        self,
        sandbox_id: str,
        source_path: str,
        filename: Optional[str] = None,
        directory: str = "data"
    ) -> UploadResult:
        """
        从本地路径复制文件到沙箱
        
        Args:
            sandbox_id: 沙箱 ID
            source_path: 源文件路径
            filename: 目标文件名（可选，默认使用源文件名）
            directory: 目标目录（"data" 或 "output"）
            
        Returns:
            UploadResult 对象
        """
        if not os.path.exists(source_path):
            return UploadResult(
                success=False,
                filename=filename or os.path.basename(source_path),
                error=f"源文件不存在: {source_path}"
            )
        
        # 读取源文件
        with open(source_path, 'rb') as f:
            content = f.read()
        
        # 使用源文件名或指定的文件名
        target_filename = filename or os.path.basename(source_path)
        
        return await self.upload_file(
            sandbox_id=sandbox_id,
            filename=target_filename,
            content=content,
            directory=directory
        )
    
    def get_container_paths(self, sandbox_id: str) -> Dict[str, str]:
        """
        获取容器内的路径映射
        
        Requirements 5.4: 提供独立的数据目录（/data）和输出目录（/output）
        
        Args:
            sandbox_id: 沙箱 ID
            
        Returns:
            包含宿主机路径和容器路径映射的字典
        """
        workspace_dir, data_dir, output_dir = self._get_sandbox_dirs(sandbox_id)
        
        return {
            "host_workspace": workspace_dir,
            "host_data": data_dir,
            "host_output": output_dir,
            "container_data": "/data",
            "container_output": "/output",
        }


# 全局文件管理器实例
file_manager = FileManager()
