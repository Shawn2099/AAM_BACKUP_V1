"""Adversarial scenarios for the whole-FY restore and bulk upload tools.

Targets: restore.py and upload_fy.py (CLIs) and core/restore.py, core/upload.py (engines).

Every rclone call goes to FakeRclone, which works against local folders that stand in for
the bucket (remote `aam_gcs:<bucket>/<prefix>` -> `<tmp>/cloud/<bucket>/<prefix>`). No network,
no credentials, no real config, temp dirs only.

Invariants checked across the scenarios:
  * a refused request makes no rclone call and writes nothing (local or gs://);
  * no delete: rclone is only ever asked for copy, size, md5sum, check or lsf, and no
    --delete / --move / --backup-dir flag ever appears.

Scenarios that expose a product gap are xfail with a "SUSPECTED BUG" reason (strict=False).
"""

from __future__ import annotations

import ctypes
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

import restore as restore_cli
import upload_fy as upload_cli
from core import restore as core_restore
from core import upload as core_upload
from core.restore import fy_info, list_fy_folders, restore_fy, valid_folder_name
from core.run_artefacts import RunArtefacts
from core.time_utils import now_iso
from core.tool_runner import ToolResult
from core.upload import upload_fy
from models.config import AppConfig

BUCKET = "fakebucket"
ALLOWED_SUBCOMMANDS = {"copy", "size", "md5sum", "check", "lsf"}
DESTRUCTIVE_SUBCOMMANDS = {
    "sync", "delete", "deletefile", "purge", "move", "moveto", "rmdir", "rmdirs",
    "cleanup", "dedupe", "rcat",
}
DESTRUCTIVE_FLAG_PREFIXES = ("--delete", "--move", "--backup-dir", "--remove")


# ---------------------------------------------------------------- fake rclone

def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _excludes(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i in range(len(argv) - 1) if argv[i] == "--exclude"]


def _files(root: Path, excludes: list[str]) -> list[tuple[str, Path]]:
    if not root.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if any(fnmatch.fnmatch(name, pat) for pat in excludes):
                continue
            path = Path(dirpath) / name
            out.append((path.relative_to(root).as_posix(), path))
    return sorted(out)


def _stats_line(copied: int, nbytes: int) -> str:
    stats = {"transfers": copied, "bytes": nbytes, "errors": 0, "checks": 0, "deletes": 0}
    return json.dumps({"level": "info", "stats": stats})


