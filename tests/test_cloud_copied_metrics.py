"""Regression tests — cloud `files_copied` / `bytes_copied` metric computation.

Reproduces the production-canary defect (2026-08-21, branch reliability-2026-08-20):
the cloud pipeline compared the live GCS manifest (rclone `ModTime` strings)
against database entries whose `mtime` is a numeric Unix float — the format
LAN records write via `file_entries.mtime`. `pendulum.parse("1783677447.705671")`
raises ParserError, the legacy exception path fell back to raw string
comparison, and EVERY existing file was reported as "copied":

    actual transfer:  5 files /  262,647 B
    reported:      576 files / 57,245,523 B

Contract pinned here (flow.compute_copied_files):
  * an existing unchanged file whose mtime is stored as a numeric Unix
    timestamp and observed as an ISO string (same instant) is NOT counted;
  * genuinely changed files (mtime moved > 1.1 s, or size changed) ARE counted;
  * multiple unchanged files with mixed representations are NOT falsely counted;
  * numeric Unix mtime values are handled correctly;
  * ISO mtime values (Z and +HH:MM offsets) are handled correctly;
  * new files are counted;
  * uninterpretable representations keep the conservative last resort.
"""

import pendulum

import flow

# ── production-shape fixtures ──────────────────────────────────────────────
# 1783677447.705671 == 2026-07-10T09:57:27.705671Z == 2026-07-10T15:27:27.705671000+05:30
NUMERIC_MTIME = 1783677447.705671
ISO_MTIME_Z = "2026-07-10T09:57:27.705671Z"
ISO_MTIME_IST = "2026-07-10T15:27:27.705671000+05:30"
LATER_ISO = "2026-07-10T10:00:00.000000000Z"  # ~2.5 min later


def _item(rel: str, size: int, modtime) -> dict:
    """rclone lsjson-shaped manifest item (what get_cloud_manifest returns)."""
    return {"Path": rel, "Size": size, "ModTime": modtime, "IsDir": False}


def _iso_z(ts: float) -> str:
    """RFC-3339 UTC string (rclone ModTime shape) for a Unix instant."""
    return pendulum.from_timestamp(ts, tz="UTC").isoformat().replace("+00:00", "Z")


# ── the defect: numeric DB mtime vs ISO manifest mtime ─────────────────────

class TestMixedRepresentationNoFalsePositive:
    def test_unchanged_file_numeric_db_mtime_iso_manifest_not_counted(self):
        """Exact production shape: LAN record wrote a Unix float, rclone
        reports the same instant as an RFC-3339 string. Must NOT count."""
        before = {"client_meeting_notes.txt": (52, NUMERIC_MTIME)}
        manifest = [_item("client_meeting_notes.txt", 52, ISO_MTIME_Z)]
        assert flow.compute_copied_files(manifest, before) == []

    def test_unchanged_file_numeric_db_mtime_ist_manifest_not_counted(self):
        """Same instant expressed with a +05:30 offset (cloud-record format)."""
        before = {"document.txt": (42, NUMERIC_MTIME)}
        manifest = [_item("document.txt", 42, ISO_MTIME_IST)]
        assert flow.compute_copied_files(manifest, before) == []

    def test_unchanged_file_iso_db_mtime_numeric_manifest_not_counted(self):
        """Inverse direction: ISO in the DB, numeric observed. Must NOT count."""
        before = {"document.txt": (42, ISO_MTIME_IST)}
        manifest = [_item("document.txt", 42, NUMERIC_MTIME)]
        assert flow.compute_copied_files(manifest, before) == []

    def test_unchanged_file_iso_db_mtime_numeric_string_manifest_not_counted(self):
        """Numeric mtime as a string (e.g. "1783677447.705671")."""
        before = {"document.txt": (42, ISO_MTIME_Z)}
        manifest = [_item("document.txt", 42, "1783677447.705671")]
        assert flow.compute_copied_files(manifest, before) == []


# ── genuine changes must still be counted ───────────────────────────────────

class TestGenuineChangesCounted:
    def test_mtime_moved_beyond_threshold_counted(self):
        before = {"a.txt": (100, NUMERIC_MTIME)}
        manifest = [_item("a.txt", 100, LATER_ISO)]
        assert flow.compute_copied_files(manifest, before) == [("a.txt", 100)]

    def test_size_change_counted_even_if_mtime_identical(self):
        before = {"a.txt": (100, NUMERIC_MTIME)}
        manifest = [_item("a.txt", 120, ISO_MTIME_Z)]
        assert flow.compute_copied_files(manifest, before) == [("a.txt", 120)]

    def test_new_file_counted(self):
        before = {"a.txt": (100, NUMERIC_MTIME)}
        manifest = [_item("a.txt", 100, ISO_MTIME_Z), _item("new.txt", 7, ISO_MTIME_Z)]
        assert flow.compute_copied_files(manifest, before) == [("new.txt", 7)]

    def test_mtime_move_within_threshold_not_counted(self):
        """Float/rounding noise under the 1.1 s guard must not count."""
        before = {"a.txt": (100, 1_700_000_000.0)}
        manifest = [_item("a.txt", 100, "2023-11-14T22:13:20.500000000Z")]  # +0.5 s
        assert flow.compute_copied_files(manifest, before) == []


