# Phase 3 report: shadow agent (autonomy level 0)

Built on synthetic data only. **Not deployed.** Plan: `/root/.claude/plans/concurrent-giggling-tower.md`,
approved with owner answers Q1–Q7 and changes S1–S10.

## What the agent does now

Every 15 minutes (`manage.py agent_cycle`, `railway/cron-agent.json`, not deployed) the agent:

1. takes a cycle-wide lock. A second cycle that overlaps exits and records `overlap_skipped` (S7);
2. checks the service user (`require_service_user`). It never runs `sync_service_user`, because that
   writes `accounts.User` (S8);
3. stops after writing the heartbeat if the kill switch is off;
4. picks up to 25 scopes, in this order:
   - `needs_run` (for example after a TC Confirm);
   - inputs changed (the scope fingerprint differs from the last observed snapshot);
   - in the window: not yet rehearsed tonight;
   - due by time (6 hours).

   Scopes inside the upload debounce are skipped;
5. for each scope, **observes**: it runs readiness, `build_summary_data(schedule_id=…)` per active
   schedule, the fingerprint, diagnose, V1, and V5 against the previous **observed** snapshot. All of
   this runs in one READ ONLY transaction that is always rolled back. It then writes only agent tables:
   - the observed snapshot;
   - the findings ledger and proposals;
   - ScopeState and ScheduleStatus.

   `needs_run` is cleared only here;
6. **rehearses** (between 01:00 and 05:00 Colombo, on PostgreSQL, within 1800 s a night) with the
   existing `dry_run`. The dry run is always rolled back. The result is stored as a shadow snapshot
   plus a PendingEffect (per brand × duration: Planned / Aired / 3rd Party / Extra / Missed, shadow
   minus observed). Authorised or locked scopes are never rehearsed (S5);
7. closes a finished shadow window with the end core fingerprint, and sends the daily digest after
   07:30 Colombo.

Nothing is applied. There is no apply path, no mapping candidate and no scoring; those are Phase 4.

## Decisions carried out (Q1–Q7, S1–S10)

| Item | Where | Test |
|---|---|---|
| Q1 window 01:00–05:00, 1800 s budget, both configurable; `agent_nightly` moved to `45 23 * * *` UTC (05:15 Colombo) | `AgentConfig`, `agent/cycle.py::night_of`, `railway/cron-audit.json` | `WindowTest.test_night_of_*`, `test_budget_stops_dry_runs` |
| Q2 no replica: the primary is used with the guard timeouts | `agent/db.py::guard` | `test_snapshots_phase3` (SET LOCAL values, real lock and statement timeouts) |
| Q3 `digest_recipients` (admins only; empty = all active super_admin/admin), `digest_time` 07:30, core SMTP via `get_setting`, log only when email is off | `agent/digest.py` | `DigestTest` |
| Q4 label CSV `account, channel, month, schedule_number, cause_code, brand, duration, as_of, note`; cause codes = diagnose codes + `TC_NOT_LINKED` + `no_issue` + `other` | `agent/labels.py`, `agent_label_scopes` | `FeedbackAndLabelsTest` |
| Q5 digest effects: \|Δ\| ≥ 1 on Aired or Missed, top 20 plus a count of the rest | `agent/digest.py::_effect_rows` | `test_effects_top_20_with_rest_count` |
| Q7 report only; core cron jobs are not paused | agent-effect section of `agent_core_audit` | `test_audit_agent_effect_section` |
| S1 observed = the Summary page, under READ ONLY on PostgreSQL; a write there is stop-and-ask (`CoreWriteAttempt` stops the cycle and raises the heartbeat alert, and is never swallowed) | `agent/snapshots.py`, `agent/cycle.py` | `ParityWithSummaryPageTest`, `test_core_write_attempt_stops_the_cycle` |
| S2a inferred alignment and coverage, display only, marked "inferred" | `agent/measure.py::inferred` | `test_precision_formula`, `test_human_change_recorded_for_inferred_coverage` |
| S2b Correct / Incorrect / Unsure + note: a human `gate.perform` on `agent.FindingLedger` only | `agent/views.py::finding_feedback`, queue page | `test_feedback_is_a_human_gate_write_on_agent_tables`, `test_feedback_refused_for_non_admin` |
| S2c `agent_label_scopes` (owner labels, `as_of`); `agent_diagnose_labelled` (needs `AGENT_DISPOSABLE_DB=1`; readiness + diagnose only; writes `labelled_eval_<date>.md`); runbook step 4b | `agent/labels.py`, the two commands, `runbook_real_data.md` | `LabelledEvalTest` |
| S2d precision = correct / (correct + incorrect), per code and overall, with n | `agent/measure.py::precision` | `test_precision_formula` |
| S3 unique `key` = sha256(scope\|schedule\|code\|brand\|duration); closes after 2 consecutive absences; `reopen_count`; flapping (≥ 2) shown in the digest | `agent/ledger.py` | `LedgerTest` |
| S4 info proposals only for actionable codes | `ledger.ACTIONABLE` | `test_s4_*` |
| S5 no dry run for authorised or locked scopes | `cycle.shadow_scope` | `test_s5_*`, `test_no_core_writes` |
| S6 core fingerprint per table, with its own statement_timeout; a failed table is recorded and the scan continues; "detection, not proof" | `agent/core_fingerprint.py` | `CoreFingerprintTest` |
| S7 cycle lock / `overlap_skipped`; duration in Heartbeat, alert above 10 minutes; `close_old_connections()` after a DB error | `agent/cycle.py` | `OverlapTest`, `test_slow_cycle_alert`, `test_needs_run_kept_when_observation_fails` |
| S8 `gate.perform` spy over a full cycle including shadow runs: 0 calls to `core.*` / `accounts.*` (0 calls at all) | — | `test_no_core_writes` |
| S9 AgentAuthorisation guard = `clean()` + pre-save check (not a DB constraint) | `agent/models.py` | `BaselineAndAuthorisationTest` |
| S10 account fingerprint parts once per account per cycle; over 60 s → recorded, fallback to needs_run + due | `agent/fingerprint.py::account_parts`, `cycle.select` | `test_s10_fingerprint_fallback_is_recorded` |

