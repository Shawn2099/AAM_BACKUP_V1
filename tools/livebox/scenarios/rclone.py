"""rclone LAN scenario skeletons XR1-XR4 (WS-D D7; gated on Phase 1b).

Each scenario is marked ``injection_wired=False`` until Phase 1b
(versions_enabled, --backup-dir, copy-only on G3 block, reminder ladder)
lands in the product.
"""
from __future__ import annotations

from tools.livebox.scenarios.base import Scenario


def _noop(rig) -> None:
    return None


def _expect_empty(res) -> list[str]:
    return []


SCENARIOS: list[Scenario] = [
    Scenario(
        id="XR1",
        wave=5,
        title="Source file held open (share mode 0) for 5 nights -> PARTIAL, no deletions, then COMPLETE",
        start="2091-01-10",
        days=7,
        lane="xr1_locked_source",
        setup=_noop,
        on_day=None,
        expect=_expect_empty,
        injection_wired=False,
    ),
    Scenario(
        id="XR2",
        wave=5,
        title="Rename burst of 300 files -> moves, 0 deletes, no G3 block",
        start="2091-01-10",
        days=3,
        lane="xr2_rename_burst",
        setup=_noop,
        on_day=None,
        expect=_expect_empty,
        injection_wired=False,
    ),
    Scenario(
        id="XR3",
        wave=5,
        title="Mass delete > G3 -> BLOCKED copy-only 30 nights, ladder mails, approval at night 31",
        start="2091-01-10",
        days=33,
        lane="xr3_copy_only_ladder",
        setup=_noop,
        on_day=None,
        expect=_expect_empty,
        injection_wired=False,
    ),
    Scenario(
        id="XR4",
        wave=5,
        title="Versions retention crossing 91 days -> old backup-dir purged, younger kept",
        start="2091-01-10",
        days=95,
        lane="xr4_versions_retention",
        setup=_noop,
        on_day=None,
        expect=_expect_empty,
        injection_wired=False,
    ),
]