class FakeRclone:
    """Stands in for rclone. Records every call; behaviour is set per scenario."""

    def __init__(self, cloud: Path):
        self.cloud = cloud
        self.calls: list[dict] = []
        self.copy_exit = 0                 # non-zero: copy transfers nothing and exits with this
        self.copy_budget: int | None = None  # copy at most N files, then exit partial_exit
        self.partial_exit = 10             # rclone's --max-duration exit code
        self.copy_timed_out = False
        self.copy_spawn_error: str | None = None
        self.copy_raise: BaseException | None = None
        self.md5_exit = 0                  # non-zero: md5sum fails (unhashed objects)
        self.md5_drop = 0                  # manifest lines left out (unhashed objects)
        self.md5_extra_lines: list[str] = []
        self.size_extra_count = 0          # objects the bucket reports beyond the ones on disk
        self.corrupt_after_copy: str | None = None

    def subs(self) -> list[str]:
        return [c["argv"][1] for c in self.calls]

    def _path(self, arg: str) -> Path:
        if arg.startswith("aam_gcs:"):
            return self.cloud / arg[len("aam_gcs:"):]
        return Path(arg)

    def _result(self, name, argv, artefacts, *, rc=0, stdout="", stderr="", log="",
                log_path=None, timeout_s=None, timed_out=False, spawn_error=None):
        out = artefacts.new_path(f"{name}.stdout", "txt")
        err = artefacts.new_path(f"{name}.stderr", "txt")
        out.write_text(stdout, encoding="utf-8")
        err.write_text(stderr, encoding="utf-8")
        if log_path is not None and not spawn_error:
            Path(log_path).write_text(log, encoding="utf-8")
        return ToolResult(
            name=name, argv=argv,
            exit_code=None if (timed_out or spawn_error) else rc,
            timed_out=timed_out, spawn_error=spawn_error,
            stdout_path=out, stderr_path=err,
            log_path=Path(log_path) if log_path is not None else None,
            started_at=now_iso(), ended_at=now_iso(), duration_s=0.0, timeout_s=timeout_s,
        )

    def __call__(self, name, argv, *, timeout_s, artefacts, log_path=None, env=None, cwd=None):
        argv = [str(a) for a in argv]
        self.calls.append({"name": name, "argv": argv, "timeout_s": timeout_s})
        sub = argv[1]
        if sub == "copy":
            return self._copy(name, argv, timeout_s, artefacts, log_path)
        handlers = {"size": self._size, "lsf": self._lsf, "md5sum": self._md5sum,
                    "check": self._check}
        if sub not in handlers:
            raise AssertionError(f"fake rclone: unexpected subcommand {sub!r}")
        rc, out, err = handlers[sub](argv)
        return self._result(name, argv, artefacts, rc=rc, stdout=out, stderr=err,
                            log_path=log_path, timeout_s=timeout_s)

    def _size(self, argv):
        root = self._path(argv[2])
        if not root.exists():
            return 3, "", "directory not found\n"
        files = _files(root, _excludes(argv))
        extra = self.size_extra_count if argv[2].startswith("aam_gcs:") else 0
        total = sum(p.stat().st_size for _, p in files)
        return 0, json.dumps({"count": len(files) + extra, "bytes": total}) + "\n", ""

    def _lsf(self, argv):
        root = self._path(argv[2])
        names = sorted(p.name + "/" for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
        return 0, "".join(n + "\n" for n in names), ""

    def _md5sum(self, argv):
        if self.md5_exit:
            return self.md5_exit, "", "could not hash object (composite)\n"
        out = Path(argv[argv.index("--output-file") + 1])
        lines = [f"{_md5(p)}  {rel}" for rel, p in _files(self._path(argv[2]), _excludes(argv))]
        if self.md5_drop:
            lines = lines[: len(lines) - self.md5_drop]
        lines += self.md5_extra_lines
        out.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        return 0, "", ""

    def _check(self, argv):
        excludes = _excludes(argv)
        restore_side = "--checkfile" in argv
        if restore_side:  # restore: pinned manifest against the local target
            i = argv.index("--checkfile")
            expected, actual_root = {}, self._path(argv[i + 3])
            for line in Path(argv[i + 2]).read_text(encoding="utf-8").splitlines():
                md5, rel = line.split("  ", 1)
                expected[rel] = md5
        else:  # upload: local source, one-way, against the bucket prefix
            expected = {rel: _md5(p) for rel, p in _files(self._path(argv[2]), excludes)}
            actual_root = self._path(argv[3])
        actual = {rel: _md5(p) for rel, p in _files(actual_root, excludes)}
        lines = []
        for rel in sorted(set(expected) | set(actual)):
            if rel in expected and rel not in actual:
                sym = "+"
            elif rel in actual and rel not in expected:
                if not restore_side:
                    continue  # one-way check: extra objects in the bucket are not compared
                sym = "-"
            elif expected[rel] != actual[rel]:
                sym = "*"
            else:
                sym = "="
            lines.append(f"{sym} {rel}")
        Path(argv[argv.index("--combined") + 1]).write_text(
            "".join(line + "\n" for line in lines), encoding="utf-8")
        diff = sum(1 for line in lines if not line.startswith("="))
        return (0 if diff == 0 else 1), f"{diff} differences found\n", ""

    def _copy(self, name, argv, timeout_s, artefacts, log_path):
        if self.copy_raise is not None:
            raise self.copy_raise
        if self.copy_timed_out:
            return self._result(name, argv, artefacts, timed_out=True, timeout_s=timeout_s)
        if self.copy_spawn_error:
            return self._result(name, argv, artefacts, spawn_error=self.copy_spawn_error,
                                timeout_s=timeout_s)
        if self.copy_exit:
            return self._result(name, argv, artefacts, rc=self.copy_exit, stderr="copy failed\n",
                                log=_stats_line(0, 0) + "\n", log_path=log_path, timeout_s=timeout_s)
        dry = "--dry-run" in argv
        src, dst = self._path(argv[2]), self._path(argv[3])
        lines, copied, nbytes, rc = [], 0, 0, 0
        for rel, path in _files(src, _excludes(argv)):
            target = dst / rel
            if target.is_file() and _md5(target) == _md5(path):
                continue  # already identical: a resumed run skips it
            if dry:
                lines.append(json.dumps({"level": "notice", "object": rel, "skipped": "copy"}))
                continue
            if self.copy_budget is not None and copied >= self.copy_budget:
                rc = self.partial_exit
                break
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            copied += 1
            nbytes += path.stat().st_size
        if self.corrupt_after_copy and not dry and argv[3].startswith("aam_gcs:"):
            victim = dst / self.corrupt_after_copy
            if victim.is_file():
                victim.write_bytes(b"CHANGED")
        lines.append(_stats_line(copied, nbytes))
        return self._result(name, argv, artefacts, rc=rc, log="\n".join(lines) + "\n",
                            log_path=log_path, timeout_s=timeout_s)


class Rig:
    def __init__(self, root: Path):
        self.root = root
        self.cloud = root / "cloud"
        self.fake = FakeRclone(self.cloud)
        self.conf = root / "rclone.conf"
        self.conf.write_text("[aam_gcs]\ntype = local\n", encoding="utf-8")
        self.logs = root / "logs"
        self._junctions: list[Path] = []

    def art(self) -> RunArtefacts:
        return RunArtefacts(self.logs, f"run-{uuid.uuid4().hex[:8]}")

    def subs(self) -> list[str]:
        return self.fake.subs()

    def put(self, prefix: str, rel: str, data: bytes) -> None:
        path = self.cloud / BUCKET / prefix / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def live(self, src: Path, name: str = "FY26-27") -> Path:
        folder = src / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "live.txt").write_text("LIVE", encoding="utf-8")
        return folder

    def junction(self, link: Path, target: Path) -> None:
        if os.name != "nt":
            pytest.skip("directory junctions are Windows-only")
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            pytest.skip(f"cannot create a junction here: {proc.stdout.strip()}")
        self._junctions.append(link)

    def cleanup(self) -> None:
        for link in self._junctions:  # remove the link itself, never the target
            try:
                os.rmdir(link)
            except OSError:
                pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path)
    monkeypatch.setattr(core_restore, "run_tool", r.fake)
    monkeypatch.setattr(core_upload, "run_tool", r.fake)
    yield r
    r.cleanup()


