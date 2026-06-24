# AAM Backup V1 — Production Readiness Roadmap

**Milestone:** v1.0 Production Ready for Windows Server 2016
**Last updated:** 2026-05-27

---

## Phase 1: Dashboard Authentication ✅ DONE

| # | Task | Status |
|---|------|--------|
| 1 | Add DashboardConfig to models/config.py | ✅ |
| 2 | Add dashboard section to config.yaml | ✅ |
| 3 | Implement login/logout + session auth in ui.py | ✅ |
| 4 | Protect all trigger/status endpoints | ✅ |
| 5 | Update run() to use configurable bind/port | ✅ |

---

## Phase 2: Config Enhancements ✅ DONE

| # | Task | Status |
|---|------|--------|
| 1 | Add max_attempts + retry_delay_seconds to LanConfig | ✅ |
| 2 | Add max_attempts + retry_delay_seconds to CloudConfig | ✅ |
| 3 | Add verify_timeout_seconds to CloudConfig | ✅ |
| 4 | Add transfers + checkers to CloudConfig | ✅ |
| 5 | Add mt_threads to LanConfig | ✅ |
| 6 | Add ScheduleConfig + schedule section | ✅ |
| 7 | Update flow.py cloud retry from config | ✅ |
| 8 | Update flow.py lan retry from config | ✅ |
| 9 | Update cloud_verify.py timeout from config | ✅ |
| 10 | Update cloud_sync.py transfers/checkers from config | ✅ |
| 11 | Update lan_sync.py mt_threads from config | ✅ |
| 12 | Update serve.py schedules from config | ✅ |

---

## Phase 3: Deep Code Review ✅ DONE

| # | Task | Status |
|---|------|--------|
| 1 | Run code review on all source files (17 files, 2 BLOCKER + 10 WARNING) | ✅ |
| 2 | Triage findings | ✅ |
| 3 | Fix BLOCKER and WARNING items (2/2 BLOCKER, 8/10 WARNING fixed) | ✅ |
| 4 | Apply automated fixes | ✅ |
| 5 | Verify fixes with syntax + import checks | ✅ |

---

## Phase 4: Test Infrastructure ✅ DONE

| # | Task | Status |
|---|------|--------|
| 1 | Create tests/conftest.py with fixtures | ✅ |
| 2 | Create tests/test_config.py (29 tests) | ✅ |
| 3 | Create tests/test_manifest.py (17 tests) | ✅ |
| 4 | Create tests/test_hashing.py (10 tests) | ✅ |
| 5 | Create tests/test_health.py (17 tests) | ✅ |
| 6 | Create tests/test_fy_router.py (10 tests) | ✅ |
| 7 | Create tests/test_report.py (13 tests) | ✅ |
| 8 | Create tests/test_lan_sync.py (17 tests) | ✅ |
| 9 | Create tests/test_cloud_sync.py (23 tests) | ✅ |
| 10 | Create tests/test_ui.py (20 tests) | ✅ |
| 11 | Verify pytest: **156 passed, 0 failed** | ✅ |

---

## Phase 5: Windows Server 2016 Hardening ✅ DONE

| # | Task | Status |
|---|------|--------|
| 1 | Add config.yaml to .gitignore (+ *.json, .pytest_cache) | ✅ |
| 2 | Add GET /health endpoint (no-auth monitoring) | ✅ |
| 3 | Add CLI enum validation for mode param in flow.py | ✅ |
| 4 | Add subprocess output size limit (core/subprocess_util.py) | ✅ |
| 5 | Security audit — gsd-security-auditor (5 threats identified + remediated) | ✅ |
| 6 | Run bandit: 0 high, 3 medium (false positives), 37 low | ✅ |
| 7 | Run ruff: 39 fixes applied, mypy: 8 remaining (Prefect type stubs) | ✅ |

---

## Phase 6: Final Verification ✅ DONE

| # | Task | Status |
|---|------|--------|
| 1 | Full pytest suite: 156 passed, 0 failed | ✅ |
| 2 | Syntax check: 24/24 source files compile clean | ✅ |
| 3 | Final review: all tasks verified against code | ✅ |
| 4 | Tag version commit: ready | ✅ |
| 5 | Planning docs synced | ✅ |

---

## v1.0 Production Ready — Complete

**17 files modified, 550 insertions, 296 deletions across 6 phases.**
