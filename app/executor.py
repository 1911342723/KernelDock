"""
代码执行器模块 - Docker 沙箱版本

使用 Docker exec 在沙箱容器内执行 Python 代码，替代原有的 subprocess 方式。
保持现有的图表捕获和表格捕获逻辑，实现超时控制和异常处理。

Requirements:
- 4.1: 在 Sandbox_Container 内执行代码并返回结果
- 4.2: 代码执行超过配置的超时时间时终止执行并返回超时错误
- 4.3: 捕获代码执行的标准输出、标准错误和返回值
- 4.4: 自动捕获 matplotlib 生成的图表并转换为 SVG 格式
- 4.5: 支持 display_table 函数捕获 DataFrame 数据
- 4.6: 代码执行产生异常时返回完整的异常堆栈信息
- 4.7: 预加载数据分析常用库（pandas、numpy、matplotlib、seaborn）
"""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from .config import settings
from .exceptions import ExecutionTimeoutError, InternalError, SandboxNotFoundError
from .infrastructure.docker_client import DockerClient, ExecResult

logger = logging.getLogger(__name__)

# 默认配置
DEFAULT_TIMEOUT = 300
MAX_OUTPUT_LENGTH = 100000


class TableSchema(TypedDict):
    """表格模式定义"""
    name: str
    variable_name: str
    columns: List[str]
    dtypes: Dict[str, str]
    row_count: int
    sample_values: Dict[str, List[str]]


class MultiTableContext(TypedDict):
    """多表上下文定义"""
    tables: List[TableSchema]
    table_count: int
    total_rows: int
    common_columns: Dict[str, List[str]]
    suggested_joins: List[Dict]


def generate_variable_name(filename: str) -> str:
    """
    根据文件名生成有效的 Python 变量名
    
    Args:
        filename: 文件名
        
    Returns:
        有效的 Python 变量名
    """
    name_without_ext = filename.rsplit('.', 1)[0] if '.' in filename else filename
    var_name = re.sub(r'[^a-zA-Z0-9_]', '_', name_without_ext)
    var_name = re.sub(r'_+', '_', var_name).strip('_')
    if not var_name:
        var_name = 'df_data'
    if var_name[0].isdigit():
        var_name = 'df_' + var_name
    return var_name


# 数据加载代码模板
# Requirements 4.7: 预加载数据分析常用库（pandas、numpy、matplotlib、seaborn）
# Requirements 4.4: 自动捕获 matplotlib 生成的图表并转换为 SVG 格式
# Requirements 4.5: 支持 display_table 函数捕获 DataFrame 数据
#
# 重构说明：原有 150+ 行的代码模板已迁移到 sandbox_runtime 包中预安装
# 现在只需导入并初始化即可，大幅减少每次执行的代码解析开销
DATA_LOADER_TEMPLATE = '''
# ===== Sandbox Runtime Initialization =====
import os
os.environ['DATA_DIR'] = r'{data_dir}'
os.environ['OUTPUT_DIR'] = r'{output_dir}'

# 初始化沙箱运行时（配置编码、matplotlib、seaborn）
from sandbox_runtime import setup
setup()

# 强制 print 刷新缓冲区
import functools
print = functools.partial(print, flush=True)

# 加载数据文件到全局命名空间
from sandbox_runtime.data_loader import load_data_files, get_default_dataframe
load_data_files(globals_dict=globals())
df = get_default_dataframe()

# 导入常用数据分析库
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns

# 导入便捷函数
from sandbox_runtime.charts import save_figure, capture_current_figures
from sandbox_runtime.tables import display_table, save_table

DATA_DIR = r'{data_dir}'
OUTPUT_DIR = r'{output_dir}'

# ===== User Code Start =====
'''


# 备份代码模板：检查未捕获的图表
# 重构说明：使用 sandbox_runtime 的 capture_current_figures 函数
BACKUP_CHART_CAPTURE_CODE = '''

# ===== 备份：检查未捕获的图表 =====
from sandbox_runtime.charts import capture_current_figures
capture_current_figures()
'''




