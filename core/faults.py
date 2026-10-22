"""Fault codes: one plain-language explanation per known cause
(docs/EMAIL_CONTENT_SPEC.md §4, audit 2026-10-18).

Every non-success run gets exactly one code (the first cause that stopped it),
stored in run_history.extended_metrics["fault_code"] and shown in the email,
the dashboard and the report. Codes never change meaning once released; a new
cause gets a new number.

Codes are attached where the cause is known - `coded(exc, code)` at the raise
site, `GuardResult.fault_code` in the guards, the rclone transport category, the
terminal status - never by parsing message text here. Anything without a code
is AAM-SYS-06 ("unexpected program error").

Texts may use only these placeholders, which the email composer always fills
from the config: {source} {dest} {nas} {leg_name}. Run-specific numbers go in
the email's "Details" line (the guard reason / error), never into these texts.
"""

from __future__ import annotations

from dataclasses import dataclass

ACTION = "ACTION NEEDED"
PROBLEM = "PROBLEM"
WARNING = "WARNING"

YOU = "you"
IT = "your IT person"
SUPPORT = "support"
NOBODY = "nobody"

UNKNOWN = "AAM-SYS-06"


@dataclass(frozen=True)
class Fault:
    code: str
    title: str
    severity: str
    who: tuple[str, ...]
    what: str
    steps: tuple[str, ...]


_FORWARD = "Forward this email to support (the attached report shows the cause)."
_NEXT_TRY = "Nothing else is needed: the next backup tries again automatically."
_DONT_APPROVE = ("If not on purpose: do NOT approve - the files are still in the backup; "
                 "call support to restore them.")


def _f(code, title, severity, who, what, *steps) -> Fault:
    return Fault(code, title, severity, tuple(who), what, tuple(steps))


