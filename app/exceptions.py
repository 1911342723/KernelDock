"""
沙箱服务异常类和错误处理模块

定义沙箱服务的异常类层次结构，包括错误代码枚举和各种具体异常类。
每个异常类都包含错误代码、消息、详情，并提供 API 响应格式化方法。

Requirements: 4.6 (代码执行产生异常时返回完整的异常堆栈信息)
"""

from enum import Enum
from typing import Optional, Dict, Any
from dataclasses import dataclass, field


class ErrorCode(Enum):
    """
    错误代码枚举
    
    定义沙箱服务中所有可能的错误类型代码。
    """
    SANDBOX_NOT_FOUND = "SANDBOX_NOT_FOUND"
    SANDBOX_CREATION_FAILED = "SANDBOX_CREATION_FAILED"
    RESOURCE_LIMIT_EXCEEDED = "RESOURCE_LIMIT_EXCEEDED"
    EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
    NETWORK_ACCESS_DENIED = "NETWORK_ACCESS_DENIED"
    POOL_EXHAUSTED = "POOL_EXHAUSTED"
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# HTTP 状态码映射
HTTP_STATUS_CODES: Dict[ErrorCode, int] = {
    ErrorCode.SANDBOX_NOT_FOUND: 404,
    ErrorCode.SANDBOX_CREATION_FAILED: 500,
    ErrorCode.RESOURCE_LIMIT_EXCEEDED: 400,
    ErrorCode.EXECUTION_TIMEOUT: 408,
    ErrorCode.NETWORK_ACCESS_DENIED: 403,
    ErrorCode.POOL_EXHAUSTED: 503,
    ErrorCode.INVALID_CONFIGURATION: 400,
    ErrorCode.FILE_NOT_FOUND: 404,
    ErrorCode.INTERNAL_ERROR: 500,
}


@dataclass
class SandboxError(Exception):
    """
    沙箱服务基础异常类
    
    所有沙箱相关异常的基类，提供统一的错误响应格式。
    
    Attributes:
        code: 错误代码枚举值
        message: 错误消息描述
        details: 可选的错误详情字典
    """
    code: ErrorCode
    message: str
    details: Optional[Dict[str, Any]] = field(default=None)
    
    def __post_init__(self):
        """初始化后设置 Exception 的 args"""
        super().__init__(self.message)
    
    @property
    def http_status_code(self) -> int:
        """获取对应的 HTTP 状态码"""
        return HTTP_STATUS_CODES.get(self.code, 500)
    
    def to_response(self) -> Dict[str, Any]:
        """
        转换为 API 响应格式
        
        Returns:
            包含错误信息的字典，格式为:
            {
                "error": {
                    "code": "ERROR_CODE",
                    "message": "错误消息",
                    "details": {...}  # 可选
                }
            }
        """
        error_body: Dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
        }
        if self.details is not None:
            error_body["details"] = self.details
        
        return {"error": error_body}
    
    def __str__(self) -> str:
        """返回异常的字符串表示"""
        return f"[{self.code.value}] {self.message}"
    
    def __repr__(self) -> str:
        """返回异常的详细表示"""
        return (
            f"{self.__class__.__name__}("
            f"code={self.code!r}, "
            f"message={self.message!r}, "
            f"details={self.details!r})"
        )


class SandboxNotFoundError(SandboxError):
    """
    沙箱不存在异常
    
    当请求的沙箱 ID 不存在时抛出。
    HTTP 状态码: 404
    处理策略: 返回错误信息，不重试
    """
    
    def __init__(self, sandbox_id: str):
        """
        初始化沙箱不存在异常
        
        Args:
            sandbox_id: 不存在的沙箱 ID
        """
        super().__init__(
            code=ErrorCode.SANDBOX_NOT_FOUND,
            message=f"沙箱不存在: {sandbox_id}",
            details={"sandbox_id": sandbox_id}
        )


