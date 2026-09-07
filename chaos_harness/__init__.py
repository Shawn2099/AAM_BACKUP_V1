"""AAM chaos harness (Batch 1) — orchestration, safety, evidence, assertions.

Never implements backup business logic. Invokes the real app on the
chaos rig (`C:\\ChaosTest\\app` via chaos Prefect deployments) and the
existing chaos tools under `C:\\ChaosTest\\tools`, then asserts on
observed application state.

The harness tests exactly one immutable application version per run:
`runner.run(..., app_commit=<sha>)` refuses to execute live scenarios
without an explicit commit pin.
"""

from chaos_harness import assertions, evidence, faults, runner, safety, scenarios

APP_COMMIT = None

__all__ = ["APP_COMMIT", "assertions", "evidence", "faults", "runner", "safety", "scenarios"]