CATALOG: dict[str, Fault] = {f.code: f for f in (
    # ── your shared folder (source) ───────────────────────────────────────
    _f("AAM-SRC-01", "Shared folder not found", PROBLEM, (YOU, SUPPORT),
       "The backup could not open your shared folder {source}. The drive may be "
       "disconnected or the folder renamed. Nothing was copied or deleted.",
       "Check on the server that {source} opens in File Explorer.",
       "If the folder was renamed or moved, put it back or tell support.",
       "Then press the Run button on the dashboard."),
    _f("AAM-SRC-02", "No permission to read the shared folder", PROBLEM, (SUPPORT,),
       "The backup program is not allowed to read {source}. Nothing was copied or deleted.",
       _FORWARD + " Someone probably changed the folder's permissions."),
    _f("AAM-SRC-03", "Shared folder is empty", ACTION, (YOU,),
       "Your shared folder {source} has no files. We stopped the backup so an empty "
       "folder cannot wipe your backup copy.",
       "Check the folder on the server - are your files there?",
       "If files are missing: do NOT approve; call support.",
       "Only if the folder is empty on purpose: approve on the dashboard."),
    _f("AAM-SRC-04", "Many files disappeared from the shared folder", ACTION, (YOU,),
       "Your shared folder has far fewer files than at the last good backup. We stopped "
       "the backup so the missing files stay safe in the backup.",
       "Ask your team whether a large folder was moved or deleted on purpose.",
       _DONT_APPROVE,
       "If it was on purpose: approve on the dashboard."),
    _f("AAM-SRC-05", "Could not count the shared folder's files", ACTION, (SUPPORT,),
       "We could not count the files in your shared folder, so we cannot prove it is "
       "safe to continue. Nothing was copied or deleted.",
       _FORWARD),

    # ── office backup box (NAS) ───────────────────────────────────────────
    _f("AAM-NAS-01", "NAS not reachable", PROBLEM, (YOU,),
       "The office backup box (NAS, {nas}) did not answer on the network. Nothing was "
       "copied or deleted.",
       "Check that the NAS is switched on (its lights are on).",
       "Check the NAS network cable and the office switch/router.",
       "Open the dashboard and press \"Run LAN Backup\".",
       "If it fails again, forward this email to support."),
    _f("AAM-NAS-02", "NAS did not wake up", PROBLEM, (YOU, SUPPORT),
       "We sent the wake-up signal to the NAS ({nas}) but it did not start in time. "
       "Nothing was copied.",
       "Switch the NAS on by hand.",
       "Open the dashboard and press \"Run LAN Backup\".",
       "Tell support: automatic wake-up needs checking."),
    _f("AAM-NAS-03", "Not allowed to write on the NAS", PROBLEM, (IT, SUPPORT),
       "The NAS refused to let the backup save files in {dest}. Usually the NAS user's "
       "password was changed or its permissions were removed. Nothing was copied.",
       _FORWARD),
    _f("AAM-NAS-04", "NAS is full", PROBLEM, (YOU, IT),
       "The NAS does not have enough free space for this backup. Nothing was copied.",
       "Free space on the NAS or add a disk.",
       "Do NOT delete the FY folders of the backup.",
       "Call support if unsure."),
    _f("AAM-NAS-05", "Could not check NAS free space", PROBLEM, (SUPPORT,),
       "We could not measure the free space on the NAS, so we did not start.",
       _FORWARD),
    _f("AAM-NAS-06", "NAS was not confirmed switched off", WARNING, (YOU,),
       "The backup finished, but the NAS ({nas}) did not confirm it switched off. "
       "Your backup is fine.",
       "Check the NAS and switch it off by hand if it is still on.",
       "Tell support if this keeps happening."),
    _f("AAM-NAS-07", "Backup target is on this server", ACTION, (SUPPORT,),
       "The backup target {dest} is a disk of the server itself, not the NAS. A copy on "
       "the same machine does not protect you, so we stopped.",
       "Do NOT approve.", _FORWARD + " The backup target setting is wrong."),
    _f("AAM-NAS-08", "Backup target changed", ACTION, (YOU,),
       "The backup target changed since the last good backup. We stopped to make sure "
       "your files go to the right place.",
       "If you or support changed it on purpose: approve on the dashboard.",
       "If not: do NOT approve; call support."),
    _f("AAM-NAS-09", "New, empty backup target", ACTION, (YOU,),
       "The backup target {dest} is new and empty. This is expected after setting up a "
       "new NAS or folder. Before the first full copy we need your OK.",
       "If this is the new NAS/folder you expected: approve on the dashboard "
       "(type the words shown there)."),
    _f("AAM-NAS-10", "Wrong disk or folder on the NAS", ACTION, (SUPPORT,),
       "{dest} has files but not the backup's marker file, so it may be a different disk "
       "or folder. We stopped so we do not overwrite it.",
       "Do NOT approve.", _FORWARD),
    _f("AAM-NAS-11", "NAS is not storing files reliably", PROBLEM, (IT, SUPPORT),
       "We saved a small test file on the NAS but could not read the same content back. "
       "Nothing was copied.",
       "Check the NAS for disk warnings.", _FORWARD),

    # ── office backup copy (LAN) ──────────────────────────────────────────
    _f("AAM-LAN-01", "Copy to the NAS failed", PROBLEM, (SUPPORT,),
       "The copy to the NAS stopped with an error. Your server's files are untouched.",
       _FORWARD, _NEXT_TRY),
    _f("AAM-LAN-02", "Some files could not be copied", WARNING, (YOU,),
       "Some files could not be copied - usually files that were open (Tally, Excel) "
       "during the backup. All other files were copied.",
       "Close programs at the end of the day.",
       "The list of files is in the attachment. Tonight's backup tries them again."),
    _f("AAM-LAN-03", "Office backup ran out of time", WARNING, (NOBODY, SUPPORT),
       "The office NAS backup reached its time limit and stopped; the rest follows in "
       "the next backup.",
       "Nothing if this happens once (for example after a day with many changes).",
       "If it keeps happening, tell support - the time limit may be too short."),
    _f("AAM-LAN-04", "Office backup did not finish cleanly", PROBLEM, (SUPPORT,),
       "The copy to the NAS ended in an unexpected way, so we cannot prove every file "
       "was copied.",
       _FORWARD, "The next backup re-checks every file."),
    _f("AAM-LAN-05", "Backup user lacks backup rights", PROBLEM, (IT, SUPPORT),
       "The backup user on the server is missing the Windows \"Back up files and "
       "directories\" right. Nothing was copied.",
       _FORWARD),

    # ── online backup (Google Cloud) ──────────────────────────────────────
    _f("AAM-CLD-01", "Cannot reach Google Cloud", PROBLEM, (YOU, IT),
       "The server could not connect to Google Cloud (no internet, DNS or firewall). "
       "Nothing was uploaded.",
       "Check that the server has internet (open google.com on it).",
       "Open the dashboard and press \"Run Cloud Backup\".",
       "If it fails again, forward this email to support."),
    _f("AAM-CLD-02", "Secure connection blocked by the firewall/antivirus", PROBLEM, (IT,),
       "Your firewall or antivirus (for example Sophos web protection) is inspecting the "
       "secure connection to Google, so the connection is rejected. Nothing was uploaded.",
       "Your IT person must exclude storage.googleapis.com and oauth2.googleapis.com "
       "from HTTPS inspection for the server.",
       "Then press \"Run Cloud Backup\" on the dashboard."),
    _f("AAM-CLD-03", "Google Cloud refused the login", PROBLEM, (SUPPORT,),
       "Google refused the backup's key (expired, deleted or permissions removed). "
       "Nothing was uploaded.",
       _FORWARD + " The cloud key must be renewed."),
    _f("AAM-CLD-04", "Cloud key file missing", PROBLEM, (SUPPORT,),
       "The file with the cloud key is missing on the server. Nothing was uploaded.",
       _FORWARD),
    _f("AAM-CLD-05", "Not allowed to save in the cloud", PROBLEM, (SUPPORT,),
       "Google accepted the login but did not allow saving files in the backup bucket. "
       "Nothing was uploaded.",
       _FORWARD),
    _f("AAM-CLD-06", "Upload failed", PROBLEM, (SUPPORT,),
       "The upload to Google Cloud stopped with an error.",
       _FORWARD, _NEXT_TRY),
    _f("AAM-CLD-07", "Online backup ran out of time", WARNING, (NOBODY, SUPPORT),
       "The online backup reached its time limit; the rest is uploaded next time.",
       "Nothing during the first full upload (it takes several nights) or after a day "
       "with many changes.",
       "If it keeps happening after that, tell support."),
    _f("AAM-CLD-08", "Online backup did not finish cleanly", PROBLEM, (SUPPORT,),
       "The upload ended in an unexpected way, so we cannot prove every file was uploaded.",
       _FORWARD, "The next backup re-checks every file."),
    _f("AAM-CLD-09", "Size check after upload failed", PROBLEM, (SUPPORT,),
       "After the upload, the online copy did not match your files in number or size.",
       _FORWARD, "The next backup re-checks every file."),
    _f("AAM-CLD-10", "Could not check the online backup before starting", PROBLEM, (SUPPORT,),
       "The check of the online backup before the upload failed, so nothing was uploaded.",
       _FORWARD, _NEXT_TRY),

    # ── protection against mass deletion ──────────────────────────────────
    _f("AAM-DEL-01", "Backup would delete many files", ACTION, (YOU,),
       "To match your shared folder, this backup would delete many files from the "
       "{leg_name}. We stopped so nothing is deleted without your OK.",
       "Ask your team whether a large folder was deleted or moved on purpose.",
       _DONT_APPROVE,
       "If on purpose: approve on the dashboard."),
    _f("AAM-DEL-02", "Could not count planned deletions", ACTION, (SUPPORT,),
       "We could not work out how many files this backup would delete, so we did not start.",
       _FORWARD),
    _f("AAM-DEL-03", "Stopped at the delete limit", ACTION, (YOU,),
       "During the backup the number of deleted files reached the safety limit, so we "
       "stopped it.",
       "Ask your team whether a large folder was deleted or moved on purpose.",
       _DONT_APPROVE,
       "If on purpose: approve on the dashboard."),

    # ── server clock and financial year ───────────────────────────────────
    _f("AAM-CLK-01", "Server clock is wrong", PROBLEM, (IT,),
       "The server's clock is off compared with internet time. A wrong clock can put "
       "files in the wrong financial-year folder, so we did not start.",
       "Correct the server's date and time (Windows: Settings > Time > Sync now).",
       "Then press the Run button on the dashboard."),
    _f("AAM-CLK-02", "Server clock could not be checked", WARNING, (IT,),
       "No time server answered, so we could not prove the clock is right. The backup ran.",
       "Nothing urgent. If this keeps happening, check the server's internet/time settings."),
    _f("AAM-CLK-03", "Clock is behind - financial-year switch refused", ACTION, (IT, SUPPORT),
       "The server's clock says it is before the financial year the backups already use. "
       "We refuse to switch back.",
       "Correct the server's date and time.", "Then tell support."),
    _f("AAM-CLK-04", "Server clock could not be checked - backup stopped", PROBLEM, (IT,),
       "No time server answered, and the settings say not to back up with an unproven "
       "clock. Nothing was uploaded.",
       "Check the server's internet access (and that time.google.com can be reached).",
       "Then press \"Run Cloud Backup\" on the dashboard."),
    _f("AAM-ROL-01", "Financial year jumped more than one year", ACTION, (YOU,),
       "The server's date says a financial year more than one year after the one the "
       "backups use. That is unusual.",
       "Check the server's date.", "Approve on the dashboard only if this is intended."),
    _f("AAM-ROL-03", "Clock not proven - financial-year switch waits", ACTION, (IT,),
       "The switch to the new financial year needs a proven server clock, but no time "
       "server answered. The backups keep using the current year's folder; nothing was "
       "changed.",
       "Check the server's internet access and its date and time.",
       "The switch is tried again every morning."),
    _f("AAM-ROL-04", "Financial-year switch could not be finished", ACTION, (SUPPORT,),
       "The switch to the new financial year stopped part-way. Your data is safe: nothing "
       "was deleted, and the backup program's settings file is exactly as it was before.",
       _FORWARD, "The switch is tried again every morning and continues where it stopped."),
    _f("AAM-ROL-05", "Old year not moved to cheaper storage yet", WARNING, (SUPPORT,),
       "The financial year switch is done, but the old year's online copy could not be "
       "moved to the cheaper archive storage. Your data is safe; it only costs more until "
       "this works.",
       "Nothing else is needed: it is tried again every morning.",
       "If this email repeats, " + _FORWARD[0].lower() + _FORWARD[1:]),
    _f("AAM-ROL-02", "Last backup of the old year incomplete", ACTION, (YOU, SUPPORT),
       "The final backup of the old financial year did not complete. Switching now would "
       "leave its last changes unprotected.",
       "Fix the backup problem first (see its own email); the switch then runs "
       "automatically."),
    _f("AAM-ROL-06", "Files in the new year are not backed up yet", ACTION, (YOU, SUPPORT),
       "Files were saved in the new financial year's folder, but the backups still use the "
       "old year's folder until the switch is done. Those new files exist on the server only.",
       "Open the dashboard and approve the switch, or fix the problem it shows.",
       "Do not move files out of the old year's folder until the switch is done."),

    # ── weekly safety check ───────────────────────────────────────────────
    _f("AAM-AUD-01", "Safety check found differences", ACTION, (SUPPORT,),
       "The weekly safety check compared every file and found differences between your "
       "shared folder and the {leg_name}.",
       "Do not delete anything.", _FORWARD + " The list of files is attached."),
    _f("AAM-AUD-02", "Safety check could not run", WARNING, (NOBODY, SUPPORT),
       "The weekly safety check could not finish. This does not mean your files differ.",
       "Nothing if it runs next week.", "Tell support if it keeps happening."),

    # ── backup program on the server ──────────────────────────────────────
    _f("AAM-SYS-01", "Backup could not start", PROBLEM, (SUPPORT,),
       "The backup could not start because another backup job was still running.",
       "Tell support if this keeps happening.", _NEXT_TRY),
    _f("AAM-SYS-02", "Backup was interrupted", PROBLEM, (YOU,),
       "The backup was interrupted - the server restarted or the backup program was "
       "stopped while it ran. We cannot prove it finished.",
       "If the server restarted for Windows Update: nothing.",
       "Tell support if this keeps happening. The next backup re-checks everything."),
    _f("AAM-SYS-03", "Backup overdue", ACTION, (YOU, SUPPORT),
       "The {leg_name} has had no successful backup for too long. Either each try fails "
       "(see the other emails) or the backup is not starting at all.",
       "Open the dashboard and look at \"Recent Backups\".", _FORWARD),
    _f("AAM-SYS-04", "Backup tool missing", PROBLEM, (SUPPORT,),
       "A program the backup needs is missing on the server.",
       _FORWARD),
    _f("AAM-SYS-05", "Server disk almost full", PROBLEM, (IT,),
       "A disk on the server is almost full, so we did not start.",
       "Free space on the server disk.", "Call support if unsure."),
    _f("AAM-SYS-08", "Backup slot stuck", PROBLEM, (SUPPORT,),
       "The backup could not start. The program believes another backup is still running, "
       "but none is - an earlier backup was cut off (for example the backup service was "
       "restarted while it ran) and its place was never given back. Your files are not "
       "affected: nothing was changed.",
       "Forward this email to support. Every backup waits for that place, so no backup "
       "will start until support clears it.",
       "Support: with no backup running, clear the stuck place (the reason line of the "
       "attached report says how), then run the backup once."),
    _f("AAM-SYS-07", "Backup night missed", WARNING, (NOBODY, SUPPORT),
       "The server was switched off or busy at the scheduled backup time, and the backup "
       "started too late to run without reaching into office hours. Your data is safe: "
       "nothing was changed.",
       "Nothing is needed. The next scheduled backup copies everything that changed.",
       "Tell support if this keeps happening - the server may be restarting at night."),
    _f(UNKNOWN, "Unexpected program error", PROBLEM, (SUPPORT,),
       "Something unexpected went wrong in the backup program.",
       _FORWARD),
    _f("AAM-SVC-01", "Backup program stopped", ACTION, (SUPPORT,),
       "The backup program on the server is stopped and could not be restarted "
       "automatically. No backups will run until it is fixed.",
       "Forward this email to support now."),
    _f("AAM-SVC-02", "Backup program stuck", ACTION, (SUPPORT,),
       "The backup program did not stop or restart properly.",
       "Forward this email to support now."),
    _f("AAM-SVC-03", "Automatic restarts paused", ACTION, (SUPPORT,),
       "The backup program restarted too often in one hour; automatic restarts are paused "
       "to avoid a loop. No backups run until it is fixed.",
       "Forward this email to support now."),
    _f("AAM-SVC-04", "Scheduler not responding", ACTION, (SUPPORT,),
       "The part that starts the backups on time did not answer and could not be "
       "repaired automatically.",
       "Forward this email to support now."),
    _f("AAM-RPT-01", "No backups recorded", ACTION, (SUPPORT,),
       "No backup ran at all in this period. Backups are not starting.",
       "Forward this email to support now."),
)}

