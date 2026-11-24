"""Red-first tests for the Opus review must-fix items D1 and D2 (derive_config).

D1: the lane forces rollover.overlap_enabled = true even when production has it false.
D2: retention. Keyword names (the contract for the fix; `retention_days` is the existing one):
      retention_days=730          -> run_logs.retention_days, maintenance.db_retention_days AND
                                     maintenance.log_retention_days are all 730 (A730)
      db_retention_days=35        -> maintenance.db_retention_days
      run_logs_retention_days=7   -> run_logs.retention_days
      log_retention_days=7        -> maintenance.log_retention_days           (R35: 35 / 7 / 7)
    A per-key keyword wins over `retention_days`; a key not named keeps the production value.
"""
from __future__ import annotations

import copy
import pathlib

import pytest

from tools.livebox.tests.test_derive_config import PROD_RAW

LANE = pathlib.Path(r"C:\AAMLivebox\20261124-0100\A")


def _derive(raw=None, **kw):
    from tools.livebox.derive_config import derive_config

    return derive_config(copy.deepcopy(PROD_RAW if raw is None else raw), LANE, smtp_port=2525, **kw)


def test_d1_overlap_enabled_forced_true_when_production_has_false():
    raw = copy.deepcopy(PROD_RAW)
    raw["rollover"]["overlap_enabled"] = False
    assert _derive(raw)["rollover"]["overlap_enabled"] is True


def test_d1_overlap_enabled_forced_true_when_production_has_no_rollover_section():
    raw = copy.deepcopy(PROD_RAW)
    raw.pop("rollover")
    assert _derive(raw)["rollover"]["overlap_enabled"] is True


def test_d1_other_rollover_keys_are_kept():
    raw = copy.deepcopy(PROD_RAW)
    raw["rollover"].update(overlap_enabled=False, overlap_activate_on="11-30")
    cfg = _derive(raw)
    assert cfg["rollover"]["overlap_activate_on"] == "11-30"


def test_d1_input_not_mutated():
    raw = copy.deepcopy(PROD_RAW)
    raw["rollover"]["overlap_enabled"] = False
    snap = copy.deepcopy(raw)
    _derive(raw)
    assert raw == snap


def test_d2_a730_sets_log_retention_days_too():
    cfg = _derive(retention_days=730)
    assert cfg["run_logs"]["retention_days"] == 730
    assert cfg["maintenance"]["db_retention_days"] == 730
    assert cfg["maintenance"]["log_retention_days"] == 730


def test_d2_r35_sets_db_35_run_logs_7_log_7():
    cfg = _derive(db_retention_days=35, run_logs_retention_days=7, log_retention_days=7)
    assert cfg["maintenance"]["db_retention_days"] == 35
    assert cfg["run_logs"]["retention_days"] == 7
    assert cfg["maintenance"]["log_retention_days"] == 7


def test_d2_per_key_keyword_wins_over_retention_days():
    cfg = _derive(retention_days=730, db_retention_days=35)
    assert cfg["maintenance"]["db_retention_days"] == 35
    assert cfg["run_logs"]["retention_days"] == 730
    assert cfg["maintenance"]["log_retention_days"] == 730


def test_d2_unnamed_retention_keys_keep_production_values():
    cfg = _derive(db_retention_days=35)
    assert cfg["maintenance"]["db_retention_days"] == 35
    assert cfg["run_logs"]["retention_days"] == PROD_RAW["run_logs"]["retention_days"]
    assert cfg["maintenance"]["log_retention_days"] == PROD_RAW["maintenance"]["log_retention_days"]
