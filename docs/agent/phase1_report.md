# Phase 1 report: Foundation

Branch `claude/design-system-extraction-vnav5h`. Phase 1 base `a4df7a3`. Brief + Amendment 01
+ the owner's answers 1–4 and plan changes P1–P6.

## Exit rules (Amendment A11)

| # | Rule | Status |
|---|---|---|
| 1 | Test baseline (A2) | **Met.** Full suite: 261 run, 0 failures on SQLite (2 skipped) and on PostgreSQL 16 (7 skipped); 168 core + 93 agent/intake. All 168 core tests pass. |
| 2 | `test_core_contract` passes | **Met.** 29 pinned signatures plus a source scan that fails on any unpinned core import. |
| 3 | Golden check matches exactly on the listed scopes, on PostgreSQL | **Met on synthetic scopes only** (a revised schedule and a two-schedule scope). **Open on real data**: you run it per `runbook_real_data.md`. |
| 4 | `agent_core_audit` ran and its report is committed | **Met on synthetic data** (`core_audit_20260925_SYNTHETIC.md`). **Open on real data** (same runbook). |
| 5 | `nova_tools_review.md` written | **Met.** |
| 6 | No protected path, nothing under `core/` | **Met for code.** The one non-listed file is `CLAUDE.md` (the §19 append that brief §17 requires); guardian check 1 needs your exemption, see below. |

## What was built

**Data (`agent/`, `intake/`; migrations for these apps only)**
- `AgentConfig`: a single row. `enabled=False`, `autonomy_level=0`, `mapping_threshold=0.92`, `grace_days=3`, `upload_debounce_minutes=10`, `tc_intake_mode='off'`.
- `AgentAccountOverride`
- `ScopeState`: exact channel/month and `lock_baseline` for V2.
- `ScheduleStatus`, `AgentRun`, `AgentAction`, `AgentProposal`
- `SummarySnapshot`: one per schedule. `AgentAuthorisation`: per schedule, stores the snapshot sha256.
- `LlmCall`, `NotificationLog` (unique dedupe key), `ScopeLockRow` (`expires_at`), `Heartbeat`
- `InboundEmail`, `InboundAttachment` (unique on message_id and email+sha256), `AllowedSender`

**Behaviour**
- `scope.py`: scope keys copied from Schedule. Active schedules come only from `active_schedule_ids`, and makeup schedules from `_makeup_schedules_for_scope`.
- `locks.py` (P2):
  - PostgreSQL: `pg_try_advisory_xact_lock(key)` taken inside `transaction.atomic()`, so it is released on commit, rollback or crash.
  - Other databases: a `ScopeLockRow` committed first, with a 15-minute expiry (expired rows count as free), released in `finally`.
  - Key: signed 64-bit integer from sha256(account_id|channel|month).
- `readiness.py`: the single source of scope state. Brief §7 states, plus NEEDS_HUMAN for V4. Rule 8 rows are "waiting" (`pending_rows`), never a failure.
- `validate.py`: V1–V5 exactly as A3. There is no "3rd Party ≥ Aired" check, and a test forbids one.
- `diagnose.py`: the CLAUDE.md §16 symptoms, all 8 A5 findings, and `LOCK_ORPHANED` with sub-codes TC_LMRB, SPONSORSHIP, MANUAL and COMMERCIAL. Relation names were checked in `core/models.py` and all exist.
- `gate.py`:
  - Tiers: T0 always; T1 at level ≥ 1; T2 never auto (A8); T3 only at level 3 with every condition, including `exact_value`; T4 never.
  - Re-reads `AgentConfig` immediately before each write.
  - Every write records an `AgentAction` with before and after values.
- `tools/read.py`: the brief's read tools; all take IDs and return JSON-safe results.
- `tools/reconcile.py`:
  - `reconcile_scope` follows A4:
    1. `run_scope` once. It is skipped with `no_commercial_rows` when the pre-check finds none (P1); any other `ValueError` propagates.
    2. Per active schedule: `reconcile_tc` then `reconcile_sponsorship`, both with `schedule_id`.
    3. `reconcile_period_sponsorship` per PeriodSponsorship.
    4. `build_summary_data(schedule_id=…)` per schedule.
  - Everything runs inside one atomic block under ScopeLock.
  - A V1/V2/V3 failure rolls the run back and sets NEEDS_HUMAN. An unexplained V5 change also sets NEEDS_HUMAN.
  - `dry_run` rolls back and is PostgreSQL-only. It is used only by tests and the golden check (P6).
