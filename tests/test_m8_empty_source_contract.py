"""M8 — empty-source-drive contract is fail-closed with actionable messaging.

The pipeline refuses to sync an empty source because robocopy /MIR and
rclone sync mirror deletions: an unmounted/wrong-path drive would otherwise
wipe the destination copies. The old message ("Source drive appears empty")
gave no recovery hint; the preflight comment even claimed empty was valid.
"""

import pytest

from core.health import check_source_drive


def test_empty_source_fails_with_actionable_message(tmp_path):
    ok, reason = check_source_drive(str(tmp_path))
    assert ok is False
    assert "appears empty" in reason
    # Operator must learn WHY refusing (mirror-wipe hazard)...
    assert "delete" in reason.lower() or "mirroring" in reason.lower()
    # ...and WHAT to do (canary file).
    assert "canary" in reason.lower()


def test_nonempty_source_still_passes(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    ok, reason = check_source_drive(str(tmp_path))
    assert ok is True
    assert reason == ""


def test_missing_source_message_unchanged(tmp_path):
    ok, reason = check_source_drive(str(tmp_path / "nope"))
    assert ok is False
    assert "not accessible" in reason
