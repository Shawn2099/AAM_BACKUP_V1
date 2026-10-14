"""Lab fixtures (pytest discovers fixtures from conftest.py only)."""
from __future__ import annotations

# WP16 test isolation (see test_support/sandbox_config.py): must precede any
# project import so CONFIG_PATH resolves to the sandbox, not config.yaml.
from test_support import sandbox_config

sandbox_config.install()

from audit_lab import labclock  # noqa: F401
from audit_lab.labconf import (  # noqa: F401
    LAB_ROOT,
    RCLONE_EXE,
    ROBOCOPY_EXE,
    _lab_root,
    assert_lab_path,
    build_config,
    md5,
    patch_cloud_modules_to_local,
    record,
    robocopy_local,
    run,
    sandbox,
    unc_nas,
    write_file,
    write_local_rclone_config,
)
