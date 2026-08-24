# Tier‑1 Fix Implementation Plan — AAM_BACKUP_V1

Companion to `CODE_AUDIT.md`. Scope: **H1, H2, M10, M7, M6** only. No scope creep; Tier‑2/3 items explicitly out of scope except where noted as follow‑ups.

---

## 0. April 1 / canary semantics — traced end‑to‑end (the feature contract)

Question answered first because Fix 1 exists to protect this chain:

1. `rollover()` (`core/fy_rollover.py:395`) detects `configured FY != computed FY` → runs final backup of closing FY → gates on success → `create_new_fy_folders()` (`fy_rollover.py:196`).
2. `create_new_fy_folders` creates the **local** FY folder (mandatory — raises on failure) and the **LAN** FY folder (best‑effort), then immediately touches `.AAM_TARGET_MOUNTED` **inside** the new LAN folder (`fy_rollover.py:215‑216`). **Folder and canary are born together.**
3. Without the canary the next LAN backup **must not run**: `lan_preflight.run_lan_dry_run` refuses to `/MIR` an unverified destination (`lan_preflight.py:36‑53`) → pipeline records `LAN_SKIPPED`, sends the alert email, retries nightly until the operator fixes it (message contains the exact recovery command). Cloud is unaffected.
   - This refusal is deliberate anti‑data‑loss design. If the NAS was offline at rollover moment: LAN folder+canary missing → nightly SKIPPED emails with instructions until fixed. Correct behavior, now finally scheduled correctly once H1 is fixed.
4. Idempotency (already probe‑verified per code comments, re‑asserted by our new tests): crash after folder creation but before config update → next daily check re‑detects mismatch → final backup re‑runs (rclone sync is idempotent) → `mkdir(exist_ok=True)` no‑ops → config update converges.
5. Known residual (Tier‑2, NOT in this change): archive transition currently runs before folder/config steps (`fy_rollover.py:450‑460`). Left as‑is per scope discipline; logged as follow‑up.

---

## Fix 1 — H1: rollover deployment never served

**File:** `launch.py` (lines 242‑248)

### Change
Add the fifth deployment to the call — nothing else:

```python
serve(
    cloud_deployment,
    lan_deployment,
    report_deployment,
    monthly_deployment,
    rollover_deployment,          # ← the fix
    pause_on_shutdown=False,
)
```

### Why this test shape
`tests/test_serve.py:21‑33` already proves `_deployments()` returns 5. The untested half of the contract is *"launch serves everything deployments() produced."* The regression test pins exactly that invariant, so any future 6th deployment added to `deployments()` automatically must be served too.

### Test (add to `tests/test_launch.py`) — RED before fix, GREEN after

```python
class TestServeCallCompleteness:
    """H1 regression: launch.main() must serve EVERY deployment from serve.deployments().
    The original bug: rollover_deployment was created but never passed to serve()."""

    def _run_main(self):
        import launch
        with patch("launch._check_prefect_api", return_value=True), \
             patch("launch._run_dashboard"), \
             patch("launch._ensure_concurrency_limit"), \
             patch("launch._cancel_orphaned_runs"), \
             patch("core.fy_rollover.rollover", return_value=False), \
             patch("prefect.serve") as mock_serve:
            launch.main()
        return mock_serve

    @patch("builtins.print")
    def test_serves_all_deployments(self, _p):
        from serve import deployments as real_deployments
        fake = [object() for _ in range(5)]
        mock_serve = self._run_main()
        # main() imports serve.deployments lazily inside main(); patch at source module
        with patch("serve.deployments", return_value=fake):
            # re-run to bind the patched factory deterministically
            mock_serve2 = self._run_main()
        args, kwargs = mock_serve2.call_args
        served = [a for a in args if not isinstance(a, bool)]
        assert len(served) == len(fake), (
            f"serve() received {len(served)} deployments but "
            f"deployments() produced {len(fake)} — drift bug (H1)"
        )
        assert kwargs.get("pause_on_shutdown") is False

    def test_rollover_deployment_included(self):
        mock_serve = self._run_main()
        args, _ = mock_serve.call_args
        names = {getattr(d, "name", "") for d in args}
        assert any("rollover" in str(n).lower() for n in names) or len(args) == 5
```