# Guard name -> code when the guard BLOCKs and gave no more specific code.
GUARD_CODES = {
    "G1_SOURCE_HAS_FILES": "AAM-SRC-03",
    "G2_SOURCE_SHRINK": "AAM-SRC-04",
    "G3_MASS_DELETE": "AAM-DEL-01",
    "G4_DESTINATION_IDENTITY": "AAM-NAS-08",
    "G5_CANARY": "AAM-NAS-10",
    "G6_FY_ROLLOVER": "AAM-ROL-02",
}

# core.rclone_errors category -> code (cloud transport failures)
TRANSPORT_CODES = {
    "TLS_INTERCEPTED": "AAM-CLD-02",
    "DNS": "AAM-CLD-01",
    "NETWORK": "AAM-CLD-01",
    "AUTH": "AAM-CLD-03",
}


def get(code: str | None) -> Fault:
    """The catalog entry; an unknown or missing code is AAM-SYS-06."""
    return CATALOG.get(code or "", CATALOG[UNKNOWN])


def coded(exc: BaseException, code: str) -> BaseException:
    """Attach a fault code at the raise site: `raise coded(HealthError(msg), "AAM-...")`."""
    if code not in CATALOG:
        raise ValueError(f"unknown fault code {code!r}")
    exc.fault_code = code
    return exc


