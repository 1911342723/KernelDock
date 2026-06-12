import os
import sys
import importlib.util
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODULE_PATH = Path(__file__).resolve().parents[1] / "sandbox_runtime" / "setup.py"
MODULE_SPEC = importlib.util.spec_from_file_location("sandbox_runtime_setup_test", MODULE_PATH)
runtime_setup_module = importlib.util.module_from_spec(MODULE_SPEC)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
MODULE_SPEC.loader.exec_module(runtime_setup_module)


def test_setup_eager_imports_are_idempotent():
    runtime_setup_module._initialized = False
    runtime_setup_module._eager_imported_modules = ()

    with patch.object(runtime_setup_module, "_setup_encoding", return_value=None):
        runtime_setup_module.setup(suppress_warnings=True)
        first_status = runtime_setup_module.get_eager_import_status()

        runtime_setup_module.setup(suppress_warnings=True)
        second_status = runtime_setup_module.get_eager_import_status()

    assert first_status["count"] > 0
    assert "pandas" in first_status["modules"]
    assert "numpy" in first_status["modules"]
    assert first_status == second_status