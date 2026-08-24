"""Tests for deploy/test_config.py — config validation exit codes (M10).

The script is the operator-facing "is my config valid?" tool. Batch scripts
check %ERRORLEVEL%, so an invalid config MUST exit non-zero. The original
script printed [ERROR] but returned exit code 0 — automation could not
detect failure (IMPLEMENTATION_PLAN.md Fix 3).
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "deploy_test_config", ROOT / "deploy" / "test_config.py"
)
mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("deploy_test_config", mod)
_spec.loader.exec_module(mod)


class TestExitCodes:
    def test_invalid_config_returns_exit_code_1(self):
        """Validation failure must produce exit code 1, not a silent 0/None."""
        if not hasattr(mod, "validate"):
            pytest.fail("M10 RED: validate() not implemented yet")
        with patch.object(mod, "validate", side_effect=ValueError("bad bucket")), \
             patch.object(mod, "_pause"):
            assert mod.main() == 1

    def test_valid_config_returns_exit_code_0(self):
        if not hasattr(mod, "validate"):
            pytest.fail("M10 RED: validate() not implemented yet")
        fake_cfg = MagicMock()
        with patch.object(mod, "validate", return_value=fake_cfg), \
             patch.object(mod, "_pause"):
            assert mod.main() == 0

    def test_missing_config_returns_exit_code_1(self):
        import inspect

        if not hasattr(mod, "main"):
            pytest.fail("M10 RED: main() missing")
        params = inspect.signature(mod.main).parameters
        if "config_path" not in params:
            pytest.fail(
                "M10 RED: main() must accept a config_path parameter "
                "so the missing-file branch is testable"
            )
        missing = str(ROOT / "deploy" / "definitely_not_here.yaml")
        with patch.object(mod, "_pause"):
            assert mod.main(config_path=missing) == 1

    def test_pause_survives_closed_stdin(self):
        if not hasattr(mod, "_pause"):
            pytest.fail("M10 RED: _pause() not implemented yet")
        with patch("builtins.input", side_effect=EOFError):
            mod._pause()  # must not raise


class TestValidateContract:
    def test_validate_wraps_load_config_and_raises_on_bad_yaml(self, tmp_path):
        if not hasattr(mod, "validate"):
            pytest.fail("M10 RED: validate() not implemented yet")
        bad = tmp_path / "config.yaml"
        bad.write_text("paths: {}\n", encoding="utf-8")  # missing required fields
        with pytest.raises(Exception):
            mod.validate(str(bad))

    def test_validate_returns_config_on_success(self, tmp_path, sample_yaml_config):
        if not hasattr(mod, "validate"):
            pytest.fail("M10 RED: validate() not implemented yet")
        good = tmp_path / "config.yaml"
        good.write_text(sample_yaml_config, encoding="utf-8")
        cfg = mod.validate(str(good))
        assert cfg is not None
