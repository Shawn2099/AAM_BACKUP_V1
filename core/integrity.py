"""Weekly independent integrity audit — read-only deep verification.

Two-concept architecture:

    BACKUP RESULT (COMPLETE/PARTIAL/SUSPECT/FAILED — transfer evidence)
    INTEGRITY STATUS (NOT_VERIFIED/VERIFIED/VERIFICATION_FAILED — this module)

`rclone check` is the verification engine for BOTH legs (measured on
rclone v1.74.2, local backend — see capability notes below). It NEVER
modifies data: no sync/copy/delete flags are used anywhere here.
Results land in the `integrity_audits` manifest table and never rewrite
historical backup rows. Absence of a row means NOT_VERIFIED.

Measured rclone capabilities (this environment, rclone v1.74.2):
  * default `check` (NO --size-only) compares size + MD5 content hash;
    same-size/different-content IS detected ("md5 differ", exit 1).
  * local-backend hashes are computed by reading file contents (no stored
    hashes on NTFS/SMB); no --download flag is required for this.
  * --size-only is BLIND to same-size corruption (exit 0) and must NEVER
    produce VERIFIED — size-only outcomes are recorded as
    VERIFICATION_FAILED with capability=SIZE_ONLY.
  * --one-way: `+` = missing from dest, `-` extras ignored for the gate
    (mirror policy: next transfer converges them); extras still reported.
  * timestamp-only differences compare EQUAL (`=`, exit 0) — content
    equality is the verified property, matching /MIR semantics.
  * exits: 0 = match, 1 = differences, >=2 = process/transport error.
  * GCS side: backend-advertised MD5/CRC32C (not live-probed from this
    rig — recorded as capability=HASH_GCS_ADVERTISED in cloud rows).

Resource model (dual-core Xeon, HDD, DDR3, ~400 GB): single checker
(--checkers 1), one audit at a time, streaming reads, progress logs.
Concurrency is NEVER raised to shorten the audit.
"""

import os
import re
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path, PureWindowsPath

from loguru import logger

from core import audit_pending
from core.process import resolve_binary
from core.rclone_errors import DEFAULT_PATTERNS, find_transport_failure
from core.run_artefacts import RunArtefacts
from core.tool_runner import run_tool
from models.config import AuditConfig, CloudConfig, LanConfig

# Fallbacks for direct callers come from the config model itself - the one
# place a default lives (owner rule); production always passes config.audit.
_AUDIT = {k: f.default for k, f in AuditConfig.model_fields.items() if f.default_factory is None}

# Completion evidence (F-08 class): rclone check's own final verdict line
# ("N differences found"). The --combined diff file is pre-created by
# mkstemp, so its mere presence proves nothing — an exit-0 kill leaves an
# empty diff file behind. VERIFIED additionally requires the summary in
# the process's own log stream plus consistency between the summary
# count and the parsed diff (measured on rclone v1.74.2: summary N ==
# added + modified + removed for a completed run).
_SUMMARY_RE = re.compile(r"(\d+)\s+differences found")
_FAILED_CHECK_RE = re.compile(r"Failed to check", re.IGNORECASE)
_ERROR_LINE_RE = re.compile(r"(?m)^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} (?:ERROR|CRITICAL)\s*:")
_ERRORS_CHECKING_RE = re.compile(r"(\d+)\s+errors while checking", re.IGNORECASE)

# F-T4-2: rclone's NORMAL mismatch output contains "Failed to check:
# N differences found", "N errors while checking" (NOTICE level), and
# per-file ERROR lines such as "ERROR : f1.txt: sizes differ" /
# "md5 differ" (measured live, rclone v1.74.2). None of those, by
# themselves, is a file read/check error — a completed check that found
# differences always emits them. A genuine read/check error is signalled
# by a `!` marker in the --combined file (authoritative) or by an
# ERROR/CRITICAL log line carrying an I/O signal that never describes a
# plain content mismatch (permission/denial, failed read/open, transport
# failure). Anything else fails closed as before; only the CLASSIFICATION
# changed, never the fail-closed verdict.
_IO_ERROR_LINE_RE = re.compile(
    r"permission denied|access (is )?denied|failed to (read|open|lstat|stat)"
    r"|i/o error|input/output error|too many open files|broken pipe"
    r"|connection (refused|reset|timed out)|timed out|unauthori[sz]ed"
    r"|checksum failure|crc error|corrupt(?!\s*diff)|cannot read",
    re.IGNORECASE,
)