- `service.py` and `agent_ensure_service_user` (A9): role `operations`, unusable password, all accounts, re-synced on each run. **Tested: it can export the Summary Excel in-process** (A12 trigger not hit).
- `agent_core_audit` (A10, P4): `SET TRANSACTION READ ONLY` on PostgreSQL, always rolled back, markdown report.
- `agent_golden_check` (P3):
  - `snapshot`, `verify --mode idempotent` (the exit gate) and `verify --mode rebuild`.
  - Rebuild refuses to run unless `AGENT_DISPOSABLE_DB=1`. It clears engine state using engine functions only: `run_scope`/`reconcile_tc` in reset mode, `auto` sponsorship assignments only, and period matches.
  - Comparison: sha256 of canonical JSON, reported per schedule and per brand.
  - Refuses SQLite outside tests.
- Preview pages, changed only as approved in items a–d:
  - column relabelled "3rd Party (TC confirmed by LMRB)"
  - the "Header fields are shared by all schedules in this scope." notice
  - state logic switched to `readiness.py`
  - No Avg 30s or ≥ check existed to remove. The 16 tests moved unchanged.
- `docs/agent/patches/0001_base_nav_rename.diff`: renames the nav group to "Reconciliation Agent" (`git apply --check` passes). The Nova chat button is a separate floating button, not in that group, so it is untouched.

## Files

**Added**
- `agent/`: `models.py`, `admin.py`, `migrations/0001_initial.py`, `canonical.py`, `scope.py`, `locks.py`, `permissions.py`, `readiness.py`, `checks.py`, `diagnose.py`, `validate.py`, `gate.py`, `service.py`, `core_audit.py`, `golden.py`, `tools/{__init__,read,reconcile}.py`, `management/commands/{agent_ensure_service_user,agent_core_audit,agent_golden_check}.py`
- `agent/tests/`: `factories.py` and 10 test modules
- `intake/`: `apps.py`, `models.py`, `admin.py`, `migrations/0001_initial.py`, `tests/test_models.py`
- `docs/agent/`: `nova_tools_review.md`, `runbook_real_data.md`, `core_audit_20260925_SYNTHETIC.md`, `synthetic/` (seed, golden list, two golden reports), `patches/0001_base_nav_rename.diff`, this report
- `.github/workflows/agent-ci.yml`

**Changed**
- `ad_monitor/settings.py`: `+ 'intake'`
- `agent/{scopes,views,apps}.py` and `templates/agent/scope_detail.html` (items a–d)
- `docs/agent/discrepancies.md`: D23, D24; D21 marked resolved
- `.claude/agents/numbers-guardian.md`: the owner's text
- `CLAUDE.md`: Section 19 appended

**Not changed:** `core/`, `verification/`, `accounts/`, `templates/base.html`, `railway.json`, `Procfile`, `requirements.txt`, `ad_monitor/urls.py`.

## Test results

| Run | Result |
|---|---|
| SQLite, full suite | 261 run, 0 failures, 2 skipped (PostgreSQL-only lock tests) |
| PostgreSQL 16, full suite | 261 run, 0 failures, 7 skipped (SQLite-only fallback/refusal tests); advisory-lock contention and release-on-rollback pass |
| `makemigrations agent intake --check` | No changes |
| Golden `verify --mode idempotent` (PostgreSQL, synthetic) | MATCH: #101 (revised v2), #201 and #202 (two schedules, one scope) |
| Golden `verify --mode rebuild` (PostgreSQL, synthetic, `AGENT_DISPOSABLE_DB=1`) | 0 differences |

## Audit headline (SYNTHETIC DATA; the real figures come from your run)

The synthetic database seeds each issue on purpose, so these counts show only that the
audit detects them. They say nothing about production:

| Issue | Rows | Accounts |
|---|---:|---:|
| Wildcard tc_theme on commercial brands | 1 | 1 |
| Scopes with superseded schedules | 2 | 2 |
| Duplicate active schedule numbers | 1 | 1 |
| Mixed-width numbers / locked schedules | 1 / 1 | 1 / 1 |
| ManualMatch rows that lost is_manual_matched | 1 | 1 |
| Time-belt TC rows without mapping | 0 | 0 |
| Channel case/whitespace variants | 1 | 1 |
| LMRB rows with more than one lock flag | 1 | 1 |
| Unlinked TC in scheduled scopes | 1 | 1 |
| LOCK_ORPHANED (all sub-codes) | 4 | 1 |