> Implementation note: `main()` does `from prefect import serve` and `from serve import deployments` **inside** the function body, so `patch("prefect.serve")` and `patch("serve.deployments")` resolve correctly at call time. Final committed test will use one clean `_run_main(patch_deployments=...)` helper rather than the double-call sketch above; the assertion contract is: **count(served) == count(deployments())** and pause_on_shutdown=False.

### Blast radius (manual impact analysis; GitNexus MCP unavailable in this session)
Callers of edited lines: none (module entry point). Risk: LOW. Rollout: restart `AamBackupAgent`, verify 5 deployments appear in Prefect UI.

---

## Fix 2 — H2: silent empty walk on inaccessible share

**File:** `core/lan_manifest.py` (`walk_lan_destination`)

### Change spec
Add an `onerror` collector to `os.walk`. Failure taxonomy:

| Case | Old behavior | New behavior |
|------|-------------|--------------|
| Root enumeration fails (share offline/unauthorized) | returns `[]` silently — indistinguishable from empty | **raises `OSError`** naming the share |
| Subtree fails (locked dir, deleted mid‑walk) | skipped silently | skipped, but warning logged with path + error count |
| Share genuinely empty | `[]` | `[]` (unchanged — guard against overcorrection) |

```python
def walk_lan_destination(unc_path: str) -> list[dict]:
    files: list[dict] = []
    errors: list[OSError] = []
    base = str(Path(unc_path).resolve())

    def _on_error(err: OSError) -> None:
        errors.append(err)

    for root, _, filenames in os.walk(unc_path, onerror=_on_error):
        ...  # unchanged body

    root_failed = any(
        getattr(e, "filename", None) in (unc_path, str(Path(unc_path)))
        for e in errors
    )
    if root_failed and not files:
        raise OSError(
            f"Cannot enumerate LAN destination {unc_path!r} "
            f"({len(errors)} access error(s)) — refusing to report an "
            f"empty inventory; destination may be offline."
        )
    if errors:
        logger.warning(
            f"LAN manifest: {len(errors)} path(s) unreadable under {unc_path} "
            f"(first: {errors[0]}) — partial inventory returned ({len(files)} files)"
        )
    logger.info(f"LAN manifest: {len(files)} files at {unc_path}")
    return files
```

### Caller semantics preserved (verified by reading call sites)
- `lan_snapshot_before_task` (`flow.py:233`): raise → task fails **before sync**. Correct: we lacked visibility *before* mirroring.
- `lan_snapshot_after_task` (`flow.py:241‑262`): already wraps walk in `except Exception → return None` (G14). Raise post‑sync degrades to "skip metrics," never fails the backup. Contract intact.
- Flow‑level tests patch `flow.walk_lan_destination` directly → unaffected.

### Tests (extend `tests/test_lan_manifest.py`) — RED before, GREEN after

```python
class TestWalkFailureSemantics:
    """H2: unreachable destination must be loud, not an empty list."""

    def _fake_walk(self, errors=None, files=None):
        """Stub os.walk honoring onerror like the real implementation."""
        errors = errors or []
        files = files or []
        def fake_walk(top, onerror=None, **kw):
            for e in errors:
                if onerror:
                    onerror(e)
            yield (top, [], [f["name"] for f in files])
        return fake_walk

    def test_root_unreachable_raises(self, tmp_path, monkeypatch):
        top = str(tmp_path)
        err = OSError(5, "Access is denied")
        err.filename = top                      # root itself failed
        monkeypatch.setattr(os, "walk", self._fake_walk(errors=[err]))
        with pytest.raises(OSError, match="Cannot enumerate LAN destination"):
            walk_lan_destination(top)

    def test_subtree_error_partial_results(self, tmp_path, monkeypatch, caplog):
        sub = os.path.join(str(tmp_path), "locked")
        err = OSError(5, "Access is denied")
        err.filename = sub                      # only a subtree failed
        monkeypatch.setattr(os, "walk", self._fake_walk(errors=[err]))
        result = walk_lan_destination(str(tmp_path))
        assert isinstance(result, list)          # degraded, not fatal
        assert any("unreadable" in r.message for r in caplog.records)

    def test_genuinely_empty_share_still_ok(self, tmp_path):
        # NO stubbed errors — real walk over empty dir must stay []
        assert walk_lan_destination(str(tmp_path)) == []

    def test_real_tree_happy_path(self, tmp_path):   # unchanged behavior pin
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_text("y")
        result = walk_lan_destination(str(tmp_path))
        assert len(result) == 2
```

