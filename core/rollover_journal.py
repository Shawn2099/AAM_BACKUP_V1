"""FY rollover step journal - the vocabulary (plan A, saga pattern).

A rollover is a list of idempotent steps whose state is persisted per (from_fy, to_fy)
in the manifest DB (`ManifestDB.rollover_*`). The daily check resumes at the first step
that is not finished, so a crash, a powered-off NAS or a busy slot never leaves a
half-done rollover that nobody knows about. Pure functions only; no I/O here.
"""

from __future__ import annotations

# Step names, in execution order.
FINAL_BACKUPS = "final_backups"    # last backup of the closing FY on every enabled leg + G6
SOURCE_FOLDER = "source_folder"    # new source FY folder + placeholder
NAS_FOLDER = "nas_folder"          # NAS awake, new NAS FY folder + mount marker
CONFIG_SWITCH = "config_switch"    # config.yaml points at the new FY (compare-and-set)
ARCHIVE = "archive"                # old FY objects to the ARCHIVE storage class
NAS_SHUTDOWN = "nas_shutdown"      # NAS powered off again (only if configured)
ANNOUNCE = "announce"              # the INFO email

STEPS = (FINAL_BACKUPS, SOURCE_FOLDER, NAS_FOLDER, CONFIG_SWITCH, ARCHIVE, NAS_SHUTDOWN,
         ANNOUNCE)

# Step states.
PENDING = "pending"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"      # not applicable (cloud disabled, shutdown off); counts as finished
STATES = (PENDING, DONE, FAILED, SKIPPED)

FINISHED_STATES = frozenset({DONE, SKIPPED})


def is_finished(step: dict) -> bool:
    return step["state"] in FINISHED_STATES


def next_step(journal: dict) -> str | None:
    """The first step that is not finished, or None when the rollover is complete."""
    for name in STEPS:
        if not is_finished(journal["steps"][name]):
            return name
    return None


def is_complete(journal: dict) -> bool:
    return next_step(journal) is None
