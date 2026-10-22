"""Pieces of a SimpleNamespace config for tests of `core.attention.compute_attention`.

Before 2026-10-22 "overdue" was a fixed 26 h window. These pieces reproduce exactly that window with
no dependence on the time of day, so the tests that are about something else (a failed night, the
audit, undelivered mail ...) keep their meaning: a schedule that fires every minute, a 24 h run budget
and 2 h grace give  deadline = last success started + 1 min + 24 h + 2 h  (about 26 h)."""
from types import SimpleNamespace

SCHEDULE = SimpleNamespace(cloud_cron="* * * * *", lan_cron="* * * * *", timezone="Asia/Kolkata")
BUDGET = SimpleNamespace(cloud_max_hours=24, lan_max_hours=24)
GRACE_HOURS = 2