class CodeExecutor:
    """
    代码执行器类
    
    使用 Docker exec 在沙箱容器内执行 Python 代码。
    
    Requirements:
    - 4.1: 在 Sandbox_Container 内执行代码并返回结果
    - 4.2: 代码执行超过配置的超时时间时终止执行并返回超时错误
    - 4.3: 捕获代码执行的标准输出、标准错误和返回值
    - 4.4: 自动捕获 matplotlib 生成的图表并转换为 SVG 格式
    - 4.5: 支持 display_table 函数捕获 DataFrame 数据
    - 4.6: 代码执行产生异常时返回完整的异常堆栈信息
    """
    
    def __init__(self, docker_client: Optional[DockerClient] = None):
        """
        初始化代码执行器
        
        Args:
            docker_client: Docker 客户端实例（可选，默认创建新实例）
        """
        self._docker_client = docker_client or DockerClient()
        logger.info("代码执行器初始化完成")

    async def _put_file_to_container(
        self,
        container_id: str,
        dest_path: str,
        content: bytes,
        user: Optional[str] = None,
    ) -> None:
        """
        将文件写入容器。

        优先使用 Docker put_archive API（单次传输，无 Base64 开销）；
        如果 put_archive 失败（例如目标路径不可写），回退到分块 echo+base64。
        """
        import tarfile
        import io

        # --- 优先方案：Docker put_archive API ---
        dest_dir = dest_path.rsplit("/", 1)[0] if "/" in dest_path else "/tmp"
        dest_name = dest_path.rsplit("/", 1)[-1] if "/" in dest_path else dest_path

        try:
            tar_stream = io.BytesIO()
            with tarfile.open(fileobj=tar_stream, mode="w") as tar:
                info = tarfile.TarInfo(name=dest_name)
                info.size = len(content)
                info.mode = 0o644
                # 设置为沙箱用户 (UID/GID 1000)
                info.uid = 1000
                info.gid = 1000
                tar.addfile(info, io.BytesIO(content))
            tar_stream.seek(0)

            await self._docker_client.put_archive(
                container_id, dest_dir, tar_stream.read()
            )
            logger.debug(f"put_archive 写入成功: {dest_path} ({len(content)} bytes)")
            return
        except Exception as e:
            logger.debug(f"put_archive 不可用（预期行为：只读 rootfs），使用 echo+base64: {e}")

        # --- 回退方案：分块 echo+base64 ---
        # 24KB raw = 32KB base64, 远低于 ARG_MAX (~128KB)
        raw_chunk_size = 24576

        for i in range(0, len(content), raw_chunk_size):
            chunk = content[i:i + raw_chunk_size]
            b64_chunk = base64.b64encode(chunk).decode('ascii')
            op = '>' if i == 0 else '>>'
            result = await self._docker_client.exec_command(
                container_id,
                f"echo '{b64_chunk}' | base64 -d {op} {dest_path}",
                user=user,
                timeout=15,
            )
            if result.exit_code != 0:
                logger.error(f"写入脚本分块失败: exit={result.exit_code}, stderr={result.stderr}")
                raise InternalError(
                    message=f"脚本文件写入容器失败(chunk {i // raw_chunk_size}): {result.stderr}"
                )

    async def execute_in_container(
        self,
        container_id: str,
        code: str,
        data_dir: str,
        output_dir: str,
        timeout: int = DEFAULT_TIMEOUT,
        user: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        在指定容器内执行代码（Kernel-first 策略）
        
        优先通过 TCP 连接容器内的 Kernel Server 执行代码：
        - DataFrame 等变量常驻内存，多轮对话性能提升 10-50 倍
        - 库只需导入一次，后续执行跳过初始化
        
        如果 Kernel Server 不可达，自动回退到 docker exec 模式。
        
        Requirements:
        - 4.1: 在 Sandbox_Container 内执行代码并返回结果
        - 4.2: 超时控制
        - 4.3: 捕获标准输出、标准错误和返回值
        - 4.4: 自动捕获 matplotlib 图表
        - 4.5: 支持 display_table 函数
        - 4.6: 返回完整的异常堆栈信息
        
        Args:
            container_id: Docker 容器 ID
            code: 要执行的 Python 代码
            data_dir: 数据目录路径（容器内路径）
            output_dir: 输出目录路径（容器内路径）
            timeout: 执行超时时间（秒）
            user: 执行用户（可选）
            
        Returns:
            执行结果字典
        """
        import time
        start_time = time.monotonic()
        
        logger.debug(f"在容器 {container_id[:12]} 中执行代码，超时: {timeout}s")
        
        # --- 优先方案：通过 Kernel Server 执行（变量常驻内存） ---
        try:
            result = await self._execute_via_kernel(container_id, code, timeout)
            if result is not None:
                return result
        except Exception as e:
            logger.debug(f"Kernel 执行不可用，回退到 docker exec: {e}")
        
        # --- 回退方案：docker exec 执行（原有模式） ---
        return await self._execute_via_docker_exec(
            container_id, code, data_dir, output_dir, timeout, user, start_time
        )

    async def _execute_via_kernel(
        self,
        container_id: str,
        code: str,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> Optional[Dict[str, Any]]:
        """
        通过 docker exec 中继连接容器内的 Kernel Server 执行代码。
        
        流程：
        1. 将请求 JSON 写入容器 /tmp/_kreq_xxx.json
        2. docker exec python -m sandbox_runtime.kernel_relay <请求文件>
        3. 中继脚本在容器内连接 localhost:9999（Kernel Server）
        4. 从 stdout 解析 JSON 响应
        
        这种方式兼容 network_mode=none（无需容器网络）。
        
        Returns:
            执行结果字典，如果 kernel 不可达则返回 None
        """
        import json as _json

        # 构建请求
        request = {
            "action": "execute",
            "code": code,
            "timeout": timeout,
        }
        request_bytes = _json.dumps(request, ensure_ascii=False).encode("utf-8")

        # 写入请求文件到容器
        req_id = uuid.uuid4().hex[:8]
        req_path = f"/tmp/_kreq_{req_id}.json"
        try:
            await self._put_file_to_container(container_id, req_path, request_bytes)
        except Exception as e:
            logger.debug(f"Kernel 请求文件写入失败: {e}")
            return None

        # 运行中继脚本
        try:
            result = await self._docker_client.exec_command(
                container_id,
                f"python -m sandbox_runtime.kernel_relay {req_path}",
                timeout=timeout + 10,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Kernel 中继执行超时: {timeout}s")
            return None
        except Exception as e:
            logger.debug(f"Kernel 中继执行失败: {e}")
            return None
        finally:
            # 清理请求文件
            try:
                await self._docker_client.exec_command(
                    container_id, f"rm -f {req_path}", timeout=5
                )
            except Exception:
                pass

        # exit_code=2 表示 Kernel Server 不可达，触发回退
        if result.exit_code == 2:
            logger.debug("Kernel Server 不可达，回退到 docker exec")
            return None

        # 解析响应
        if result.stdout:
            try:
                response = _json.loads(result.stdout)
                logger.debug(
                    f"Kernel 执行完成: success={response.get('success')}, "
                    f"耗时={response.get('execution_time_ms', 0)}ms"
                )
                return response
            except _json.JSONDecodeError:
                logger.warning(f"Kernel 响应解析失败: {result.stdout[:200]}")

        return None

    async def _execute_via_docker_exec(
        self,
        container_id: str,
        code: str,
        data_dir: str,
        output_dir: str,
        timeout: int,
        user: Optional[str],
        start_time: float,
    ) -> Dict[str, Any]:
        """
        通过 docker exec 执行代码（原有模式，作为 Kernel 的回退方案）
        """
        import time
        
        try:
            # 构建完整的执行代码
            full_code = DATA_LOADER_TEMPLATE.format(
                data_dir=data_dir.replace('\\', '\\\\'),
                output_dir=output_dir.replace('\\', '\\\\')
            ) + code + BACKUP_CHART_CAPTURE_CODE
            
            # 生成临时脚本文件名
            script_name = f"exec_{uuid.uuid4().hex[:8]}.py"
            script_path = f"/tmp/{script_name}"
            
            # 将代码写入容器内的临时文件
            await self._put_file_to_container(
                container_id, script_path, full_code.encode('utf-8'), user=user
            )

            # 验证文件已写入
            verify_result = await self._docker_client.exec_command(
                container_id,
                f"test -f {script_path} && echo OK || echo MISSING",
                user=user,
                timeout=5
            )
            verify_out = (verify_result.stdout or "").strip()
            if "OK" not in verify_out:
                raise InternalError(
                    message=f"脚本文件写入容器失败: {script_path}"
                )
            
            # 设置环境变量
            env = {
                "MPLBACKEND": "Agg",
                "QT_QPA_PLATFORM": "offscreen",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            }
            
            # 执行代码
            try:
                result = await self._docker_client.exec_command(
                    container_id,
                    f"python {script_path}",
                    user=user,
                    environment=env,
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - start_time
                logger.warning(f"代码执行超时: {timeout}s")
                return {
                    "success": False,
                    "stdout": "",
                    "stderr": "",
                    "output": f"[Timeout]: 执行超过 {timeout} 秒限制",
                    "charts": [],
                    "tables": [],
                    "images": [],
                    "error": f"执行超时（{timeout}秒）",
                    "execution_time_ms": int(elapsed * 1000)
                }
            
            # 清理临时脚本文件
            try:
                await self._docker_client.exec_command(
                    container_id,
                    f"rm -f {script_path}",
                    user=user,
                    timeout=10
                )
            except Exception:
                pass
            
            # 解析执行结果
            return self._parse_execution_result(
                result,
                output_dir,
                start_time
            )
            
        except InternalError as e:
            elapsed = time.monotonic() - start_time
            logger.error(f"Docker 执行错误: {e}")
            return {
                "success": False,
                "stdout": "",
                "stderr": str(e),
                "output": f"[Error]: {str(e)}",
                "charts": [],
                "tables": [],
                "images": [],
                "error": str(e),
                "execution_time_ms": int(elapsed * 1000)
            }
        except Exception as e:
            import traceback
            elapsed = time.monotonic() - start_time
            tb_str = traceback.format_exc()
            logger.error(f"代码执行失败: {e}\n{tb_str}")
            
            return {
                "success": False,
                "stdout": "",
                "stderr": tb_str,
                "output": f"[Error]: {str(e)}",
                "charts": [],
                "tables": [],
                "images": [],
                "error": str(e),
                "execution_time_ms": int(elapsed * 1000)
            }

    
    def _parse_execution_result(
        self,
        result: ExecResult,
        output_dir: str,
        start_time: float
    ) -> Dict[str, Any]:
        """
        解析执行结果
        
        从执行输出中提取图表、表格等数据。
        
        Requirements:
        - 4.3: 捕获标准输出、标准错误和返回值
        - 4.4: 自动捕获 matplotlib 图表
        - 4.5: 支持 display_table 函数
        - 4.6: 返回完整的异常堆栈信息
        
        Args:
            result: Docker exec 执行结果
            output_dir: 输出目录路径
            start_time: 开始时间
            
        Returns:
            解析后的执行结果字典
        """
        import time
        elapsed = time.monotonic() - start_time
        
        stdout_text = result.stdout
        stderr_text = result.stderr
        
        # Requirements 4.4: 提取 SVG base64 图表
        charts = []
        svg_pattern = re.compile(r'SVG_BASE64_START:(.+?):SVG_BASE64_END', re.DOTALL)
        for match in svg_pattern.finditer(stdout_text):
            svg_b64 = match.group(1).strip()
            if svg_b64:
                charts.append({
                    "path": None,
                    "base64": svg_b64,
                    "format": "svg"
                })
        
        # 清理输出中的 SVG base64 标记
        clean_stdout = svg_pattern.sub('[图表已生成]', stdout_text)
        
        # Requirements 4.5: 提取表格数据
        tables = []
        table_pattern = re.compile(r'TABLE_DATA_START:(.+?):TABLE_DATA_END', re.DOTALL)
        for match in table_pattern.finditer(stdout_text):
            try:
                table_data = json.loads(match.group(1).strip())
                tables.append(table_data)
            except json.JSONDecodeError:
                pass
        
        # 清理输出中的表格数据标记
        clean_stdout = table_pattern.sub('[表格数据已捕获]', clean_stdout)
        
        # 构建输出
        output = clean_stdout
        if stderr_text:
            output += f"\n[stderr]:\n{stderr_text}"
        
        # 截断过长的输出
        if len(output) > MAX_OUTPUT_LENGTH:
            output = output[:MAX_OUTPUT_LENGTH] + "\n... (输出已截断)"
        
        # 判断是否成功
        # Requirements 4.6: 返回完整的异常堆栈信息
        has_error = result.exit_code != 0
        error_message = None
        if has_error:
            error_message = stderr_text.strip() if stderr_text.strip() else f"Process exited with code {result.exit_code}"
        
        return {
            "success": not has_error,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "output": output,
            "charts": charts,
            "tables": tables,
            "images": [c["path"] for c in charts if c.get("path")],
            "error": error_message,
            "execution_time_ms": int(elapsed * 1000)
        }
    
    async def close(self) -> None:
        """关闭代码执行器"""
        await self._docker_client.close()
        logger.debug("代码执行器已关闭")



# ==================== 兼容性层 ====================
# 保持与现有代码的兼容性，提供原有的函数和类接口

async def execute_code_in_workspace(
    code: str,
    workspace_dir: str,
    data_dir: str,
    output_dir: str,
    timeout: int = DEFAULT_TIMEOUT,
    container_id: Optional[str] = None,
    docker_client: Optional[DockerClient] = None,
    on_stdout: Optional[Any] = None,
    on_stderr: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    在工作空间中执行代码（兼容性函数）
    
    如果提供了 container_id，则使用 Docker exec 执行；
    否则回退到本地 subprocess 执行（用于测试或无 Docker 环境）。
    
    Args:
        code: 要执行的 Python 代码
        workspace_dir: 工作空间目录
        data_dir: 数据目录
        output_dir: 输出目录
        timeout: 执行超时时间（秒）
        container_id: Docker 容器 ID（可选）
        docker_client: Docker 客户端实例（可选）
        
    Returns:
        执行结果字典
    """
    os.makedirs(workspace_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    
    if container_id:
        # 使用 Docker exec 执行
        executor = CodeExecutor(docker_client)
        try:
            return await executor.execute_in_container(
                container_id=container_id,
                code=code,
                data_dir=data_dir,
                output_dir=output_dir,
                timeout=timeout
            )
        finally:
            await executor.close()
    else:
        # 回退到本地 subprocess 执行（用于测试）
        return await _execute_code_subprocess(
            code=code,
            workspace_dir=workspace_dir,
            data_dir=data_dir,
            output_dir=output_dir,
            timeout=timeout,
            on_stdout=on_stdout,
            on_stderr=on_stderr
        )


async def _execute_code_subprocess(
    code: str,
    workspace_dir: str,
    data_dir: str,
    output_dir: str,
    timeout: int = DEFAULT_TIMEOUT,
    on_stdout: Optional[Any] = None,
    on_stderr: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    使用 subprocess 在本地执行代码（回退方案）
    
    用于测试或无 Docker 环境时的代码执行。
    已重构为使用 asyncio.create_subprocess_exec 实现非阻塞和流式输出。
    
    Args:
        code: 要执行的 Python 代码
        workspace_dir: 工作空间目录
        data_dir: 数据目录
        output_dir: 输出目录
        timeout: 执行超时时间（秒）
        on_stdout: 标准输出回调函数
        on_stderr: 标准错误回调函数
        
    Returns:
        执行结果字典
    """
    import sys
    import tempfile
    import time
    
    logger.info(f"[SUBPROCESS] 开始执行代码: workspace={workspace_dir}, data_dir={data_dir}")
    start_time = time.monotonic()
    
    full_code = DATA_LOADER_TEMPLATE.format(
        data_dir=data_dir.replace('\\', '\\\\'),
        output_dir=output_dir.replace('\\', '\\\\')
    ) + code + BACKUP_CHART_CAPTURE_CODE
    
    logger.info(f"[SUBPROCESS] 完整代码长度: {len(full_code)} 字符")
    
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".py", prefix="exec_")
        os.close(fd)
        
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(full_code)
        
        logger.info(f"[SUBPROCESS] 临时文件: {tmp_path}")
        
        child_env = os.environ.copy()
        child_env["MPLBACKEND"] = "Agg"
        child_env["QT_QPA_PLATFORM"] = "offscreen"
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        # 添加 sandbox_runtime 模块路径
        child_env["PYTHONPATH"] = "/app:" + child_env.get("PYTHONPATH", "")
        child_env.pop("DISPLAY", None)
        
        logger.info(f"[SUBPROCESS] 启动子进程执行...")
        
        # 使用 asyncio.create_subprocess_exec 非阻塞执行
        # limit=1MB 防止 "Separator is not found, and chunk exceed the limit" 错误
        process = await asyncio.create_subprocess_exec(
            sys.executable, tmp_path,
            cwd=workspace_dir,
            env=child_env,
            limit=10 * 1024 * 1024,  # 10MB buffer limit (default is 64KB)
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        
        stdout_chunks = []
        stderr_chunks = []
        
        # 定义流读取器
        async def read_stream(stream, chunks, callback):
            while True:
                try:
                    line = await stream.readline()
                except ValueError as e:
                    # 处理极端情况：单行超过 limit 限制
                    logger.warning(f"Stream read error (line too long): {e}")
                    continue
                if not line:
                    break
                text = line.decode('utf-8', errors='ignore')
                chunks.append(text)
                if callback:
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(text)
                        else:
                            callback(text)
                    except Exception as e:
                        logger.warning(f"Callback error: {e}")
        
        # 并发执行读取和等待
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    read_stream(process.stdout, stdout_chunks, on_stdout),
                    read_stream(process.stderr, stderr_chunks, on_stderr),
                    process.wait()
                ),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            # 超时处理
            try:
                process.kill()
            except ProcessLookupError:
                pass
                
            elapsed = time.monotonic() - start_time
            logger.warning(f"[SUBPROCESS] 执行超时: {elapsed:.2f}s")
            return {
                "success": False,
                "stdout": "".join(stdout_chunks),
                "stderr": "".join(stderr_chunks),
                "output": f"[Timeout]: 执行超过 {timeout} 秒限制",
                "charts": [],
                "tables": [],
                "images": [],
                "error": f"执行超时（{timeout}秒）",
                "execution_time_ms": int(elapsed * 1000)
            }
            
        returncode = process.returncode
        stdout_text = "".join(stdout_chunks)
        stderr_text = "".join(stderr_chunks)
        
        logger.info(f"[SUBPROCESS] 子进程完成: returncode={returncode}, stdout_len={len(stdout_text)}, stderr_len={len(stderr_text)}")
        if returncode != 0 and stderr_text:
            logger.error(f"[SUBPROCESS] 错误输出: {stderr_text[:500]}")
        
        elapsed = time.monotonic() - start_time
        logger.info(f"[SUBPROCESS] 执行耗时: {elapsed:.2f}s")
        
        # 提取 SVG base64
        charts = []
        svg_pattern = re.compile(r'SVG_BASE64_START:(.+?):SVG_BASE64_END', re.DOTALL)
        for match in svg_pattern.finditer(stdout_text):
            svg_b64 = match.group(1).strip()
            if svg_b64:
                charts.append({"path": None, "base64": svg_b64, "format": "svg"})
        
        clean_stdout = svg_pattern.sub('[图表已生成]', stdout_text)
        
        # 提取表格数据
        tables = []
        table_pattern = re.compile(r'TABLE_DATA_START:(.+?):TABLE_DATA_END', re.DOTALL)
        for match in table_pattern.finditer(stdout_text):
            try:
                table_data = json.loads(match.group(1).strip())
                tables.append(table_data)
            except json.JSONDecodeError:
                pass
        
        clean_stdout = table_pattern.sub('[表格数据已捕获]', clean_stdout)
        
        output = clean_stdout
        if stderr_text:
            output += f"\n[stderr]:\n{stderr_text}"
        
        if len(output) > MAX_OUTPUT_LENGTH:
            output = output[:MAX_OUTPUT_LENGTH] + "\n... (输出已截断)"
        
        # 从文件读取图表（如果没有从输出提取到）
        if not charts:
            for fname in os.listdir(output_dir):
                if fname.endswith('.svg'):
                    chart_path = os.path.join(output_dir, fname)
                    try:
                        with open(chart_path, 'rb') as f:
                            img_data = base64.b64encode(f.read()).decode('utf-8')
                        charts.append({"path": chart_path, "base64": img_data, "format": "svg"})
                    except Exception:
                        pass
        
        has_error = returncode != 0
        error_message = None
        if has_error:
            error_message = stderr_text.strip() if stderr_text.strip() else f"Process exited with code {returncode}"
        
        result = {
            "success": not has_error,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "output": output,
            "charts": charts,
            "tables": tables,
            "images": [c["path"] for c in charts if c["path"]],
            "error": error_message,
            "execution_time_ms": int(elapsed * 1000)
        }
        
        # 调试日志
        logger.info(f"[SUBPROCESS] 返回结果: success={result['success']}, charts={len(charts)}, tables={len(tables)}, output_len={len(output)}")
        
        return result
            
    except Exception as e:
        import traceback as tb
        elapsed = time.monotonic() - start_time
        return {
            "success": False,
            "stdout": "",
            "stderr": tb.format_exc(),
            "output": f"[Error]: {str(e)}",
            "charts": [],
            "tables": [],
            "images": [],
            "error": str(e),
            "execution_time_ms": int(elapsed * 1000)
        }
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass



class StatelessSession:
    """
    无状态会话类
    
    管理单个会话的数据和代码执行。
    支持 Docker 容器执行和本地 subprocess 执行两种模式。
    """
    
    def __init__(
        self,
        session_id: str,
        workspace_dir: str = None,
        container_id: Optional[str] = None,
        docker_client: Optional[DockerClient] = None,
    ):
        """
        初始化会话
        
        Args:
            session_id: 会话 ID
            workspace_dir: 工作空间基础目录
            container_id: Docker 容器 ID（可选）
            docker_client: Docker 客户端实例（可选）
        """
        self.session_id = session_id
        self.workspace_base = workspace_dir or settings.workspace_base
        self.workspace_dir = os.path.join(self.workspace_base, session_id)
        self.data_dir = os.path.join(self.workspace_dir, "data")
        self.output_dir = os.path.join(self.workspace_dir, "output")
        self.generated_dir = os.path.join(self.workspace_dir, "generated")
        
        # Docker 相关
        self.container_id = container_id
        self._docker_client = docker_client
        self._executor: Optional[CodeExecutor] = None
        
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.generated_dir, exist_ok=True)
        
        self.data_files: List[str] = []
        self.created_at = datetime.now()
    
    def set_container(
        self,
        container_id: str,
        docker_client: Optional[DockerClient] = None
    ) -> None:
        """
        设置 Docker 容器
        
        Args:
            container_id: Docker 容器 ID
            docker_client: Docker 客户端实例（可选）
        """
        self.container_id = container_id
        if docker_client:
            self._docker_client = docker_client
    
    def get_data_file_path(self, filename: str = "data.csv") -> str:
        """获取数据文件路径"""
        return os.path.join(self.data_dir, filename)
    
    async def load_data(self, data_json: str, filename: str = "data.csv") -> Dict[str, Any]:
        """
        加载 JSON 数据到文件
        
        Args:
            data_json: JSON 格式的数据
            filename: 保存的文件名
            
        Returns:
            加载结果字典
        """
        try:
            import pandas as pd
            data = json.loads(data_json)
            df = pd.DataFrame(data)
            for existing in os.listdir(self.data_dir):
                existing_path = os.path.join(self.data_dir, existing)
                if os.path.isfile(existing_path) and existing.endswith(('.csv', '.xlsx', '.xls')):
                    os.remove(existing_path)
            self.data_files.clear()
            file_path = self.get_data_file_path(filename)
            df.to_csv(file_path, index=False, encoding='utf-8-sig')
            self.data_files = [filename]
            return {
                "success": True,
                "file_path": file_path,
                "rows": len(df),
                "columns": len(df.columns),
                "column_names": list(df.columns)
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def load_file(self, file_content: bytes, filename: str) -> Dict[str, Any]:
        """
        加载文件内容
        
        Args:
            file_content: 文件内容（字节）
            filename: 文件名
            
        Returns:
            加载结果字典
        """
        try:
            for existing in os.listdir(self.data_dir):
                existing_path = os.path.join(self.data_dir, existing)
                if os.path.isfile(existing_path) and existing.endswith(('.csv', '.xlsx', '.xls')):
                    os.remove(existing_path)
            self.data_files.clear()
            file_path = os.path.join(self.data_dir, filename)
            with open(file_path, 'wb') as f:
                f.write(file_content)
            self.data_files = [filename]
            
            import pandas as pd
            if filename.endswith('.csv'):
                for encoding in ['utf-8', 'gbk', 'gb2312', 'latin1']:
                    try:
                        df = pd.read_csv(file_path, encoding=encoding)
                        break
                    except UnicodeDecodeError:
                        continue
                else:
                    df = pd.read_csv(file_path, encoding='utf-8', errors='ignore')
            elif filename.endswith(('.xlsx', '.xls')):
                df = pd.read_excel(file_path)
            else:
                return {"success": True, "file_path": file_path, "message": f"File saved: {filename}"}
            
            return {
                "success": True,
                "file_path": file_path,
                "rows": len(df),
                "columns": len(df.columns),
                "column_names": list(df.columns)
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def get_table_schemas(self) -> List[TableSchema]:
        """
        获取所有数据表的模式信息
        
        Returns:
            表格模式列表
        """
        schemas = []
        if not os.path.exists(self.data_dir):
            return schemas
        
        import pandas as pd
        data_files = [f for f in os.listdir(self.data_dir) if f.endswith(('.csv', '.xlsx', '.xls'))]
        
        for filename in data_files:
            file_path = os.path.join(self.data_dir, filename)
            try:
                if filename.endswith('.csv'):
                    for encoding in ['utf-8', 'gbk', 'gb2312', 'latin1']:
                        try:
                            df = pd.read_csv(file_path, encoding=encoding)
                            break
                        except UnicodeDecodeError:
                            continue
                    else:
                        df = pd.read_csv(file_path, encoding='utf-8', errors='ignore')
                else:
                    df = pd.read_excel(file_path)
                
                var_name = generate_variable_name(filename)
                sample_values = {}
                for col in df.columns:
                    non_null = df[col].dropna().head(3)
                    sample_values[col] = [str(v) for v in non_null.tolist()]
                
                schemas.append({
                    "name": filename,
                    "variable_name": var_name,
                    "columns": list(df.columns),
                    "dtypes": {col: str(dtype) for col, dtype in df.dtypes.items()},
                    "row_count": len(df),
                    "sample_values": sample_values
                })
            except Exception:
                pass
        
        return schemas
    
    def get_multi_table_context(self) -> MultiTableContext:
        """
        获取多表上下文信息
        
        Returns:
            多表上下文字典
        """
        schemas = self.get_table_schemas()
        total_rows = sum(s["row_count"] for s in schemas)
        
        column_to_tables: Dict[str, List[str]] = {}
        for schema in schemas:
            for col in schema["columns"]:
                if col not in column_to_tables:
                    column_to_tables[col] = []
                column_to_tables[col].append(schema["name"])
        
        common_columns = {col: tables for col, tables in column_to_tables.items() if len(tables) > 1}
        
        return {
            "tables": schemas,
            "table_count": len(schemas),
            "total_rows": total_rows,
            "common_columns": common_columns,
            "suggested_joins": []
        }
    
    async def execute_code(
        self,
        code: str,
        timeout: int = DEFAULT_TIMEOUT,
        on_stdout: Optional[Any] = None,
        on_stderr: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        执行代码
        
        如果设置了 container_id，则使用 Docker exec 执行；
        否则使用本地 subprocess 执行。
        
        Args:
            code: 要执行的 Python 代码
            timeout: 执行超时时间（秒）
            
        Returns:
            执行结果字典
        """
        if self.container_id:
            # 使用 Docker exec 执行
            if self._executor is None:
                self._executor = CodeExecutor(self._docker_client)
            
            # 容器内的路径
            container_data_dir = "/data"
            container_output_dir = "/output"
            
            return await self._executor.execute_in_container(
                container_id=self.container_id,
                code=code,
                data_dir=container_data_dir,
                output_dir=container_output_dir,
                timeout=timeout
            )
        else:
            # 使用本地 subprocess 执行
            return await execute_code_in_workspace(
                code=code,
                workspace_dir=self.workspace_dir,
                data_dir=self.data_dir,
                output_dir=self.output_dir,
                timeout=timeout,
                on_stdout=on_stdout,
                on_stderr=on_stderr
            )
    
    def cleanup(self):
        """清理会话资源"""
        try:
            if os.path.exists(self.workspace_dir):
                shutil.rmtree(self.workspace_dir)
        except Exception as e:
            logger.error(f"清理会话资源失败: {e}")
    
    async def close(self):
        """关闭会话"""
        if self._executor:
            await self._executor.close()
            self._executor = None



class StatelessSessionManager:
    """
    无状态会话管理器
    
    管理多个会话的创建、获取和删除。
    """
    
    def __init__(self, workspace_base: str = None):
        """
        初始化会话管理器
        
        Args:
            workspace_base: 工作空间基础目录
        """
        self.workspace_base = workspace_base or settings.workspace_base
        self.sessions: Dict[str, StatelessSession] = {}
    
    def create_session(
        self,
        session_id: str = None,
        container_id: Optional[str] = None,
        docker_client: Optional[DockerClient] = None,
    ) -> StatelessSession:
        """
        创建新会话
        
        Args:
            session_id: 会话 ID（可选，自动生成）
            container_id: Docker 容器 ID（可选）
            docker_client: Docker 客户端实例（可选）
            
        Returns:
            新创建的会话
        """
        if session_id is None:
            session_id = str(uuid.uuid4())
        
        session = StatelessSession(
            session_id,
            self.workspace_base,
            container_id=container_id,
            docker_client=docker_client
        )
        self.sessions[session_id] = session
        logger.info(f"创建会话: {session_id}")
        return session
    
    def get_session(self, session_id: str) -> Optional[StatelessSession]:
        """
        获取会话
        
        Args:
            session_id: 会话 ID
            
        Returns:
            会话对象，如果不存在则返回 None
        """
        if session_id in self.sessions:
            return self.sessions[session_id]
        
        # 尝试从文件系统恢复会话
        session_dir = os.path.join(self.workspace_base, session_id)
        data_dir = os.path.join(session_dir, "data")
        if os.path.exists(data_dir):
            data_files = [f for f in os.listdir(data_dir) if f.endswith(('.csv', '.xlsx', '.xls'))]
            if data_files:
                session = StatelessSession(session_id, self.workspace_base)
                session.data_files = data_files
                self.sessions[session_id] = session
                return session
        return None
    
    def get_or_create_session(
        self,
        session_id: str,
        container_id: Optional[str] = None,
        docker_client: Optional[DockerClient] = None,
    ) -> StatelessSession:
        """
        获取或创建会话
        
        Args:
            session_id: 会话 ID
            container_id: Docker 容器 ID（可选）
            docker_client: Docker 客户端实例（可选）
            
        Returns:
            会话对象
        """
        existing = self.get_session(session_id)
        if existing:
            # 更新容器信息
            if container_id:
                existing.set_container(container_id, docker_client)
            return existing
        return self.create_session(session_id, container_id, docker_client)
    
    def delete_session(self, session_id: str) -> bool:
        """
        删除会话
        
        Args:
            session_id: 会话 ID
            
        Returns:
            是否成功删除
        """
        if session_id in self.sessions:
            self.sessions[session_id].cleanup()
            del self.sessions[session_id]
            return True
        return False
    
    async def cleanup_old_sessions(self, max_age_hours: float = 12) -> int:
        """
        清理过期会话
        
        Args:
            max_age_hours: 最大存活时间（小时）
            
        Returns:
            清理的会话数量
        """
        now = datetime.now()
        to_delete = []
        for session_id, session in self.sessions.items():
            age = (now - session.created_at).total_seconds() / 3600
            if age > max_age_hours:
                to_delete.append(session_id)
        for session_id in to_delete:
            self.delete_session(session_id)
        return len(to_delete)


# 全局实例
session_manager = StatelessSessionManager()
