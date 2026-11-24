# Live-box test: addendum 2026-11-25 (read after the STATE block in LIVEBOX_TEST_PROGRESS_2026-11-24.md)

## Box facts learned
- Box has loguru 0.7.3 (dev PC 0.8.0). Windows Server 2016 CRT returns a wrapped tm_gmtoff for dates after 2038 (days=-49711), which broke loguru under the fake clock. rig.py now patches loguru's offset under the fake clock (version-robust, 0.7.x and 0.8.x). Pushed.
- Sandbox config was built for FY90-91, so a 2029 start gave a mismatched FY (rollover blocked, 228 false fails). Fixed: FY labels are now derived from --start (any year works; 2090 stays the default and X01-X04 keep their 2090 dates). Lane A for 2026: --start 2026-10-01 --days 728 (ends 2028-09, before 2038).
- Dev PC and box differ in dependency versions: record box package versions in preflight evidence (not done yet).

## Smoke s14e (14 days, 2090 clock, loguru fix proven on the box)
292 PASS, 8 FINDING, 2 FAIL, 7 KNOWN, 0 loguru errors, safety S1-S14 ok, production unchanged. Evidence 244 MB per 14 days (lane A at 509/728 days: ~241 MB, so no size problem).
- 2 FAIL = SPY_AUDIT (2090-12-31 and Sunday 2091-01-07: wake 1 / shutdown 0, expected 1/1). Not yet classified; likely downstream of F-E1 (the failed Sunday audit).

## Harness changes this session (commit a00001c on baseline-v1-overlap, pushed)
Detached runner tools/livebox/box_run.ps1 (start/status), progress.json heartbeat, flow_state + exc_type per event (ok unchanged), FY derived from start date. Not yet on the box; copy before the next run and test the launcher survives an ssh close.

## Lane A (stamp a728, 2090-12-01, 728 days, old code, no progress.json)
Running since 2026-11-24 22:22; at 00:18 on 2026-11-25: 509/728 days, 3071 events, ETA ~01:00. Watchdog printed ERROR "Stale backup" for FY91-92/FY92-93 legs ("last success 482 h ago"): to classify (expected for inactive legs vs finding).

## Product findings so far (owner decides fixes after REPORT.md)
P1 escalation mail suppressed by 24 h cooldown on early start; P2 no-internet classified as login/key error; P3 tiny new-year folder trips AAM-DEL-01; P4 switch completes though new-year cloud backup blocked; P5 blocked rollover leaves NAS on ~15 h; P6 NAS woken once per FY pass (flow.py:1129); P7 two near-identical PROBLEM mails 31 s apart; P8 activation-blocked WARNING mailed as "ACTION NEEDED"; F-E1 2090-12-31 Sunday audit fails verification before first FY91-92 copy; F-OWN-1/2/3/5.

## Next
Lane A report -> classify FAILs/stale-backup ERRORs -> copy new harness to box -> X01, X02, X04, X05 (then X34/35, X09, X44, X24, X40, A730 retention) via the detached launcher -> REPORT.md -> cleanup + production-unchanged check. X42 gated; X17, X37-X39, X45-X48 NOT_RUN. No commits of product code.
