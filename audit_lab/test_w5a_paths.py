"""WAVE 5a â€” real-world filesystem conditions: long paths, Unicode, reserved
Windows names, trailing dots/spaces, emoji, and very deep trees.

Accounting data is exactly this: deep "Clients/<name>/FY/<doc>" trees, non-ASCII
client names, and legacy files with names Windows tolerates but tools don't.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from audit_lab import labconf as L

pytestmark = pytest.mark.timeout(600


)

EXT = "\\\\?\\"   # extended-length prefix: creates paths > MAX_PATH regardless of the registry


def _deep_target(root: Path, depth: int) -> Path:
    """Build <root>/seg00/seg01/... (created through the \\\\?\\ prefix) and return it."""
    d = root
    for i in range(depth):
        d = d / f"seg{i:02d}"
    os.makedirs(EXT + str(d), exist_ok=True)
    return d


def _write_raw(path: Path, data: bytes) -> None:
    """Write via the \\\\?\\ prefix so deep paths can be created at all."""
    os.makedirs(EXT + str(path.parent), exist_ok=True)
    fd = os.open(EXT + str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _names() -> list[str]:
    return [
        "Sharma & Sons _ FY25 Final Accounts.pdf",
        "ÃœnÃ¯cÃ¶dÃ©_ã‚¯ãƒ©ã‚¤ã‚¢ãƒ³ãƒˆ_2026.txt",
        "à¤°à¤¾à¤® à¤•à¥à¤®à¤¾à¤° à¤¹à¤¿à¤¸à¤¾à¤¬.xlsx",
        "emoji_ðŸ“Š_ledger.csv",
        "trailing.dots...txt",
        "trailing space .txt",
        "new\nline.txt",
        "tab\tname.txt",
        "CON",                     # reserved device name
        "NUL.txt",
        "AUX",
        "prn.dat",
        "COM1.txt",
    ]


def test_w5a_path_variants_through_lan_sync(sandbox):
    """Put every hostile-but-legal filename through the real robocopy mirror."""
    import core.lan_sync as ls
    _orig = ls.build_robocopy_command
    ls.build_robocopy_command = lambda s, d, c: L.strip_zb(_orig(s, d, c))
    from core.lan_sync import run_lan_sync

    src = sandbox / "source" / "FY26-26"
    dest = L.make_canary(sandbox / "nas" / "FY26-26")
    cfg = L.build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")

    outcomes: dict[str, str] = {}
    payload = b"AAM ledger\n" * 40
    for i, name in enumerate(_names()):
        try:
            L.write_file(src / "Clients" / name, payload)
            outcomes[name] = "created"
        except OSError as e:
            outcomes[name] = f"SOURCE CREATE FAILED: {type(e).__name__}: {e}"

    res = run_lan_sync(str(src), str(dest), cfg.lan)
    landed = {p.name for p in (dest / "Clients").iterdir()} if (dest / "Clients").exists() else set()
    missing = [n for n, s in outcomes.items() if s == "created" and n not in landed]

    L.record("W5a.path_variants",
             "filenames with spaces/Unicode/reserved names/trailing dots survive the LAN mirror",
             f"created={sum(1 for s in outcomes.values() if s == 'created')}/{len(_names())} | "
             f"robocopy exit={res['exit_code']} status={res['status']} files_failed={res.get('files_failed')} | "
             f"landed at destination={len(landed)} | "
             f"NOT COPIED: {missing} | create failures: "
             f"{ {k: v[:60] for k, v in outcomes.items() if v != 'created'} }",
             f"ISSUE: {len(missing)} of the source files never reached the destination "
             f"(status={res['status']}) â€” silent file loss for exotic-but-legal names"
             if missing else "all hostile names mirrored correctly")
    ls.build_robocopy_command = _orig
    print(f"[W5a] created={sum(1 for s in outcomes.values() if s == 'created')}/{len(_names())} "
          f"landed={len(landed)} status={res['status']} missing={missing}")

    # â”€â”€ the same names through the cloud leg (log parsers read UTF-8 logs) â”€â”€â”€â”€â”€
    import os as _os

    bucket_root = sandbox / "buckets"
    bucket_root.mkdir(parents=True, exist_ok=True)
    rc = L.write_local_rclone_config(sandbox / "runtime" / "rclone_local.conf")
    saved = L.patch_cloud_modules_to_local(rc)
    cwd = _os.getcwd()
    _os.chdir(bucket_root)
    try:
        from core.cloud_sync import run_cloud_sync

        cres = run_cloud_sync(
            source=str(src), bucket="lab-bucket", fy_prefix="FY26-26",
            gcs_key_path=str(sandbox / "lab-key.json"), project_number="000000000000",
            storage_class="STANDARD", bwlimit="1000M", retries=1, transfers=2,
            checkers=2, buffer_size="16M", timeout=180, max_duration_seconds=0,
        )
        landed_cloud = {p.name for p in (bucket_root / "lab-bucket" / "FY26-26" / "Clients").rglob("*")
                        if p.is_file()} if (bucket_root / "lab-bucket" / "FY26-26" / "Clients").exists() else set()
        missing_cloud = [n for n, s in outcomes.items() if s == "created" and n not in landed_cloud]
        L.record("W5a.path_variants.cloud",
                 "the same hostile filenames sync to the cloud",
                 f"rclone exit={cres['exit_code']} status={cres['status']} "
                 f"error={(cres.get('error') or '')[:200]} | landed={len(landed_cloud)} "
                 f"NOT SYNCED: {missing_cloud}",
                 "ISSUE: cloud leg lost " f"{len(missing_cloud)} of the names" if missing_cloud
                 else "cloud leg handled all names")
        print(f"[W5a-cloud] status={cres['status']} landed={len(landed_cloud)} missing={missing_cloud}")
    finally:
        _os.chdir(cwd)
        for (mod, attr), orig in saved.items():
            setattr(mod, attr, orig)
    # A11: no hostile-but-legal filename may be silently lost, on either leg.
    # A success status that hides a missing file is the C-1/H-1 class of bug.
    assert missing == [], f"LAN leg lost {len(missing)} file(s): {missing}"
    assert missing_cloud == [], f"cloud leg lost {len(missing_cloud)} file(s): {missing_cloud}"
    assert res["status"] == "LAN_COMPLETE", res
    assert cres["status"] in ("CLOUD_COMPLETE", "CLOUD_NO_CHANGES_COMPLETE"), cres


def test_w5a_deep_paths_and_destination_walk(sandbox):
    """>260-char paths: are they copied, and does walk_lan_destination see them?"""
    import core.lan_sync as _ls
    from core.lan_manifest import walk_lan_destination

    src = sandbox / "source" / "FY26-26"
    deep = _deep_target(src / "Clients", 32)
    for i in range(5):
        _write_raw(deep / f"ledger_{i}.txt", b"deep\n" * 20)
    depth_chars = len(str(deep / "ledger_0.txt"))
    assert depth_chars > 260, depth_chars

    dest = L.make_canary(sandbox / "nas" / "FY26-26")
    cfg = L.build_config(sandbox, source=src, lan_dest=str(dest), runtime=sandbox / "runtime")

    _orig = _ls.build_robocopy_command
    _ls.build_robocopy_command = lambda s, d, c: L.strip_zb(_orig(s, d, c))
    try:
        res = _ls.run_lan_sync(str(src), str(dest), cfg.lan)
    finally:
        _ls.build_robocopy_command = _orig

    truth = {n for _r, _d, names in os.walk(EXT + str(dest)) for n in names}
    walked = walk_lan_destination(str(dest))
    src_files = [p for p in src.rglob("*.txt")]
    walked_deep = [f for f in walked if "seg" in f["path"]]
    L.record("W5a.deep_paths",
             "deep (>260 char) paths are copied AND enumerated consistently",
             f"deepest source path = {depth_chars} chars | source files={len(src_files)} | "
             f"robocopy exit={res['exit_code']} status={res['status']} files_failed={res.get('files_failed')} | "
             f"destination files (ground truth, \\\\?\\ prefix)={len(truth)} | "
             f"walk_lan_destination() saw={len(walked)} (deep: {len(walked_deep)})",
             "ENUMERATION GAP CONFIRMED — files copied but os.walk() cannot see them: the LAN "
             "snapshot/manifest records a PARTIAL inventory as truth"
             if len(truth) > len(walked) else
             "COPY GAP CONFIRMED — deep files never reached the destination "
             f"(status={res['status']})"
             if len(truth) < len(src_files) + 1 else "deep paths handled consistently")
    print(f"[W5a-deep] {depth_chars} chars, src={len(src_files)} status={res['status']} "
          f"dest_truth={len(truth)} walk={len(walked)}")