### S4: codes you asked about

- **Actionable** (one info proposal each): NO_TC_MAPPING, CHANNEL_VARIANT, TC_NOT_LINKED (from
  readiness `tc_not_linked`), WILDCARD_TC_THEME_COMMERCIAL, MANUAL_LOCK_LOST,
  DUPLICATE_ACTIVE_NUMBER, SCHEDULE_LOCKED.
- **Info only, as you listed:** MAKEUP_LINKED, SCHEDULE_NUMBER_WIDTH, SUPERSEDED_ROWS_PRESENT,
  TIME_BELT_UNATTRIBUTED, LOCK_ORPHANED.
- **Not covered by S4, so treated as info only. Please decide:** TC_NO_ROWS, LMRB_THEME_NO_ROWS,
  SPONSORSHIP_NOT_RUN, LMRB_MULTI_FLAG, BASELINE.
- **Two gaps to flag:**
  - "Other unmapped-brand findings" and "column or alias findings" have no diagnose code today.
    Only NO_TC_MAPPING exists for unmapped brands. The only column problem diagnose reports is
    TC_NO_ROWS, which S4 leaves uncovered.
  - The unmapped-brand precision bar (criterion 2) is therefore measured on NO_TC_MAPPING alone.

## Other decisions I made (please check)

1. **Unexplained V5 during observation.** When V5 finds a number change that nothing explains, the
   agent:
   - sets the scope to `NEEDS_HUMAN / unexplained_change`;
   - records the schedule ids in `AgentRun.detail['v5_unexplained']`;
   - lists them in the digest.

   The next cycle compares against the new observed snapshot, so the flag does not persist. The
   14-day count for criterion 5 comes from those AgentRun rows.
2. **"Due by time" uses the wall clock**, not the `--now` override, because run timestamps are
   wall-clock. `--now` exists for synthetic walk-throughs only.
3. **Shadow runs repeat nightly per scope.** A yielded or failed shadow step is retried by a later
   cycle the same night, never within the same cycle. "Skipped" (S5, budget) counts as done for
   that night.
4. **A shadow window run** (`AgentRun kind='shadow_window'`) opens at the first in-window cycle of the
   night (start core fingerprint) and closes at the first cycle after 05:00 (end fingerprint and
   diff). It also counts AgentActions in the window, with agent actions and human actions reported
   separately.
