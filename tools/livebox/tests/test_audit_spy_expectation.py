"""SPY_AUDIT expectation follows the lane config (flow.py _shut_nas_down gate)."""
from tools.livebox.run_lane import audit_spy_expectation as exp

ON = {"lan": {"shutdown_after_backup": True}, "wol": {"enabled": True}}


def test_shutdown_expected_only_when_flag_and_wol_on():
    assert exp(True, ON) == (1, 1)
    assert exp(True, {"lan": {"shutdown_after_backup": False}, "wol": {"enabled": True}}) == (1, 0)
    assert exp(True, {"lan": {"shutdown_after_backup": True}, "wol": {"enabled": False}}) == (1, 0)
    assert exp(True, {}) == (1, 0)


def test_no_lan_leg_expects_nothing():
    assert exp(False, ON) == (0, 0)
