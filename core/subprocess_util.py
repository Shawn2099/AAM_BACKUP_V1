"""Subprocess safety — output size limit to prevent memory pressure."""

import subprocess

MAX_OUTPUT_BYTES = 10_000_000  # 10 MB cap for captured stdout/stderr


class SubprocessOutputError(RuntimeError):
    """Raised when subprocess output exceeds the safety limit."""


def run_with_limit(cmd, **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess with an output byte limit.

    Identical to subprocess.run() but raises SubprocessOutputError if
    captured output exceeds MAX_OUTPUT_BYTES. Prevents runaway rclone
    or robocopy output from consuming all available memory.
    """
    timeout = kwargs.pop("timeout", None)
    capture = kwargs.pop("capture_output", False)

    kwargs["stdout"] = subprocess.PIPE if capture else None
    kwargs["stderr"] = subprocess.PIPE if capture else None

    proc = subprocess.Popen(cmd, **kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(proc.args, timeout, output=stdout, stderr=stderr)

    if stdout and len(stdout) > MAX_OUTPUT_BYTES:
        raise SubprocessOutputError(
            f"stdout exceeded {MAX_OUTPUT_BYTES:,} bytes ({len(stdout):,})"
        )
    if stderr and len(stderr) > MAX_OUTPUT_BYTES:
        raise SubprocessOutputError(
            f"stderr exceeded {MAX_OUTPUT_BYTES:,} bytes ({len(stderr):,})"
        )

    stdout_str = stdout.decode(errors="replace") if stdout else None
    stderr_str = stderr.decode(errors="replace") if stderr else None

    return subprocess.CompletedProcess(
        args=proc.args,
        returncode=proc.returncode,
        stdout=stdout_str if capture else None,
        stderr=stderr_str if capture else None,
    )