5. **Label semantics.**
   - A code label marks matching agent findings (same code; brand and duration when given; seen on
     or before `as_of`) as `correct`.
   - If there is no match, an owner-only row records a **miss**. If the agent finds it later, the row
     becomes a normal finding: no reopen, label `correct`.
   - `no_issue` marks the scope's findings as `incorrect`.
   - Any bad row refuses the whole file.
6. **Labelled eval.** It scores the codes the labels use plus the actionable codes. The info-only
   codes are not scored unless a label names them. An unmatched finding in a labelled scope counts
   as an FP.
7. **Settings form.** The Phase 3 fields are on Agent Settings. Two intake tests that posted only
   the Phase 2 fields now also send the Phase 3 defaults (`factories.PHASE3_CONFIG_POST`).

## S1: does the Summary GET write?

**No.** Nothing reachable from a GET of `/dashboard/summary/` (`summary_report`, `core/views.py:6184`)
writes to the database. The only write in the view is on the POST branch.

| Code reachable from the view | file:line | Runs when | Writes? | Covered under READ ONLY by |
|---|---|---|---|---|
| `SummaryReportMeta.objects.get_or_create` + `meta.save()` | `core/views.py:6205` (inside `if request.method == 'POST'`, :6197) | POST only (Save metadata) | yes, **POST only** | not reachable from a GET |
| schedule auto-select | `core/views.py:6333` | scope given, no `schedule_id`, exactly one schedule | no | `SummaryGetReadOnlyTest.test_first_visit_without_meta_and_without_schedule_id` |
| `build_summary_data(..., schedule_id=sid)` | `core/views.py:6337` → `verification/tc_engine.py:767` (helpers `_lmrb_row_count` :814, `_sch_qs` :839, `_leftover_lmrb_count` :994; querysets and aggregates only) | scope given; `sid=None` when several schedules and none chosen | no | `ParityWithSummaryPageTest` (sid given), `test_several_schedules_without_schedule_id` (sid None) |
| `SummaryReportMeta.objects.filter(...).first()` | `core/views.py:6339` | scope given; first visit = no row, and **no row is created** | no | `test_first_visit_without_meta_and_without_schedule_id` (asserts no row afterwards) |
| card grid: `build_summary_data(schedule_id=sched.id)` per schedule | `core/views.py:6500` | account only, no channel/month | no | `test_card_grid` |
| `SpecialNotesService(...).calculate()` | `core/views.py:6559` (`verification/special_notes.py`) | `Account.enable_special_notes` and meta has costs | no | `test_meta_with_costs_and_special_notes` |
| `build_recon_context(...)` | `core/views.py:6569` (`verification/media_recon.py`) | whenever `summary_data` exists | no | `ParityWithSummaryPageTest` and every scope test above |
| context processors `branding` (`get_setting`), `site_notifications` | `core/context_processors.py:14`, `:29` | every page | no (`get_setting` is `objects.get`, `core/models.py:1469`) | every test above |
| `_ensure_defaults()` (`get_or_create` of SystemSetting rows) | `core/models.py:1443`, called only from `system_settings` (`core/views.py:8393`) | the System Settings page only | yes, **not reachable** from the Summary GET | not reachable |
| `tc_three_way` live coverage resolution | `core/views.py:5224`–`5510` (`live_cover_sr` :5249, resolution :5315–5385) | a separate view (`/dashboard/tc/detail/`); the Summary template never calls it | no (in-memory dicts only) | `test_tc_three_way_live_resolution` |
| messages framework | `core/views.py:6323` (`Access denied`), `:6356` (build failure warning) | only on an error branch | cookie / session storage only; not reached in a normal view | — |

**What the parity test compares:** the page's context variable **`summary_data`**
(`core/views.py:6585`), after canonical JSON (`to_jsonable` + `canonical_json`), byte-equal with the
stored observed snapshot's `data` (`agent/tests/test_snapshots_phase3.py:42`).

All tests in `SummaryGetReadOnlyTest` wrap the GET in `agent/db.py::guard(read_only=True)`. On PostgreSQL
the transaction is `READ ONLY`, so any write would raise `CoreWriteAttempt`. **Result: all pass on
PostgreSQL 16 and on SQLite; no write was hit.** (On SQLite the guard only rolls back, so enforcement
is proven by the PostgreSQL run.)

## Core tables covered by the fingerprint and the no-core-writes test (S6)

These are the 31 tables of every `core` and `accounts` model, including the many-to-many tables:

