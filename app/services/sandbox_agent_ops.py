"""
沙箱 Agent 操作 Mixin：shell 命令执行、容器内文件系统操作、运行时 pip 装包

设计约束：
- 一切操作以容器为安全边界，不在网关侧做"智能"过滤（AST 黑名单的教训）；
  但路径类操作限定在容器内可写目录白名单，防止误操作只读 rootfs。
- shell 超时用容器内 coreutils timeout 强制（SIGKILL），外层 asyncio 只兜底。
- pip 装包仅支持 session 沙箱（污染池容器不可接受），且要求 egress_mode=proxy
  （物理禁网下 pypi 不可达，直接报错而不是让用户等超时）。
"""

import logging
import re
import shlex
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ..config import settings
from ..exceptions import InternalError, SandboxNotFoundError

if TYPE_CHECKING:
    import asyncio
    from ..infrastructure.container_pool import ContainerPool
    from ..infrastructure.docker_client import DockerClient
    from .sandbox_models import _SandboxRecord

logger = logging.getLogger(__name__)

# 容器内允许文件操作的目录白名单（与 _cleanup_container_workspace 的可写挂载点一致）
ALLOWED_FS_ROOTS = ("/data", "/output", "/tmp", "/home/sandbox")

# 单文件读写大小上限
FS_MAX_FILE_BYTES = 32 * 1024 * 1024

# shell 输出截断上限（stdout/stderr 各自）
SHELL_MAX_OUTPUT_BYTES = 256 * 1024

# pip 包规格白名单：name[extras]==version 形态，挡掉 --index-url 之类的选项注入
_PACKAGE_SPEC_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"          # 包名
    r"(\[[A-Za-z0-9 ,._-]+\])?"              # 可选 extras
    r"((==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!_-]+(,(==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!_-]+)*)?$"  # 可选版本约束
)


def validate_container_path(path: str) -> str:
    """
    校验并规范化容器内路径：绝对路径、无 .. 逃逸、落在可写目录白名单内。

    Returns:
        规范化后的路径

    Raises:
        ValueError: 路径非法
    """
    import posixpath

    if not path or "\x00" in path:
        raise ValueError("路径不能为空且不能包含空字符")
    if not path.startswith("/"):
        raise ValueError(f"必须是绝对路径: {path}")

    normalized = posixpath.normpath(path)
    for root in ALLOWED_FS_ROOTS:
        if normalized == root or normalized.startswith(root + "/"):
            return normalized
    raise ValueError(
        f"路径不在允许范围内: {normalized}（允许: {', '.join(ALLOWED_FS_ROOTS)}）"
    )


def validate_package_specs(packages: List[str]) -> List[str]:
    """
    校验 pip 包规格列表，挡掉选项注入与空列表。

    Raises:
        ValueError: 规格非法
    """
    if not packages:
        raise ValueError("packages 不能为空")
    if len(packages) > 30:
        raise ValueError("单次最多安装 30 个包")
    cleaned = []
    for spec in packages:
        spec = (spec or "").strip()
        if not spec or not _PACKAGE_SPEC_PATTERN.match(spec):
            raise ValueError(f"非法的包规格: {spec!r}（仅支持 name[extras]==version 形态）")
        cleaned.append(spec)
    return cleaned