def _restore(rig, dest, fy, **kw):
    kw.setdefault("skip_space_check", True)
    kw.setdefault("bucket", BUCKET)
    return restore_fy(dest, fy=fy, rclone_config_path=str(rig.conf),
                      artefacts=rig.art(), **kw)


def _upload(rig, local, fy, **kw):
    kw.setdefault("storage_class", "ARCHIVE")
    return upload_fy(local, bucket=BUCKET, fy=fy, rclone_config_path=str(rig.conf),
                     artefacts=rig.art(), **kw)


def make_cfg(root: Path, *, overlap: bool) -> AppConfig:
    return AppConfig(**{
        "paths": {"source_drive": str(root / "src" / "FY26-27"),
                  "lan_destination": r"\\nas\share\FY26-27",
                  "runtime_dir": str(root / "rt"),
                  "gcs_key_path": str(root / "k.json")},
        "cloud": {"enabled": True, "bucket": "aam-test-bucket", "project_number": "1"},
        "rollover": {"overlap_enabled": overlap},
        "wol": {"enabled": False},
    })


def _short_name(path: Path) -> str | None:
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 1024)
    return buf.value if n else None


# ---------------------------------------------------------------- folder names

def test_traversal_absolute_and_unc_names_refused_by_both_tools(rig):
    dest = rig.root / "dest"
    dest.mkdir()
    src = rig.root / "src"
    rig.live(src)
    for name in ["..\\FY26-27", "../FY26-27", "FY26-27\\", "\\\\nas\\share\\FY26-27"]:
        assert not valid_folder_name(name), name
        assert _restore(rig, dest, name)["status"] == "FAILED", name
        assert _upload(rig, src, name)["status"] == "FAILED", name
    assert rig.fake.calls == []
    assert not any(dest.iterdir())


def test_drive_qualified_and_stream_names_are_not_folder_names():
    for name in ["D:evil", "archive:stream"]:
        assert not valid_folder_name(name), name


def test_lowercase_fy_name_reads_the_canonical_cloud_prefix(rig):
    rig.put("FY26-27", "a.txt", b"A")
    dest = rig.root / "dest"
    dest.mkdir()
    _restore(rig, dest, "fy26-27")
    remotes = {arg for c in rig.fake.calls for arg in c["argv"] if arg.startswith("aam_gcs:")}
    assert "aam_gcs:fakebucket/FY26-27" in remotes


