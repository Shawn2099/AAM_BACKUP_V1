# Release hygiene scan: fy-overlap (2026-11-23)

Read-only scan. No tracked file was edited, nothing was committed, pushed, tested or fetched. This file is the only new file. Secret-like values are masked here because the repo is public.

Owner note received mid-scan: the data is described as fake or to be rotated. Verdicts below record that statement, which I have not verified. Items that cannot be rotated (history, metadata, LAN addresses) still need an owner decision.

## 0. Scope and base

- There is **no local `main` branch**. Base used: `origin/main` = `8efcccc`.
- Local `fy-overlap` tip = `3b2a0c7`. That commit is **not pushed**. `origin/fy-overlap` = `0432564` is an ancestor of it.
- `git merge-base origin/main fy-overlap` = `8efcccc` (fy-overlap sits directly on origin/main).
- Net diff `origin/main..fy-overlap`: 643 files (447 A, 113 D, 82 M, 1 R), about 93k added lines.

## 1. Secrets and private data

| # | Finding | Evidence | Verdict |
|---|---|---|---|
| 1 | Plaintext NAS login password (13 chars) in a committed doc | `15e58c1` (2026-10-20), `SESSION_HANDOFF.md` line 18 (that revision). Commit is on public remote branches `origin/fy-overlap` and `origin/mvp-hardening`. HEAD no longer has the value (`SESSION_HANDOFF.md` ~line 210 says "in chat" only). `SESSION_HANDOFF.md:68` and `docs/RELEASE_NOTES_DRAFT_2026-11-23.md:210` still list it as open. | **REAL SECRET** in public history. Owner says rotated (UNVERIFIED). Only rotation fixes it; history is not rewritten. |
| 2 | PEM `-----BEGIN PRIVATE KEY-----` headers | Net diff: `docs/TEST_DESIGN_RELEASE_2026-11-22.md:100-101`, `tests/test_break_release_secrets.py:63-66`, `tests/test_s3_gcs_key.py:17`, `tests/test_m5_support_bundle.py:23`. History only adds `deploy/bin/README.*` and `rclone.1` (`2f7572b`, rclone docs). | **TEST FIXTURE**. Bodies are markers (`KEYMARK-…`, `SECRETBODY`). No real key body found in any commit. |
| 3 | Service-account JSON shapes | 41 added lines with `"type": "service_account"`, e.g. `tests/test_s3_gcs_key.py:16`, `tests/test_break_release_secrets.py:63`, `test_support/sandbox_config.py:39`. Project `aam-test-sandbox`, fake client emails. History: 0 real `private_key_id` values. | **TEST FIXTURE** |
| 4 | Cloud API keys (AIza, ghp_, AKIA, xox, webhook URLs) | 0 hits in net diff, 0 in fy-overlap history | none found |
| 5 | Literal test passwords | `test_support/ui_login.py:12-13`, `tests/test_s2_smtp_secret.py:35`, `tests/test_m5_support_bundle.py:21`, `tests/run_chaos_suite.py:108`, `tests/test_m4_settings.py:38`, `tests/test_review_2026_10_19.py:71` | **TEST FIXTURE** |
| 6 | Dashboard password hashes and salts | Tracked hashes only in tests: `test_support/ui_login.py:38-39`, `tests/test_m4_settings_admin.py`. History digests (356 lines) are in test files only (counts: 3 tests/test_m4_settings_admin.py, 2 test_support/ui_login.py, 1 tests/test_ui_auth.py). Config templates have empty hashes (`config.example.yaml:274,278`). | **TEST FIXTURE**. UNSURE for the 356 count, which I spot-checked by file rather than by value. |
| 7 | Email addresses in tree | 53 distinct in net diff, all example or test domains (`*.example`, `*.test`, `*.invalid`, `x.com`, `test.local`). `owner@example.com` in `deploy/config.install.yaml:42`, `noreply@anthropic.com` in `CLAUDE.md:65`. | **TEST FIXTURE / placeholder** |
| 8 | Real identity in git commit metadata | Full fy-overlap history: 207 commits authored with a personal Gmail address, 50 with a company-domain address, 442 `audit-lab@localhost`, 5 `aam-audit@localhost`. Branch range: 4 + 11 commits with the personal or company identity. | **INTERNAL / PERSONAL INFO TO CONSIDER.** Not rotatable. If pushed, already visible on GitHub. Owner decision: mailmap, rewrite, or accept. |
| 9 | Company email addresses in history | Root `config.yaml` (`b33542b`, 2026-05-25) had `smtp_username`, `sender` and `recipients` on the firm domain. | **INTERNAL INFO TO CONSIDER** (no password: `smtp_password` is empty in every version, see section 2) |
| 10 | Client LAN addresses (10.10.x.x, four or more distinct: NAS, server, dashboard) | `MVP_HARDENING_PLAN.md:481`, `SESSION_HANDOFF.md:121,124,210`, `core/email_samples.py:101`, `docs/EMAIL_CONTENT_SPEC.md:235,241,247`, `docs/DEPLOY_LOG_2026-10-19.md:81`, `tests/test_d2_dashboard_link.py:62`, `tests/test_m2_guards.py:107`. Also a 192.168.10.x address in `b33542b` config. Most other IPs are RFC 5737 or 1918 examples. | **INTERNAL INFO TO CONSIDER** (values read as real) |
| 11 | MAC address of the NAS-side NIC, same host as item 10 | `audit_lab/LEDGER.md:1787` (networks list, "Ethernet"), `tests/test_v113_remediation.py:139,141,151` (same MAC) | **INTERNAL INFO TO CONSIDER**. Other MACs are `00:11:22:…` placeholders. |
| 12 | Hostnames, domain, service and PC names | Domain-controller target `AAMBDC001` and domain `caaam.com`: `SESSION_HANDOFF.md:89`, `docs/INSTALL_FRESH.md:93`, `docs/REVIEW_WINDOWS_COMPAT_2026-11-23.md:118`, `tests/test_inst3_install_scripts.py:121`. Trial SSH alias: `CLAUDE.md:67`, `AGENT_TASKS.md:18`. Account `innovizta_2`: `HANDOVER.md:62`, `audit_lab/setup_service_account.ps1:96`. PC name: `docs/RELEASE_BREAK_SESSION_2026-11-22.md:4`, `docs/AUDIT_MAIL_CONTENTS_2026-11-23.md:22`. Discovery snapshot host name in `deploy/server_discovery_report.json` (already on origin/main). | **INTERNAL INFO TO CONSIDER** |
| 13 | Cloud identifiers | GCS buckets `aam-backup-demo-innovizta` (`chaos_harness/safety.py:25`, `restore.py:14`, `docs/GCS_LIFECYCLE.md:3`) and `aam-backup-qa-bilcvx` (`docs/RELEASE_BREAK_SESSION_2026-11-22.md:66`). GCP project number in `AUDIT_FINDINGS.md:95` and `docs/RELEASE_BREAK_SESSION_2026-11-22.md:66` (last in history at `e408543`; not in HEAD code). | **INTERNAL INFO TO CONSIDER**. Identifiers, not credentials. |
| 14 | Personal absolute paths (Windows username) | About 90 hits in `audit_lab/LEDGER.md` (e.g. `:179`, `:1039`). 56 lines each in `audit_lab/raw/evidence.jsonl` and `audit_lab/raw/evidence_snapshot_2026-10-10.jsonl`. Docs: `docs/RELEASE_BREAK_SESSION_2026-11-22.md:17,73`, `docs/PROMPT_FY_OVERLAP_BUILD.md:9`. Others: `tools/server_health/README.md:117`, `AGENT_TASKS.md:40`, `HANDOVER.md:3`, `HANDOVER_PROMPT.md:9`. Old path `C:/Users/Shawn A/…` in `AUDIT_REPORT_LATEST_CHANGES.md:32-39` (already on origin/main). | **INTERNAL / PERSONAL INFO** (low) |
| 15 | Live lab evidence with real values | `audit_lab/raw/evidence.jsonl` (658 KB) and `audit_lab/LEDGER.md` contain items 10, 11 and 14. The `.gitignore` comment says the evidence ledger is tracked on purpose. | **INTERNAL INFO TO CONSIDER** (owner decision: keep, redact, or untrack) |
| 16 | Claude session links in commit messages | 76 commits in fy-overlap history carry `claude.ai/code/session` trailers. | **INTERNAL INFO** (low) |
| 17 | GitHub username | `CODE_RABBIT_DEEP_REVIEW.md:4` (already on main), `docs/PROMPT_FY_OVERLAP_BUILD.md:97` (repo name) | LOW, public by design |
| 18 | Phone-like values | 8 hits. All are fixture or UUID fragments: `tests/test_f1_run_artefact_names.py:59`, `audit_lab/raw/evidence.jsonl:644` (run UUID fragment). | **TEST FIXTURE** |

