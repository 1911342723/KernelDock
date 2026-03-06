"""
代码验证器模块

使用 AST 静态分析进行代码安全检查：
- 禁止危险模块导入
- 禁止危险函数调用
- 提供详细的验证结果
"""

import ast
from dataclasses import dataclass, field
from typing import List, Set, Optional


@dataclass
class ValidationIssue:
    """验证问题"""
    type: str          # 问题类型：import, call, attribute
    name: str          # 问题名称
    line: int          # 行号
    message: str       # 详细信息


@dataclass
class ValidationResult:
    """验证结果"""
    is_valid: bool
    issues: List[ValidationIssue] = field(default_factory=list)
    
    def add_issue(self, issue: ValidationIssue) -> None:
        self.issues.append(issue)
        self.is_valid = False
    
    def to_dict(self) -> dict:
        return {
            "is_valid": self.is_valid,
            "issues": [
                {
                    "type": i.type,
                    "name": i.name,
                    "line": i.line,
                    "message": i.message
                }
                for i in self.issues
            ]
        }


class CodeValidator:
    """
    AST 代码安全检查器
    
    在代码执行前进行静态分析，阻止危险代码。
    使用黑名单机制禁止特定模块、函数和属性访问模式。
    
    用法:
        validator = CodeValidator()
        result = validator.validate(code)
        if not result.is_valid:
            print(result.issues)
    """
    
    FORBIDDEN_MODULES: Set[str] = {
        # 系统操作
        'os', 'sys', 'subprocess', 'shutil', 'pathlib',
        # 网络
        'socket', 'http', 'urllib', 'requests', 'httpx', 'aiohttp',
        # 进程/线程
        'multiprocessing', 'threading', 'concurrent',
        # 危险模块
        'ctypes', 'importlib', 'builtins', '__builtins__',
        'code', 'codeop', 'commands', 'pdb', 'pickle', 'marshal', 'shelve',
        'signal', 'resource', 'pty', 'fcntl', 'termios',
    }
    
    FORBIDDEN_CALLS: Set[str] = {
        'exec', 'eval', 'compile', '__import__',
        'globals', 'locals', 'vars', 'dir',
        'getattr', 'setattr', 'delattr', 'hasattr',
        'breakpoint', 'input', 'help',
        'memoryview', 'bytearray',
    }

    FORBIDDEN_DUNDER_ATTRS: Set[str] = {
        '__import__', '__loader__', '__spec__', '__builtins__',
        '__subclasses__', '__bases__', '__mro__', '__class__',
        '__globals__', '__code__', '__func__', '__self__',
        '__dict__', '__module__', '__qualname__',
    }
    
    ALLOWED_EXCEPTIONS: Set[str] = {
        'open',
    }
    
    def __init__(
        self,
        forbidden_modules: Optional[Set[str]] = None,
        forbidden_calls: Optional[Set[str]] = None,
        allow_open: bool = True,
        strict_mode: bool = False,
    ):
        self.forbidden_modules = self.FORBIDDEN_MODULES.copy()
        self.forbidden_calls = self.FORBIDDEN_CALLS.copy()
        self.forbidden_dunder_attrs = self.FORBIDDEN_DUNDER_ATTRS.copy()
        
        if forbidden_modules:
            self.forbidden_modules.update(forbidden_modules)
        if forbidden_calls:
            self.forbidden_calls.update(forbidden_calls)
        
        if allow_open and 'open' in self.forbidden_calls:
            self.forbidden_calls.discard('open')
        elif not allow_open:
            self.forbidden_calls.add('open')
        
        if strict_mode:
            self.forbidden_modules.update({
                'io', 'tempfile', 'glob', 'fnmatch',
            })
    
    def validate(self, code: str) -> ValidationResult:
        result = ValidationResult(is_valid=True)
        
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            result.add_issue(ValidationIssue(
                type="syntax",
                name="SyntaxError",
                line=e.lineno or 0,
                message=f"语法错误: {e.msg}"
            ))
            return result
        
        for node in ast.walk(tree):
            self._check_node(node, result)
        
        return result
    
    def _check_node(self, node: ast.AST, result: ValidationResult) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_name = alias.name.split('.')[0]
                if module_name in self.forbidden_modules:
                    result.add_issue(ValidationIssue(
                        type="import",
                        name=alias.name,
                        line=node.lineno,
                        message=f"禁止导入模块 '{alias.name}'"
                    ))
        
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                module_name = node.module.split('.')[0]
                if module_name in self.forbidden_modules:
                    result.add_issue(ValidationIssue(
                        type="import",
                        name=node.module,
                        line=node.lineno,
                        message=f"禁止从 '{node.module}' 导入"
                    ))
        
        elif isinstance(node, ast.Call):
            func_name = self._get_call_name(node.func)
            if func_name and func_name in self.forbidden_calls:
                result.add_issue(ValidationIssue(
                    type="call",
                    name=func_name,
                    line=node.lineno,
                    message=f"禁止调用函数 '{func_name}()'"
                ))

        elif isinstance(node, ast.Attribute):
            attr = node.attr
            if attr in self.forbidden_dunder_attrs:
                result.add_issue(ValidationIssue(
                    type="attribute",
                    name=attr,
                    line=node.lineno,
                    message=f"禁止访问属性 '{attr}'"
                ))

        elif isinstance(node, ast.Subscript):
            self._check_string_subscript(node, result)
    
    def _check_string_subscript(self, node: ast.Subscript, result: ValidationResult) -> None:
        """Block dict/subscript access with forbidden dunder string keys like
        obj['__import__'] or obj["__builtins__"]."""
        slic = node.slice
        if isinstance(slic, ast.Constant) and isinstance(slic.value, str):
            if slic.value in self.forbidden_dunder_attrs:
                result.add_issue(ValidationIssue(
                    type="attribute",
                    name=slic.value,
                    line=node.lineno,
                    message=f"禁止通过下标访问 '{slic.value}'"
                ))

    def _get_call_name(self, node: ast.AST) -> Optional[str]:
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return node.attr
        return None


# 全局验证器实例
_default_validator: Optional[CodeValidator] = None


def get_validator() -> CodeValidator:
    """获取默认验证器实例"""
    global _default_validator
    if _default_validator is None:
        _default_validator = CodeValidator()
    return _default_validator


def validate_code(code: str) -> ValidationResult:
    """
    验证代码安全性（便捷函数）
    
    Args:
        code: Python 代码字符串
        
    Returns:
        ValidationResult 对象
    """
    return get_validator().validate(code)