### Blast radius
Direct callers: `flow.lan_snapshot_before_task`, `flow.lan_snapshot_after_task` (both analyzed above), `tests/test_rt_01_lan_sync.py:216,226` (real dirs — unaffected). Existing comprehensive walk tests use real tmp trees → unaffected. Risk: MEDIUM‑LOW (behavioral change by design); covered by 4 new tests + full suite.

---

## Fix 3 — M10: `deploy/test_config.py` always exits 0

### Change spec
Split script into testable units; exit codes become real; interactive pause can never crash automation:

```python
def validate(config_path: str):
    """Return AppConfig or raise. No printing, no exiting."""
    from models.config import load_config
    return load_config(config_path)

def _pause():
    try:
        input("\nPress Enter to exit...")
    except (EOFError, OSError):
        pass            # non‑interactive (scheduled/batch) context

def main() -> int:
    ...
    try:
        cfg = validate(config_path)
    except Exception as e:
        print(f"[ERROR] in config.yaml validation:\n{e}")
        _pause()
        return 1
    ...success prints...
    _pause()
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

Missing‑file branch: same `_pause()` guard + `return 1`.

### Before/after demonstration (manual, for the record)
```powershell
# corrupt config.yaml temporarily (e.g. bucket: "BAD NAME!")
python deploy\test_config.py; echo $LASTEXITCODE   # BEFORE: 0  (bug)
#                                                   AFTER:  1  (fixed)
```

### Tests (new `tests/test_deploy_test_config.py`)

```python
import importlib.util, sys
from pathlib import Path
from unittest.mock import patch
import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "deploy_test_config", ROOT / "deploy" / "test_config.py"
)
mod = importlib.util.module_from_spec(spec)
sys.modules.setdefault("deploy_test_config", mod)
spec.loader.exec_module(mod)


class TestExitCodes:
    def test_invalid_config_returns_exit_code_1(self):
        with patch.object(mod, "validate", side_effect=ValueError("bad bucket")), \
             patch.object(mod, "_pause"):
            assert mod.main() == 1                 # OLD CODE: returned None → bug

    def test_valid_config_returns_zero(self, tmp_path, monkeypatch):
        fake_cfg = type("C", (), {})()
        with patch.object(mod, "validate", return_value=fake_cfg), \
             patch.object(mod, "_pause"):
            assert mod.main() == 0

    def test_pause_survives_closed_stdin(self):
        with patch("builtins.input", side_effect=EOFError):
            mod._pause()                            # must not raise

    def test_validate_wraps_load_config(self, tmp_path):
        bad = tmp_path / "config.yaml"
        bad.write_text("paths: {}\n", encoding="utf-8")
        with pytest.raises(Exception):
            mod.validate(str(bad))
```

### Blast radius
Consumers: deploy .bat scripts checking `%ERRORLEVEL%` — behavior change is the fix. Risk: LOW.

---

## Fix 4 — M7: `build_rclone_sync_command` hardcodes `"rclone"`

**File:** `core/cloud_sync.py:65‑66`

### Change
Match every sibling module's resolution pattern (`cloud_preflight.py:86`, `cloud_verify.py:44`, both reporter functions):

```python
def build_rclone_sync_command(...):
    ...
    robocopy_style_resolution = resolve_binary("rclone") or "rclone"
    return [
        robocopy_style_resolution, "sync",
        source, dest,
        ...
    ]
```
(+ `from core.process import resolve_binary` import.)

### Deterministic tests — existing suite stays green
Existing flag‑structure tests (`tests/test_cloud_sync_comprehensive.py:79‑166`, `test_cloud_sync_edge_cases.py:87‑255`) call the builder without patching the resolver; on machines where rclone IS installed, `cmd[0]` would become an absolute path and break `cmd[0]=="rclone"` style assertions. So add an autouse fixture at the top of both files:

```python
@pytest.fixture(autouse=True)
def _pin_rclone_exe():
    with patch("core.cloud_sync.resolve_binary", return_value="rclone"):
        yield