Also checked: `dpapi:` values in tree are placeholders (`docs/PLAN_OPEN_TOPICS_2026-10-18.md:557`, `tests/test_review_2026_10_19.py:71`). No real DPAPI blob appears in config history.

## 2. Tracked files that should not be tracked

- **config.yaml**: not tracked on fy-overlap (`.gitignore:16`). History: root `config.yaml` was added in `b33542b` (2026-05-25), updated by `2bd328c` ("update config.yaml with real server values"), and removed in `bb812cd` (2026-05-27). Real-looking values (LAN IP, bucket, firm-domain addresses), but `smtp_password` is empty in every version. `client server data/config.yaml` (`f296349`, removed in `9cf4edf`) has placeholders only, plus `192.168.10.10`. Verdict: **INTERNAL INFO TO CONSIDER** (history only).
- **config.example.yaml**, **deploy/config.install.yaml**: templates with placeholders (`example.com`, `00:11:22:33:44:55`, `192.0.2.x`). OK.
- **.env**: not tracked, not in history (`.gitignore:11`).
- **Keys**: no `*.pem`, `*.key`, `*.p12`, `*.pfx` or service-account JSON was ever added. `deploy/keys/` holds only `.gitkeep`. There is **no ignore rule for key files in `deploy/keys/`**; add one.
- **deploy/*.json**: `gcs_lifecycle.json`, `gcs_lifecycle.fy_overlap_proposed.json`, `server_discovery_report.json` (the last has a host name and a 192.168.1.x address; already on origin/main). Config or data, low risk.
- **.cursor/mcp.json**: in history (`22b1517` to `9cf4edf`), deleted. Keys only (`command`, `args`, `env`), no long literal values. Low.
- **Runtime data**: no `*.db`, `manifest.db`, `run_logs/`, `logs/`, `agent_runs/`, `restore_logs/`, `upload_logs/` or `reports/server_health/` in HEAD or in the added-path history. Good.
- **Files over 1 MB in HEAD**: only `deploy/bin/rclone.exe` (85,647,872 bytes, rclone 1.76.0 since `681e7d8`, 2026-11-20, on this branch). 4 commits touch it, each adds about 85 MB to clone size. Intended (installer tool). Owner decision: keep, LFS, or accept.
  - Other binaries, all intended: `deploy/bin/nssm.exe`, `deploy/bin/7za.exe` (under 1 MB), `static/fonts/Geist-Variable.woff2` (56,800 bytes, new on this branch).
- **Untracked scratch (confirmed NOT tracked, `git ls-files` = 0 each)**: `analyze_logs.py`, `analyze_logs2.py`, `coverage_func_scan.py`, `qc`, `query`, `scan_stale_imports.py`, `search_lan.py`, `test_cloud_verify_timeout.py`, `server_discovery_report.md`, `TESTING_SPEC.md`, `AGENTS.md`, `.claude/`, `reports/`, `research_notes/`. `git status` shows them as `??`. None of them is ignored, so `git add -A` would pick them up.
  - `.claude/` holds `settings.json`, `settings.local.json`, `skills/`, `worktrees/`. The two JSON files are covered by `*.json` (`.gitignore:17`). `skills/` and `worktrees/` are not.
  - `qc` and `query` are 13-byte CRLF text files.
- **.gitignore gaps**: covered now: `config.yaml`, `.env`, `*.db*`, `*.log`, `logs/`, `*.json` (except `deploy/*.json`), `agent_runs/`, `reports/server_health/`, `restore_logs/`, `upload_logs/`, `deploy/bin/*.zip`. **Missing**: `.claude/`, `research_notes/`, `reports/` (except server_health), the root scratch files above, `run_logs/`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.dpapi`, `deploy/keys/*` (keep `!deploy/keys/.gitkeep`), `*.bak`.

## 3. Branch structure for `production-release`

- Commits `origin/main..fy-overlap`: **457** (456 on origin/fy-overlap, 1 local-only).
- Merge commits in that range: **18**. First-parent commits: **378**. Non-merge: **439**.
- Rewrite check: `fy-overlap` reflog (88 entries) has no rebase, reset, amend or filter entries. `origin/fy-overlap` is an ancestor of local, so the remote was not force-moved. No squash visible.
- Lineage note: `origin/production` (`3e8673e`) is an ancestor of fy-overlap but **not** of origin/main, so the release carries the production lineage (for example merge `a12aec0`, "Merge origin/production into chaos-validation", is in the range). Owner should know what ships.
- Dry run: `git merge-tree --write-tree origin/main fy-overlap` exits 0, tree `ca49758cbc621e65456759d0231afaeca93aaca2`, no conflict output. A `git merge --no-ff fy-overlap` into a branch from origin/main is **conflict-free at this snapshot**. Recheck if origin/main moves.
- Create the release branch from `origin/main` after a fetch, since there is no local `main`. Not done here.

## 4. Documentation hygiene (docs added or modified by the branch)

- 42 `docs/*.md` files added or modified (54 `*.md` in the diff overall).
- No session-scratchpad path with a session id, no `claude.ai/code` links, no UUID-shaped session ids, no tokens, no owner email in docs.
- Personal absolute path (Windows username): `docs/RELEASE_BREAK_SESSION_2026-11-22.md:17` and `:73`, `docs/PROMPT_FY_OVERLAP_BUILD.md:9`. **INTERNAL / PERSONAL INFO** (low).
- Scratchpad mention without an id: `docs/RELEASE_BREAK_SESSION_2026-11-22.md:68-69` (`<session scratchpad>\elevq\`, `scratchpad\elev_worker2.ps1`). LOW.
- Real client-environment facts in docs: `docs/INSTALL_FRESH.md:93` (AAMBDC001, caaam.com), `docs/EMAIL_CONTENT_SPEC.md:235-247` (server name and two LAN addresses), `docs/DEPLOY_LOG_2026-10-19.md:81` (dashboard address), `docs/GCS_LIFECYCLE.md:3` (bucket), `docs/RELEASE_BREAK_SESSION_2026-11-22.md:66` (QA bucket and GCP project number), `docs/AUDIT_MAIL_CONTENTS_2026-11-23.md:22` (PC name), `docs/REVIEW_WINDOWS_COMPAT_2026-11-23.md:118` (DC name). **INTERNAL INFO TO CONSIDER.**

## 5. Suggested actions (owner decisions, nothing done here)

1. Confirm the NAS password rotation (owner says done; UNVERIFIED). The handoff also says to turn the NAS lockout back on. Decide whether to rewrite history on `fy-overlap` and `mvp-hardening` before any further push. The value is already on public branches, so rotation is the real fix.
2. Decide on identity in commit metadata (mailmap, rewrite, or accept).
3. Add the `.gitignore` rules listed in section 2 before the release push.
4. Decide whether to redact or untrack `audit_lab/raw/evidence*.jsonl` and `audit_lab/LEDGER.md` (LAN addresses, MAC, personal paths), and whether to redact the internal hostnames in docs.
5. Push or not: local `3b2a0c7` is unpushed.
6. UNSURE: whether all 356 pbkdf2 digest lines are test-only (spot-checked by file, not by value). Whether the NAS password is still valid (owner says rotated).