## Numbers-guardian review

First review (range `a4df7a3...527ae8d`): **BLOCK** on 2 of 17 checks; 15 PASS or N/A. Both suites passed.

| Check | Finding | Resolution |
|---|---|---|
| 5 (core writes through the gate) | `reconcile_scope(change=…)` ran the hook even when `dry=False`: a committed write outside `gate.perform`. It was latent, since only tests passed `change`. | **Fixed.** `reconcile_scope` now raises before anything runs if `change` is given without `dry=True`, with a test. Also hardened: `golden._clear_engine_state` now checks `AGENT_DISPOSABLE_DB=1` and an active transaction itself, instead of trusting its caller (with a test). |
| 1 (allowed paths) | `CLAUDE.md` changed (the §19 append). It is not on the check-1 list. | **Needs your decision.** Brief §17 step 4 requires the append, but check 1 has no exemption for it. Either add "CLAUDE.md §19 (append only)" to check 1, or I move the text elsewhere. I have not reverted it. |

Non-blocking notes from the guardian, for later phases:
1. **AgentAction stores summary hashes, not values.** The full before/after summaries are in the rolled-back result, not on the action. Store the per-schedule summaries before Phase 4 so reverts and audits can show *what* changed.
2. **No actor yet.** `reconcile_scope` has no actor by default. A9 wants the service user on every action; this gets wired in with `agent_cycle` (Phase 2/3).
3. **The service user command writes `accounts.User` without an AgentAction.** A9 asks for this and a person runs the command, but please confirm that exemption.
4. **`AgentAuthorisation` uses `on_delete=PROTECT`** on Schedule and User. Once authorisations exist (Phase 5), core's delete views would raise `ProtectedError`. Revisit before Phase 5.
5. **The standalone TC↔LMRB path has no V2/V3 check** after a real run.
6. **T3 trusts the caller's `exact_value` flag.** The gate should inspect the value for `*` itself before Phase 4.
7. **The CI push filter** doesn't include `docs/agent/**`, which is intentional since docs don't affect tests.

## Open questions

1. **Makeup schedules and per-schedule TC.** `reconcile_tc(schedule_id=sid)` does not include makeup rows. Makeup rows are only added when `schedule_id` is None (`tc_engine.py:295-300`), which A4 forbids. The agent includes makeup schedules exactly as `run_scope` does, but a makeup schedule's TC is reconciled only in its own scope's run. Is that the intended behaviour?
2. **V5 can't see BrandMapping edits.** BrandMapping has no timestamp. A mapping edit made by a person between runs changes the numbers with no record to explain it, so V5 marks the scope NEEDS_HUMAN ("unexplained change"). Accept this, or add a timestamp to BrandMapping? The latter is a core change and needs your approval.
3. **The "Agent Overview" audit card (A10)** is not built, because this phase has no new UI. Should it come in Phase 2 with the other tabs?
4. **Page subtitles** under `templates/agent/` still say "Nova agent". Renaming them to "Reconciliation Agent" (A7) was not in the approved items a–d. Approve that one-word change?
5. **CI** runs on every push touching `agent/**`, `core/**` and so on. Actions minutes and a workflow token scope are needed on the GitHub side.

## Next step

You:
- apply `patches/0001_base_nav_rename.diff` after reviewing it
- run `runbook_real_data.md` on a restored copy, with your golden list
- send back the audit and golden reports

Then Phase 2 (simple workflow and email TC intake in off/suggest mode).

---

# Phase 1.1

Range `a21c5bb..HEAD`, done in small commits. The owner's decisions 1–6 and items 7–9 are
implemented. Phase 2 has not started.

## Decisions

