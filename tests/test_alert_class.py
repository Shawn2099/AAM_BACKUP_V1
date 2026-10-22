"""Alert classes (plan docs/PLAN_ALERT_CONTROLS_2026-10-22.md, step A)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core import alert_class as ac

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("kind,key,expected", [
    ("FAILURE", "backup:lan:LAN_FAILED", ac.FAILURE),
    ("FAILURE", "rollover:abandoned", ac.FAILURE),
    ("WARNING", "missed:cloud", ac.FAILURE),
    ("WARNING", "nas_shutdown:10.0.0.5", ac.FAILURE),
    ("WARNING", "rollover:FY27-28:archive", ac.FAILURE),          # a WARNING stays 'failure', whatever the key
    ("WARNING", "audit:all:lan=AUDIT_INCOMPLETE", ac.FAILURE),
    ("ACTION_REQUIRED", "blocked:cloud", ac.DECISION),
    ("ACTION_REQUIRED", "stale:lan", ac.OVERDUE),
    ("ACTION_REQUIRED", "rollover:clock_behind", ac.ACTION),
    ("ACTION_REQUIRED", "rollover:FY27-28:archive", ac.ACTION),   # escalated archive alert is an action
    ("ACTION_REQUIRED", "audit:all:lan=VERIFICATION_FAILED", ac.ACTION),
    ("ACTION_REQUIRED", "watchdog:agent", ac.ACTION),
    ("RECOVERED", "recovered:lan", ac.RESOLVED),
    ("RECOVERED", "watchdog:recovered", ac.RESOLVED),
    ("INFO", "rollover:done:FY27-28", ac.INFO),
    ("INFO", "password:admin", ac.SECURITY),
    ("INFO", "test:2026-10-22", ac.TEST),
    ("SURPRISE", "x:y", ac.ACTION),                                # unknown kind: fail closed
])
def test_classify_table(kind, key, expected):
    assert ac.classify(kind, key) == expected


def test_a_kind_wins_over_a_key_that_looks_like_another_class():
    # a stale:-looking key on a FAILURE is still a failure; the class never comes from the key alone
    assert ac.classify("FAILURE", "stale:lan") == ac.FAILURE
    assert ac.classify("RECOVERED", "blocked:lan") == ac.RESOLVED


# ── coverage: no alert in the code base can dodge the rules ──────────────────

def _key_prefix(node) -> str | None:
    """The literal prefix of a key (a string or an f-string), up to the first placeholder."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = ""
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                out += part.value
            else:
                break
        return out
    return None


def _alert_call_sites():
    """(file, line, key prefix) of every AlertEvent(...) call and every watchdog `_notify({...})` dict."""
    sites = []
    for path in list(REPO.glob("*.py")) + list((REPO / "core").glob("*.py")):
        if path.name in ("alert_class.py", "email_samples.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "AlertEvent":
                key = next((k.value for k in node.keywords if k.arg == "key"), None)
                if key is not None:
                    sites.append((path.name, node.lineno, _key_prefix(key)))
            if isinstance(node, ast.Dict) and path.name == "watchdog.py":
                pairs = {k.value: v for k, v in zip(node.keys, node.values)
                         if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                if "key" in pairs and "kind" in pairs:
                    sites.append((path.name, node.lineno, _key_prefix(pairs["key"])))
    return sites


def test_the_scan_finds_the_known_producers():
    files = {f for f, _l, _p in _alert_call_sites()}
    assert {"flow.py", "watchdog.py", "ui.py", "backup_repository.py"} <= files


def test_every_alert_key_in_the_code_base_has_a_registered_prefix():
    unknown = [(f, line, prefix) for f, line, prefix in _alert_call_sites()
               if not prefix or not any(prefix.startswith(k) for k in ac.KNOWN_PREFIXES)]
    assert not unknown, ("register the key prefix in core/alert_class.KNOWN_PREFIXES (and give the new alert "
                         f"a class in classify): {unknown}")


def test_known_prefixes_all_exist_in_the_code():
    used = {p for _f, _l, p in _alert_call_sites() if p}
    stale = [k for k in ac.KNOWN_PREFIXES if not any(u.startswith(k) for u in used)]
    assert not stale, f"prefixes registered but no alert uses them: {stale}"


def test_classify_event_reads_kind_and_key():
    from core.notify import AlertEvent
    assert ac.classify_event(AlertEvent(key="stale:lan", kind="ACTION_REQUIRED", mode="lan",
                                        title="t", message="m")) == ac.OVERDUE
