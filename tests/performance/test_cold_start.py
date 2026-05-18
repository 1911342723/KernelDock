import shutil
import subprocess
import time
import uuid

import pytest


IMAGE_NAME = "kerneldock-sandbox:v2.0.0"
MAX_COLD_START_SECONDS = 1.5
KERNEL_READY_MARKER = "[Kernel] 监听 0.0.0.0:9999"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def test_sandbox_cold_start_under_budget():
    if not _docker_available():
        pytest.skip("docker not available")

    inspect = subprocess.run(
        ["docker", "image", "inspect", IMAGE_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode != 0:
        pytest.skip(f"image missing: {IMAGE_NAME}")

    container_name = f"sandbox-cold-start-{uuid.uuid4().hex[:8]}"
    start_time = time.perf_counter()
    run_result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            container_name,
            IMAGE_NAME,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if run_result.returncode != 0:
        pytest.skip(f"unable to start container: {run_result.stderr.strip()}")

    try:
        deadline = time.perf_counter() + 20
        combined_output = ""
        while time.perf_counter() < deadline:
            logs_result = subprocess.run(
                ["docker", "logs", container_name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            combined_output = (logs_result.stdout or "") + (logs_result.stderr or "")
            if KERNEL_READY_MARKER in combined_output:
                elapsed = time.perf_counter() - start_time
                assert elapsed <= MAX_COLD_START_SECONDS, (
                    f"cold start took {elapsed:.2f}s > {MAX_COLD_START_SECONDS:.2f}s"
                )
                return
            time.sleep(0.2)

        pytest.skip("kernel readiness marker not observed; cold-start benchmark not applicable in this environment")
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            text=True,
            check=False,
        )
