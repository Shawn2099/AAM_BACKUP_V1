"""P1 C10 (docs/PLAN_P1_RCLONE_LAN_2026-11-25.md §3, §7 C10a/C10b, matrix K1-K3, K-del, K-grep).

The LAN leg moves from robocopy to rclone. These tests pin the end state:

* K1      a deprecated robocopy-era key loads with a warning and is dropped (not an error)
* K2      LanConfig no longer has the robocopy-era fields; the new keys keep their defaults/bounds
* K3      every LanConfig field has help text, no help text is left for removed keys, and
          config.example.yaml carries no removed key and loads with the real model
* K-del   the robocopy functions, the lan manifest module and the robocopy helpers are gone
* K-grep  "robocopy" survives in the product source only in an explicit allow-list

Written before the code change: most of them fail on the current code on purpose.
"""

import ast
import importlib
import io
import tokenize
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import models.config as config_mod
from models.config import AppConfig, DEPRECATED_KEYS, LanConfig, MaintenanceConfig

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "config.example.yaml"

REMOVED_LAN_KEYS = (
    "mt_threads",
    "log_tail_kb",
    "dry_run_timeout_seconds",
    "use_backup_mode",
    "privilege_check_timeout_seconds",
)
REMOVED_MAINTENANCE_KEYS = ("orphan_robocopy_log_max_age_hours",)

# Plan §3 new keys: name -> (default, min, max)
NEW_LAN_KEYS = {
    "transfers": (4, 1, 32),
    "checkers": (4, 1, 32),
    "modify_window_seconds": (2, 0, 10),
    "error_tail_lines": (100, 1, 10000),
    "verify_timeout_seconds": (1800, 60, 14400),
}

UNC_DEST = "\\\\nas\\backup\\FY26-27"


def _base(**overrides) -> dict:
    data = {
        "paths": {"source_drive": "E:\\FY26-27", "lan_destination": UNC_DEST},
        "wol": {"enabled": False},
        "cloud": {"enabled": False},
        "dashboard": {},
    }
    for section, values in overrides.items():
        data.setdefault(section, {}).update(values)
    return data


# ---------------------------------------------------------------- K1

DEPRECATED_SAMPLES = [
    ("lan", "mt_threads", 4),
    ("lan", "log_tail_kb", 100),
    ("lan", "dry_run_timeout_seconds", 900),
    ("lan", "use_backup_mode", True),
    ("lan", "privilege_check_timeout_seconds", 60),
    ("maintenance", "orphan_robocopy_log_max_age_hours", 24),
]


@pytest.mark.parametrize("section, key, value", DEPRECATED_SAMPLES)
def test_k1_deprecated_key_is_registered(section, key, value):
    assert key in DEPRECATED_KEYS.get(section, frozenset()), (
        f"{section}.{key} must be listed in models.config.DEPRECATED_KEYS"
    )


@pytest.mark.parametrize("section, key, value", DEPRECATED_SAMPLES)
def test_k1_deprecated_key_loads_with_warning_and_is_dropped(tmp_path, section, key, value):
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.safe_dump(_base(**{section: {key: value}})), encoding="utf-8"
        )
        cfg = config_mod.load_config(str(cfg_file))   # must not raise
    finally:
        logger.remove(sink_id)

    assert not hasattr(getattr(cfg, section), key), f"{section}.{key} must be dropped"
    assert any(key in m and "deprecated" in m.lower() for m in messages), (
        f"no 'deprecated' warning naming {section}.{key}; warnings seen: {messages}"
    )


# ---------------------------------------------------------------- K2

@pytest.mark.parametrize("key", REMOVED_LAN_KEYS)
def test_k2_removed_lan_field_is_gone(key):
    assert key not in LanConfig.model_fields, f"LanConfig.{key} must be removed"


@pytest.mark.parametrize("key", REMOVED_MAINTENANCE_KEYS)
def test_k2_removed_maintenance_field_is_gone(key):
    assert key not in MaintenanceConfig.model_fields, f"MaintenanceConfig.{key} must be removed"


@pytest.mark.parametrize("key, spec", NEW_LAN_KEYS.items(), ids=list(NEW_LAN_KEYS))
def test_k2_new_lan_key_default(key, spec):
    default, _lo, _hi = spec
    assert LanConfig.model_fields[key].default == default


@pytest.mark.parametrize("key, spec", NEW_LAN_KEYS.items(), ids=list(NEW_LAN_KEYS))
def test_k2_new_lan_key_bounds_accepted_at_edges(key, spec):
    _default, lo, hi = spec
    assert getattr(LanConfig(**{key: lo}), key) == lo
    assert getattr(LanConfig(**{key: hi}), key) == hi


@pytest.mark.parametrize("key, spec", NEW_LAN_KEYS.items(), ids=list(NEW_LAN_KEYS))
def test_k2_new_lan_key_bounds_rejected_outside_range(key, spec):
    _default, lo, hi = spec
    with pytest.raises(ValidationError):
        LanConfig(**{key: lo - 1})
    with pytest.raises(ValidationError):
        LanConfig(**{key: hi + 1})


# ---------------------------------------------------------------- K3