# F-T4-1: dest-side mount-health sentinel. Required on every LAN
# destination by preflight (core/lan_preflight.py: strict is_file gate —
# a missing canary refuses the mirror), excluded from every /MIR
# transfer by /XF (core/lan_sync.py + dry-run), and created on new FY
# shares by rollover. It is operational layout, NOT backup data: the
# transfer never converges it, so the bidirectional audit must not
# treat it as divergent backup content.
# Exclusion is by exact basename at ANY depth (never dotfiles in
# general): robocopy /XF matches the filename in every directory, so a
# canary under an FY subdir (e.g. `E2E_TEST_FY/.AAM_TARGET_MOUNTED`) is
# excluded from the transfer exactly like the root one and must be
# excluded from the audit verdict on BOTH sides (missing-from-dest `+`
# as well as extra-in-dest `-` and content-changed `*`).
_CANARY_REL_PATHS = frozenset({".AAM_TARGET_MOUNTED"})
_CANARY_BASENAME = ".AAM_TARGET_MOUNTED"


def _is_canary(rel: str) -> bool:
    """True iff rel path is the operational mount sentinel (any depth)."""
    return PureWindowsPath(rel).name == _CANARY_BASENAME

# Alert/detail payload bound: full mismatch COUNTS are always recorded;
# the path list is truncated (audit.detail_max_paths) so a large divergence
# cannot blow up the manifest row or the alert email.

# Verification capability labels. VERIFIED is recorded ONLY for
# hash-capable comparisons; anything else fails closed.
CAPABILITY_HASH_LOCAL = "HASH_MD5_RCLONE_LOCAL"      # measured §probe
CAPABILITY_HASH_GCS = "HASH_GCS_ADVERTISED"          # backend-advertised
CAPABILITY_SIZE_ONLY = "SIZE_ONLY_INSUFFICIENT"      # never VERIFIED


def _norm_rel(rel: str) -> str:
    return PureWindowsPath(rel).as_posix().lstrip("/")


