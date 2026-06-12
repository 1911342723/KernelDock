"""
API 请求/响应模型

main.py 与各路由模块共用的 Pydantic 模型集中定义在这里。
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class CreateSessionRequest(BaseModel):
    """创建会话请求"""
    session_id: Optional[str] = None


class CreateSessionResponse(BaseModel):
    """创建会话响应"""
    session_id: str
    workspace_dir: str
    data_dir: str
    output_dir: str


class ExecuteCodeRequest(BaseModel):
    """执行代码请求"""
    code: str
    timeout: int = 300
    context_id: Optional[str] = None
    # multi-table-analysis (executor_protocol_multitable.md):
    # 可选字段；成对出现。base64 编码的 parquet 字节 + backend 渲染的
    # DataLoaderBootstrap Python 源码。
    pre_load_parquet: Optional[dict] = None
    bootstrap_source: Optional[str] = None


class ChartData(BaseModel):
    """图表数据"""
    path: Optional[str]
    base64: str
    format: str


class TableData(BaseModel):
    """表格数据"""
    id: str
    name: str
    columns: List[str]
    data: List[dict]
    totalRows: int
    displayedRows: int
    dtypes: dict
    description: Optional[str]
    csvData: Optional[str]


class QueueInfoResponse(BaseModel):
    """排队信息"""
    position_on_entry: int = 0
    waited_seconds: float = 0.0
    estimated_wait_seconds: float = 0.0
    queue_depth: int = 0
    executing_count: int = 0
    max_concurrent: int = 0
    avg_execution_time: float = 0.0
    total_enqueued: int = 0
    total_executed: int = 0


class SandboxInfoResponse(BaseModel):
    """沙箱运行信息"""
    sandbox_id: Optional[str] = None
    container_id_short: Optional[str] = None
    mode: str = "unknown"
    state: Optional[str] = None
    cpu_limit: Optional[float] = None
    memory_limit_mb: Optional[int] = None
    network_enabled: Optional[bool] = None
    pool_available: Optional[int] = None
    pool_total: Optional[int] = None


class ExecutionInfoResponse(BaseModel):
    """执行细节信息"""
    execution_time_ms: int = 0
    execution_path: str = "unknown"
    code_size_bytes: int = 0
    timeout_configured: int = 0
    timed_out: bool = False
    chart_count: int = 0
    table_count: int = 0
    output_truncated: bool = False
    output_size_bytes: int = 0


class ExecuteCodeResponse(BaseModel):
    """执行代码响应"""
    success: bool
    output: str
    stdout: str
    stderr: str
    charts: List[dict]
    tables: List[dict]
    images: List[str]
    error: Optional[str]
    queue_info: Optional[QueueInfoResponse] = None
    sandbox_info: Optional[SandboxInfoResponse] = None
    execution_info: Optional[ExecutionInfoResponse] = None


class StatelessExecuteRequest(BaseModel):
    """无状态执行请求（即用即毁模式）"""
    code: str
    timeout: int = 30
    context_id: Optional[str] = None
    data_files: Optional[dict] = None  # {filename: base64_content}
    # multi-table-analysis
    pre_load_parquet: Optional[dict] = None
    bootstrap_source: Optional[str] = None


class LoadDataRequest(BaseModel):
    """加载数据请求"""
    data_json: str
    filename: str = "data.csv"


class LoadDataResponse(BaseModel):
    """加载数据响应"""
    success: bool
    file_path: Optional[str]
    rows: Optional[int]
    columns: Optional[int]
    column_names: Optional[List[str]]
    error: Optional[str]


class TableSchemaResponse(BaseModel):
    """表格模式响应"""
    name: str
    variable_name: str
    columns: List[str]
    dtypes: dict
    row_count: int
    sample_values: dict


class MultiTableContextResponse(BaseModel):
    """多表上下文响应"""
    tables: List[TableSchemaResponse]
    table_count: int
    total_rows: int
    common_columns: dict
    suggested_joins: List[dict]


class ShellExecuteRequest(BaseModel):
    """Shell 命令执行请求"""
    command: str
    timeout: int = 60
    workdir: Optional[str] = None


class ShellExecuteResponse(BaseModel):
    """Shell 命令执行响应"""
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output_truncated: bool = False
    execution_time_ms: int = 0


class FsEntry(BaseModel):
    """文件系统目录条目"""
    name: str
    type: str  # "file" | "dir"
    size: int
    mtime: int


class FsWriteRequest(BaseModel):
    """写文件请求"""
    path: str
    content_base64: str


class InstallPackagesRequest(BaseModel):
    """运行时 pip 装包请求"""
    packages: List[str]
    timeout: int = 300


class InstallPackagesResponse(BaseModel):
    """运行时 pip 装包响应"""
    success: bool
    packages: List[str]
    user_site: Optional[str] = None
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None
    execution_time_ms: int = 0


class SubmitJobRequest(BaseModel):
    """后台任务提交请求"""
    code: str
    timeout: int = 600
    session_id: Optional[str] = None


class JobResponse(BaseModel):
    """后台任务状态响应"""
    job_id: str
    kind: str
    status: str
    session_id: Optional[str] = None
    timeout: int
    created_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    result: Optional[dict] = None


class CreateContextRequest(BaseModel):
    """创建代码上下文请求"""
    fork_from: Optional[str] = None
    language: str = "python"


class ContextResponse(BaseModel):
    """代码上下文响应"""
    context_id: str
    session_id: str
    language: str
    created_at: datetime
    last_used_at: datetime
    parent_context_id: Optional[str] = None
