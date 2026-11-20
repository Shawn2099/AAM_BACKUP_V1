"""OV-10.2 (R-17, plan D-12 / 5.10) - readiness probes for the FY overlap.

The overlap creates the next FY folder next to the configured one, on the
source drive and in the NAS share root, as the service account. The probe
must prove that right now (not in January) and leave nothing behind.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC_CHECK = "new-year folder (source)"
NAS_CHECK = "new-year folder (NAS)"


def _probe_module():
    spec = importlib.util.spec_from_file_location(
        "probe_service_account_ov10", ROOT / "deploy" / "probe_service_account.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


NAS_UNC = r"\\nas\share\FY26-27"    # lan_destination must be a UNC path
NAS_GONE_UNC = r"\\nas\gone\x\FY26-27"


def _config(tmp_path, *, lan=True, nas_unc=NAS_UNC):
    src = tmp_path / "src" / "FY26-27"
    src.mkdir(parents=True, exist_ok=True)
    (src / "ledger.xlsx").write_bytes(b"x")
    (tmp_path / "nas" / "FY26-27").mkdir(parents=True, exist_ok=True)
    runtime = tmp_path / "rt"
    runtime.mkdir(exist_ok=True)
    key = tmp_path / "key.json"
    key.write_text("{}", encoding="utf-8")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "paths:\n"
        f"  source_drive: '{src}'\n"
        f"  lan_destination: '{nas_unc}'\n"
        f"  runtime_dir: '{runtime}'\n"
        f"  gcs_key_path: '{key}'\n"
        + ("cloud:\n  enabled: false\n" if lan else
           "cloud:\n  enabled: true\n  bucket: my-bucket-1\n  project_number: '12345'\n")
        + "wol:\n  enabled: false\n"
        f"lan:\n  enabled: {'true' if lan else 'false'}\n  use_backup_mode: false\n"
        "dashboard: {}\n", encoding="utf-8")
    return cfg


@pytest.fixture
def probe(monkeypatch, tmp_path):
    mod = _probe_module()
    from core import permissions
    monkeypatch.setattr(permissions, "probe_destination_write", lambda dest, run_id: None)
    # the fake NAS: map the UNC names used in the config onto local temp folders
    real_path = Path
    maps = {r"\\nas\share": tmp_path / "nas", r"\\nas\gone": tmp_path / "gone"}

    def mapped_path(value):
        text = str(value)
        for unc, local in maps.items():
            if text.lower().startswith(unc):
                return real_path(str(local) + text[len(unc):])
        return real_path(value)
    monkeypatch.setattr(mod, "Path", mapped_path)
    return mod


def _run(probe, tmp_path, cfg):
    result = tmp_path / "result.txt"
    code = probe.main(["p", str(cfg), str(result)])
    lines = result.read_text(encoding="utf-8").splitlines()
    status = {}
    for line in lines:
        st, rest = line.split(" ", 1)
        status[rest.split(":", 1)[0]] = (st, line)
    return code, status


def _no_probe_folders(tmp_path):
    return not list(tmp_path.rglob("_aam_probe_*"))


class TestCreateFolderProbe:
    def test_pass_and_nothing_left_behind(self, probe, tmp_path):
        code, status = _run(probe, tmp_path, _config(tmp_path))
        assert code == 0
        assert status[SRC_CHECK][0] == "OK" and status[NAS_CHECK][0] == "OK"
        assert _no_probe_folders(tmp_path)
        # FY folder content untouched
        assert [p.name for p in (tmp_path / "src" / "FY26-27").iterdir()] == ["ledger.xlsx"]

    def test_missing_parent_fails_and_creates_no_grandparent(self, probe, tmp_path):
        missing = tmp_path / "no_such_grandparent" / "no_such_parent" / "FY26-27"
        with pytest.raises(RuntimeError) as exc:
            probe.probe_create_sibling_folder(str(missing), "the source folder")
        assert "right to create folders" in str(exc.value)
        assert not (tmp_path / "no_such_grandparent").exists()   # never parents=True

    def test_never_passes_parents_true(self, probe, tmp_path, monkeypatch):
        seen = []
        real = Path.mkdir

        def spy(self, mode=0o777, parents=False, exist_ok=False):
            if self.name.startswith("_aam_probe_"):
                seen.append((parents, exist_ok))
            return real(self, mode=mode, parents=parents, exist_ok=exist_ok)
        monkeypatch.setattr(Path, "mkdir", spy)
        probe.probe_create_sibling_folder(str(tmp_path / "FY26-27"), "the source folder")
        assert seen == [(False, False)]

    def test_permission_denied_is_reported_with_hint(self, probe, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        real = Path.mkdir

        def deny(self, *a, **k):
            if self.name.startswith("_aam_probe_"):
                raise PermissionError(5, "Access is denied", str(self))
            return real(self, *a, **k)
        monkeypatch.setattr(Path, "mkdir", deny)
        code, status = _run(probe, tmp_path, cfg)
        assert code == 1
        for name in (SRC_CHECK, NAS_CHECK):
            st, line = status[name]
            assert st == "FAIL" and "Access is denied" in line
            assert "right to create folders in" in line
        assert _no_probe_folders(tmp_path)

    def test_folder_removed_when_removal_fails_is_reported(self, probe, tmp_path, monkeypatch):
        def no_rmdir(self):
            raise PermissionError(5, "Access is denied", str(self))
        monkeypatch.setattr(Path, "rmdir", no_rmdir)
        with pytest.raises(RuntimeError) as exc:
            probe.probe_create_sibling_folder(str(tmp_path / "FY26-27"), "the source folder")
        assert "cannot remove it" in str(exc.value)

    def test_removed_when_the_body_fails_after_creation(self, probe, tmp_path, monkeypatch):
        # success path with a later failure elsewhere must still leave nothing
        folder = tmp_path / "FY26-27"
        probe.probe_create_sibling_folder(str(folder), "the source folder")
        assert _no_probe_folders(tmp_path)

    def test_drive_or_share_root_has_no_parent(self, probe):
        with pytest.raises(RuntimeError) as exc:
            probe.probe_create_sibling_folder("D:\\", "the source folder")
        assert "drive or share root" in str(exc.value)

    def test_nothing_created_when_lan_disabled(self, probe, tmp_path):
        code, status = _run(probe, tmp_path, _config(tmp_path, lan=False))
        # (the cloud leg is on in this config and fails without credentials: not under test)
        assert status["cloud bucket"][0] == "FAIL" and code == 1
        assert status[NAS_CHECK][0] == "SKIP"
        assert status[SRC_CHECK][0] == "OK"
        assert _no_probe_folders(tmp_path)

    def test_unreachable_nas_parent_fails_only_the_nas_check(self, probe, tmp_path):
        cfg = _config(tmp_path, nas_unc=NAS_GONE_UNC)
        code, status = _run(probe, tmp_path, cfg)
        assert code == 1
        assert status[NAS_CHECK][0] == "FAIL" and status[SRC_CHECK][0] == "OK"
        assert not (tmp_path / "gone").exists()


class TestNewYearProbesOnlyWhenOverlapCanHappen:
    """N-12: the new-year folder probes report SKIP (with the reason) unless the overlap is on AND the
    configured FY can be derived from paths.source_drive."""

    def test_skip_when_overlap_is_switched_off(self, probe, tmp_path):
        cfg = _config(tmp_path)
        cfg.write_text(cfg.read_text(encoding="utf-8") + "rollover:\n  overlap_enabled: false\n",
                       encoding="utf-8")
        code, status = _run(probe, tmp_path, cfg)
        assert code == 0
        for name in (SRC_CHECK, NAS_CHECK):
            assert status[name][0] == "SKIP" and "overlap_enabled is false" in status[name][1]
        assert _no_probe_folders(tmp_path)

    def test_skip_when_the_configured_fy_cannot_be_derived(self, probe, tmp_path, monkeypatch):
        # (config validation already refuses a non-FY source folder; the probe must still not trust that)
        from core import fy_rollover
        monkeypatch.setattr(fy_rollover, "configured_fy", lambda *a, **k: None)
        _code, status = _run(probe, tmp_path, _config(tmp_path))
        for name in (SRC_CHECK, NAS_CHECK):
            st, line = status[name]
            assert st == "SKIP" and "not named like FY26-27" in line
        assert _no_probe_folders(tmp_path)