accounts_user, accounts_user_accounts, accounts_user_clients, accounts_user_groups,
accounts_user_user_permissions, core_account, core_auditlog, core_brandmapping, core_channel,
core_channelofficer, core_client, core_lmrbrow, core_manualmatch, core_matchresult,
core_monitoringdata, core_periodsponsorship, core_periodsponsorshipmatch, core_schedule,
core_schedulerow, core_scheduletemplate, core_sitenotification, core_sponsorshiplmrbassignment,
core_spotnote, core_spotnotification, core_summaryreportmeta, core_systemsetting,
core_tcchannelprompt, core_tclmrbmatch, core_tclmrbthememap, core_tcrow, core_transmissionreport

## PostgreSQL sequences during dry runs

`nextval` is not transactional, so a rolled-back insert still uses up an id.
- In `test_no_core_writes` on PostgreSQL, **only `core_matchresult_id_seq` advanced**.
- `run_scope` inserts MatchResult rows, and the rollback removes them.
- On real data the following can also advance, whenever the dry run would create rows in those
  tables:
  - `core_sponsorshiplmrbassignment_id_seq`
  - `core_periodsponsorshipmatch_id_seq`
  - `core_tclmrbmatch_id_seq` (standalone scopes)

**Id gaps in those tables are expected and harmless.** The agent-effect check compares
count / hash, not id continuity. `max(id)` does not move on a rollback.

## Results

| Run | Run | Passed | Skipped | Failed |
|---|---|---|---|---|
| Full suite, SQLite (`manage.py test --exclude-tag=eval`), before guardian fixes | 470 | 462 | 8 | 0 |
| Full suite, PostgreSQL 16 (same command), before guardian fixes | 470 | 462 | 8 | 0 |
| Full suite, SQLite, after guardian fixes | 475 | 466 | 9 | 0 |
| Full suite, PostgreSQL 16, after guardian fixes | 475 | 467 | 8 | 0 |

- Skipped tests are database-specific (`skipIf` / `skipUnless` PostgreSQL). Each database skips the
  tests written for the other one.
- The Phase 2.1 figures were 402 run on each database. Phase 3 adds 68 tests.
- `makemigrations --check --dry-run`: **No changes detected** (migration `agent/0007_phase3`).
- Golden idempotent on the `synthetic` database after migrating to 0007: **MATCH** (3 of 3 schedules).
- `test_no_core_writes`: passed on both databases. On PostgreSQL the core sequence that advanced was
  `core_matchresult_id_seq`; on SQLite none did.

### Guardian review (numbers-guardian, owner 8-check version)

**Verdict: PASS on all 8 checks.** No path writes or commits a core or accounts row, and no billing
number changes. The guardian re-ran the suites (SQLite and PostgreSQL: OK), golden idempotent
(MATCH), golden rebuild (all rows match) and three `agent_cycle` runs on a seeded PostgreSQL
database: the core/accounts fingerprint was unchanged and 0 AgentActions were made.

| Check | Result | Note |
|---|---|---|
| 1 Protected paths | PASS | only `CLAUDE.md` §19 appended; borderline: three earlier agent/intake tests edited (snapshot kind rename; new required form fields) |
| 2 Smart only, no reset/de-match/delete | PASS | shadow reuses `dry_run` (always rolled back); only agent-table deletes/updates |
| 3 Channel/month from Schedule | PASS | labels match Schedule strings exactly |
| 4 LMRB candidates exclude 4 flags | PASS (n/a) | no new candidate query; fingerprint only counts |
| 5 Writes through gate.py | PASS for core; wording borderline | agent bookkeeping tables written directly (Phase 1 precedent) → checklist text updated in patch 0006 |
| 6 No wildcard auto-apply | PASS | finding proposals are `tier=4, apply_payload={}` |
| 7 LLM output validated | PASS (n/a) | no LLM code |
| 8 Email/file content as data | PASS (n/a) | digest only sends |

Other findings, and what was done:

