"""P1 R7 / matrix row K4: the lan_post_reserve_minutes text says what the reserve is for.

The reserve (kept after the LAN mirror) pays for the arrival check(s) and the verified NAS shutdown.
The old model text said "destination walk", which is not what the reserve covers any more, and the
plain-language help had no mention of the arrival check at all.
"""

from models.config import BudgetConfig
from core.config_help import HELP, label_and_help

KEY = "budget.lan_post_reserve_minutes"
FIELD = "lan_post_reserve_minutes"


def test_model_description_mentions_arrival_check_and_shutdown():
    description = BudgetConfig.model_fields[FIELD].description or ""
    assert "arrival check" in description
    assert "shutdown" in description


def test_model_description_no_longer_mentions_destination_walk():
    description = BudgetConfig.model_fields[FIELD].description or ""
    assert "destination walk" not in description


def test_config_help_entry_exists_with_label_and_help():
    assert KEY in HELP
    label, help_text = HELP[KEY]
    assert label.strip()
    assert help_text.strip()


def test_config_help_label_and_help_mention_arrival_check_and_nas_shutdown():
    label, help_text = label_and_help(KEY, "technical")
    assert label.strip()
    assert help_text.strip()
    assert "arrival check" in label
    assert "arrival check" in help_text
    assert "NAS shutdown" in help_text