```

New dedicated tests (same files):

```python
class TestBinaryResolution:
    def test_bundled_binary_preferred(self):
        with patch("core.cloud_sync.resolve_binary", return_value=r"C:\deploy\bin\rclone.exe"):
            cmd = build_rclone_sync_command(**_defaults())
        assert cmd[0] == r"C:\deploy\bin\rclone.exe"

    def test_falls_back_to_path_name(self):
        with patch("core.cloud_sync.resolve_binary", return_value=None):
            cmd = build_rclone_sync_command(**_defaults())
        assert cmd[0] == "rclone"

# BEFORE-fix demonstration (fails today): resolver returns bundled path,
# builder ignores it → cmd[0] == "rclone". After fix: cmd[0] is the bundled path.
```

Also update the stale comment/test at `tests/test_rt_07_log_quality.py:215` ("no resolve_binary call in cloud_sync") — after this change cloud_sync resolves like everyone else.

### Blast radius
Production caller: `run_cloud_sync` only (passes cmd through to subprocess). Test callers: the two comprehensive files + edge cases (handled above). Risk: LOW.

---

## Fix 5 — M6: cloud reporter failures collapse into “zero”

### Design decision (deliberately NOT changing `get_cloud_manifest`'s return shape)
A tuple/dict return would churn ~15 assertions across `test_cloud_reporter*.py` + `rt_02`. Instead — consistent with the codebase's own G14 pattern (*reporters report, tasks decide policy*):

- **Reporters:** guaranteed never‑raise, failures carried as data:
  - `get_cloud_size`: also catch `FileNotFoundError` → `{count:0, bytes:0, sizeless:"0", ok:False, _error:...}` (currently raises!). Success gains `ok:True`.
  - `get_cloud_manifest`: raises typed **`CloudReporterError(RuntimeError)`** on nonzero exit / timeout / missing binary / unparsable JSON (replacing the silent `[]`). Success returns list unchanged → all existing happy‑path assertions survive untouched.
  - `get_cloud_diff`: timeout/OSError branch now returns diff **with `_partial: True, _error: str(e)`** instead of clean zeros; `FileNotFoundError` handled same way. Exit≥2 path already sets `_partial` — add `_error` there too.
- **Flow (`cloud_verify_and_report_task`, `flow.py:130‑175`)** owns degradation policy:
  ```python
  manifest_error = None
  try:
      manifest = get_cloud_manifest(...)
  except Exception as e:
      logger.warning(f"Cloud manifest query failed — metrics degraded: {e}")
      manifest, manifest_error = [], str(e)
  return {"verified":..., "size":..., "manifest":manifest,
          "diff":..., "manifest_error":manifest_error}
  ```
- **Pipeline (`_run_cloud_pipeline`)** honors truthfulness:
  - Skip `cloud_record_task` when `manifest_error` (log why) — failed queries are NEVER written as truth; healthy-empty bucket still records/prunes exactly as before.
  - Differential calc + artifact totals run only when `manifest_error is None`; otherwise `files_copied`/`bytes_copied` stay 0 with explicit "metrics unavailable" log line.
  - `extended_metrics` gains: `"manifest_ok": manifest_error is None, "size_ok": "_error" not in size, "diff_partial": diff.get("_partial", False)`.
  - Verify-failure message counts unchanged (diff keys unchanged).

### Tests

Reporter level (extend `tests/test_cloud_reporter_comprehensive.py`):

```python
class TestManifestRaisesOnFailure:
    """M6: a failed listing may NEVER masquerade as an empty bucket."""

    def test_nonzero_exit_raises(self):
        mock_result = MagicMock(returncode=7, stdout="[]", stderr="boom")
        with patch("core.cloud_reporter.resolve_binary", return_value="rclone"), \
             patch("core.cloud_reporter.subprocess.run", return_value=mock_result):
            with pytest.raises(core.cloud_reporter.CloudReporterError):
                get_cloud_manifest("b", "FY", "/cfg")

    def test_timeout_raises(self):
        with patch("core.cloud_reporter.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="rclone", timeout=1)):
            with pytest.raises(CloudReporterError):
                get_cloud_manifest("b", "FY", "/cfg")

    def test_missing_binary_raises(self):
        with patch("core.cloud_reporter.resolve_binary", return_value=None), \
             patch("core.cloud_reporter.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(CloudReporterError, match="rclone"):
                get_cloud_manifest("b", "FY", "/cfg")

    def test_success_shape_unchanged(self):
        payload = json.dumps([{"Path": "a.txt", "Size": 1, "IsDir": False}])
        mock_result = MagicMock(returncode=0, stdout=payload, stderr="")
        ... assert get_cloud_manifest(...) == [{"Path": "a.txt", ...}]  # pin


class TestSizeNeverRaises:
    def test_file_not_found_returns_error_dict(self):
        with patch("core.cloud_reporter.subprocess.run", side_effect=FileNotFoundError):
            r = get_cloud_size("b", "FY", "/cfg")
        assert r["ok"] is False and "rclone" in r["_error"]

    def test_failure_carries_error_flag(self):
        mock_result = MagicMock(returncode=5, stdout="", stderr="net down")
        ... r = get_cloud_size(...)
        assert r["ok"] is False and r["count"] == 0


class TestDiffCarriesDegradation:
    def test_timeout_diff_marked_partial(self):
        with patch("core.cloud_reporter.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1)):
            d = get_cloud_diff("src", "b", "FY", "/cfg")
        assert d.get("_partial") is True and d.get("_error")
```

Flow level (extend `tests/test_flow_orchestration.py` near L128):

```python
def test_manifest_query_failure_not_recorded_as_truth(tmp_path):
    """Failed manifest query ⇒ zero rows written, run row still recorded, metrics flagged."""
    verify_data = {
        "verified": True, "size": {"count": 0, "bytes": 0},
        "manifest": [], "diff": {...}, "manifest_error": "timeout after 900s",
    }
    db_path = str(tmp_path / "m.db")
    cloud_record_task.fn(db_path, verify_data, {"status": "CLOUD_COMPLETE"})   # updated task skips on error
    db = ManifestDB(db_path)
    assert db.file_count("cloud_status") == 0          # truth preserved: NOTHING recorded
    # pipeline-side: extended_metrics JSON must contain manifest_ok=false (asserted in pipeline test)
```

Plus one pipeline‑level assertion: after `_run_cloud_pipeline` with a mocked failing manifest, `run_history.extended_metrics` parses to `"manifest_ok": false`.

### Blast radius
Production callers: `flow.py` only (updated in same commit). Test callers: happy‑path shapes unchanged; failure‑path assertions in `test_cloud_reporter*.py` that expect `[]` on failure get inverted to `pytest.raises` (mechanical, listed file‑by‑file during execution). `rt_02` e2e CLOUD‑08 updates to try/except form. Risk: MEDIUM (most invasive of the five — hence last in sequence).

---

## Execution order & verification

| Step | Action | Gate |
|------|--------|------|
| 1 | Write ALL new tests first against current code → confirm the five RED | `pytest <new tests>` shows expected failures |
| 2 | Fix M10 (isolated script) → GREEN | `python deploy\test_config.py` exit‑code demo |
| 3 | Fix M7 + fixtures → GREEN | cloud_sync test files fully pass |
| 4 | Fix H2 → GREEN | lan_manifest suites pass |
| 5 | Fix H1 → GREEN | new launch completeness test passes |
| 6 | Fix M6 (reporter → flow) → GREEN | reporter + flow suites pass |
| 7 | Full suite | `uv run pytest -q` — zero failures, zero new skips |
| 8 | Manual smoke (documented in PR) | `python serve.py --dry-run`-style deployment listing shows 5; Prefect UI shows rollover‑check after agent restart |

## Definition of done
- All 5 fixes merged with their RED→GREEN evidence captured in the PR description.
- `CODE_AUDIT.md` statuses annotated: H1/H2/M6/M7/M10 → FIXED, with test references.
- Follow‑ups filed (not implemented): M9 ordering (archive before folders), M11 bridge comment, rt_07 stale comment removal (done inside step 3).

Ready to start executing step 1 (writing the failing tests) on your go.