def test_upload_cli_refuses_live_fy_in_every_spelling(rig, monkeypatch):
    cfg = make_cfg(rig.root, overlap=True)
    src = rig.root / "src"
    rig.live(src)
    monkeypatch.setattr(upload_cli, "_load_config", lambda path: cfg)
    for name in ["FY26-27", "fy26-27", "Fy26-27"]:
        code = upload_cli.main([
            "--config", "ignored.yaml", "--bucket", BUCKET,
            "--rclone-config", str(rig.conf), "--source", str(src),
            "--fy", name, "--tier", "ARCHIVE", "--yes", "--log-dir", str(rig.logs),
        ])
        assert code == 2, name
    assert rig.fake.calls == []
    assert not (rig.cloud / BUCKET / "FY26-27").exists()


def test_trailing_dot_or_space_cannot_reach_the_live_folder(rig):
    src = rig.root / "src"
    live = rig.live(src)
    for name in ["FY26-27.", "FY26-27 "]:
        res = _restore(rig, src, name, allow_non_empty=True, forbidden_paths=[str(live)])
        assert res["ok"] is False, name
        assert "protected" in (res["error"] or "") or "folder name" in (res["error"] or ""), name
    # Upload: the trailing character matches no folder on disk, so nothing is resolved.
    assert _upload(rig, src, "FY26-27.")["status"] == "FAILED"
    assert rig.fake.calls == []
    assert (live / "live.txt").read_text(encoding="utf-8") == "LIVE"


def test_short_8_3_alias_of_protected_parent_is_resolved_and_refused(rig):
    if os.name != "nt":
        pytest.skip("8.3 short names are Windows-only")
    parent = rig.root / "Client Books Long"
    live = rig.live(parent)
    short = _short_name(parent)
    if not short or short == str(parent):
        pytest.skip("8.3 short names are not generated on this volume")
    res = _restore(rig, Path(short), "FY26-27", allow_non_empty=True, forbidden_paths=[str(live)])
    assert res["ok"] is False
    assert "protected" in (res["error"] or "")
    assert rig.fake.calls == []


# ---------------------------------------------------------------- junctions

def test_junction_to_live_fy_refused_by_restore_cli_even_with_force(rig, monkeypatch):
    cfg = make_cfg(rig.root, overlap=True)
    live = rig.live(rig.root / "src")
    dest = rig.root / "dest"
    dest.mkdir()
    rig.junction(dest / "FY24-25", live)
    rig.put("FY24-25", "old.txt", b"OLD")
    monkeypatch.setattr(restore_cli, "_load_config", lambda path: (cfg, None))
    code = restore_cli.main([
        "--config", "ignored.yaml", "--bucket", BUCKET, "--rclone-config", str(rig.conf),
        "--fy", "FY24-25", "--target", str(dest), "--force", "--yes",
        "--log-dir", str(rig.logs),
    ])
    assert code == 1
    assert "copy" not in rig.subs()
    assert (live / "live.txt").read_text(encoding="utf-8") == "LIVE"


def test_junction_destination_is_refused_without_a_caller_list(rig):
    other = rig.root / "other"
    other.mkdir()
    rig.put("FY24-25", "x.txt", b"X")
    dest = rig.root / "dest"
    dest.mkdir()
    rig.junction(dest / "FY24-25", other)
    res = _restore(rig, dest, "FY24-25")
    assert res["ok"] is False
    assert "junction" in (res["error"] or "")


@pytest.mark.parametrize("mode", ["child", "root"])
def test_junction_named_like_a_folder_is_not_an_upload_source(rig, mode):
    live = rig.live(rig.root / "src")
    if mode == "child":
        parent = rig.root / "srcparent"
        parent.mkdir()
        rig.junction(parent / "archive", live)
        local = parent
    else:
        local = rig.root / "archive"
        rig.junction(local, live)
    res = _upload(rig, local, "archive")
    assert res["status"] == "FAILED", mode
    assert "junction" in (res["error"] or ""), mode
    assert rig.subs() == []
    assert not (rig.cloud / BUCKET / "archive").exists()


def test_junction_inside_destination_refused_even_with_force(rig):
    dest = rig.root / "dest"
    dest.mkdir()
    (dest / "keep.txt").write_text("keep", encoding="utf-8")
    rig.junction(dest / "link", rig.live(rig.root / "src"))
    res = _restore(rig, dest, "archive", into=True, allow_non_empty=True)
    assert res["ok"] is False
    assert "junction" in (res["error"] or "")
    assert rig.subs() == []
    assert (dest / "keep.txt").read_text(encoding="utf-8") == "keep"


# ---------------------------------------------------------------- destinations