| # | Decision | Done |
|---|---|---|
| 1 | CLAUDE.md §19 kept; guardian check 1 made append-only | Check 1 text updated; the §19 "Phase 1.1" block is additions only |
| 2 | Makeup schedules, per schedule only | Test: each makeup schedule is reconciled in exactly one scope loop, and never with `schedule_id=None`. No makeup schedule is left unreconciled. Also added: the `MAKEUP_LINKED` finding (parent and makeups, each with its scope and report row count), `ScheduleStatus.makeup_linked`, and `makeup_linked` / `makeup_never_reconciled` audit sections. D25 records the pending finance billing rule |
| 3 | V5 and edits made by people | See "External-change fingerprint" below |
| 4 | Audit card preparation | `agent_core_audit` saves an `AgentRun(kind='audit')` holding counts and the report, in its own transaction after the read-only one has rolled back. Guardian check 17 updated |
| 5 | Rename | "Reconciliation Agent" in `templates/agent/**` and in agent/ strings. The Nova chat widget and core are untouched |
| 6 | Service user | `ensure_service_user` is run by a person: it logs no AgentAction and prints every change. `sync_service_user` runs in the system cycle: it only adds accounts and logs `service_user_account_sync` with before/after ids. `require_service_user` checks that the role is `operations`; if not, it logs an error, sets `Heartbeat.alert` and stops. Guardian check 18 added |
| 7 | Guardian notes | `phase1_followups.md`. Notes 1, 2, 3, 5 and 6 fixed; note 4 goes to Phase 5; note 7 won't fix |
| 8 | D24 `.distinct()` | All 36 core call sites are classified in `discrepancies.md`. **None affects a Summary number** |
| 9 | CI | Workflow permission is `contents: read`, with no secrets. It runs on `pull_request` and on pushes to this branch |

## External-change fingerprint

Field names were checked in `core/models.py` and none are missing.

`agent/fingerprint.py` records a snapshot per account of:
- BrandMapping rows (all mapping fields) and TcLmrbThemeMap rows
- ManualMatch ids and manual SponsorshipLmrbAssignment ids
- PeriodSponsorship (dates, planned_count, theme)
- TransmissionReport (schedule_id, uploaded_at, row_count)
- Schedule (version, is_superseded, is_locked)
- the latest MonitoringData upload, and the latest MatchResult run for the scope
- the tolerance, every `tc_extra_*` / `lmrb_extra_*` alias, and the sponsorship keywords, all read through `get_setting`

How it is used:
- The JSON and its sha256 are stored with every `SummarySnapshot`.
- V5 accepts a number change when an AgentAction was logged or the fingerprint changed. The row-level diff (added / removed / changed, by id and field) goes into `AgentRun.detail['external_changes']`.
- A number change with no explanation → NEEDS_HUMAN.
- **AUTHORISED scopes run no engine.** They are compared with the authorised snapshot, inside one read-only snapshot transaction. An explained change opens an `AgentProposal(kind='amendment', tier T4)` that holds both full summaries and the diff; duplicates are not created. An unexplained change → NEEDS_HUMAN.
- The 4 required tests are in `agent/tests/test_fingerprint.py`, with 5 further tests:
  - editing a mapping → explained, and the diff names the mapping id
  - changing an alias setting → explained
  - a number shift with nothing else changed → NEEDS_HUMAN
  - an authorised schedule changes → amendment proposal

## Results

| Run | Result |
|---|---|
| SQLite, full suite | 283 run, 0 failures, 2 skipped |
| PostgreSQL 16, full suite | 283 run, 0 failures, 7 skipped |
| `makemigrations agent intake --check` | No changes (migration `agent/0002_phase1_1`) |
| Golden `verify --mode idempotent` (PostgreSQL, synthetic) | **MATCH**: #101, #201, #202 |
| Golden `verify --mode rebuild` (synthetic, `AGENT_DISPOSABLE_DB=1`) | 0 differences |

## Numbers-guardian review (range `a21c5bb...b86371f`)

**BLOCK on 2 of 18 checks.** The rest pass or are N/A. 282 tests passed.

| Check | Finding | Resolution |
|---|---|---|
| 5 | `gate.perform` skipped the kill switch for T0, so the service-user account sync (a write to `accounts.User`) never read `AgentConfig.enabled` | **Fixed.** `perform` re-reads the kill switch before every write, for every tier. Test: `test_sync_respects_kill_switch` |
| 1 | `.claude/agents/numbers-guardian.md` changed (checks 1, 17 and 18). It is not on the allowed list | **Needs your confirmation.** The change carries out your decisions 1, 4 and 6 word for word. The guardian itself started with the older 17-check file, so it asks you to confirm the checklist change or add `.claude/agents/` to the allowed list |

Non-blocking notes:
1. `_authorised_check` read summaries outside a lock. **Fixed:** all its reads now run in one always-rolled-back transaction, `REPEATABLE READ READ ONLY` on PostgreSQL.
2. The fingerprint covers the whole account, so an edit on another channel also counts as an explanation. This is follow-up F2, assigned to Phase 3; the diff is always saved.
3. Snapshots made before 1.1 have no fingerprint. The first 1.1 run treats any number change as unexplained, which puts the scope in NEEDS_HUMAN. That is the safe direction; expect it once.
4. Deleting an older MonitoringData file does not move `monitoring_uploaded_max`. The resulting LMRB drop counts as unexplained, which is the safe direction.
5. Standalone V2 counts the whole channel (follow-up note 5).

