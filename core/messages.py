"""Every sentence a person reads in the dashboard, in one place.

House style (owner 2026-10-19): say what to DO first, then why, then (for support) the detail.

    act("Wait for the running backup to finish, then try again.", "Two runs cannot start together.")

`act` returns "<action> <reason>"; `detail` goes last in square brackets. `looks_action_first`
is what the message test uses to keep the catalogue honest. Pydantic's built-in validation errors
(a number out of range, a missing value ...) are translated by `validation_text`.
"""

from __future__ import annotations

# ── building blocks ───────────────────────────────────────────────────────────


def act(action: str, reason: str = "", detail: str = "") -> str:
    """"<action> <reason> [detail]" - the action ends with a full stop."""
    action = action.strip()
    if not action.endswith((".", "!", "?")):
        action += "."
    parts = [action]
    if reason:
        reason = reason.strip()
        parts.append(reason if reason.endswith((".", "!", "?")) else reason + ".")
    if detail:
        parts.append(f"[{detail.strip()}]")
    return " ".join(parts)


# An action starts with one of these words (a command, not a description of what went wrong).
ACTION_WORDS = frozenset({
    "add", "ask", "back", "change", "check", "choose", "click", "close", "contact", "enter", "fix",
    "give", "keep", "leave", "log", "make", "move", "open", "pick", "press", "raise", "read",
    "refresh", "remove", "rename", "restart", "retry", "run", "save", "select", "send", "set",
    "sign", "start", "switch", "try", "turn", "type", "use", "wait", "nothing", "dismissed",
    "approved", "saved", "done", "started", "stopped", "sent", "no", "everything",
})


def looks_action_first(text: str) -> bool:
    words = text.strip().split(None, 1)
    return bool(words) and words[0].strip(".,:;!").lower() in ACTION_WORDS


# ── shared dashboard messages ─────────────────────────────────────────────────

TOO_MANY_REQUESTS = act("Wait a few minutes, then try again",
                        "Too many requests were sent in a short time")
TOO_MANY_SIGN_INS = act("Wait a few minutes, then sign in again", "Too many sign-in attempts were made")
SCHEDULER_NOT_ANSWERING = act(
    "Try again in a minute",
    "The scheduler is not answering, so the dashboard cannot tell whether a backup is already "
    "running. Nothing was started")


def leg_name(leg: str) -> str:
    return {"cloud": "cloud backup", "lan": "LAN backup"}.get(leg, f"{leg} backup")


def leg_off(leg: str, what: str = "started") -> str:
    return act(f"Switch the {leg_name(leg)} on in Settings, then try again",
               f"It is turned off, so nothing was {what}")


def leg_running(leg: str) -> str:
    return act(f"Wait for the running {leg_name(leg)} to finish, then try again",
               "Two runs cannot start together")


def could_not_start(what: str, error: object) -> str:
    return act("Try again, and ask support if it keeps failing", f"The {what} could not be started: {error}")


# ── validation errors from pydantic's own checks ──────────────────────────────


def validation_text(err_type: str, ctx: dict | None, fallback: str, unit: str = "") -> str:
    """Action-first text for a built-in pydantic error type; `fallback` when we have none."""
    ctx = ctx or {}
    u = f" {unit}" if unit else ""
    if err_type in ("greater_than_equal", "greater_than") and ("ge" in ctx or "gt" in ctx):
        n = ctx.get("ge", ctx.get("gt"))
        return act(f"Enter {n}{u} or more", "Smaller values are not accepted")
    if err_type in ("less_than_equal", "less_than") and ("le" in ctx or "lt" in ctx):
        n = ctx.get("le", ctx.get("lt"))
        return act(f"Enter {n}{u} or less", "Larger values are not accepted")
    if err_type in ("int_parsing", "int_from_float", "int_type"):
        return act("Enter a whole number", "Letters, decimals and empty values are not accepted")
    if err_type in ("float_parsing", "float_type"):
        return act("Enter a number", "Letters and empty values are not accepted")
    if err_type in ("bool_parsing", "bool_type"):
        return act("Choose on or off")
    if err_type in ("string_type", "string_too_short") or err_type == "missing":
        return act("Enter a value", "This setting cannot be empty")
    if err_type == "string_too_long":
        return act("Enter a shorter value", f"At most {ctx.get('max_length', '?')} characters are accepted")
    if err_type == "literal_error":
        return act(f"Choose one of: {ctx.get('expected', '?')}")
    if err_type == "extra_forbidden":
        return act("Remove this setting", "It is not a setting this version knows")
    return fallback