def test_drive_root_destination_refused_even_with_into(rig):
    res = _restore(rig, Path(rig.root.anchor), "FY26-27", into=True, allow_non_empty=True)
    assert res["ok"] is False
    assert "drive root" in (res["error"] or "")
    assert rig.subs() == []


def test_nas_twins_of_live_and_early_folder_follow_the_overlap_switch(rig):
    on = restore_cli._forbidden_paths(make_cfg(rig.root, overlap=True), None)
    off = restore_cli._forbidden_paths(make_cfg(rig.root, overlap=False), None)
    assert r"\\nas\share\FY26-27" in on
    assert r"\\nas\share\FY27-28" in on
    assert r"\\nas\share\FY26-27" in off
    assert r"\\nas\share\FY27-28" not in off


def test_restore_into_source_parent_refused_even_with_force(rig):
    src = rig.root / "src"
    live = rig.live(src)
    res = _restore(rig, src, "FY26-27", into=True, allow_non_empty=True,
                   forbidden_paths=[str(live)])
    assert res["ok"] is False
    assert "protected" in (res["error"] or "")
    assert rig.subs() == []
    assert (live / "live.txt").read_text(encoding="utf-8") == "LIVE"


def test_restore_cli_always_passes_the_protected_list_to_the_engine(rig, monkeypatch):
    """opus-decision C: restore_fy() defaults forbidden_paths=() (accepted; the only caller is the CLI,
    which always passes the list). Pin that the CLI passes the live source folder and, while the overlap
    is on, the early folder. Any new caller must do the same (ARCHITECTURE.md, restore row)."""
    from unittest.mock import MagicMock

    cfg = make_cfg(rig.root, overlap=True)
    src = rig.root / "src"
    live = rig.live(src)
    (rig.root / "dest").mkdir()
    rig.put("FY24-25", "old.txt", b"OLD")
    fake = MagicMock(return_value={"ok": True, "status": "OK"})
    monkeypatch.setattr(restore_cli, "restore_fy", fake)
    monkeypatch.setattr(restore_cli, "_load_config", lambda path: (cfg, None))
    code = restore_cli.main([
        "--config", "ignored.yaml", "--bucket", BUCKET, "--rclone-config", str(rig.conf),
        "--fy", "FY24-25", "--target", str(rig.root / "dest"), "--yes", "--log-dir", str(rig.logs),
    ])
    assert code == 0
    forbidden = [str(p) for p in fake.call_args.kwargs["forbidden_paths"]]
    assert str(live) in forbidden
    assert str(src / "FY27-28") in forbidden


# ---------------------------------------------------------------- early next FY

def test_restore_cli_refuses_early_fy_while_overlap_is_on(rig, monkeypatch):
    cfg = make_cfg(rig.root, overlap=True)
    src = rig.root / "src"
    rig.live(src)
    rig.put("FY27-28", "n.txt", b"N")
    monkeypatch.setattr(restore_cli, "_load_config", lambda path: (cfg, None))
    for name in ["FY27-28", "fy27-28"]:
        code = restore_cli.main([
            "--config", "ignored.yaml", "--bucket", BUCKET, "--rclone-config", str(rig.conf),
            "--fy", name, "--target", str(src), "--yes", "--log-dir", str(rig.logs),
        ])
        assert code == 1, name
    assert not (src / "FY27-28").exists()
    assert not {"copy", "md5sum"} & set(rig.subs())


def test_restore_cli_restores_early_fy_when_overlap_is_off(rig, monkeypatch):
    cfg = make_cfg(rig.root, overlap=False)
    src = rig.root / "src"
    rig.live(src)
    rig.put("FY27-28", "n.txt", b"N")
    monkeypatch.setattr(restore_cli, "_load_config", lambda path: (cfg, None))
    code = restore_cli.main([
        "--config", "ignored.yaml", "--bucket", BUCKET, "--rclone-config", str(rig.conf),
        "--fy", "FY27-28", "--target", str(src), "--yes", "--log-dir", str(rig.logs),
    ])
    assert code == 0
    assert (src / "FY27-28" / "n.txt").read_bytes() == b"N"
    assert (src / "FY26-27" / "live.txt").read_text(encoding="utf-8") == "LIVE"