## Open for you

1. Guardian check 1: confirm the guardian checklist change (commit `4b19724`).
2. First 1.1 run: expect NEEDS_HUMAN on scopes whose numbers moved since their last pre-1.1 snapshot (note 3).
3. The Phase 5 items in `phase1_followups.md`: PROTECT, partial authorisation, and the amendment UI.

### Guardian checklist replaced (owner decision, after the Phase 1.1 review)

The owner replaced `.claude/agents/numbers-guardian.md` with the original 8-check version
from the brief. This resolves the check 1 BLOCK above. Checks 9–18 are no longer part of the
guardian. The rules they covered (always pass `schedule_id`, ScopeLock, the read-only
commands, the core contract, the service user) are still enforced by the agent tests.

---

# Phase 1.2

Items 1–5 from the owner's Phase 1.2 message. Phase 2 exists as a plan only.

## 1. Guardian governance
- I made no change to commit `4b19724` or to any file under `.claude/`. Every `.claude/`
  change is now delivered as a patch file (`docs/agent/patches/README.md`):
  - `0002_claude_settings_deny.diff` adds `Edit(./.claude/**)` and `Write(./.claude/**)` to the deny list.
  - `0003_guardian_check1_claude_paths.diff` changes guardian check 1: `.claude/**` may change
    only through a patch a person applied. The guardian checks the commit author. The author
    `Claude <noreply@anthropic.com>` means FAIL; an author it can't identify reliably means
    "needs human confirmation".
- `git apply --check` passes for both patches.
- Every commit from this session is authored `Claude <noreply@anthropic.com>`. A commit made
  under your own git identity is therefore easy to tell apart.

## 2. First-run baseline
| Case | V5 result | Stored | Shown |
|---|---|---|---|
| No earlier SummarySnapshot | ok, `baseline: no_prior_snapshot` | `ScheduleStatus.baseline_reason` | `BASELINE` info finding; one grouped "Baseline runs" card on the overview |
| Earlier snapshot saved before 1.1 (no fingerprint, or the 1.1 layout) | ok, `baseline: baseline_no_fingerprint` | same | same |

- A baseline never produces NEEDS_HUMAN. The scope keeps the state `readiness.py` gives it,
  and the baseline appears as a reason on the schedule. I used the reason form rather than a
  new BASELINE state so a real state such as MAPPING is never hidden.
- The next run compares against the new baseline and clears the reason.
- Exception: an **authorised** schedule never accepts a baseline. If its authorised snapshot
  has no current fingerprint, a number change stays unexplained and goes to NEEDS_HUMAN.
  Authorised numbers are never accepted silently. (AgentAuthorisation rows cannot predate 1.1,
  so this cannot happen in production.)
- Tests: `test_baseline_no_prior_snapshot`, `test_baseline_pre_1_1_snapshot_without_fingerprint`,
  `test_first_run_is_baseline_not_needs_human`, `test_pre_1_1_snapshot_with_changed_numbers_is_baseline_and_grouped`.

## 3. Scope-relevant fingerprint (F2, moved here from Phase 3)
The whole-account diff is still saved in `AgentRun.detail['external_changes'][schedule]['account_wide']`,
for information. Only `['relevant']` can explain a change:

| Section | Relevant to the scope when |
|---|---|
| BrandMapping | the old or new row's `brand` (lower-case, trimmed) is a brand of the scope's active ScheduleRows, **or** its `theme` matches a scope LMRB theme, **or** its `tc_theme` matches a scope TC theme. Themes are lower-case and trimmed as the engines do; pipe parts are split; a `*` suffix is a prefix match |
| TcLmrbThemeMap | `tc_theme` matches a scope TC theme (same matching) |
| ManualMatch, manual SponsorshipLmrbAssignment, PeriodSponsorship, TransmissionReport, Schedule | channel **and** month equal the scope's, byte for byte |
| settings (tolerance, aliases, sponsorship keywords) | always |
| MonitoringData ids, LMRB count, MatchResult last run | always (they are already scope-level) |
| latest MonitoringData upload time (whole account) | never; information only |