def code_of(exc: BaseException | None) -> str | None:
    """The code attached to an exception or its causes (`raise ... from e`)."""
    seen = 0
    while exc is not None and seen < 10:
        code = getattr(exc, "fault_code", None)
        if code:
            return code
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return None


def blocked_guard_code(guard_record: dict | None) -> str | None:
    """Code of the first BLOCKing guard (its own code, else by guard name)."""
    for result in (guard_record or {}).get("results", []):
        if result.get("verdict") == "BLOCK":
            return result.get("fault_code") or GUARD_CODES.get(result.get("guard"))
    return None


def classify_run(leg: str, status: str, *, exc: BaseException | None = None,
                 guard_record: dict | None = None, sync_result: dict | None = None) -> str | None:
    """The one fault code of a finished run; None for a success.

    Order: a code attached where the cause was known, then the blocking guard,
    then the terminal status with the tool's own evidence. Never parses the
    error message."""
    from core import statuses

    if statuses.is_success(status):
        return None
    attached = code_of(exc)
    if attached:
        return attached
    if exc is not None and not isinstance(exc, Exception):
        return "AAM-SYS-02"      # A2: the flow was cancelled / stopped (KeyboardInterrupt, TerminationSignal)
    if status in statuses.BLOCKED_STATUSES:
        return blocked_guard_code(guard_record) or UNKNOWN
    cloud = leg == "cloud"
    sync_result = sync_result or {}
    if sync_result.get("fault_code") in CATALOG:        # set by the tool wrapper itself
        return sync_result["fault_code"]
    if status.endswith("_SLOT_TIMEOUT"):
        return "AAM-SYS-01"
    if status.endswith("_ABANDONED"):
        return "AAM-SYS-02"
    if status.endswith("_MISSED"):
        return "AAM-SYS-07"
    if status.endswith("_SUSPECT"):
        return "AAM-CLD-08" if cloud else "AAM-LAN-04"
    if status == "CLOUD_VERIFY_FAILED":
        return "AAM-CLD-09"
    if status.endswith("_PARTIAL"):
        if not cloud and (sync_result.get("files_failed") or 0) > 0:
            return "AAM-LAN-02"
        return "AAM-CLD-07" if cloud else "AAM-LAN-03"
    if status.endswith("_FAILED") and (sync_result.get("timed_out")
                                       or sync_result.get("exit_code") not in (None, -1)):
        if cloud:
            return TRANSPORT_CODES.get(sync_result.get("transport_category"), "AAM-CLD-06")
        return "AAM-LAN-01"
    return UNKNOWN