def test_upload_cli_early_fy_refused_only_while_overlap_is_on(rig, monkeypatch):
    src = rig.root / "src"
    rig.live(src)
    early = src / "FY27-28"
    early.mkdir()
    (early / "e.txt").write_bytes(b"E")
    argv = ["--config", "ignored.yaml", "--bucket", BUCKET, "--rclone-config", str(rig.conf),
            "--source", str(src), "--fy", "FY27-28", "--tier", "ARCHIVE", "--yes",
            "--log-dir", str(rig.logs)]

    monkeypatch.setattr(upload_cli, "_load_config", lambda path: make_cfg(rig.root, overlap=True))
    assert upload_cli.main(argv) == 2
    assert not (rig.cloud / BUCKET / "FY27-28").exists()

    monkeypatch.setattr(upload_cli, "_load_config", lambda path: make_cfg(rig.root, overlap=False))
    assert upload_cli.main(argv) == 0
    assert (rig.cloud / BUCKET / "FY27-28" / "e.txt").read_bytes() == b"E"


# ---------------------------------------------------------------- bad inputs

def test_invalid_storage_tier_refused_before_any_bucket_call(rig):
    misc = rig.root / "misc"
    misc.mkdir()
    (misc / "m.txt").write_bytes(b"M")
    for tier in ["SUPERCOLD", "", "STANDARD; rm -rf /", "DEEP_ARCHIVE"]:
        res = _upload(rig, misc, "misc", storage_class=tier)
        assert res["status"] == "FAILED", tier
        assert "storage class" in (res["error"] or ""), tier
    assert rig.fake.calls == []
    with pytest.raises(SystemExit) as exc:
        upload_cli.main(["--bucket", BUCKET, "--source", str(misc), "--fy", "misc",
                         "--tier", "SUPERCOLD", "--yes"])
    assert exc.value.code == 2


def test_invalid_bucket_names_refused_by_every_entry_point(rig):
    dest = rig.root / "dest"
    dest.mkdir()
    misc = rig.root / "misc"
    misc.mkdir()
    (misc / "m.txt").write_bytes(b"M")
    for bucket in ["", "Bad_Bucket", "../escape", "a"]:
        assert _restore(rig, dest, "FY26-27", bucket=bucket)["status"] == "FAILED", bucket
        assert list_fy_folders(bucket=bucket, rclone_config_path=str(rig.conf))["error"], bucket
        res = upload_fy(misc, bucket=bucket, fy="misc", storage_class="ARCHIVE",
                        rclone_config_path=str(rig.conf), artefacts=rig.art())
        assert res["status"] == "FAILED", bucket
    assert rig.fake.calls == []


def test_missing_protected_key_fails_closed_before_any_bucket_call(rig):
    key = rig.root / "restore-ro.json.dpapi"  # never created
    dest = rig.root / "dest"
    dest.mkdir()
    misc = rig.root / "misc"
    misc.mkdir()
    (misc / "m.txt").write_bytes(b"M")
    res_r = restore_fy(dest, fy="FY26-27", bucket=BUCKET, gcs_key_path=str(key),
                       skip_space_check=True, artefacts=rig.art())
    res_u = upload_fy(misc, bucket=BUCKET, fy="misc", storage_class="ARCHIVE",
                      gcs_key_path=str(key), artefacts=rig.art())
    listing = list_fy_folders(bucket=BUCKET, gcs_key_path=str(key))
    assert res_r["status"] == "FAILED" and res_r["ok"] is False
    assert res_u["status"] == "FAILED" and res_u["ok"] is False
    assert listing["error"]
    assert rig.fake.calls == []


def test_empty_bucket_and_empty_prefix_restore_nothing_and_upload_creates(rig):
    listing = list_fy_folders(bucket=BUCKET, rclone_config_path=str(rig.conf))
    assert listing["error"] is None and listing["folders"] == []

    (rig.cloud / BUCKET / "FY26-27").mkdir(parents=True)  # the prefix exists, holds nothing
    dest = rig.root / "dest"
    dest.mkdir()
    res = _restore(rig, dest, "FY26-27")
    assert res["status"] == "FAILED"
    assert "empty" in (res["error"] or "")
    assert not {"copy", "md5sum"} & set(rig.subs())

    misc = rig.root / "misc"
    misc.mkdir()
    (misc / "m.txt").write_bytes(b"M")
    assert _upload(rig, misc, "misc")["status"] == "UPLOADED_VERIFIED"


# ---------------------------------------------------------------- rclone mid-run