def test_k3_every_lan_field_has_help():
    from core.config_help import HELP

    missing = [f"lan.{k}" for k in LanConfig.model_fields if f"lan.{k}" not in HELP]
    assert not missing, f"LanConfig fields without a config_help entry: {missing}"


def test_k3_no_help_left_for_removed_lan_or_maintenance_keys():
    from core.config_help import HELP

    stale = [f"lan.{k}" for k in REMOVED_LAN_KEYS if f"lan.{k}" in HELP]
    stale += [f"maintenance.{k}" for k in REMOVED_MAINTENANCE_KEYS if f"maintenance.{k}" in HELP]
    assert not stale, f"config_help entries for removed keys (delete them): {stale}"


def test_k3_no_help_for_lan_key_the_model_lacks():
    from core.config_help import HELP

    stale = [k for k in HELP if k.startswith("lan.") and k[4:] not in LanConfig.model_fields]
    assert not stale, f"config_help has lan.* entries LanConfig does not define: {stale}"


def test_k3_example_has_no_removed_key():
    example = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    found = [f"lan.{k}" for k in REMOVED_LAN_KEYS if k in (example.get("lan") or {})]
    found += [
        f"maintenance.{k}" for k in REMOVED_MAINTENANCE_KEYS
        if k in (example.get("maintenance") or {})
    ]
    assert not found, f"config.example.yaml still documents removed keys: {found}"


def test_k3_example_loads_with_real_model():
    example = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    AppConfig(**example)   # strict: any leftover removed key fails here


# ---------------------------------------------------------------- K-del

K_DEL_NAMES = [
    ("core.lan_sync", "build_robocopy_command"),
    ("core.lan_sync", "decide_lan_result"),
    ("core.lan_sync", "scan_robocopy_log"),
    ("core.lan_sync", "run_lan_sync"),
    ("flow", "lan_snapshot_before_task"),
    ("flow", "lan_snapshot_after_task"),
    ("flow", "lan_record_task"),
    ("flow", "lan_publish_artifact_task"),
    ("flow", "lan_deletes_of"),
    ("core.permissions", "check_backup_privilege"),
    ("test_support.fake_tools", "robocopy_log"),
]


@pytest.mark.parametrize("module, name", K_DEL_NAMES, ids=[f"{m}.{n}" for m, n in K_DEL_NAMES])
def test_k_del_symbol_is_removed(module, name):
    mod = importlib.import_module(module)
    assert not hasattr(mod, name), f"{module}.{name} must be deleted with the robocopy code"


def test_k_del_lan_manifest_module_is_gone():
    with pytest.raises(ImportError):
        importlib.import_module("core.lan_manifest")


# ---------------------------------------------------------------- K-grep

SCAN_GLOBS = ["core/*.py", "models/*.py", "flow.py", "watchdog.py", "serve.py"]
# Whole files that may keep the word: the old UTF-16 robocopy log reader for support bundles.
ALLOWED_FILES = {"core/support_bundle.py"}
# A line that mentions DEPRECATED is allowed (the DEPRECATED_KEYS notes).
ALLOWED_MARKER = "DEPRECATED"
# Comment or docstring lines that carry one of these words are allowed.
ALLOWED_WORDS = ("legacy", "formerly", "replaced")


def _docstring_line_ranges(src: str) -> list[tuple[int, int]]:
    ranges = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ranges.append((body[0].lineno, body[0].end_lineno))
    return ranges


def _robocopy_offenders(path: Path) -> list[str]:
    rel = path.relative_to(REPO).as_posix()
    if rel in ALLOWED_FILES:
        return []
    src = path.read_text(encoding="utf-8", errors="replace")
    lines = src.splitlines()
    hits = [n for n, line in enumerate(lines, 1) if "robocopy" in line.lower()]
    if not hits:
        return []

    # Line numbers whose comment mentions robocopy together with an allowed word.
    allowed_comment_lines: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            text = tok.string.lower()
            if "robocopy" in text and any(w in text for w in ALLOWED_WORDS):
                allowed_comment_lines.add(tok.start[0])

    doc_ranges = _docstring_line_ranges(src)
    offenders = []
    for n in hits:
        line = lines[n - 1]
        low = line.lower()
        if ALLOWED_MARKER in line:
            continue
        if n in allowed_comment_lines:
            continue
        in_doc = any(lo <= n <= hi for lo, hi in doc_ranges)
        if in_doc and any(w in low for w in ALLOWED_WORDS):
            continue
        offenders.append(f"{rel}:{n}: {line.strip()}")
    return offenders


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for pattern in SCAN_GLOBS:
        files.extend(sorted(REPO.glob(pattern)))
    return files


def test_k_grep_robocopy_only_in_allow_list():
    offenders: list[str] = []
    for path in _scanned_files():
        offenders.extend(_robocopy_offenders(path))
    assert not offenders, (
        f"{len(offenders)} line(s) mention robocopy outside the allow-list "
        "(allowed: DEPRECATED lines, core/support_bundle.py, comment/docstring lines "
        "with legacy/formerly/replaced):\n" + "\n".join(offenders)
    )