# ── batch behaviour (the canary shape at scale) ─────────────────────────────

class TestBatchBehaviour:
    def test_multiple_unchanged_files_not_falsely_counted(self):
        """572 unchanged files with MIXED stored representations
        (numeric Unix floats from LAN records + ISO strings from cloud
        records), manifest reports the same instants as RFC-3339.
        Only the 5 genuinely new files may be counted — pre-fix code
        counted all 576 (the canary inflation)."""
        before = {}
        manifest = []
        for i in range(572):
            ts = NUMERIC_MTIME + i  # distinct instant per file
            # alternate the stored format the way production does
            stored_mtime = ts if i % 2 == 0 else _iso_z(ts)
            size = 50 + i
            rel = f"test_data/file_{i:04d}.dat"
            before[rel] = (size, stored_mtime)
            manifest.append(_item(rel, size, _iso_z(ts)))
        # 5 new canary files
        canary = [
            ("AAM_CANARY/canary_01_small.txt", 142),
            ("AAM_CANARY/canary_02_content.txt", 179),
            ("AAM_CANARY/nested/sub/deep/canary_03_nested.txt", 92),
            ("AAM_CANARY/canary_04_large.bin", 262144),
            ("AAM_CANARY/canary_05_modify.txt", 90),
        ]
        for rel, size in canary:
            manifest.append(_item(rel, size, ISO_MTIME_Z))

        copied = flow.compute_copied_files(manifest, before)
        copied_paths = {p for p, _ in copied}
        assert copied_paths == {rel for rel, _ in canary}, (
            f"expected exactly the 5 new files, got {len(copied_paths)} entries: "
            f"{sorted(copied_paths)[:10]}"
        )
        assert sum(s for _, s in copied) == 262647  # exact byte total, no inflation

    def test_batch_one_modified_file_among_unchanged(self):
        """One resaved file (same size, mtime moved 3 s — beyond the 1.1 s
        noise guard) among 49 unchanged files: exactly that one is counted.
        (A resave within the 1.1 s guard is below the metric's reporting
        resolution by pre-existing design — the sync/verify layer, not this
        metric, is authoritative for such transfers.)"""
        before = {f"f{i}.dat": (100, NUMERIC_MTIME + i) for i in range(50)}
        manifest = [_item(f"f{i}.dat", 100, _iso_z(NUMERIC_MTIME + i)) for i in range(50)]
        # f7 resaved: same size, mtime 3 s after its stored instant
        manifest[7] = _item("f7.dat", 100, _iso_z(NUMERIC_MTIME + 7 + 3))

        copied = flow.compute_copied_files(manifest, before)
        assert copied == [("f7.dat", 100)]


# ── format-handling unit pins ───────────────────────────────────────────────

class TestMtimeNormalization:
    def test_equivalent_z_and_offset_strings_not_counted(self):
        before = {"a.txt": (10, ISO_MTIME_IST)}
        manifest = [_item("a.txt", 10, ISO_MTIME_Z)]
        assert flow.compute_copied_files(manifest, before) == []

    def test_datetime_instance_in_db_not_counted(self):
        """A datetime/pendulum object stored in before_dict normalizes to
        the same instant as the ISO manifest string."""
        before = {"a.txt": (10, pendulum.parse(ISO_MTIME_Z))}
        manifest = [_item("a.txt", 10, ISO_MTIME_IST)]
        assert flow.compute_copied_files(manifest, before) == []

    def test_unparseable_differing_strings_conservatively_counted(self):
        """Last resort (only for genuinely uninterpretable values):
        differing raw representations count, identical do not."""
        before = {"a.txt": (10, "not-a-timestamp-1")}
        assert flow.compute_copied_files([_item("a.txt", 10, "not-a-timestamp-2")], before) == [("a.txt", 10)]
        assert flow.compute_copied_files([_item("a.txt", 10, "not-a-timestamp-1")], before) == []

    def test_empty_manifest(self):
        assert flow.compute_copied_files([], {"a": (1, NUMERIC_MTIME)}) == []

    def test_empty_before_dict_counts_everything(self):
        manifest = [_item("a", 5, ISO_MTIME_Z), _item("b", 6, ISO_MTIME_Z)]
        assert flow.compute_copied_files(manifest, {}) == [("a", 5), ("b", 6)]