class SandboxCreationError(SandboxError):
    """
    沙箱创建失败异常
    
    当创建沙箱容器失败时抛出。
    HTTP 状态码: 500
    处理策略: 记录日志，返回错误，可重试
    """
    
    def __init__(
        self, 
        reason: str, 
        session_id: Optional[str] = None,
        original_error: Optional[str] = None
    ):
        """
        初始化沙箱创建失败异常
        
        Args:
            reason: 创建失败的原因
            session_id: 关联的会话 ID（可选）
            original_error: 原始错误信息（可选）
        """
        details: Dict[str, Any] = {"reason": reason}
        if session_id:
            details["session_id"] = session_id
        if original_error:
            details["original_error"] = original_error
        
        super().__init__(
            code=ErrorCode.SANDBOX_CREATION_FAILED,
            message=f"沙箱创建失败: {reason}",
            details=details
        )


class ResourceLimitExceededError(SandboxError):
    """
    资源限制超出异常
    
    当请求的资源超出允许的限制时抛出。
    HTTP 状态码: 400
    处理策略: 返回错误信息，建议调整参数
    """
    
    def __init__(
        self,
        resource_type: str,
        requested: Any,
        limit: Any,
        suggestion: Optional[str] = None
    ):
        """
        初始化资源限制超出异常
        
        Args:
            resource_type: 资源类型（如 "cpu", "memory", "disk"）
            requested: 请求的资源值
            limit: 资源限制值
            suggestion: 调整建议（可选）
        """
        details: Dict[str, Any] = {
            "resource_type": resource_type,
            "requested": requested,
            "limit": limit,
        }
        if suggestion:
            details["suggestion"] = suggestion
        
        message = f"资源限制超出: {resource_type} 请求值 {requested} 超过限制 {limit}"
        if suggestion:
            message += f"。建议: {suggestion}"
        
        super().__init__(
            code=ErrorCode.RESOURCE_LIMIT_EXCEEDED,
            message=message,
            details=details
        )


class ExecutionTimeoutError(SandboxError):
    """
    执行超时异常
    
    当代码执行超过配置的超时时间时抛出。
    HTTP 状态码: 408
    处理策略: 终止执行，返回部分结果
    """
    
    def __init__(
        self,
        timeout_seconds: int,
        partial_output: Optional[str] = None,
        sandbox_id: Optional[str] = None
    ):
        """
        初始化执行超时异常
        
        Args:
            timeout_seconds: 超时时间（秒）
            partial_output: 部分执行输出（可选）
            sandbox_id: 沙箱 ID（可选）
        """
        details: Dict[str, Any] = {"timeout_seconds": timeout_seconds}
        if partial_output:
            details["partial_output"] = partial_output
        if sandbox_id:
            details["sandbox_id"] = sandbox_id
        
        super().__init__(
            code=ErrorCode.EXECUTION_TIMEOUT,
            message=f"代码执行超时: 超过 {timeout_seconds} 秒限制",
            details=details
        )


class NetworkAccessDeniedError(SandboxError):
    """
    网络访问被拒绝异常
    
    当沙箱尝试访问被禁止的网络地址时抛出。
    HTTP 状态码: 403
    处理策略: 记录日志，返回错误信息
    """
    
    def __init__(
        self,
        target_address: str,
        reason: Optional[str] = None,
        sandbox_id: Optional[str] = None
    ):
        """
        初始化网络访问被拒绝异常
        
        Args:
            target_address: 尝试访问的目标地址
            reason: 拒绝原因（可选）
            sandbox_id: 沙箱 ID（可选）
        """
        details: Dict[str, Any] = {"target_address": target_address}
        if reason:
            details["reason"] = reason
        if sandbox_id:
            details["sandbox_id"] = sandbox_id
        
        message = f"网络访问被拒绝: {target_address}"
        if reason:
            message += f" ({reason})"
        
        super().__init__(
            code=ErrorCode.NETWORK_ACCESS_DENIED,
            message=message,
            details=details
        )


class ContainerPoolExhaustedError(SandboxError):
    """
    容器池耗尽异常
    
    当容器池中没有可用容器且无法创建新容器时抛出。
    HTTP 状态码: 503
    处理策略: 返回错误，建议稍后重试
    """
    
    def __init__(
        self,
        pool_size: int,
        active_count: int,
        max_concurrent: Optional[int] = None
    ):
        """
        初始化容器池耗尽异常
        
        Args:
            pool_size: 容器池大小
            active_count: 当前活跃容器数
            max_concurrent: 最大并发数（可选）
        """
        details: Dict[str, Any] = {
            "pool_size": pool_size,
            "active_count": active_count,
            "suggestion": "请稍后重试，或联系管理员扩展容器池容量"
        }
        if max_concurrent is not None:
            details["max_concurrent"] = max_concurrent
        
        super().__init__(
            code=ErrorCode.POOL_EXHAUSTED,
            message=f"容器池已耗尽: 当前活跃 {active_count} 个容器，池大小 {pool_size}",
            details=details
        )