class SandboxAgentOpsMixin:
    """SandboxManager 的 Agent 扩展操作职责。"""

    if TYPE_CHECKING:
        _lock: "asyncio.Lock"
        _sandboxes: Dict[str, "_SandboxRecord"]
        _docker_client: "DockerClient"
        _container_pool: Optional["ContainerPool"]

        async def _cleanup_and_reset_for_reuse(self, container_id: str, reset_kernel: bool = True) -> bool: ...
        async def _release_or_destroy_stateless_container(self, container_id: str, reusable: bool) -> None: ...
        async def _wait_kernel_ready(self, container_id: str, timeout_seconds: int = 45) -> bool: ...
        async def execute_code(self, sandbox_id: str, code: str, timeout: int = 300, **kwargs) -> Dict[str, Any]: ...

    # ===== 内部公共原语 =====

    async def _resolve_container_id(self, sandbox_id: str) -> str:
        """sandbox_id → container_id，同时刷新活动时间。"""
        async with self._lock:
            record = self._sandboxes.get(sandbox_id)
            if record is None:
                raise SandboxNotFoundError(sandbox_id=sandbox_id)
            record.info.last_activity = datetime.now()
            return record.info.container_id

    async def _run_shell_in_container(
        self,
        container_id: str,
        command: str,
        timeout: int,
        workdir: Optional[str] = None,
        environment: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        在容器内执行 shell 命令。

        超时由容器内 coreutils timeout 强制（--kill-after 2s），
        外层 asyncio 超时只作为传输层兜底（+10s 宽限）。
        """
        start = time.monotonic()
        wrapped = f"timeout -k 2 {int(timeout)} /bin/sh -c {shlex.quote(command)}"
        try:
            result = await self._docker_client.exec_command(
                container_id,
                wrapped,
                workdir=workdir,
                environment=environment,
                timeout=timeout + 10,
            )
            exit_code = result.exit_code if result.exit_code is not None else -1
            stdout, stderr = result.stdout, result.stderr
        except InternalError as e:
            # 外层兜底超时或 Docker API 失败
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return {
                "success": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": str(e),
                "timed_out": "超时" in str(e),
                "execution_time_ms": elapsed_ms,
            }

        truncated = False
        if len(stdout.encode("utf-8", errors="replace")) > SHELL_MAX_OUTPUT_BYTES:
            stdout = stdout.encode("utf-8", errors="replace")[:SHELL_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace") + "\n...(输出已截断)"
            truncated = True
        if len(stderr.encode("utf-8", errors="replace")) > SHELL_MAX_OUTPUT_BYTES:
            stderr = stderr.encode("utf-8", errors="replace")[:SHELL_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace") + "\n...(输出已截断)"
            truncated = True

        # coreutils timeout: 124=超时, 137=kill-after SIGKILL
        timed_out = exit_code in (124, 137)
        return {
            "success": exit_code == 0,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
            "output_truncated": truncated,
            "execution_time_ms": int((time.monotonic() - start) * 1000),
        }

    # ===== Shell 执行 =====

    async def execute_shell(
        self,
        sandbox_id: str,
        command: str,
        timeout: int = 60,
        workdir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """在 session 沙箱内执行 shell 命令。"""
        container_id = await self._resolve_container_id(sandbox_id)
        if workdir is not None:
            workdir = validate_container_path(workdir)
        return await self._run_shell_in_container(
            container_id, command, timeout, workdir=workdir
        )

    async def execute_shell_stateless(
        self,
        command: str,
        timeout: int = 60,
    ) -> Dict[str, Any]:
        """
        无状态 shell 执行：独占借池容器 → 执行 → 清理 + kernel 健康检查 → 归还/销毁。

        shell 没有 fork 隔离（可能 kill kernel、写脏 /tmp），因此：
        - 不走共享租约，只独占借用；
        - 归还前必须确认 kernel 仍然存活，否则销毁补池。
        """
        if not self._container_pool:
            raise InternalError(message="容器池未初始化")

        pooled = await self._container_pool.acquire_for_execution()
        if not pooled:
            raise InternalError(message="容器池为空，无法执行无状态 shell（请稍后重试）")

        container_id = pooled.container_id
        try:
            result = await self._run_shell_in_container(container_id, command, timeout)
        except Exception:
            cleanup_ok = await self._cleanup_and_reset_for_reuse(container_id, reset_kernel=False)
            kernel_alive = await self._wait_kernel_ready(container_id, timeout_seconds=5)
            await self._release_or_destroy_stateless_container(
                container_id, cleanup_ok and kernel_alive
            )
            raise

        cleanup_ok = await self._cleanup_and_reset_for_reuse(container_id, reset_kernel=False)
        kernel_alive = await self._wait_kernel_ready(container_id, timeout_seconds=5)
        if not kernel_alive:
            logger.warning(f"无状态 shell 执行后 kernel 失活，销毁容器: {container_id[:12]}")
        await self._release_or_destroy_stateless_container(
            container_id, cleanup_ok and kernel_alive
        )
        return result

    # ===== 文件系统操作（session 沙箱） =====

    async def fs_list(self, sandbox_id: str, path: str) -> List[Dict[str, Any]]:
        """列出容器内目录条目（name/type/size/mtime）。"""
        container_id = await self._resolve_container_id(sandbox_id)
        safe_path = validate_container_path(path)

        script = (
            "import os,json,sys\n"
            "p = sys.argv[1]\n"
            "if not os.path.exists(p):\n"
            "    print(json.dumps({'error': 'not_found'})); raise SystemExit(0)\n"
            "if not os.path.isdir(p):\n"
            "    print(json.dumps({'error': 'not_a_directory'})); raise SystemExit(0)\n"
            "entries = []\n"
            "for name in sorted(os.listdir(p)):\n"
            "    fp = os.path.join(p, name)\n"
            "    try:\n"
            "        st = os.lstat(fp)\n"
            "        entries.append({'name': name,\n"
            "                        'type': 'dir' if os.path.isdir(fp) else 'file',\n"
            "                        'size': st.st_size,\n"
            "                        'mtime': int(st.st_mtime)})\n"
            "    except OSError:\n"
            "        pass\n"
            "print(json.dumps({'entries': entries}))\n"
        )
        result = await self._docker_client.exec_command(
            container_id, ["python", "-c", script, safe_path], timeout=15
        )
        if result.exit_code != 0:
            raise InternalError(message=f"fs_list 失败: {result.stderr.strip()}")

        import json as _json
        payload = _json.loads(result.stdout.strip() or "{}")
        if payload.get("error") == "not_found":
            raise FileNotFoundError(safe_path)
        if payload.get("error") == "not_a_directory":
            raise NotADirectoryError(safe_path)
        return payload.get("entries", [])

    async def fs_read(self, sandbox_id: str, path: str) -> bytes:
        """读取容器内单个文件内容（get_archive，单次 API 调用）。"""
        import io
        import tarfile

        container_id = await self._resolve_container_id(sandbox_id)
        safe_path = validate_container_path(path)

        try:
            tar_bytes = await self._docker_client.get_archive(
                container_id, safe_path, max_bytes=FS_MAX_FILE_BYTES
            )
        except InternalError as e:
            if "404" in str(e) or "No such" in str(e) or "not found" in str(e).lower():
                raise FileNotFoundError(safe_path)
            raise

        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
            members = tar.getmembers()
            if not members:
                raise FileNotFoundError(safe_path)
            member = members[0]
            if member.isdir():
                raise IsADirectoryError(safe_path)
            fobj = tar.extractfile(member)
            if fobj is None:
                raise FileNotFoundError(safe_path)
            return fobj.read()

    async def fs_write(self, sandbox_id: str, path: str, content: bytes) -> Dict[str, Any]:
        """写入容器内单个文件（put_archive 到父目录，自动建目录）。"""
        import io
        import posixpath
        import tarfile

        container_id = await self._resolve_container_id(sandbox_id)
        safe_path = validate_container_path(path)
        if safe_path in ALLOWED_FS_ROOTS:
            raise ValueError(f"不能把根目录当文件写: {safe_path}")
        if len(content) > FS_MAX_FILE_BYTES:
            raise ValueError(f"文件超过大小上限 {FS_MAX_FILE_BYTES} bytes")

        parent = posixpath.dirname(safe_path)
        filename = posixpath.basename(safe_path)

        # 确保父目录存在（仍在白名单内，因为 safe_path 已校验）
        mkdir_result = await self._docker_client.exec_command(
            container_id, f"mkdir -p {shlex.quote(parent)}", timeout=10
        )
        if mkdir_result.exit_code != 0:
            raise InternalError(message=f"创建目录失败: {mkdir_result.stderr.strip()}")

        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w") as tar:
            info = tarfile.TarInfo(name=filename)
            info.size = len(content)
            info.mode = 0o644
            info.uid = 1000
            info.gid = 1000
            tar.addfile(info, io.BytesIO(content))
        tar_stream.seek(0)
        await self._docker_client.put_archive(container_id, parent, tar_stream.read())
        return {"path": safe_path, "size": len(content)}

    async def fs_delete(self, sandbox_id: str, path: str) -> Dict[str, Any]:
        """删除容器内文件或目录（白名单根目录本身不可删）。"""
        container_id = await self._resolve_container_id(sandbox_id)
        safe_path = validate_container_path(path)
        if safe_path in ALLOWED_FS_ROOTS:
            raise ValueError(f"不能删除白名单根目录: {safe_path}")

        result = await self._docker_client.exec_command(
            container_id,
            f"rm -rf -- {shlex.quote(safe_path)}",
            timeout=30,
        )
        if result.exit_code != 0:
            raise InternalError(message=f"删除失败: {result.stderr.strip()}")
        return {"path": safe_path, "deleted": True}

    # ===== 运行时 pip 装包（session 沙箱） =====

    async def install_packages(
        self,
        sandbox_id: str,
        packages: List[str],
        timeout: int = 300,
    ) -> Dict[str, Any]:
        """
        在 session 沙箱内 pip install --user 安装包。

        前置条件：egress_mode=proxy（物理禁网下 pypi 不可达，直接报错）。
        安装目标是用户 site（/home/sandbox/.local，tmpfs 可写），
        成功后通过 kernel 执行 sys.path 修补，新包立即可 import
        （kernel 启动时用户 site 目录可能还不存在、不在 sys.path 上）。
        """
        if getattr(settings.network, "egress_mode", "none") != "proxy":
            raise ValueError(
                "pip 装包需要 SANDBOX_NETWORK__EGRESS_MODE=proxy（白名单代理出站）。"
                "当前为物理禁网模式，pypi 不可达。"
            )

        specs = validate_package_specs(packages)
        container_id = await self._resolve_container_id(sandbox_id)

        proxy_url = settings.network.egress_proxy_url
        env = {
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
            "NO_PROXY": "localhost,127.0.0.1",
            "HOME": "/home/sandbox",
            "TMPDIR": "/tmp",
        }

        # 1. 确保用户 site 目录存在
        ensure_site = await self._docker_client.exec_command(
            container_id,
            [
                "python", "-c",
                "import site,os;usp=site.getusersitepackages();os.makedirs(usp,exist_ok=True);print(usp)",
            ],
            environment=env,
            timeout=15,
        )
        if ensure_site.exit_code != 0:
            raise InternalError(message=f"准备用户 site 目录失败: {ensure_site.stderr.strip()}")
        user_site = ensure_site.stdout.strip().splitlines()[-1] if ensure_site.stdout.strip() else ""

        # 2. pip install --user（列表形式传参，不经 shell）
        start = time.monotonic()
        pip_cmd = [
            "python", "-m", "pip", "install",
            "--user", "--no-cache-dir",
            "--disable-pip-version-check", "--no-warn-script-location",
            *specs,
        ]
        result = await self._docker_client.exec_command(
            container_id, pip_cmd, environment=env, timeout=timeout
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)

        if result.exit_code != 0:
            return {
                "success": False,
                "packages": specs,
                "stdout": result.stdout[-4096:],
                "stderr": result.stderr[-4096:],
                "error": f"pip install 退出码 {result.exit_code}",
                "execution_time_ms": elapsed_ms,
            }

        # 3. kernel 进程 sys.path 修补（有状态执行路径直达 kernel 父进程，
        #    对后续所有执行与 fork 生效）。
        #    必须注入 pip 实际安装到的 user_site 具体路径：kernel 进程的
        #    HOME（如 /workspace）与装包 exec 的 HOME（/home/sandbox）不同，
        #    在 kernel 里重算 site.getusersitepackages() 会得到错误目录。
        fixup_code = (
            f"import sys\n"
            f"_usp = {user_site!r}\n"
            "if _usp and _usp not in sys.path:\n"
            "    sys.path.insert(0, _usp)\n"
        )
        try:
            await self.execute_code(sandbox_id, fixup_code, timeout=15)
        except Exception as e:
            logger.warning(f"kernel sys.path 修补失败（包已安装，但 import 可能需重建会话）: {e}")

        return {
            "success": True,
            "packages": specs,
            "user_site": user_site,
            "stdout": result.stdout[-4096:],
            "stderr": result.stderr[-2048:],
            "error": None,
            "execution_time_ms": elapsed_ms,
        }