- A row counts when either its old or its new version is relevant, so moving a brand out of
  the scope still counts.
- The fingerprint format is now version 2. Snapshots in the 1.1 layout are treated as
  `baseline_no_fingerprint`.
- Tests: a mapping edit for a brand on another channel does NOT explain a change; a mapping
  edit in this scope does; a wildcard theme mapping that matches this scope's LMRB does.

## 4. Monitoring deletions
- The fingerprint now includes the sorted MonitoringData ids for the account and channel, and
  the scope's LMRB row count. The channel filter is the engines' own `_lmrb_channel_q`, because
  MonitoringData stores the clean channel name, as LMRB rows do.
- Test: deleting a MonitoringData file counts as explained, and the diff names the removed id.

## 5. Exact test counts
| Point | Database | Run | Passed | Skipped | Failed |
|---|---|---:|---:|---:|---:|
| Phase 1.1 guardian review (`b86371f`) | SQLite | 282 | 280 | 2 | 0 |
| Phase 1.1 final (`e833ca5`, +1 kill-switch test) | SQLite | 283 | 281 | 2 | 0 |
| Phase 1.1 final (`e833ca5`) | PostgreSQL 16 | 283 | 276 | 7 | 0 |
| Phase 1.2 (`a9b9317`, +7 fingerprint/baseline tests) | SQLite | 290 | 288 | 2 | 0 |
| Phase 1.2 (`a9b9317`) | PostgreSQL 16 | 290 | 283 | 7 | 0 |
| **Phase 1.2 final (+1 guardian-note test)** | **SQLite** | **291** | **289** | **2** | **0** |
| **Phase 1.2 final** | **PostgreSQL 16** | **291** | **284** | **7** | **0** |

Why "283 run, 282 passed" didn't add up: the 282 came from the guardian's run at `b86371f`,
before the kill-switch test was added. After that commit, SQLite ran 283 tests with 281 passed;
nothing failed. Skipped tests are expected on every run, because some tests only apply to one
database:
- SQLite skips 2 tests that need PostgreSQL advisory locks
  (`AdvisoryLockTest.test_contention_between_connections_and_release_on_rollback`,
  `test_scope_lock_requires_atomic_and_nests`).
- PostgreSQL skips 7:
  - 4 `FallbackLockTest` tests (the table lock is for other databases);
  - `DryRunTest.test_refused_on_sqlite_without_setting`;
  - `ReconcileScopeTest.test_lock_contention_skips_nothing_silently` (fallback lock only);
  - `GoldenCheckTest.test_refuses_sqlite_outside_tests` (it tests the SQLite refusal).
- The `eval` tag is excluded by default, and no eval tests exist yet.

Also: `makemigrations agent intake --check` shows no changes (new migration `agent/0003_phase1_2`).
The golden check `verify --mode idempotent` on the synthetic PostgreSQL data gives **MATCH**.

## Numbers-guardian review (range `ef8ccee...a9b9317`, the owner's 8-check list)

**PASS on all 8 checks.** SQLite suite: 290 run, 288 passed, 2 skipped, 0 failed. Golden
idempotent: MATCH (#101, #201, #202). No `.claude/**` file changed in the range.

It raised two points for you to confirm:
- **Checklist provenance.** The 8-check file on disk was committed by this session
  (`ef8ccee`), at your request. Git alone can't prove the text is yours, so please confirm it.
- **Existing tests were edited.** `agent/tests/test_fingerprint.py` (added in Phase 1.1) was
  changed, not only added to. The edits repoint assertions to the scope-relevant diff and
  add fields; no test was removed or weakened. I read "never modify existing tests" as
  covering the pre-existing core tests (A2 names `core/tests.py`). Please confirm.

Non-blocking notes:
1. **An authorised schedule outside a fully AUTHORISED scope could baseline.** **Fixed:** V5
   never baselines a schedule that has an AgentAuthorisation. If its numbers changed and the
   snapshot has no current fingerprint, the change stays unexplained. Test:
   `test_authorised_schedule_is_never_baselined`.
2. **Every Phase 1.1 (version 1) snapshot baselines once.** This is by design. Production has
   none, because the agent has never run there.
3. **The "always relevant" sections are broad.** Any LMRB upload on the channel, or any
   settings edit, explains a change. I kept this as you specified in item 3.
4. **No comments on the fingerprint's LMRB reads.** **Fixed:** both flag-agnostic reads in
   `fingerprint.py` now carry a comment saying they are not candidate queries.