class InvalidConfigurationError(SandboxError):
    """
    无效配置异常
    
    当配置参数无效时抛出。
    HTTP 状态码: 400
    处理策略: 使用默认值，记录警告
    """
    
    def __init__(
        self,
        config_key: str,
        invalid_value: Any,
        default_value: Any,
        reason: Optional[str] = None
    ):
        """
        初始化无效配置异常
        
        Args:
            config_key: 配置键名
            invalid_value: 无效的配置值
            default_value: 将使用的默认值
            reason: 无效原因（可选）
        """
        details: Dict[str, Any] = {
            "config_key": config_key,
            "invalid_value": invalid_value,
            "default_value": default_value,
        }
        if reason:
            details["reason"] = reason
        
        message = f"无效配置: {config_key}={invalid_value}"
        if reason:
            message += f" ({reason})"
        message += f"，将使用默认值 {default_value}"
        
        super().__init__(
            code=ErrorCode.INVALID_CONFIGURATION,
            message=message,
            details=details
        )


class FileNotFoundError(SandboxError):
    """
    文件不存在异常
    
    当请求的文件在沙箱中不存在时抛出。
    HTTP 状态码: 404
    处理策略: 返回错误信息，不重试
    
    注意: 此类名与内置 FileNotFoundError 冲突，
    使用时需要通过模块名引用: exceptions.FileNotFoundError
    """
    
    def __init__(
        self,
        filename: str,
        sandbox_id: Optional[str] = None,
        directory: Optional[str] = None
    ):
        """
        初始化文件不存在异常
        
        Args:
            filename: 文件名
            sandbox_id: 沙箱 ID（可选）
            directory: 目录路径（可选）
        """
        details: Dict[str, Any] = {"filename": filename}
        if sandbox_id:
            details["sandbox_id"] = sandbox_id
        if directory:
            details["directory"] = directory
        
        super().__init__(
            code=ErrorCode.FILE_NOT_FOUND,
            message=f"文件不存在: {filename}",
            details=details
        )


class InternalError(SandboxError):
    """
    内部错误异常
    
    当发生未预期的内部错误时抛出。
    HTTP 状态码: 500
    处理策略: 记录日志，返回通用错误信息
    """
    
    def __init__(
        self,
        message: str,
        original_error: Optional[Exception] = None,
        traceback: Optional[str] = None
    ):
        """
        初始化内部错误异常
        
        Args:
            message: 错误消息
            original_error: 原始异常（可选）
            traceback: 异常堆栈信息（可选）
        """
        details: Dict[str, Any] = {}
        if original_error:
            details["original_error"] = str(original_error)
            details["error_type"] = type(original_error).__name__
        if traceback:
            details["traceback"] = traceback
        
        super().__init__(
            code=ErrorCode.INTERNAL_ERROR,
            message=f"内部错误: {message}",
            details=details if details else None
        )


# 便捷函数：从异常创建 API 响应
def create_error_response(
    error: SandboxError
) -> tuple[Dict[str, Any], int]:
    """
    从 SandboxError 创建 API 响应
    
    Args:
        error: SandboxError 实例
        
    Returns:
        (响应体字典, HTTP 状态码) 元组
    """
    return error.to_response(), error.http_status_code


def wrap_exception(
    exc: Exception,
    default_message: str = "发生未知错误"
) -> SandboxError:
    """
    将普通异常包装为 SandboxError
    
    如果异常已经是 SandboxError，则直接返回。
    否则包装为 InternalError。
    
    Args:
        exc: 要包装的异常
        default_message: 默认错误消息
        
    Returns:
        SandboxError 实例
    """
    if isinstance(exc, SandboxError):
        return exc
    
    import traceback
    tb_str = traceback.format_exc()
    
    return InternalError(
        message=str(exc) or default_message,
        original_error=exc,
        traceback=tb_str
    )