def test_restore_time_budget_is_resumable_without_deletes(rig):
    for name in "abcde":
        rig.put("FY26-27", f"{name}.txt", name.encode())
    dest = rig.root / "dest"
    dest.mkdir()
    rig.fake.copy_budget = 2
    first = _restore(rig, dest, "FY26-27", timeout_s=300)
    assert first["status"] == "INCOMPLETE" and first["ok"] is False
    copy_argv = next(c["argv"] for c in rig.fake.calls if c["argv"][1] == "copy")
    assert "--max-duration" in copy_argv and "--cutoff-mode" in copy_argv

    rig.fake.copy_budget = None
    second = _restore(rig, dest, "FY26-27", allow_non_empty=True)
    assert second["status"] == "RESTORED_VERIFIED", second.get("error")
    assert second["matched"] == 5
    assert not set(rig.subs()) & DESTRUCTIVE_SUBCOMMANDS


@pytest.mark.parametrize("failure", ["exit", "timeout", "spawn"])
def test_restore_copy_failure_never_deletes_local_files(rig, failure):
    rig.put("FY26-27", "a.txt", b"NEW")
    rig.put("FY26-27", "b.txt", b"B")
    dest = rig.root / "dest"
    target = dest / "FY26-27"
    target.mkdir(parents=True)
    (target / "local-only.txt").write_text("keep", encoding="utf-8")
    (target / "a.txt").write_text("OLD", encoding="utf-8")
    if failure == "exit":
        rig.fake.copy_exit = 1
    elif failure == "timeout":
        rig.fake.copy_timed_out = True
    else:
        rig.fake.copy_spawn_error = "FileNotFoundError: rclone"

    res = _restore(rig, dest, "FY26-27", allow_non_empty=True, timeout_s=300)
    assert res["status"] == "FAILED" and res["ok"] is False
    assert (target / "local-only.txt").read_text(encoding="utf-8") == "keep"
    assert (target / "a.txt").read_text(encoding="utf-8") == "OLD"
    assert not set(rig.subs()) & DESTRUCTIVE_SUBCOMMANDS


@pytest.mark.parametrize("kind", ["md5-error", "missing-hash-line"])
def test_restore_never_copies_without_full_hash_coverage(rig, kind):
    for name in "abc":
        rig.put("FY26-27", f"{name}.bin", name.encode())
    if kind == "md5-error":
        rig.fake.md5_exit = 1
    else:
        rig.fake.md5_drop = 1
    dest = rig.root / "dest"
    dest.mkdir()
    res = _restore(rig, dest, "FY26-27")
    assert res["status"] == "FAILED" and res["ok"] is False
    assert "copy" not in rig.subs()
    assert not (dest / "FY26-27").exists()


def test_case_only_name_collision_in_the_cloud_is_refused(rig):
    rig.put("FY26-27", "Report.pdf", b"R")
    rig.fake.md5_extra_lines = ["0" * 32 + "  REPORT.pdf"]
    rig.fake.size_extra_count = 1
    dest = rig.root / "dest"
    dest.mkdir()
    res = _restore(rig, dest, "FY26-27")
    assert res["status"] == "FAILED"
    assert "collision" in (res["error"] or "")
    assert "copy" not in rig.subs()
    assert not (dest / "FY26-27").exists()


def test_upload_time_budget_resumes_and_keeps_objects_already_in_bucket(rig):
    local = rig.root / "src" / "FY24-25"
    local.mkdir(parents=True)
    for name in "wxyz":
        (local / f"{name}.txt").write_bytes(name.encode())
    rig.put("FY24-25", "old-extra.txt", b"X")
    rig.fake.copy_budget = 2
    first = _upload(rig, local, "FY24-25")
    assert first["status"] == "INCOMPLETE"
    rig.fake.copy_budget = None
    second = _upload(rig, local, "FY24-25")
    assert second["status"] == "UPLOADED_VERIFIED", second.get("error")
    assert second["matched"] == 4
    assert (rig.cloud / BUCKET / "FY24-25" / "old-extra.txt").read_bytes() == b"X"
    assert not set(rig.subs()) & DESTRUCTIVE_SUBCOMMANDS


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_upload_copy_failure_never_removes_bucket_objects(rig, failure):
    local = rig.root / "misc"
    local.mkdir()
    (local / "m.txt").write_bytes(b"M")
    rig.put("misc", "old-extra.txt", b"X")
    if failure == "exit":
        rig.fake.copy_exit = 1
    else:
        rig.fake.copy_timed_out = True
    res = _upload(rig, local, "misc")
    assert res["status"] == "FAILED" and res["ok"] is False
    assert (rig.cloud / BUCKET / "misc" / "old-extra.txt").read_bytes() == b"X"
    assert not set(rig.subs()) & DESTRUCTIVE_SUBCOMMANDS


