"""Property-based characterization of core invariants (Hypothesis).

This extends the existing test_property_based.py with properties derived
directly from the module docstrings/contracts: fiscal-year routing, path
algebra, snapshot diffing, duration-cap precedence, exit-code classification,
CSV-injection neutralization and path normalization.

Each property asserts an invariant that must hold for ALL generated inputs, so
it catches behaviour the example-based tests miss (boundary months, unusual
separators, extreme codes, unicode).
"""

import string
from datetime import date

from hypothesis import HealthCheck, given, settings, strategies as st

from core.backup_repository import _clean_path
from core.cloud_sync import classify_rclone_exit, resolve_max_duration_seconds
from core.fy_rollover import _child_path, _parent_path
from core.lan_manifest import diff_snapshots
from core.lan_preflight import _extract_unc_host
from core.lan_sync import classify_exit_code
from core.report import _csv_safe
from core.time_utils import get_fy_prefix


# ══════════════════════════════════════════════════════════════════════════
# Fiscal-year routing
# ══════════════════════════════════════════════════════════════════════════

class TestFyPrefixProperties:
    @given(st.dates())
    @settings(max_examples=400)
    def test_month_rule_and_format(self, d):
        """April–December → FY{yy}-{yy+1}; January–March → FY{yy-1}-{yy}."""
        prefix = get_fy_prefix(d)
        assert len(prefix) == 7
        assert prefix.startswith("FY")
        assert prefix[2:4].isdigit() and prefix[5:7].isdigit()
        assert prefix[4] == "-"
        if d.month >= 4:
            assert prefix == f"FY{d.year % 100:02d}-{(d.year + 1) % 100:02d}"
        else:
            assert prefix == f"FY{(d.year - 1) % 100:02d}-{d.year % 100:02d}"

    @given(st.dates())
    @settings(max_examples=200)
    def test_prefix_is_stable_within_a_month(self, d):
        """The FY prefix depends only on (year, month)."""
        same_month = date(d.year, d.month, 1)
        assert get_fy_prefix(d) == get_fy_prefix(same_month)


# ══════════════════════════════════════════════════════════════════════════
# FY path algebra
# ══════════════════════════════════════════════════════════════════════════

class TestFyPathProperties:
    _FY = st.from_regex(r"FY[0-9]{2}-[0-9]{2}", fullmatch=True)

    @given(
        root=st.sampled_from([r"E:\src", "A/B", "/mnt/data", r"\\nas\share", "C:/x"]),
        fy=_FY,
    )
    @settings(max_examples=200)
    def test_parent_child_roundtrip(self, root, fy):
        """parent(child(root, fy)) == root with its trailing separator removed."""
        child = _child_path(root, fy)
        assert child.endswith(fy)
        parent = _parent_path(child)
        sep = "\\" if "\\" in root else "/"
        assert parent == root.rstrip(sep)

    @given(root=st.sampled_from(["A/B", r"E:\src"]), fy=_FY)
    @settings(max_examples=100)
    def test_child_appends_exactly_one_segment(self, root, fy):
        child = _child_path(root, fy)
        assert child.count(fy) == 1


# ══════════════════════════════════════════════════════════════════════════
# Snapshot diff
# ══════════════════════════════════════════════════════════════════════════

_snapshot = st.dictionaries(
    st.text(min_size=1, max_size=20, alphabet=string.ascii_letters + string.digits + "/._"),
    st.tuples(st.integers(min_value=0, max_value=10**9), st.integers(min_value=0, max_value=10**9)),
    max_size=15,
)


class TestDiffSnapshotProperties:
    @given(before=_snapshot, after=_snapshot)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_partition_is_disjoint_and_total(self, before, after):
        result = diff_snapshots(before, after)
        buckets = {k: set(result[k]) for k in ("added", "removed", "modified", "unchanged")}
        # pairwise disjoint
        keys = list(buckets)
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                assert not (buckets[keys[i]] & buckets[keys[j]])
        # total coverage
        assert set().union(*buckets.values()) == set(before) | set(after)

    @given(data=_snapshot)
    @settings(max_examples=100)
    def test_size_change_is_modified(self, data):
        """Changing only the size of a path marks it modified, not unchanged."""
        if not data:
            return
        path = next(iter(data))
        size, mtime = data[path]
        after = dict(data)
        after[path] = (size + 1, mtime)
        result = diff_snapshots(data, after)
        assert path in result["modified"]

    @given(data=_snapshot)
    @settings(max_examples=100)
    def test_mtime_change_is_modified(self, data):
        if not data:
            return
        path = next(iter(data))
        size, mtime = data[path]
        after = dict(data)
        after[path] = (size, mtime + 1)
        result = diff_snapshots(data, after)
        assert path in result["modified"]

    @given(data=_snapshot)
    @settings(max_examples=100)
    def test_identical_snapshot_all_unchanged(self, data):
        result = diff_snapshots(data, data)
        assert result["added"] == []
        assert result["removed"] == []
        assert result["modified"] == []
        assert set(result["unchanged"]) == set(data)


