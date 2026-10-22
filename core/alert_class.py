"""Which kind of alert is this? (plan docs/PLAN_ALERT_CONTROLS_2026-10-22.md, step A)

ONE pure function decides the class of every alert, from its kind first and its key prefix second - never
from message text. The class decides which switch, which reminder rule and whether the failure delay may
apply (core.notify). `tests/test_alert_class.py` scans every `AlertEvent(...)` call site of the code base and
fails when a key is not covered here, so a new alert cannot dodge the rules.
"""
from __future__ import annotations

FAILURE = "failure"      # a run failed / partly worked / was missed, a warning
DECISION = "decision"    # a backup is blocked and waits for a person (approval)
OVERDUE = "overdue"      # a leg has had no successful backup by its schedule (watchdog)
ACTION = "action"        # any other "a person must act": rollover, audit differences, service problems
RESOLVED = "resolved"    # the problem is fixed
INFO = "info"            # informational (financial year started)
SECURITY = "security"    # password changed - always sent, not switchable
TEST = "test"            # the Settings "Send test email" - always sent

CLASSES = (FAILURE, DECISION, OVERDUE, ACTION, RESOLVED, INFO, SECURITY, TEST)

# Every key prefix an AlertEvent is built with. A new alert with a new prefix must be registered here (the
# scan test enforces it); the class itself still comes from `classify`, this is only the coverage list.
KNOWN_PREFIXES = frozenset({
    "backup:", "missed:", "nas_shutdown:", "recovered:", "blocked:", "stale:", "rollover:", "audit:",
    "watchdog:", "password:", "test:",
})


def classify(kind: str, key: str) -> str:
    """The class of an alert with this `kind` (FAILURE | WARNING | ACTION_REQUIRED | RECOVERED | INFO)
    and `key`. An unknown kind is treated as `action` (fail closed: it needs a person and stays switchable
    only through the action switch)."""
    if kind == "RECOVERED":
        return RESOLVED
    if kind == "INFO":
        if key.startswith("test:"):
            return TEST
        if key.startswith("password:"):
            return SECURITY
        return INFO
    if kind in ("FAILURE", "WARNING"):
        return FAILURE
    if kind == "ACTION_REQUIRED":
        if key.startswith("blocked:"):
            return DECISION
        if key.startswith("stale:"):
            return OVERDUE
        return ACTION
    return ACTION


def classify_event(event) -> str:
    return classify(event.kind, event.key)


# The switch of each class (config key, dotted). `security` and `test` have none: they are always sent.
SWITCH_KEYS = {
    FAILURE: "notifications.send_on_failure",
    DECISION: "alerts.send_decision_needed",
    OVERDUE: "alerts.send_overdue",
    ACTION: "alerts.send_action_needed",
    RESOLVED: "alerts.send_resolved",
    INFO: "alerts.send_info",
}
# What a person reads about a class ("problem and warning emails are switched off")
LABELS = {
    FAILURE: "problem and warning emails",
    DECISION: "'a backup is waiting for your decision' emails",
    OVERDUE: "'backup overdue' emails",
    ACTION: "'action needed' emails (financial year, safety check, service problems)",
    RESOLVED: "'resolved' emails",
    INFO: "information emails (new financial year)",
}


def is_enabled(config, cls: str) -> bool:
    """Is this class switched on? Classes without a switch are always on; a config that does not carry
    the key (an older or partial one) counts as on - never silently off."""
    dotted = SWITCH_KEYS.get(cls)
    if dotted is None:
        return True
    section, name = dotted.split(".", 1)
    return bool(getattr(getattr(config, section, None), name, True))


def switched_off(config) -> list[str]:
    """The classes that are switched off, in a stable order."""
    return [cls for cls in SWITCH_KEYS if not is_enabled(config, cls)]
