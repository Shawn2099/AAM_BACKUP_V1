"""House style for what people read: say what to DO first, then why (core/messages.py)."""
import pytest

from core import messages as m


def test_act_puts_the_action_first_and_the_detail_last():
    assert m.act("Wait a minute", "The server is busy", "code 5") == \
        "Wait a minute. The server is busy. [code 5]"
    assert m.looks_action_first(m.act("Press Save", "Nothing was saved"))


@pytest.mark.parametrize("text", [
    m.TOO_MANY_REQUESTS, m.TOO_MANY_SIGN_INS, m.SCHEDULER_NOT_ANSWERING,
    m.leg_off("cloud"), m.leg_off("lan", "checked"), m.leg_running("lan"),
    m.could_not_start("safety check", "boom"),
])
def test_shared_dashboard_messages_start_with_an_action(text):
    assert m.looks_action_first(text), text
    assert text.count(".") >= 2          # an action AND a reason


@pytest.mark.parametrize("err_type, ctx, expect", [
    ("greater_than_equal", {"ge": 60}, "Enter 60 seconds or more."),
    ("less_than_equal", {"le": 3600}, "Enter 3600 seconds or less."),
    ("int_parsing", {}, "Enter a whole number."),
    ("bool_parsing", {}, "Choose on or off."),
])
def test_pydantic_errors_become_actions(err_type, ctx, expect):
    assert m.validation_text(err_type, ctx, "x", "seconds").startswith(expect)


def test_unknown_error_types_keep_their_own_text():
    assert m.validation_text("something_new", {}, "original text") == "original text"


def test_a_range_error_on_a_real_setting_is_action_first():
    from core import config_edit as ce
    from models.config import WolConfig
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as e:
        WolConfig(wake_timeout_seconds=5)
    [msg] = [x["message"] for x in ce._validation_messages(e.value)]
    assert m.looks_action_first(msg) and "60" in msg