| # | Finding | Action |
|---|---|---|
| 1 | Cycle lock (session advisory lock) silently lost: `_recover()` called `close_old_connections()`, which always closes with `CONN_MAX_AGE=0` | **Fixed.** `_recover()` closes only a broken connection and then re-takes the lock; if another cycle took it, the cycle stops with `lock_lost`. Tests: `CycleLockKeptTest` (incl. PostgreSQL: a second connection cannot take the lock after a scope DB error). Deviation from the literal S7 wording, on purpose. |
| 2 | Dry runs advance `core_matchresult_id_seq` and hold core row locks up to the statement timeout at night | Documented (above). Row locks can delay, never change, a person's night run. |
| 3 | Unvalidated `next` redirect in the feedback view | **Fixed** with `url_has_allowed_host_and_scheme`; test `test_feedback_next_must_be_local`. |

Checklist changes: **patch `docs/agent/patches/0006_guardian_phase3.diff`** (for you to apply;
`git apply --check` passes). Check 1 defines "existing tests"; check 5 exempts the agent's own
bookkeeping tables; new check 9 (engine calls outside the gate only in rolled-back dry runs, never
authorised/locked; CoreWriteAttempt never swallowed), 10 (`schedule_id` always passed), 11 (session
locks survive error recovery; redirect targets validated).

After the two fixes: SQLite 475 run, 466 passed, 9 skipped, 0 failed; PostgreSQL 475 run, 467
passed, 8 skipped, 0 failed.

## Synthetic walk-through (copy `synthetic_ui`, console email backend)

The prep runs on the copy only:
- agent on, level 0;
- debounce 0, because the seed's uploads are seconds old;
- core email on with a fake host.

| Step | `--now` (Colombo) | Result |
|---|---|---|
| 1 daytime | 25 Sep 12:00 | 4 scopes observed read-only (`due`); 0 dry runs; the digest printed to the console (first cycle after 07:30) |
| 2 daytime | 25 Sep 12:15 | nothing due, `scopes: {}` |
| 3 in window | 26 Sep 02:00 | 4 observed; **3 shadow runs ok, 1 skipped** (Elephant House: `schedule_locked`, S5); budget used 0.31 s |
| 4 after window | 26 Sep 07:45 | window closed: **0 agent actions in window, 0 core tables changed**; digest sent once to the one admin (console) |
| 5 audit | — | `agent_core_audit` "Agent effect" row: `2026-09-26 \| 3 \| 0.309 \| 0 \| 0 \| none \| none` |
| 6 synthetic TC re-upload for Keells | — | TC rows reset to unreconciled, as a core upload leaves them |
| 7 daytime | 26 Sep 12:00 | Keells picked as `changed`; V5 explained by `external_change` (TransmissionReport uploaded_at) |
| 8 in window | 27 Sep 02:00 | Keells PendingEffect: **Nexus 30s Aired 0 → 3, Missed 3 → 0**; nothing committed |

Screenshots (`docs/agent/screens/`):
- `p3_overview.png`
- `p3_quality_cycle.png` (Diagnosis quality and Agent cycle cards)
- `p3_scope_pending_effect.png`
- `p3_queue_findings.png`
- `p3_queue_feedback.png` (after clicking Correct)
- `p3_settings.png`

## Exit criteria: how each is measured

| # | Criterion | Measured by |
|---|---|---|
| 1 | Zero core writes | `test_no_core_writes` + S8 spy on PostgreSQL in CI; per night: shadow window `agent_actions_in_window` = 0; core fingerprint diff listed in the nightly audit ("Agent effect") for your review |
| 2 | Precision | Overview → Diagnosis quality (labelled, owner / feedback, unmapped-brand n), from `measure.precision()` |
| 3 | Recall | `agent_diagnose_labelled` on a pre-fix copy → `labelled_eval_<date>.md` Overall recall |
| 4 | No blocking | Overview → Agent cycle "Yielded visits" (busy + timeout ÷ visits, observe and shadow steps, 14 days); user reports are yours |
| 5 | V5 | scope runs with `v5_unexplained` (digest section "Unexplained number changes") |
| 6 | Shadow in window | Agent cycle "Shadow nights in budget" (closed windows with no `budget_exhausted` skip) |
| 7 | Job health | Heartbeat `agent_cycle` stale after 60 minutes (health card); "Duration p95 / max" |

## Not done / open

- Guardian patch 0006 is for you to apply (`.claude/` is never edited by the session).

- Not deployed. The Railway cron service for `cron-agent.json` has to be created by you.
- A live run needs `AgentConfig.enabled = True` (Agent Settings). Level stays 0 (capped).
- The owner labels and the pre-fix restore are yours (runbook step 4b).
- The S4 gaps above need a decision.