def _parse_combined_file(diff_file: str | None) -> tuple[list, list, list, list, list, bool]:
    """Parse `rclone check --combined` output. Returns (added, removed,
    modified, unchanged, errors, file_present). added=`+` missing-from-dest,
    removed=`-` extra-in-dest, modified=`*` differ, unchanged=`=`,
    errors=`!` read/access error."""
    added: list[str] = []
    removed: list[str] = []
    modified: list[str] = []
    unchanged: list[str] = []
    errors: list[str] = []
    if not diff_file or not os.path.exists(diff_file):
        return added, removed, modified, unchanged, errors, False
    try:
        with open(diff_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if len(line) >= 3 and line[1] == " ":
                    p = _norm_rel(line[2:].strip())
                    if not p or p == ".":
                        continue
                    if line[0] == "+":
                        added.append(p)
                    elif line[0] == "-":
                        removed.append(p)
                    elif line[0] == "*":
                        modified.append(p)
                    elif line[0] == "=":
                        unchanged.append(p)
                    elif line[0] == "!":
                        errors.append(p)
    except OSError:
        return added, removed, modified, unchanged, errors, False
    return added, removed, modified, unchanged, errors, True


def _incomplete(scope: str, capability: str, detail: str, elapsed: float) -> dict:
    """The audit could not reach a verdict - NEVER reported as divergence."""
    return {
        "status": "AUDIT_INCOMPLETE", "scope": scope, "capability": capability,
        "termination": "abnormal", "files_checked": None, "bytes_checked": None,
        "mismatches": -1, "missing": [], "extra": [], "modified": [], "errors": [],
        "raw_missing": 0, "raw_extra": 0, "raw_changed": 0, "raw_mismatches": 0,
        "canary_excluded": 0, "canary_excluded_paths": [],
        "detail": detail, "elapsed_s": round(elapsed, 1),
    }


def _run_rclone_check(
    source: str,
    dest: str,
    scope: str,
    timeout: int,
    extra_args: list[str] | None = None,
    size_only: bool = False,
    capability: str | None = None,
    checkers: int = _AUDIT["checkers"],
    retries: int = _AUDIT["retries"],
    retries_sleep_seconds: int = _AUDIT["retries_sleep_seconds"],
    detail_max_paths: int = _AUDIT["detail_max_paths"],
    network_error_patterns: tuple[str, ...] | list[str] = (),
    modify_window_seconds: int = _AUDIT["modify_window_seconds"],
    artefacts: RunArtefacts | None = None,
    pending: audit_pending.PendingContext | None = None,
) -> dict:
    """Run one read-only `rclone check --combined` and classify the result.

    NEVER passes sync/copy/delete/move flags. Returns a result dict with
    an explicit capability label; VERIFIED requires hash capability AND a
    clean diff AND a completed process (exit 0/1 with a present diff file
    AND rclone's own "N differences found" summary in its log stream,
    consistent with the parsed diff). A killed/interrupted run — even one
    the OS reports as exit 0 — has no summary and fails closed.
    """
    started = time.monotonic()
    rclone_exe = resolve_binary("rclone") or "rclone"
    diff_file = None
    # Decided BEFORE anything is logged (plan WP7: the cloud label used to be
    # rewritten after the verdict line had already been written).
    capability = CAPABILITY_SIZE_ONLY if size_only else (capability or CAPABILITY_HASH_LOCAL)
    patterns = DEFAULT_PATTERNS + tuple(
        ("CONFIGURED", p, "Matched audit.network_error_patterns in config.")
        for p in network_error_patterns)
    try:
        fd, diff_file = tempfile.mkstemp(suffix=".txt", prefix="integrity_audit_")
        os.close(fd)
        # NOTE: deliberately NO --one-way here (unlike the daily check).
        # The weekly audit is the deep comparison: extra destination objects
        # (`-`) are a reportable divergence (§16 unexpected files), even
        # though the mirror transfers converge them on the next daily run.
        cmd = [
            rclone_exe, "check",
            source, dest,
            "--combined", diff_file,
            "--fast-list",
            "--modify-window", f"{int(modify_window_seconds)}s",
            "--checkers", str(int(checkers)),
            "--retries", str(int(retries)),
            "--retries-sleep", f"{int(retries_sleep_seconds)}s",
        ]
        if size_only:
            cmd.append("--size-only")
        if extra_args:
            cmd.extend(extra_args)
        logger.info(f"Integrity audit ({scope}): rclone check {source} <-> {dest}")
        # plan 5.1: output streams to the run folder; a timeout kills the
        # process tree and is reported as such (never parsed as a verdict).
        result = run_tool(f"rclone-audit-{scope.split('/')[0]}", cmd, timeout_s=timeout,
                          artefacts=artefacts or RunArtefacts.ephemeral("integrity-audit"))
        if not result.completed:
            reason = result.describe_failure()
            logger.error(f"Integrity audit ({scope}) could not run: {reason}")
            return _incomplete(scope, capability,
                               f"capability={capability} audit could not run - execution failed: "
                               f"{reason}", time.monotonic() - started)
        returncode = result.exit_code
        stderr_text = result.stderr_text()
        added, removed, modified, unchanged, errors, present = _parse_combined_file(diff_file)
        elapsed = time.monotonic() - started
        log_text = f"{stderr_text}\n{result.stdout_text()}"
        summaries = _SUMMARY_RE.findall(log_text)
        error_log_lines = [ln for ln in log_text.splitlines() if _ERROR_LINE_RE.match(ln)]
        io_error_lines = [ln for ln in error_log_lines if _IO_ERROR_LINE_RE.search(ln)]
        read_error = bool(errors) or bool(io_error_lines)

        # F-T4-1: drop the operational mount sentinel from the verdict
        # before verdict/counts. /XF excludes this basename at EVERY
        # depth from the transfer, so the audit must mirror that on both
        # sides: source-only canary (`+` missing-from-dest, e.g.
        # `E2E_TEST_FY/.AAM_TARGET_MOUNTED`), dest-only canary (`-`
        # extra-in-dest), and same-path content difference (`*`) are all
        # contractually not backup content. The summary-consistency
        # check below uses the RAW (pre-filter) count: rclone itself
        # counts the canary as a difference, so comparing its verdict
        # against the filtered set would false-positive as
        # "contradictory" on every canary-carrying destination.
        # Phase-3 evidence preservation: snapshot the RAW parse (exactly
        # what rclone reported) BEFORE any control-plane filtering, so the
        # raw_* evidence fields below can never be reconstructed from -- or
        # be lost behind -- the filtered verdict counts.
        raw_added, raw_removed, raw_modified = list(added), list(removed), list(modified)

        added_canary = [p for p in added if _is_canary(p)]
        removed_canary = [p for p in removed if _is_canary(p)]
        modified_canary = [p for p in modified if _is_canary(p)]
        canary_excluded = added_canary + removed_canary + modified_canary
        if added_canary:
            added = [p for p in added if not _is_canary(p)]
        if removed_canary:
            removed = [p for p in removed if not _is_canary(p)]
        if modified_canary:
            modified = [p for p in modified if not _is_canary(p)]
        unchanged_canary = [p for p in unchanged if _is_canary(p)]
        if unchanged_canary:
            unchanged = [p for p in unchanged if not _is_canary(p)]
        raw_mismatches = (
            len(added) + len(modified) + len(removed) + len(canary_excluded)
        )

        # F-T4-3: files actually compared = every classified combined
        # entry (matched `=` rows included — rclone compared them).
        # bytes_checked stays unknown: the checker emits no byte
        # accounting, and stat-based inference would misattribute
        # (both sides are read for hashing; the far side may be remote).
        files_checked = (
            len(added) + len(modified) + len(removed)
            + len(unchanged) + len(errors)
        ) if present else None

        termination = (
            "normal"
            if summaries and returncode in (0, 1) and present
            else "abnormal"
        )
        # WP7 classification order: (1) the audit could not run - transport
        # failure (TLS interception, DNS, auth, network, listing) or a
        # process that never produced a verdict -> AUDIT_INCOMPLETE with the
        # cause, never "content mismatch" (production Sep 27 / Oct 4 / Oct 11
        # showed "missing-from-dest=20, elapsed_s=0" for an unreachable
        # destination); (2) completed with differences -> VERIFICATION_FAILED;
        # (3) completed clean -> VERIFIED.
        transport = find_transport_failure(log_text, patterns)
        if transport is not None:
            detail = (f"capability={capability} audit could not run - {transport.category}: "
                      f"{transport.hint} Evidence: {transport.evidence}")
            logger.warning(f"Integrity audit ({scope}) AUDIT_INCOMPLETE: {detail}")
            return _incomplete(scope, capability, detail, elapsed)
        if returncode >= 2 or not present:
            detail = (f"capability={capability} audit could not run - process error "
                      f"(exit {returncode}): {stderr_text.strip()[:2000]}")
            logger.warning(f"Integrity audit ({scope}) AUDIT_INCOMPLETE: {detail}")
            return _incomplete(scope, capability, detail, elapsed)
        if not summaries:
            # F-08 class: the check did not emit its completion verdict
            # (killed/crashed/truncated - the pre-created diff file may
            # still exist but proves nothing on its own).
            detail = (f"capability={capability} audit could not run - no completion summary "
                      f"(exit {returncode}, termination={termination}): interrupted "
                      "before rclone reported a verdict")
            logger.warning(f"Integrity audit ({scope}) AUDIT_INCOMPLETE: {detail}")
            return _incomplete(scope, capability, detail, elapsed)
        if read_error:
            # BUG-04: read/permission errors must NEVER be recorded as
            # VERIFIED. Fail closed. (F-T4-2: normal mismatch wording —
            # "Failed to check", "N errors while checking", per-file
            # "sizes/md5 differ" ERROR lines — no longer routes here.)
            status = "VERIFICATION_FAILED"
            mismatches = len(added) + len(modified) + len(removed) + len(errors)
            detail = (
                f"capability={capability} file read or check errors occurred during audit "
                f"(error_files={len(errors)}: {errors[:detail_max_paths]}, exit {returncode})"
            )
        elif size_only:
            # §11/acceptance-15: a size-only comparison is NOT deep
            # verification and must never be recorded VERIFIED.
            status = "VERIFICATION_FAILED"
            mismatches = len(added) + len(modified)
            detail = (
                f"capability={capability} size-only comparison is insufficient "
                f"for VERIFIED (missing={len(added)} changed={len(modified)})"
            )
        else:
            mismatches = len(added) + len(modified) + len(removed)
            summary_n = int(summaries[-1])
            if (summary_n == 0) != (raw_mismatches == 0):
                # The process finished but its verdict contradicts the diff
                # it wrote — fail closed rather than trusting either side.
                status = "VERIFICATION_FAILED"
                detail = (
                    f"capability={capability} contradictory audit evidence "
                    f"(summary={summary_n} differences, parsed mismatches={mismatches})"
                )
            else:
                # F-T4-2: this branch is now REACHABLE for normal content
                # mismatches (previously dead code — every diverged run
                # was swallowed by the read-error branch above). The
                # verdict stays fail-closed; only the explanation is now
                # truthful and the divergent paths are preserved.
                status = "VERIFIED" if mismatches == 0 else "VERIFICATION_FAILED"
                detail_paths = (added + modified + removed)[:detail_max_paths]
                waiting = None
                if mismatches and pending is not None:
                    # Only a completed check that found differences reaches the classifier; every
                    # fail-closed branch above was decided without it.
                    waiting = audit_pending.classify(
                        [p for p in added], [p for p in modified], [p for p in removed],
                        replace(pending, now=time.time(), files_checked=files_checked))
                    mismatches = waiting.real_count
                    detail_paths = waiting.real_paths[:detail_max_paths]
                    if mismatches == 0:
                        status = "PENDING_BACKUP"
                if status == "PENDING_BACKUP":
                    detail = (
                        f"capability={capability} every backed-up file matches; "
                        f"{waiting.pending_count} newer file(s) wait for the next backup "
                        f"({waiting.summary}; checked={files_checked} elapsed_s={elapsed:.0f})"
                    )
                elif mismatches == 0:
                    detail = (
                        f"capability={capability} verified clean "
                        f"(checked={files_checked}"
                        f"{f', canary_excluded={len(canary_excluded)}' if canary_excluded else ''} "
                        f"elapsed_s={elapsed:.0f})"
                    )
                else:
                    # Phase-3 evidence integrity: `mismatches` and the
                    # counts below are the EFFECTIVE (canary-filtered)
                    # numbers. The excluded control-plane count and the RAW
                    # pre-filter counts are appended so the durable
                    # telemetry (flow.py persists `detail` into the
                    # integrity_audits manifest row) never loses them.
                    canary_note = (
                        f" canary_excluded={len(canary_excluded)}"
                        f" raw_mismatches={raw_mismatches}"
                        if canary_excluded else ""
                    )
                    real_kinds = ([k for k, _ in waiting.real] if waiting is not None else None)
                    counts = (
                        f"missing-from-dest={real_kinds.count('new')} "
                        f"extra-in-dest={real_kinds.count('removed')} "
                        f"content-changed={real_kinds.count('changed')}"
                        if real_kinds is not None else
                        f"missing-from-dest={len(added)} extra-in-dest={len(removed)} "
                        f"content-changed={len(modified)}"
                    )
                    waiting_note = ""
                    if waiting is not None and waiting.pending_count:
                        waiting_note = (f" {waiting.pending_count} more file(s) wait for the "
                                        "next backup.")
                    if waiting is not None and waiting.tripwire:
                        waiting_note += f" {waiting.tripwire}."
                    detail = (
                        f"capability={capability} content mismatch detected "
                        f"({counts} "
                        f"elapsed_s={elapsed:.0f}{canary_note} paths={detail_paths})"
                        f"{waiting_note}"
                    )
        logger.info(f"Integrity audit ({scope}) {status}: {detail}")
        return {
            "status": status, "scope": scope, "capability": capability,
            "termination": termination,
            # F-T4-3: files_checked is the parsed compared-file count
            # (None when the check never completed). bytes_checked is
            # genuinely unknown — the checker emits no byte accounting.
            "files_checked": files_checked, "bytes_checked": None,
            "mismatches": mismatches, "missing": added, "extra": removed,
            "modified": modified, "errors": errors,
            # Phase-3 evidence integrity: the verdict fields above are
            # EFFECTIVE (canary-filtered). These are the RAW pre-filter
            # results rclone actually reported, plus the control-plane
            # subset removed from the verdict. Raw evidence is ADDITIVE
            # here and is never replaced by the filtered result.
            "raw_missing": len(raw_added),
            "raw_extra": len(raw_removed),
            "raw_changed": len(raw_modified),
            "raw_mismatches": raw_mismatches,
            "canary_excluded": len(canary_excluded),
            "canary_excluded_paths": canary_excluded[:detail_max_paths],
            "detail": detail, "elapsed_s": round(elapsed, 1),
        }
    except OSError as e:
        logger.error(f"Integrity audit ({scope}) could not run: {e}")
        return _incomplete(scope, capability,
                           f"capability={capability} audit could not run - execution failed: {e}",
                           time.monotonic() - started)
    finally:
        if diff_file:
            try:
                Path(diff_file).unlink()
            except OSError:
                pass


def audit_lan(
    source: str,
    dest: str,
    scope_prefixes: list[str] | None = None,
    timeout: int = LanConfig.model_fields["subprocess_timeout_seconds"].default,
    **settings,
) -> dict:
    """Read-only LAN audit via `rclone check` (local backend, hash-aware).

    The LAN backup was created by Robocopy; Robocopy does not participate
    here — rclone independently compares source path ↔ backup path by
    size + MD5 content (measured). No --size-only: size-only can never
    yield VERIFIED. UNC destinations (\\\\server\\share) work through the
    local backend like any other path.

    F-T4-1: the mount sentinel `.AAM_TARGET_MOUNTED`
    (preflight-required, /XF-excluded from every transfer at any depth)
    is filtered by exact basename from both sides before the verdict —
    it is operational layout, not backup data. A clean mirror therefore
    reaches VERIFIED.
    """
    scope = "full" if not scope_prefixes else f"shard:{','.join(sorted(scope_prefixes))}"
    extra = None
    if scope_prefixes:
        extra = []
        for p in scope_prefixes:
            extra.extend(["--include", f"{p.strip('/')}/**"])
    # NOTE: include-filter form is validated by test_scope_sharding; if a
    # future rclone changes filter semantics the test fails closed first.
    return _run_rclone_check(source, dest, f"lan/{scope}", timeout, extra_args=extra, **settings)


def audit_cloud(
    source: str,
    bucket: str,
    fy_prefix: str,
    config_path: str,
    timeout: int = CloudConfig.model_fields["verify_timeout_seconds"].default,
    **settings,
) -> dict:
    """Read-only cloud audit: hash-aware `rclone check` (NOT --size-only).

    Uses the existing GCS/rclone configuration. GCS exposes stored content
    hashes (backend-advertised MD5/CRC32C), so no source re-read beyond what
    rclone performs and no --download flag. Single checker.
    """
    dest = f"aam_gcs:{bucket}/{fy_prefix}"
    return _run_rclone_check(
        source, dest, "cloud/full", timeout,
        extra_args=["--config", config_path, "--gcs-no-check-bucket"],
        capability=CAPABILITY_HASH_GCS, **settings,
    )


def new_audit_id(mode: str, scope: str) -> str:
    """Traceable audit execution id: links the manifest row to the run."""
    return f"{uuid.uuid4()}-{mode}-{scope}"