# ══════════════════════════════════════════════════════════════════════════
# Cloud — duration cap precedence and exit classification
# ══════════════════════════════════════════════════════════════════════════

class TestDurationCapProperties:
    @given(
        timeout=st.integers(min_value=0, max_value=100000),
        configured=st.one_of(st.none(), st.integers(min_value=0, max_value=100000)),
    )
    @settings(max_examples=400)
    def test_precedence(self, timeout, configured):
        result = resolve_max_duration_seconds(timeout, configured)
        if configured is not None:
            expected = configured if configured > 0 else None
        else:
            auto = timeout - 300
            expected = auto if auto > 0 else None
        assert result == expected
        # A returned cap is always a positive int or None (never 0/negative).
        assert result is None or result > 0


class TestRcloneExitProperties:
    _KNOWN = {"CLOUD_COMPLETE", "CLOUD_FAILED", "CLOUD_PARTIAL", "CLOUD_NO_CHANGES_COMPLETE"}

    @given(st.integers(min_value=-1000, max_value=1000))
    @settings(max_examples=400)
    def test_always_in_known_set(self, code):
        assert classify_rclone_exit(code) in self._KNOWN

    def test_specific_unknown_maps_failed(self):
        assert classify_rclone_exit(11) == "CLOUD_FAILED"
        assert classify_rclone_exit(-1) == "CLOUD_FAILED"


class TestLanExitProperties:
    _KNOWN = {"LAN_COMPLETE", "LAN_PARTIAL", "LAN_FAILED"}

    @given(st.integers(min_value=-1000, max_value=1000))
    @settings(max_examples=400)
    def test_always_in_known_set(self, code):
        assert classify_exit_code(code) in self._KNOWN

    @given(st.integers(min_value=0, max_value=7))
    @settings(max_examples=50)
    def test_low_codes_complete(self, code):
        assert classify_exit_code(code) == "LAN_COMPLETE"

    @given(st.integers(min_value=8, max_value=15))
    @settings(max_examples=50)
    def test_bit3_partial(self, code):
        assert classify_exit_code(code) == "LAN_PARTIAL"

    @given(st.integers(min_value=16, max_value=31))
    @settings(max_examples=50)
    def test_bit4_failed(self, code):
        assert classify_exit_code(code) == "LAN_FAILED"


# ══════════════════════════════════════════════════════════════════════════
# Report CSV-injection neutralization
# ══════════════════════════════════════════════════════════════════════════

class TestCsvSafeProperties:
    _PREFIXES = ("=", "+", "-", "@", "\t", "\r")

    @given(st.text(max_size=60))
    @settings(max_examples=400)
    def test_formula_prefixes_are_quoted(self, text):
        result = _csv_safe(text)
        if text.startswith(self._PREFIXES):
            assert result == "'" + text
        else:
            assert result == text

    @given(st.text(max_size=30))
    @settings(max_examples=200)
    def test_result_never_starts_with_raw_dangerous_prefix(self, text):
        """Safety net: any dangerous cell is neutralized so it cannot execute."""
        result = _csv_safe(text)
        if result != text:  # neutralization happened
            assert result.startswith("'")

    def test_none_becomes_empty(self):
        assert _csv_safe(None) == ""


# ══════════════════════════════════════════════════════════════════════════
# Path normalization + UNC extraction
# ══════════════════════════════════════════════════════════════════════════

class TestPathNormalizationProperties:
    # Valid path characters only — control/space characters are stripped by
    # design (see the whitespace example test), which would break idempotence.
    _PATH_TEXT = st.text(
        alphabet=st.characters(blacklist_categories=("Cs", "Zs", "Cc", "Zl", "Zp")),
        max_size=80,
    )

    @given(_PATH_TEXT)
    @settings(max_examples=400)
    def test_clean_path_reaches_fixed_point(self, raw):
        """_clean_path always strips leading slashes and is stable by pass two.

        Characterized behaviour: PureWindowsPath folds a trailing separator on
        the NEXT call (e.g. '\\\\0\\' -> '0/' -> '0'), so strict one-pass
        idempotence does not hold — but the result never has a leading slash
        and is a fixed point after at most two applications.
        """
        cleaned = _clean_path(raw)
        assert isinstance(cleaned, str)
        assert not cleaned.startswith("/")
        once_more = _clean_path(cleaned)
        assert _clean_path(once_more) == once_more

    @given(st.text(alphabet=string.ascii_letters + string.digits + "._-\\", max_size=40))
    @settings(max_examples=200)
    def test_local_relative_paths_have_no_unc_host(self, text):
        """A path with no leading slash/backslash is never treated as UNC."""
        candidate = text.lstrip("\\/")
        assert _extract_unc_host(candidate) is None

    @given(
        host=st.from_regex(r"[a-zA-Z][a-zA-Z0-9.\-]{0,20}", fullmatch=True),
        share=st.from_regex(r"[a-zA-Z0-9_$]{1,12}", fullmatch=True),
    )
    @settings(max_examples=200)
    def test_unc_host_extracted(self, host, share):
        assert _extract_unc_host(f"\\\\{host}\\{share}") == host
        assert _extract_unc_host(f"//{host}/{share}") == host
