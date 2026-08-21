"""Phase 4 (v3): prove test_pipe_01's pre-run safety guard aborts BEFORE any
destructive rclone operation when the FY-prefix interception fails.

Failure mode simulated — EXACT section-7.3 incident: the test's patch of the
FY prefix lands on the WRONG module (`core.fy_router.get_fy_prefix`) and is a
no-op, because `flow.py` holds its OWN binding via
`from core.fy_router import get_fy_prefix` (flow.py L29). The pipeline would
then compute the LIVE FY ("FY26-27") and mirror-sync into the PRODUCTION
bucket prefix.

Mechanism:
 - The test does a LOCAL `import flow` inside its body (line 107), fetching the
   module from sys.modules. We therefore swap sys.modules["flow"] for a proxy
   whose `get_fy_prefix` SETTER redirects the assignment to core.fy_router
   (the incident's no-op), while every read delegates to the REAL flow module.
 - The test's own guard then reads `flow.get_fy_prefix()` (the binding the
   pipeline actually calls) -> LIVE FY -> "S3 SAFETY ABORT" -> the test dies
   BEFORE it calls `_run_cloud_pipeline`.
 - Spies on the test module's `_run_cloud_pipeline` binding (the one the test
   calls) and on subprocess.run (the rclone spawn path) prove nothing ran.
 - sys.modules["flow"] and core.fy_router.get_fy_prefix are ALWAYS restored.

Expected verdict: guard fired, zero rclone events in the call phase, pipeline
spy never reached. Fixture-teardown events (suite-owned E2E_TEST_FY purge only)
are tagged phase=teardown and excluded from the verdict.

Run: .venv_audit\\Scripts\\python.exe _s3_guard_proof.py
"""
import sys

import pytest

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\Administrator\Desktop\testing\AAM_BACKUP_V1")

EVENTS = []  # (phase, kind, detail)
PHASE = {"v": "collect"}
STATE = {"real_flow": None, "real_fy_prefix_fn": None, "swapped": False}


def ev(kind, detail):
    EVENTS.append((PHASE["v"], kind, detail))
    print(f"  [{PHASE['v']:8s}][{kind:13s}] {detail}", flush=True)


class WrongTargetFlow:
    """Stands in for `flow` in sys.modules during the test.

    READS  -> the real flow module (the pipeline's actual bindings).
    WRITE  of get_fy_prefix -> redirected to core.fy_router (the section-7.3
    no-op: the patch 'succeeds' on the wrong module, pipeline unaffected).
    """

    def __init__(self, real_flow, wrong_module):
        object.__setattr__(self, "_real", real_flow)
        object.__setattr__(self, "_wrong", wrong_module)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name, value):
        if name == "get_fy_prefix":
            object.__getattribute__(self, "_wrong").get_fy_prefix = value
            ev("interception", "get_fy_prefix patch REDIRECTED to core.fy_router "
                               "(section-7.3 no-op — pipeline binding untouched)")
            return
        setattr(object.__getattribute__(self, "_real"), name, value)


def restore():
    if STATE["swapped"]:
        import core.fy_router as fr
        sys.modules["flow"] = STATE["real_flow"]
        STATE["swapped"] = False
        fr.get_fy_prefix = STATE["real_fy_prefix_fn"]
        ev("restore", "sys.modules['flow'] + core.fy_router.get_fy_prefix restored")


class GuardProofPlugin:
    def pytest_runtest_setup(self, item):
        if item.name != "test_pipe_01_cloud_pipeline":
            return
        PHASE["v"] = "setup"
        tm = item.module
        import flow as real_flow
        import core.fy_router as fr

        STATE["real_flow"] = real_flow
        STATE["real_fy_prefix_fn"] = fr.get_fy_prefix

        def pipeline_spy(*a, **k):
            ev("pipeline", f"_run_cloud_pipeline REACHED: {a[:1]}")
            raise AssertionError("PIPELINE REACHED — guard did not abort first")

        tm._run_cloud_pipeline = pipeline_spy
        real_flow._run_cloud_pipeline = pipeline_spy
        ev("setup", "pipeline spies installed (test-module binding + real module attr)")

        def rec(cmd, *a, **k):
            detail = " ".join(str(x) for x in cmd) if isinstance(cmd, list) else str(cmd)
            ev("rclone", f"subprocess.run: {detail[:220]}")
            raise AssertionError(f"SUBPROCESS SPAWN — guard did not abort first")

        import subprocess as sp
        sp.run = rec
        ev("setup", "subprocess.run recorder installed (global — covers all rclone paths)")

        proxy = WrongTargetFlow(real_flow, fr)
        sys.modules["flow"] = proxy
        STATE["swapped"] = True
        ev("setup", f"sys.modules['flow'] swapped for WrongTargetFlow; "
                    f"LIVE FY via real binding = {real_flow.get_fy_prefix()!r}")

    def pytest_runtest_call(self, item):
        if item.name == "test_pipe_01_cloud_pipeline":
            PHASE["v"] = "call"

    def pytest_runtest_teardown(self, item, nextitem):
        if item.name == "test_pipe_01_cloud_pipeline":
            PHASE["v"] = "teardown"
            restore()

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        if item.name != "test_pipe_01_cloud_pipeline" or call.when != "call":
            yield
            return
        outcome = yield
        report = outcome.get_result()
        if report.failed and report.longrepr:
            msg = str(report.longrepr)
            ev("result", f"test FAILED: {msg[:200].replace(chr(10), ' | ')}")
        elif report.passed:
            ev("result", "test PASSED (unexpected — interception must fail)")
        else:
            ev("result", f"test outcome={report.outcome}")


def main():
    import os
    os.environ["AAM_RUN_REAL_HARDWARE"] = "1"

    import pytest

    try:
        rc = pytest.main(
            [
                "tests/test_rt_06_flow_pipeline.py::test_pipe_01_cloud_pipeline",
                "-x", "-q", "--no-header", "-p", "no:cacheprovider",
            ],
            plugins=[GuardProofPlugin()],
        )
    finally:
        restore()

    print("\n=== verdict ===")
    guard_fired = any(k == "result" and "S3 SAFETY ABORT" in d for _, k, d in EVENTS)
    call_rclone = [(p, k, d) for p, k, d in EVENTS if p == "call" and k == "rclone"]
    pipeline_reached = any(k == "pipeline" for _, k, _ in EVENTS)
    teardown_rclone = [d for p, k, d in EVENTS if p == "teardown" and k == "rclone"]

    # capture the result line for the record
    for p, k, d in EVENTS:
        if k == "result":
            print(f"  result: {d}")
    print(f"  guard fired (S3 SAFETY ABORT):   {guard_fired}")
    print(f"  rclone events in CALL phase:     {len(call_rclone)}")
    print(f"  pipeline spy reached:            {pipeline_reached}")
    print(f"  teardown namespace-purge events: {len(teardown_rclone)} (suite E2E_TEST_FY only)")
    ok = rc != 0 and guard_fired and not call_rclone and not pipeline_reached
    print(f"  ABORTED BEFORE ANY RCLONE:       {ok}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
