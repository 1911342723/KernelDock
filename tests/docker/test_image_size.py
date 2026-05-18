import json
import shutil
import subprocess

import pytest


IMAGE_NAME = "kerneldock-sandbox:v2.0.0"
MAX_IMAGE_MB = 3072


def _inspect_image_size_bytes(image_name: str) -> int | None:
    if shutil.which("docker") is None:
        return None
    result = subprocess.run(
        ["docker", "image", "inspect", image_name, "--format", "{{json .Size}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return int(json.loads(result.stdout.strip()))


def test_sandbox_image_size_guard():
    size_bytes = _inspect_image_size_bytes(IMAGE_NAME)
    if size_bytes is None:
        pytest.skip(f"docker unavailable or image missing: {IMAGE_NAME}")

    size_mb = size_bytes / (1024 * 1024)
    assert size_mb <= MAX_IMAGE_MB, f"image {IMAGE_NAME} is {size_mb:.1f}MB > {MAX_IMAGE_MB}MB"