def test_ctrl_c_mid_run_keeps_objects_and_exits_cleanly(rig, monkeypatch):
    # Upload: the engine re-raises after writing a CANCELLED report.
    local = rig.root / "misc"
    local.mkdir()
    (local / "m.txt").write_bytes(b"M")
    rig.put("misc", "old-extra.txt", b"X")
    rig.fake.copy_raise = KeyboardInterrupt()
    artefacts = RunArtefacts(rig.logs, "upload-ctrlc")
    with pytest.raises(KeyboardInterrupt):
        upload_fy(local, bucket=BUCKET, fy="misc", storage_class="ARCHIVE",
                  rclone_config_path=str(rig.conf), artefacts=artefacts)
    report = json.loads((artefacts.dir / "upload-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "CANCELLED" and report["resumable"] is True
    assert (rig.cloud / BUCKET / "misc" / "old-extra.txt").read_bytes() == b"X"

    # Restore CLI: Ctrl-C is reported as exit 130, not a traceback.
    rig.fake.copy_raise = KeyboardInterrupt()
    rig.put("FY26-27", "a.txt", b"A")
    dest = rig.root / "dest"
    dest.mkdir()
    monkeypatch.setattr(restore_cli, "_load_config", lambda path: (None, None))
    code = restore_cli.main([
        "--config", "ignored.yaml", "--bucket", BUCKET, "--rclone-config", str(rig.conf),
        "--fy", "FY26-27", "--target", str(dest), "--yes", "--log-dir", str(rig.logs),
    ])
    assert code == 130


def test_upload_changed_during_verify_is_unverified(rig):
    local = rig.root / "src" / "FY24-25"
    local.mkdir(parents=True)
    (local / "a.txt").write_bytes(b"AAA")
    rig.fake.corrupt_after_copy = "a.txt"
    res = _upload(rig, local, "FY24-25")
    assert res["status"] == "UPLOADED_UNVERIFIED"
    assert res["ok"] is False
    assert res["modified_at_remote"] == 1


# ---------------------------------------------------------------- dry run and invariants

def test_dry_run_never_transfers_bytes(rig):
    rig.put("FY26-27", "a.txt", b"A")
    rig.put("FY26-27", "b.txt", b"B")
    dest = rig.root / "dest"
    dest.mkdir()
    res = _restore(rig, dest, "FY26-27", dry_run=True)
    assert res["status"] == "DRY_RUN"
    assert "copy" not in rig.subs()
    assert not (dest / "FY26-27").exists()

    local = rig.root / "src" / "FY24-25"
    local.mkdir(parents=True)
    for name in ["x", "y", "z"]:
        (local / f"{name}.txt").write_bytes(name.encode())
    res = _upload(rig, local, "FY24-25", dry_run=True)
    assert res["status"] == "DRY_RUN"
    assert res["planned_transfers"] == 3
    copies = [c["argv"] for c in rig.fake.calls if c["argv"][1] == "copy"]
    assert copies and all("--dry-run" in argv for argv in copies)
    assert not (rig.cloud / BUCKET / "FY24-25").exists()


def test_every_engine_call_is_copy_size_md5_check_or_lsf(rig):
    rig.put("FY26-27", "a.txt", b"A")
    rig.put("FY26-27", "sub/b.txt", b"B")
    dest = rig.root / "dest"
    dest.mkdir()
    misc = rig.root / "misc"
    misc.mkdir()
    (misc / "m.txt").write_bytes(b"M")

    _restore(rig, dest, "FY26-27")
    _restore(rig, dest, "FY26-27", dry_run=True, allow_non_empty=True)
    _restore(rig, dest, "FY26-27", verify_only=True)
    list_fy_folders(bucket=BUCKET, rclone_config_path=str(rig.conf))
    fy_info("FY26-27", bucket=BUCKET, rclone_config_path=str(rig.conf))
    _upload(rig, misc, "misc")
    _upload(rig, misc, "misc", dry_run=True)
    _upload(rig, misc, "misc", verify_only=True)

    assert rig.subs(), "the scenario made no rclone calls"
    assert set(rig.subs()) <= ALLOWED_SUBCOMMANDS
    for call in rig.fake.calls:
        assert not any(a.startswith(DESTRUCTIVE_FLAG_PREFIXES) for a in call["argv"]), call["argv"]
