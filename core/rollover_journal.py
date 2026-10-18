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


# A BLOCKING step that fails stops the run (nothing after it is attempted until the next
# resume); a non-blocking step that fails is recorded and the run goes on. E.g. a NAS that
# is off must not stop the config switch: the cloud has to protect the new FY from day 1.
BLOCKING = frozenset({FINAL_BACKUPS, SOURCE_FOLDER, CONFIG_SWITCH})

# Steps that only make sense once another step is done.
REQUIRES = {
    NAS_SHUTDOWN: (NAS_FOLDER,),     # only shut down a NAS we actually reached
    ANNOUNCE: (CONFIG_SWITCH,),
}


def prerequisites_met(journal: dict, step: str) -> bool:
    return all(journal["steps"][r]["state"] == DONE for r in REQUIRES.get(step, ()))


def skipped_prerequisite(journal: dict, step: str) -> str | None:
    """The prerequisite that was SKIPPED (then this step is not needed either), if any."""
    for required in REQUIRES.get(step, ()):
        if journal["steps"][required]["state"] == SKIPPED:
            return required
    return None


def attempt_order(journal: dict) -> list[str]:
    """Every step that is not finished, in execution order."""
    return [name for name in STEPS if not is_finished(journal["steps"][name])]


def reset_at_start(journal: dict) -> list[str]:
    """Steps to put back to pending at the start of a run (owner D-17): while the config
    switch is still to do, a `final_backups` that is `done` dates from an earlier run, and
    files changed in the closing FY since then would be left behind by the switch."""
    if (not is_finished(journal["steps"][CONFIG_SWITCH])
            and journal["steps"][FINAL_BACKUPS]["state"] == DONE):
        return [FINAL_BACKUPS]
    return []


# Plain-language names for the dashboard card (no technical words).
LABELS = {
    FINAL_BACKUPS: "Last backup of the old year",
    SOURCE_FOLDER: "New year folder on the server",
    NAS_FOLDER: "New year folder on the NAS",
    CONFIG_SWITCH: "Switch the backups to the new year",
    ARCHIVE: "Move the old year to cheaper online storage",
    NAS_SHUTDOWN: "Turn the NAS off again",
    ANNOUNCE: "Tell the office by email",
}


def describe_steps(journal: dict) -> list[dict]:
    """The journal as display rows (dashboard card), in execution order."""
    rows = []
    for name in STEPS:
        step = journal["steps"][name]
        rows.append({"step": name, "label": LABELS[name], "state": step["state"],
                     "attempts": step["attempts"], "error": step["last_error"],
                     "finished_at": step["finished_at"]})
    return rows
