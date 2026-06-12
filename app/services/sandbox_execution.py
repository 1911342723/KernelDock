"""
沙箱执行 Mixin：有状态执行、流式执行、无状态（池借用/共享租约/临时容器）执行，
以及数据注入、工作空间清理与 kernel 命名空间重置。

由 SandboxManager 组合使用；方法内通过 self 访问管理器持有的组件。
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional

from ..config import settings
from ..exceptions import InternalError, SandboxNotFoundError
from .sandbox_models import _SandboxRecord

if TYPE_CHECKING:
    from ..infrastructure.container_pool import ContainerPool
    from ..infrastructure.docker_client import DockerClient
    from ..infrastructure.resource_limiter import ResourceLimiter
    from ..infrastructure.security_policy import SecurityPolicy

logger = logging.getLogger(__name__)


class SandboxExecutionMixin:
    """SandboxManager 的代码执行职责。"""

    if TYPE_CHECKING:
        _lock: asyncio.Lock
        _sandboxes: Dict[str, _SandboxRecord]
        _docker_client: "DockerClient"
        _container_pool: Optional["ContainerPool"]
        _resource_limiter: "ResourceLimiter"
        _security_policy: "SecurityPolicy"
        _docker_image: str

        def _get_egress_kwargs(self) -> Dict[str, Any]: ...
        async def _wait_kernel_ready(self, container_id: str, timeout_seconds: int = 45) -> bool: ...

    def _run_code_validation(self, code: str) -> Optional[str]:
        """
        按 settings.validation_mode 执行 AST 静态检查。

        Returns:
            需要拦截时返回问题描述字符串；放行（通过/warn/off）返回 None。
        """
        mode = getattr(settings, "validation_mode", "warn")
        if mode == "off":
            return None

        from .code_validator import validate_code
        validation_result = validate_code(code)
        if validation_result.is_valid:
            return None

        issues_msg = "; ".join(
            f"Line {i.line}: {i.message}" for i in validation_result.issues
        )
        if mode == "block":
            return issues_msg

        logger.warning(f"代码静态检查告警（warn 模式放行）: {issues_msg}")
        return None

    async def execute_code(
        self,
        sandbox_id: str,
        code: str,
        timeout: int = 300,
        pre_load_parquet: Optional[Dict[str, str]] = None,
        bootstrap_source: Optional[str] = None,
        context_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        在沙箱中执行代码

        使用 CodeExecutor 在 Docker 容器内执行代码，支持图表和表格捕获。

        multi-table-analysis (executor_protocol_multitable.md):
        - ``pre_load_parquet``: ``{TableRef: base64(parquet bytes)}``，调用方
          负责与 ``bootstrap_source`` 成对提供。
        - ``bootstrap_source``: DataLoaderBootstrap Python 源码，透传到
          ``CodeExecutor.execute_in_container``，由 kernel 在 user code
          之前 exec。
        - ``context_id``: kernel 上下文 ID，同一沙箱内多上下文隔离。

        Args:
            sandbox_id: 沙箱 ID
            code: Python 代码
            timeout: 执行超时（秒）
            pre_load_parquet: TableRef → base64 parquet 字节（可选）
            bootstrap_source: DataLoaderBootstrap 源码（可选）

        Returns:
            执行结果字典，包含:
            - success: 是否成功
            - output: 输出内容
            - stdout: 标准输出
            - stderr: 标准错误
            - charts: 图表列表（SVG base64）
            - tables: 表格数据列表
            - images: 图片路径列表
            - error: 错误信息（如果有）
            - execution_time_ms: 执行时间（毫秒）

        Raises:
            SandboxNotFoundError: 沙箱不存在
        """
        # 导入 CodeExecutor
        from ..executor import CodeExecutor

        # 获取沙箱记录
        async with self._lock:
            record = self._sandboxes.get(sandbox_id)
            if record is None:
                raise SandboxNotFoundError(sandbox_id=sandbox_id)

            # 更新最后活动时间
            record.info.last_activity = datetime.now()

        container_id = record.info.container_id

        # 使用 CodeExecutor 执行代码
        # 容器内的数据目录和输出目录路径
        container_data_dir = "/data"
        container_output_dir = "/output"

        # 代码静态检查（AST）。安全边界由容器隔离保证，
        # 静态检查按 validation_mode 决定拦截/告警/关闭。
        issues_msg = self._run_code_validation(code)
        if issues_msg is not None:
            return {
                "success": False,
                "output": "",
                "stdout": "",
                "stderr": f"代码安全验证失败: {issues_msg}",
                "charts": [],
                "tables": [],
                "images": [],
                "error": f"CodeValidationError: {issues_msg}",
                "execution_time_ms": 0
            }

        # multi-table-analysis: 把 pre_load_parquet 落盘到容器 /data/<ref>.parquet
        if pre_load_parquet:
            if not bootstrap_source:
                return {
                    "success": False,
                    "output": "",
                    "stdout": "",
                    "stderr": "pre_load_parquet provided without bootstrap_source",
                    "charts": [],
                    "tables": [],
                    "images": [],
                    "error": "ProtocolError: pre_load_parquet requires bootstrap_source",
                    "execution_time_ms": 0,
                }
            await self._ensure_materialized_pre_load_parquet(
                container_id,
                pre_load_parquet,
            )

        executor = CodeExecutor(self._docker_client)
        try:
            result = await executor.execute_in_container(
                container_id=container_id,
                code=code,
                data_dir=container_data_dir,
                output_dir=container_output_dir,
                timeout=timeout,
                bootstrap_source=bootstrap_source,
                context_id=context_id,
            )
            return result
        finally:
            # 注意：不关闭 executor，因为它使用的是共享的 docker_client
            pass

    async def stream_execute_code(
        self,
        sandbox_id: str,
        code: str,
        timeout: int = 300,
        pre_load_parquet: Optional[Dict[str, str]] = None,
        bootstrap_source: Optional[str] = None,
        context_id: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """流式执行代码，返回 kernel 发出的事件流。"""
        from ..executor import CodeExecutor

        async with self._lock:
            record = self._sandboxes.get(sandbox_id)
            if record is None:
                raise SandboxNotFoundError(sandbox_id=sandbox_id)
            record.info.last_activity = datetime.now()

        issues_msg = self._run_code_validation(code)
        if issues_msg is not None:
            yield {
                "type": "error",
                "error": f"CodeValidationError: {issues_msg}",
            }
            yield {
                "type": "done",
                "success": False,
                "error": f"CodeValidationError: {issues_msg}",
                "execution_time_ms": 0,
                "context_id": context_id,
                "timed_out": False,
            }
            return

        if pre_load_parquet:
            if not bootstrap_source:
                yield {
                    "type": "error",
                    "error": "ProtocolError: pre_load_parquet requires bootstrap_source",
                }
                yield {
                    "type": "done",
                    "success": False,
                    "error": "ProtocolError: pre_load_parquet requires bootstrap_source",
                    "execution_time_ms": 0,
                    "context_id": context_id,
                    "timed_out": False,
                }
                return
            await self._ensure_materialized_pre_load_parquet(
                record.info.container_id,
                pre_load_parquet,
            )

        executor = CodeExecutor(self._docker_client)
        async for event in executor.stream_in_container(
            container_id=record.info.container_id,
            code=code,
            timeout=timeout,
            bootstrap_source=bootstrap_source,
            context_id=context_id,
        ):
            yield event

    async def _materialize_pre_load_parquet(
        self,
        container_id: str,
        pre_load_parquet: Dict[str, str],
    ) -> None:
        """把 ``{ref: base64}`` 解码后写入容器 ``/data/<ref>.parquet``。

        见 executor_protocol_multitable.md §"Server-Side Handler Requirements"：
        - 解码失败 → 抛 ``InternalError``，调用方负责包装成错误响应；
        - 目录需要先确保存在（容器启动时已经 mkdir /data，但无状态 temp
          容器场景下也一并处理）；
        - 优先走 put_archive，失败回退到分块 echo+base64（复用
          :meth:`_inject_data_files` 的同款策略）。
        """
        import base64 as _b64
        import tarfile
        import io

        if not pre_load_parquet:
            return

        # 先确保 /data 目录存在（对无状态临时容器而言）
        try:
            await self._docker_client.exec_command(
                container_id,
                "mkdir -p /data",
                timeout=5,
            )
        except Exception:
            pass

        # 收集解码结果（把失败放在落盘之前抛出，避免只写了一部分）
        decoded: Dict[str, bytes] = {}
        for ref, b64_content in pre_load_parquet.items():
            safe_ref = os.path.basename(ref)
            if not safe_ref or safe_ref != ref:
                raise InternalError(
                    message=f"pre_load_parquet: invalid table_ref {ref!r}"
                )
            try:
                decoded[safe_ref] = _b64.b64decode(b64_content)
            except Exception as e:
                raise InternalError(
                    message=f"pre_load_parquet: base64 decode failed for {ref}: {e}"
                )

        # [Phase 1 诊断] 写盘前：打印 container_id、ref 列表、字节数总和
        sorted_refs = sorted(decoded.keys())
        total_bytes = sum(len(raw) for raw in decoded.values())
        short_cid = container_id[:12] if container_id else ""
        logger.info(
            f"[SandboxManager.materialize_pre_load] pre-write "
            f"container={short_cid} refs={sorted_refs} "
            f"total_bytes={total_bytes}"
        )

        async def _log_data_dir_listing() -> None:
            """[Phase 1 诊断] 写盘后：通过 docker exec ls /data 读取目录内容。"""
            try:
                ls_result = await self._docker_client.exec_command(
                    container_id,
                    "ls /data",
                    timeout=5,
                )
                # 把多行输出压成单行便于 grep
                data_listing = ls_result.stdout.strip().replace("\n", " ")
                logger.info(
                    f"[SandboxManager.materialize_pre_load] post-write "
                    f"container={short_cid} /data={data_listing!r}"
                )
            except Exception as err:
                logger.info(
                    f"[SandboxManager.materialize_pre_load] post-write "
                    f"container={short_cid} /data=<exec_failed: {err}>"
                )

        # 优先 put_archive
        try:
            tar_stream = io.BytesIO()
            with tarfile.open(fileobj=tar_stream, mode="w") as tar:
                for ref, raw in decoded.items():
                    info = tarfile.TarInfo(name=f"{ref}.parquet")
                    info.size = len(raw)
                    info.mode = 0o644
                    info.uid = 1000
                    info.gid = 1000
                    tar.addfile(info, io.BytesIO(raw))
            tar_stream.seek(0)
            await self._docker_client.put_archive(
                container_id, "/data", tar_stream.read()
            )
            logger.debug(
                f"[pre_load_parquet] put_archive 写入 {len(decoded)} 张表 -> {short_cid}"
            )
            await _log_data_dir_listing()
            return
        except Exception as e:
            logger.warning(
                f"[pre_load_parquet] put_archive 失败，回退 echo+base64: "
                f"container={short_cid} error={e!r}"
            )

        # 回退 echo+base64
        # Docker exec 通过 shell 传超长 echo 命令时在部分 Docker Desktop/
        # named-pipe 场景会偶发失败；把 chunk 控制在更保守的长度。
        RAW_CHUNK_SIZE = 8192
        for ref, raw in decoded.items():
            target = f"/data/{ref}.parquet"
            for i in range(0, len(raw), RAW_CHUNK_SIZE):
                chunk = raw[i:i + RAW_CHUNK_SIZE]
                b64_chunk = _b64.b64encode(chunk).decode("ascii")
                op = ">" if i == 0 else ">>"
                result = await self._docker_client.exec_command(
                    container_id,
                    f"echo '{b64_chunk}' | base64 -d {op} {target}",
                    timeout=30,
                )
                if result.exit_code != 0:
                    raise InternalError(
                        message=(
                            f"[pre_load_parquet] chunk write failed ref={ref} "
                            f"chunk={i // RAW_CHUNK_SIZE} exit={result.exit_code} "
                            f"stdout={result.stdout!r} stderr={result.stderr!r}"
                        )
                    )
        await _log_data_dir_listing()

    async def _data_dir_has_materialized_parquet(self, container_id: str) -> bool:
        """Return whether the container currently exposes any parquet under /data."""
        try:
            result = await self._docker_client.exec_command(
                container_id,
                "sh -lc \"ls /data/*.parquet >/dev/null 2>/dev/null\"",
                timeout=5,
            )
        except Exception as err:
            logger.info(
                "[SandboxManager.pre_load_health] container=%s parquet_check_failed=%r",
                container_id[:12],
                err,
            )
            return False

        has_parquet = result.exit_code == 0
        logger.info(
            "[SandboxManager.pre_load_health] container=%s has_parquet=%s",
            container_id[:12],
            has_parquet,
        )
        return has_parquet

    async def _ensure_materialized_pre_load_parquet(
        self,
        container_id: str,
        pre_load_parquet: Dict[str, str],
    ) -> None:
        """Check /data first and only re-materialize parquet when the container is empty."""
        if not pre_load_parquet:
            return

        if await self._data_dir_has_materialized_parquet(container_id):
            return

        logger.info(
            "[SandboxManager.pre_load_health] container=%s /data empty -> materialize_pre_load_parquet",
            container_id[:12],
        )
        await self._materialize_pre_load_parquet(container_id, pre_load_parquet)

    async def execute_stateless(
        self,
        code: str,
        data_files: Optional[Dict[str, str]] = None,
        timeout: int = 30,
        pre_load_parquet: Optional[Dict[str, str]] = None,
        bootstrap_source: Optional[str] = None,
        context_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        无状态执行：借容器 → 注入数据 → 执行代码 → 清理 → 归还容器。

        容器不与任何 session 绑定，执行完毕立即归还池，实现快速轮转。

        multi-table-analysis: 支持 ``pre_load_parquet`` + ``bootstrap_source``
        契约（见 executor_protocol_multitable.md）。若两者都提供，沙箱会：
        1) 先把 ``{ref: base64(parquet)}`` 落到容器 ``/data/<ref>.parquet``；
        2) 在 user code *之前* exec ``bootstrap_source``。

        Args:
            code: Python 代码
            data_files: 数据文件字典 {filename: base64_content}
            timeout: 执行超时（秒）
            pre_load_parquet: {TableRef: base64(parquet bytes)}（可选，多表分析）
            bootstrap_source: DataLoaderBootstrap 源码（可选，多表分析）
            context_id: 内核上下文 ID（可选）

        Returns:
            执行结果字典（与 execute_code 格式一致）
        """
        from ..executor import CodeExecutor

        issues_msg = self._run_code_validation(code)
        if issues_msg is not None:
            return {
                "success": False,
                "output": "",
                "stdout": "",
                "stderr": f"代码安全验证失败: {issues_msg}",
                "charts": [],
                "tables": [],
                "images": [],
                "error": f"CodeValidationError: {issues_msg}",
                "execution_time_ms": 0,
            }

        if pre_load_parquet and not bootstrap_source:
            return {
                "success": False,
                "output": "",
                "stdout": "",
                "stderr": "pre_load_parquet provided without bootstrap_source",
                "charts": [],
                "tables": [],
                "images": [],
                "error": "ProtocolError: pre_load_parquet requires bootstrap_source",
                "execution_time_ms": 0,
            }

        if not self._container_pool:
            raise InternalError(message="容器池未初始化")

        # 0. 共享租约快路径：纯代码执行（无数据注入）共享池容器并发 fork，
        #    不独占借出、不做 per-request 清理（fork 命名空间零残留；
        #    /output 残留由容器 max_age 周期销毁兜底）。
        #    并发密度 = 池大小 × shared_max_per_container。
        if (
            settings.pool.shared_stateless
            and not data_files
            and not pre_load_parquet
        ):
            shared_cid = await self._container_pool.acquire_shared(
                settings.pool.shared_max_per_container
            )
            if shared_cid:
                from ..executor import CodeExecutor as _CE
                try:
                    executor = _CE(self._docker_client)
                    return await executor.execute_in_container(
                        container_id=shared_cid,
                        code=code,
                        data_dir="/data",
                        output_dir="/output",
                        timeout=timeout,
                        bootstrap_source=bootstrap_source,
                        context_id=context_id,
                        isolated=True,
                    )
                finally:
                    await self._container_pool.release_shared(shared_cid)

        # 1. 从池中借一个容器（独占路径：有数据注入或共享容量已满）
        pooled = await self._container_pool.acquire_for_execution()
        if not pooled:
            # 池空，回退到创建临时容器
            logger.warning("容器池为空，创建临时容器执行")
            return await self._execute_stateless_with_temp_container(
                code,
                data_files,
                timeout,
                pre_load_parquet=pre_load_parquet,
                bootstrap_source=bootstrap_source,
                context_id=context_id,
            )

        container_id = pooled.container_id
        try:
            # 2. 注入数据文件
            if data_files:
                await self._inject_data_files(container_id, data_files)
            if pre_load_parquet:
                await self._ensure_materialized_pre_load_parquet(
                    container_id,
                    pre_load_parquet,
                )

            # 3. 执行代码（isolated=True：kernel 内 fork 子进程执行，
            #    父命名空间零污染，归还池前无需 reset）
            executor = CodeExecutor(self._docker_client)
            result = await executor.execute_in_container(
                container_id=container_id,
                code=code,
                data_dir="/data",
                output_dir="/output",
                timeout=timeout,
                bootstrap_source=bootstrap_source,
                context_id=context_id,
                isolated=True,
            )

            if result.get("kernel_unhealthy"):
                cleanup_ok = False
            elif result.get("isolated"):
                # fork 隔离路径：命名空间未被污染，只需清理数据文件
                cleanup_ok = await self._cleanup_and_reset_for_reuse(
                    container_id, reset_kernel=False
                )
            else:
                cleanup_ok = await self._cleanup_and_reset_for_reuse(container_id)
            await self._release_or_destroy_stateless_container(container_id, cleanup_ok)

            return result

        except Exception as e:
            logger.error(f"无状态执行失败: {e}, 容器: {container_id[:12]}")
            cleanup_ok = await self._cleanup_and_reset_for_reuse(container_id)
            await self._release_or_destroy_stateless_container(container_id, cleanup_ok)
            raise

    async def _execute_stateless_with_temp_container(
        self,
        code: str,
        data_files: Optional[Dict[str, str]],
        timeout: int,
        pre_load_parquet: Optional[Dict[str, str]] = None,
        bootstrap_source: Optional[str] = None,
        context_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """池空时创建临时容器执行，执行完销毁。"""
        from ..executor import CodeExecutor

        resource_limits = self._resource_limiter.get_limits()
        resource_kwargs = resource_limits.to_container_create_kwargs()
        security_kwargs = self._security_policy.to_docker_config()
        security_kwargs.pop("pids_limit", None)
        security_kwargs.pop("privileged", None)

        # 无状态容器没有 volume 挂载，需要将 /data 和 /output 也加入 tmpfs
        tmpfs_config = self._security_policy.get_tmpfs_config()
        tmpfs_config["/data"] = "size=500M,mode=1777,uid=1000,gid=1000"
        tmpfs_config["/output"] = "size=500M,mode=1777,uid=1000,gid=1000"
        tmpfs_config["/var/cache/fontconfig"] = "size=10M,mode=1777,uid=1000,gid=1000"

        temp_kwargs: Dict[str, Any] = dict(self._get_egress_kwargs())
        if settings.security.use_gvisor:
            temp_kwargs["runtime"] = settings.security.gvisor_runtime

        container_info = await self._docker_client.create_container(
            image=self._docker_image,
            detach=True,
            tmpfs=tmpfs_config,
            **resource_kwargs,
            **security_kwargs,
            **temp_kwargs,
        )
        container_id = container_info.container_id
        await self._docker_client.start_container(container_id)

        # 等待 kernel 就绪：不等就执行必然 relay 失败而回退 docker exec 慢路径
        await self._wait_kernel_ready(container_id, timeout_seconds=45)

        try:
            if data_files:
                await self._inject_data_files(container_id, data_files)
            if pre_load_parquet:
                await self._ensure_materialized_pre_load_parquet(
                    container_id,
                    pre_load_parquet,
                )

            executor = CodeExecutor(self._docker_client)
            result = await executor.execute_in_container(
                container_id=container_id,
                code=code,
                data_dir="/data",
                output_dir="/output",
                timeout=timeout,
                bootstrap_source=bootstrap_source,
                context_id=context_id,
                isolated=True,
            )
            return result
        finally:
            try:
                await self._docker_client.stop_container(container_id, timeout=5)
                await self._docker_client.remove_container(container_id, force=True)
            except Exception as e:
                logger.warning(f"清理临时容器失败: {e}")

    async def _cleanup_and_reset_for_reuse(
        self, container_id: str, reset_kernel: bool = True
    ) -> bool:
        try:
            await self._cleanup_container_workspace(container_id)
            if not reset_kernel:
                return True
            return await self._reset_kernel_namespace(container_id)
        except Exception as e:
            logger.warning(f"容器 {container_id[:12]} 清理或重置失败: {e}")
            return False

    async def _release_or_destroy_stateless_container(
        self, container_id: str, reusable: bool
    ) -> None:
        if reusable:
            await self._container_pool.release(container_id)
            return
        logger.warning(f"容器 {container_id[:12]} 不可安全复用，销毁并补池")
        try:
            await self._docker_client.stop_container(container_id, timeout=5)
            await self._docker_client.remove_container(container_id, force=True)
        except Exception as e:
            logger.warning(f"销毁不可复用容器失败: {container_id[:12]}, {e}")
        asyncio.create_task(self._container_pool.replenish())

    async def _inject_data_files(
        self, container_id: str, data_files: Dict[str, str]
    ) -> None:
        """
        将 base64 编码的数据文件注入容器的 /data 目录。

        优先使用 Docker put_archive API（单次 tar 传输，零 Base64 分块损耗）；
        如果 put_archive 失败，回退到分块 echo+base64。

        Args:
            container_id: 容器 ID
            data_files: {filename: base64_content}
        """
        import base64 as b64
        import tarfile
        import io

        logger.info(f"[数据注入] 开始注入 {len(data_files)} 个文件到容器 {container_id[:12]}")
        for fname in data_files.keys():
            logger.info(f"[数据注入] 文件: {fname}, base64长度: {len(data_files[fname])}")

        await self._docker_client.exec_command(
            container_id,
            "find /data -mindepth 1 -maxdepth 1 -type f -delete",
            timeout=10,
        )

        # --- 优先方案：将所有文件打包为 tar 一次性传输 ---
        try:
            tar_stream = io.BytesIO()
            total_size = 0
            file_count = 0

            with tarfile.open(fileobj=tar_stream, mode="w") as tar:
                for filename, b64_content in data_files.items():
                    safe_name = os.path.basename(filename)
                    if not safe_name:
                        continue

                    try:
                        raw_data = b64.b64decode(b64_content)
                    except Exception as e:
                        logger.warning(f"解码数据文件失败: {safe_name}, {e}")
                        continue

                    info = tarfile.TarInfo(name=safe_name)
                    info.size = len(raw_data)
                    info.mode = 0o644
                    info.uid = 1000
                    info.gid = 1000
                    tar.addfile(info, io.BytesIO(raw_data))
                    total_size += len(raw_data)
                    file_count += 1

            tar_stream.seek(0)

            await self._docker_client.put_archive(
                container_id, "/data", tar_stream.read()
            )

            logger.debug(
                f"put_archive 注入 {file_count} 个数据文件 "
                f"({total_size} bytes) -> {container_id[:12]}"
            )
            return

        except Exception as e:
            logger.debug(f"put_archive 不可用（预期行为：只读 rootfs），使用 echo+base64: {e}")

        # --- 回退方案：分块 echo+base64 ---
        RAW_CHUNK_SIZE = 24576

        for filename, b64_content in data_files.items():
            safe_name = os.path.basename(filename)
            if not safe_name:
                continue

            target_path = f"/data/{safe_name}"

            try:
                raw_data = b64.b64decode(b64_content)
            except Exception as e:
                logger.warning(f"解码数据文件失败: {safe_name}, {e}")
                continue

            for i in range(0, len(raw_data), RAW_CHUNK_SIZE):
                chunk = raw_data[i:i + RAW_CHUNK_SIZE]
                b64_chunk = b64.b64encode(chunk).decode('ascii')
                op = '>' if i == 0 else '>>'
                result = await self._docker_client.exec_command(
                    container_id,
                    f"echo '{b64_chunk}' | base64 -d {op} {target_path}",
                    timeout=30,
                )
                if result.exit_code != 0:
                    logger.error(
                        f"写入数据文件分块失败: {safe_name}, "
                        f"chunk {i // RAW_CHUNK_SIZE}, "
                        f"exit={result.exit_code}, stderr={result.stderr}"
                    )
                    break

            logger.debug(
                f"echo+base64 注入数据文件: {safe_name} ({len(raw_data)} bytes, "
                f"{(len(raw_data) - 1) // RAW_CHUNK_SIZE + 1} chunks) "
                f"-> {container_id[:12]}"
            )

    async def _cleanup_container_workspace(self, container_id: str) -> None:
        """
        清理容器内所有可写挂载点的内容，为下次复用做准备。

        覆盖 /data, /output, /tmp, /var/tmp, /run, /home/sandbox 等
        tmpfs 和 bind-mount 目录，防止跨用户数据泄漏。

        Args:
            container_id: 容器 ID
        """
        try:
            is_running = await self._docker_client.is_container_running(container_id)
            if not is_running:
                logger.warning(f"容器未运行，跳过清理: {container_id[:12]}")
                raise InternalError(message=f"容器 {container_id[:12]} 未运行")
        except InternalError:
            raise
        except Exception as e:
            logger.warning(f"检查容器状态失败: {container_id[:12]}, {e}")
            raise

        cleanup_cmd = (
            "find /data -mindepth 1 -delete 2>/dev/null; "
            "find /output -mindepth 1 -delete 2>/dev/null; "
            "find /tmp -mindepth 1 -delete 2>/dev/null; "
            "find /var/tmp -mindepth 1 -delete 2>/dev/null; "
            "find /run -mindepth 1 -delete 2>/dev/null; "
            "find /home/sandbox -mindepth 1 -delete 2>/dev/null; "
            "true"
        )
        cleanup_timeout = settings.fire_and_forget.cleanup_timeout
        try:
            result = await self._docker_client.exec_command(
                container_id, cleanup_cmd, timeout=cleanup_timeout
            )
            logger.info(
                f"容器工作空间已清理: {container_id[:12]}, "
                f"exit_code={result.exit_code}, "
                f"清理目录: /data, /output, /tmp, /var/tmp, /run, /home/sandbox"
            )
        except Exception as e:
            logger.warning(f"清理容器工作空间失败: {container_id[:12]}, {e}")
            raise InternalError(message=f"清理容器工作空间失败: {str(e)}", original_error=e)

    async def _reset_kernel_namespace(self, container_id: str) -> bool:
        """
        Reset the Kernel Server namespace inside the container to prevent
        cross-user variable leakage in stateless execution mode.

        Sends a reset request via kernel_relay. Returns False on failure so
        callers can avoid returning a contaminated container to the pool.
        """
        try:
            reset_cmd = (
                'python -c "'
                "import socket,json;"
                "s=socket.socket();"
                "s.connect(('127.0.0.1',9999));"
                "p=json.dumps({'action':'reset'}).encode();"
                "s.sendall(len(p).to_bytes(4,'big')+p);"
                "s.recv(4096);"
                "s.close()"
                '"'
            )
            result = await self._docker_client.exec_command(
                container_id, reset_cmd, timeout=10
            )
            if result.exit_code == 0:
                logger.debug(f"Kernel namespace reset: {container_id[:12]}")
                return True
            else:
                logger.warning(
                    f"Kernel namespace reset failed: {container_id[:12]}, "
                    f"exit={result.exit_code}"
                )
                return False
        except Exception as e:
            logger.warning(f"Kernel namespace reset error: {container_id[:12]}, {e}")
            return False
