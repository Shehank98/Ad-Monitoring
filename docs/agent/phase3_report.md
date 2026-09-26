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

---

# Phase 3.1 (owner follow-ups T1–T10)

Synthetic data only. Not deployed. Autonomy stays 0.

## Owner decisions carried out

| Decision | Where |
|---|---|
| TC_NO_ROWS, LMRB_THEME_NO_ROWS, SPONSORSHIP_NOT_RUN actionable (one T4 info proposal each) | `ledger.ACTIONABLE` |
| LMRB_MULTI_FLAG info only | `ledger.INFO_ONLY` |
| BASELINE is a state: out of the ledger, proposals, precision, recall and the labelled eval | `ledger.STATES` (skipped by `ledger.group`), label cause codes, `measure.precision`, `agent_diagnose_labelled`; migration `agent/0008_phase3_1` deletes stored BASELINE ledger rows (agent table) |
| Mapping group for criterion 2: NO_TC_MAPPING, LMRB_THEME_NO_ROWS, NO_BRAND_MAPPING; the group and each code, each with n | `ledger.MAPPING_GROUP`; Overview → Diagnosis quality "Mapping group precision" |

V5_UNEXPLAINED and RECONCILE_PENDING are made by the cycle, not by diagnose. They carry no
Correct/Incorrect buttons and are never label cause codes, precision or eval inputs.

## T1–T10

| Item | What was built | Tests |
|---|---|---|
| **T1** unexplained changes persist | An unexplained V5 change opens one `V5_UNEXPLAINED` row per schedule and change (key includes the new sha, so the same change is never re-opened). It is actionable with a T4 proposal. Evidence: the before/after observed numbers per brand (`effects.diff_summaries`) plus the fingerprint diff (empty). The baseline still moves forward. Rows never close by absence. The scope stays `NEEDS_HUMAN / unexplained_change` while any row is open. **Acknowledge** (Review queue, admins): root cause `fingerprint_gap` / `core_bug` / `outside_data_fix` / `accepted` plus a required note, as a human `gate.perform` on `agent.FindingLedger`; the scope's state is re-derived once the last row is acknowledged. Authorised schedules keep their existing handling. Criterion 5 = open rows + rows acknowledged as `fingerprint_gap` (14 days), on the quality card. The digest lists open rows with their age. | `V5PersistsTest` (7): next cycle does not clear it, acknowledging clears it, NEEDS_HUMAN until every row is acknowledged, note/cause required, digest |
| **T2** no mapping at all | Checked: NO_TC_MAPPING **does** fire for a brand with no BrandMapping row. But a brand that has a TC theme and **no LMRB theme** (MatchResult `no_mapping`) produced **no finding at all** (LMRB_THEME_NO_ROWS skipped it). Added **NO_BRAND_MAPPING** (actionable, mapping group) for any commercial brand + duration with no `BrandMapping.theme`; evidence says whether any BrandMapping row exists. A brand with no row at all gets both codes. | `NoBrandMappingTest` (2) |
| **T3** window close | `agent_nightly` first runs `cycle.nightly_close()`: it takes the cycle lock (waits up to 10 minutes) and closes any ended window (end core fingerprint + diff + agent/human action counts). Closing is idempotent: each window is re-read with `select_for_update` and closed only while `running`, and both jobs hold the cycle lock. On a lock timeout the window stays open and is reported **pending**; the audit's agent-effect section lists pending windows, and the next cycle or night closes and includes them. | `NightlyCloseTest` (3): nightly closes a still-open window; nightly and cycle racing close it once (end fingerprint not overwritten); lock timeout → pending, the next cycle closes it |
| **T4** labelled eval | Optional CSV column `complete` (yes/no, default no; validated). An unmatched agent finding is a false positive only in a scope with `complete=yes` or a `no_issue` label; otherwise it is **unverified**, listed in `labelled_eval_<date>.md` and left out of precision. Precision is reported over complete-labelled scopes only and overall. `agent_label_scopes` applies the same rule on the ledger (unexplained findings in a complete scope → `incorrect`, one human gate write per scope). | `test_evaluate_counts_tp_fp_miss` (both ways), `test_complete_scope_marks_unexplained_findings_incorrect`, `test_complete_column_is_validated` |
| **T5** RECONCILE_PENDING | When the latest PendingEffect of a schedule has \|Δ\| ≥ 1 on Aired or Missed for any brand, an actionable `RECONCILE_PENDING` finding opens ("Running reconciliation would change these numbers."), with the PendingEffect as evidence. When a later observed snapshot equals that shadow (a person ran the core reconcile), it closes `reconciled_by_human` and records `seconds_to_resolve`. A newer zero effect closes it `no_longer_pending`. Evaluated after every observation and after every shadow run. The quality card shows open / reconciled-by-a-person counts and the median hours. | `ReconcilePendingUnitTest` (4), `ReconcilePendingCycleTest` |
| **T6** S1 answer | Section "S1: does the Summary GET write?" above, with file:line for every path. No write is reachable from the GET. New READ ONLY tests for every path the parity test did not reach (no `schedule_id` with one schedule, several schedules → `schedule_id=None`, card grid, meta with costs + special notes, `tc_three_way`). **No write was hit on PostgreSQL.** | `SummaryGetReadOnlyTest` (5) |
| **T7** `--now` | `agent_cycle --now` is refused unless `DEBUG=True`, `AGENT_DISPOSABLE_DB=1`, or a test run. | `NowFlagTest` |
| **T8** service user coverage | Each cycle compares the service user's accounts with all accounts (read only; never `sync_service_user`). Missing accounts go into the heartbeat counts, the health card ("… run agent_ensure_service_user") and the digest. | `ServiceUserCoverageTest` |
| **T9** fingerprint noise | `accounts_user.last_login` is left out of the accounts_user hash (PostgreSQL `to_jsonb(t) - 'last_login'`). Per-table fingerprint time and total are recorded. The agent-effect section lists changed tables in two groups, always both: **reconciliation data** (`core_schedule*`, `core_lmrbrow`, `core_tcrow`, `core_transmissionreport`, `core_brandmapping`, `core_manualmatch`, `core_*sponsorship*`, `core_tclmrb*`, `core_matchresult`, `core_summaryreportmeta`, `core_systemsetting`) and **activity tables** (everything else), with the start/end fingerprint times. | `FingerprintNoiseTest` (3) |
| **T10** settings form | A field missing from the POST keeps its stored value (M2M, times, booleans included). The page sends `_fields` (every field it rendered), so an unticked checkbox on the real page still turns the setting off. The two intake tests are back to posting only their own fields (identical to their pre-Phase-3 content); the `PHASE3_CONFIG_POST` helper was removed. | `SettingsFormKeepsStoredValuesTest` (2) |

Two other test adjustments, both agent tests: the kill-switch test now checks
`counts['disabled']` (the heartbeat also carries T8's list), and the audit/eval tests follow the new
table layouts. `agent_nightly` keeps `steps` as before and records the window result under
`counts['shadow_window']`, so the Phase 2.1 nightly test is unchanged.

## Phase 3.1 results

| Run | Run | Passed | Skipped | Failed |
|---|---|---|---|---|
| Full suite, SQLite (`manage.py test --exclude-tag=eval`), before the guardian fixes | 507 | 498 | 9 | 0 |
| Full suite, PostgreSQL 16, before the guardian fixes | 507 | 499 | 8 | 0 |
| **Full suite, SQLite, final** | **510** | **501** | **9** | **0** |
| **Full suite, PostgreSQL 16, final** | **510** | **502** | **8** | **0** |

- `makemigrations --check --dry-run`: **No changes detected** (new migration `agent/0008_phase3_1`).
- Golden idempotent on `synthetic` after migrating to 0008: **MATCH** (3 of 3 schedules), before and after the
  guardian fixes.

### Guardian review of Phase 3.1

**Verdict: PASS on all 8 checks.** There is no core or accounts write, no engine call and no way for
a billing number to change. The guardian's SQLite suite gave 507 run, OK; its PostgreSQL runs of the
Phase 3 test files were OK. Golden idempotent: MATCH. Its rolled-back PostgreSQL probes confirmed that
`close_window` leaks no READ ONLY state and that `last_login` no longer changes the accounts_user hash.
Protected paths are unchanged, and the two intake tests are byte-identical to d4eb2d0.

It found two defects in agent-only logic and two gaps in the gate record. All four are fixed:

| # | Finding | Fix | Test |
|---|---|---|---|
| 1 | **T10 kill switch.** A Settings page loaded before this deploy sends no `_fields`, so unticking **Enabled** kept the stored True and still showed "saved". | Checkboxes are never filled: a checkbox the person did not send is always False (fails safe). Other missing fields still keep their stored value. | `test_fields_missing_from_the_post_keep_their_stored_value` (asserts `enabled` becomes False) |
| 2 | **T1 key.** Keyed on the new sha only, so a change back to numbers that were acknowledged before (A→B, ack, B→A, A→B) opened no row and was lost. | Keyed on the baseline observed snapshot the change was measured against, plus the new sha. The baseline moves forward every observation, so each change gets its own row. | `test_flip_back_to_acknowledged_numbers_opens_a_new_row` |
| 3 | Acknowledge also writes ScopeState, but `before` did not record it. | `before` / `after` now include the scope's state and reason. | `test_acknowledge_logs_scope_state_and_refuses_a_second_ack` |
| 4 | Two admins acknowledging at once could both pass `row.open`. | The row is re-read with `select_for_update(of=('self',))` inside the write; the second acknowledgement is refused. | same test |

**One more defect, found while writing test 2 (not by the guardian):** V5 counted *any* AgentAction on the
scope as an explanation. That included the admin's own acknowledgement, feedback and label writes,
which only touch agent tables. So a change right after an acknowledgement was silently "explained".
`validate.explaining_actions()` now ignores actions whose target is an `agent.*` or `intake.*` table. It
is used by V5 and by the authorised check. Test: `V5AgentTableActionsExplainNothingTest`.

Checklist text: the guardian recommended three more changes. They are added to **patch 0006**, which
is still for you to apply (`git apply --check` passes):
- check 5: before/after must cover every row `apply()` changes;
- check 5: the exemption covers "proposals with kind='finding' and an empty apply_payload" rather than "info-only";
- new check 12: the kill switch fails safe.

## Not done / open (Phase 3.1)

- Not deployed. Guardian patch 0006 is for you to apply.
- The Nova Agent Console mockup you linked is not built. Its approve/apply actions (mapping proposals,
  tolerance changes, manual-match proposals, TC split) need autonomy above 0, which is Phase 4.
  See the question in my final message.

---

# Phase 3.2 (owner items 1–5)

Synthetic data only. Not deployed. Autonomy stays 0.

## 1. Reconciliation Agent console (option (a), level 0 only)

| Part | What it does | Where |
|---|---|---|
| Sidebar agent card | Status (Running · level N / Paused), last cycle, next expected cycle (the next `*/15` tick), heartbeat health (OK / Check / Alert, with the first problem as a tooltip), "Run requested" when pending. Admins also see **Pause/Resume** and **Run cycle**. Channel officers and anonymous users see nothing. | `agent/templatetags/agent_console.py` (`{% agent_card %}`), `templates/agent/_agent_card.html`; into `templates/base.html` by **patch `0007_base_agent_card.diff`** (for you to apply; applies alone or after 0001 and 0005) |
| Pause / Resume | Toggles `AgentConfig.enabled` (the kill switch). Admin only, POST only (GET → 405), a human `gate.perform` on `agent.AgentConfig` with before/after (`agent_pause` / `agent_resume`). | `agent/views.py::console_pause` |
| Run cycle | Sets the new `AgentConfig.run_requested_at` (admin only, POST only, logged as `agent_run_requested`). **The request never runs a cycle.** The next `agent_cycle` (cron) treats it as run-now: every scope counts as due (still capped at `max_scopes_per_cycle`, still level 0). It clears the flag afterwards, but only if nobody clicked again during the cycle. | `agent/views.py::console_run`, `agent/cycle.py` (`select(run_now=…)`), migration `agent/0009_phase3_2` |
| Schedules | Latest **observed** SummarySnapshot per schedule: state, Planned / Aired / 3rd Party / Missed, when observed, link to the core Summary Sheet. No Summary calculation. | `/dashboard/agent/console/schedules/` |
| Reports | Per scope: sign-off state (core `authorised_by`, AgentAuthorisation, Ready for sign-off, Not ready), totals from observed snapshots, prepared/checked by, links to the Summary Sheet, PDF and Excel. | `/dashboard/agent/console/reports/` |
| Theme tester | Client + TC or LMRB theme + optional duration → brand(s). TC uses `_build_reverse_tc_theme_map` + `_brands_for_tc_theme`. LMRB uses `_build_lmrb_theme_map` + `_lmrb_themes_for_brand` per brand with the engines' exact / `*`-prefix rule. All are pinned in `test_core_contract`. GET only, no save action; accounts outside the user's access → 403. | `/dashboard/agent/console/theme-tester/` |

There are **no approve / apply controls** anywhere in the console, not even disabled ones (tested).
The three views are also tabs in the agent pages (`templates/agent/_base.html`).

Tests: `agent/tests/test_console.py`.
- **No core writes:** every console view, plus the Overview, runs inside a READ ONLY guard (enforced on PostgreSQL).
- **Run cycle:** the request only sets the flag (no cycle, no scope run); the next cycle runs everything and clears the flag; a newer click during a cycle survives.
- **Switches:** Pause/Resume and Run are admin-only (team_head, planner, operations and channel_officer → 403), POST-only (405) and logged.
- **Access:** channel_officer gets 403 on every console view.
- **Card:** admin, other staff, channel officer, paused and requested states; the card is rendered under READ ONLY.

Screenshots: `docs/agent/screens/p32_schedules.png`, `p32_reports.png`, `p32_theme_tester.png`. The
sidebar card needs patch 0007, which I did not apply. It is covered by the tag tests instead.

## 2. Fail-safe check of AgentConfig booleans

A checkbox missing from the POST is always False (Phase 3.1 guardian fix). AgentConfig has exactly
three boolean fields, and False is the safe value for each:

| Field | When False | Safe? |
|---|---|---|
| `enabled` | Kill switch: the cycle writes its heartbeat and does nothing else; no observation, no rehearsal, no digest | yes |
| `intake_fetch_enabled` | No mailbox is read | yes |
| `intake_gemini_enabled` | Intake PDFs are not sent to Gemini; they are read once by the heuristic reader and always go to review | yes |

No boolean is unsafe when False. `BooleanFailSafeTest` pins this list, so a new boolean field fails
the test until its safe value is reviewed. It also posts the settings form without any boolean and
checks that all three turn off. (`AgentAccountOverride.enabled` is a nullable choice on a separate
form, not a checkbox: its "Use global" choice is None, never True.)

## 3. CI

agent-ci passed on GitHub for the latest pushed commit before this phase (4dbd937):
https://github.com/Shehank98/Ad-Monitoring/actions/runs/36220049982 (run #16, success). The
workflow runs `makemigrations agent intake --check` and the full suite on PostgreSQL 16; no change
was needed. After the first Phase 3.2 pushes it also passed on 43ea79f (https://github.com/Shehank98/Ad-Monitoring/actions/runs/36221097244) and 38016a1 (https://github.com/Shehank98/Ad-Monitoring/actions/runs/36221118230). The result for the final Phase 3.2 commit is given in the reply that hands this phase over, because a report cannot quote the CI run of the commit that contains it.

## 4. Staging runbook

`docs/agent/runbook_staging.md`. It covers:
- restoring the backup;
- **switching off every outbound message before any service starts**, with a table of each
  SystemSetting key (`whatsapp_enabled`, `whatsapp_access_token`, `whatsapp_phone_number_id`,
  `whatsapp_test_number`, `email_enabled`, `email_host`, `email_host_password`, `nova_enabled`),
  what reads it and the value to set;
- a one-off upsert SQL, tested on a copy. It inserts missing rows because a missing `nova_enabled`
  row reads as on;
- the environment variables to leave unset (`GEMINI_API_KEY`, `FIREBASE_STORAGE_BUCKET` if it is
  production, `ANTHROPIC_*`, `INTAKE_*`);
- digest recipients limited to named testers;
- migrations, `agent_ensure_service_user` and the runbook_real_data steps;
- `AGENT_DISPOSABLE_DB=1` only on the one-off rebuild or eval command;
- the two cron services with their variables;
- the Agent Settings values;
- a 13-row daily checklist with where to read each number.

## Phase 3.2 follow-ups (owner review before acceptance)

### Item 1: the Phase 2.1 "400" error

**It was never captured.** No live API call was made from this environment (no API key), so there is no
observed error and **no model id it happened with**. The only text I have is what the Claude API
reference bundled with my tooling documents, quoted in the Phase 2.1 section of
`docs/agent/phase2_report.md`:

> `tool_choice: type "tool" and "any" are not supported for this model.`

That reference names models (Claude Fable 5.1, Claude Mythos 5.1, Claude Opus 5.5), not a model id
that produced the error. I have not reconstructed anything beyond that quote.

### Item 5: sidebar card safety

- `agent/console.py::card()` wraps everything: **any** exception (including `ProgrammingError` when the
  agent tables do not exist yet) is logged (`agent.console`, "agent card failed; not shown") and the card
  is not rendered. The page itself is never affected.
- **A fixed number of queries: 4** for staff (AgentConfig, last cycle run, and `health()`'s AgentConfig +
  Heartbeat list), the same with 1 or many scopes (`assertNumQueries(4)`). **0 queries** for
  channel officers and anonymous users, who get nothing.
- **Patch 0007 applies after 0001 and 0005:** tested in the suite (`Patch0007Test` copies `base.html`
  to a temp folder, applies 0001 and 0005, then `git apply --check` 0007 and applies it; the card tag
  appears once).
- Tests: `CardSafetyTest` (5) and `Patch0007Test`.

### Item 6: exactly what `run_requested_at` changes in the next cycle

| Part | Behaviour | Test |
|---|---|---|
| Every scope counts as due | `select(run_now=True)` sets the "due" cut-off to now, so every scope not observed since the request is picked as `due`. Nothing else in the selection changes. | `test_every_scope_counts_as_due` |
| Same caps | `max_scopes_per_cycle` applies as usual; the rest wait for the next cycle | `test_same_cap` |
| Same debounce | a scope inside the upload debounce is still skipped (`debounced`) | `test_same_debounce` |
| Observe-only outside the shadow window | outside 01:00–05:00 the cycle only observes; no dry run, no shadow window, no shadow snapshot. Inside the window the scopes get their normal nightly rehearsal (still within the night budget). Nothing is ever applied (level 0). | `test_observe_only_outside_the_shadow_window` |
| Cleared after the cycle | cleared only if nobody clicked again during that cycle | `test_newer_request_during_a_cycle_survives`, `test_cycle_treats_request_as_run_now_and_clears_it` |
| Second click while pending | changes nothing (no write, no AgentAction) and says "A cycle is already requested (at HH:MM)…" | `test_second_click_while_pending_changes_nothing` |
| While paused | refused, nothing recorded, message "The Reconciliation Agent is paused. Resume it first." (guardian finding 5) | `test_refused_while_paused` |

### Item 4: file storage on staging (runbook step 1)

`docs/agent/runbook_staging.md` now **starts** with step 1, done before any service starts:
- **1a:** what I found about `FIREBASE_STORAGE_BUCKET` and `MEDIA_ROOT`:
  - with a bucket set, every file read, write and delete goes to Firebase; without one, files go to
    `MEDIA_ROOT`, which Railway loses on redeploy unless a volume is mounted;
  - saves never overwrite, but deletes do reach the bucket;
  - restored rows point to production file paths, so downloads on staging may fail, which is expected;
  - with the production bucket, deleting on staging deletes the production file. Every core place that
    deletes stored files is listed with file:line: `schedule_delete`, `monitoring_delete`,
    `monitoring_delete_group`, `tc_delete`, `schedule_template_upload`, **the automatic MapOnline 30-day
    purge after any MapOnline upload**, `purge_maponline`, and `branding_upload` (local only).
- **1b:** staging uses its own empty bucket or no bucket (local storage on a staging volume). Never the
  production bucket.
- **1c:** every environment variable not to copy from production, with its staging value. This includes
  `GEMINI_API_KEY` (unset; it is the real lock for Nova chat and PDF conversion, not `nova_enabled`, as the
  guardian pointed out), `ANTHROPIC_API_KEY`, all `FIREBASE_*`, all `INTAKE_*`, `SUPER_ADMIN_EMAILS`
  (unset), `SUPER_ADMIN_EMAIL/PASSWORD/NAME` (one named tester; `ensure_superadmin` runs on every
  deploy), `SECRET_KEY` (new), `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` (staging domain only).
  Email and WhatsApp credentials are database settings, switched off in step 3.
- **1d:** a separate domain, and a person's one-off SQL that deactivates every restored user except the
  named testers (and the agent's service user) and clears restored sessions.

## Phase 3.2 results

| Run | Run | Passed | Skipped | Failed |
|---|---|---|---|---|
| Full suite, SQLite (`manage.py test --exclude-tag=eval`), first Phase 3.2 run | 530 | 521 | 9 | 0 |
| Full suite, PostgreSQL 16, first Phase 3.2 run | 530 | 522 | 8 | 0 |
| **Full suite, SQLite, final** | **546** | **537** | **9** | **0** |
| **Full suite, PostgreSQL 16, final** | **546** | **538** | **8** | **0** |

**Why the numbers differ between the databases.** Both run the same 546 tests. Some tests exercise a
feature that exists on only one database, and skip themselves on the other:

- **Skipped on SQLite (9), PostgreSQL-only features:**
  - `OverlapTest.test_second_cycle_exits_with_overlap_skipped_pg` (session advisory lock)
  - `CycleLockKeptTest.test_lock_still_held_after_a_scope_db_error` (session advisory lock)
  - `RealTimeoutTest.test_lock_held_elsewhere_gives_busy_yielded` (lock_timeout)
  - `AdvisoryLockTest.test_contention_between_connections_and_release_on_rollback`
  - `AdvisoryLockTest.test_scope_lock_requires_atomic_and_nests`
  - `ParityWithSummaryPageTest.test_write_inside_read_only_guard_is_stop_and_ask` (READ ONLY enforcement)
  - `GuardTimeoutsTest.test_lock_timeout_yields_busy`
  - `GuardTimeoutsTest.test_set_local_values_applied`
  - `GuardTimeoutsTest.test_statement_timeout_yields`
- **Skipped on PostgreSQL (8), SQLite / fallback-only features:**
  - `OverlapTest.test_second_cycle_exits_with_overlap_skipped` (ScopeLockRow cycle lock)
  - `FallbackLockTest.test_contention_and_release`
  - `FallbackLockTest.test_expired_row_is_free`
  - `FallbackLockTest.test_refuses_inside_atomic`
  - `FallbackLockTest.test_released_in_finally_on_error`
  - `DryRunTest.test_refused_on_sqlite_without_setting`
  - `ReconcileScopeTest.test_lock_contention_skips_nothing_silently` (fallback lock)
  - `GoldenCheckTest.test_refuses_sqlite_outside_tests`

So PostgreSQL passes one more test than SQLite (538 vs 537), because nine tests are PostgreSQL-only and
eight are SQLite-only.

- `makemigrations --check --dry-run`: **No changes detected** (migration `agent/0009_phase3_2`).
- Golden idempotent on `synthetic` after 0009: **MATCH** (3 of 3 schedules), before and after the fixes.

### Guardian review of Phase 3.2

**Verdict: PASS on all 8 checks.**
- Protected paths are unchanged; `templates/base.html` is byte-identical.
- The console calls no engine. It imports only the four pinned resolvers (plus `active_schedule_ids`,
  also pinned).
- Pause/Resume and Run are human gate writes with before/after; both forms carry CSRF tokens (a POST
  without a token → 403, tested by the guardian).
- The READ ONLY view tests pass on PostgreSQL.
- Golden idempotent: MATCH.
- The guardian also applied patch 0007 to a copy and confirmed:
  - an admin page renders under READ ONLY;
  - channel officers and anonymous users see no card;
  - a page still returns 200 with the AgentConfig row deleted (the row is not re-created).
- Borderline under the current check 5: the cycle clears `run_requested_at` with a direct `update()`. It
  is allowed as bookkeeping under patch 0006's check 5, whose text now names it.

Findings, all fixed:

| # | Finding | Fix | Test |
|---|---|---|---|
| 1 | **Schedules/Reports showed superseded and deleted schedules**, so Reports double-counted Planned/Aired/Missed after a re-upload (Rule 12) | only the latest observed snapshot of each **active** schedule (`active_schedule_ids`) per scope of the user's accounts | `test_superseded_version_is_not_counted_twice` |
| 2 | Reports sign-off label counted AgentAuthorisation rows itself | uses the scope state from `agent/readiness.py` (AUTHORISED / READY_FOR_SIGNOFF), naming the core `authorised_by` | `test_signoff_comes_from_scope_state` |
| 3 | Theme tester returned 500 on a non-numeric `account_id` | input validated; messages instead of errors | `test_theme_tester_bad_input_never_500` |
| 4 | A blank duration gave a misleading "No brand" (the resolvers then match only duration-less mappings) | duration is now required | same test |
| 5 | Pause showed "paused" even when the gate refused; Run could be requested while paused | message only on success; Run refused while paused | `test_pause_message_only_on_success`, `test_refused_while_paused` |
| — | Card not fail-safe (a missing table would break every staff page) | `card()` catches, logs and hides (item 5) | `CardSafetyTest` |
| — | Runbook said `nova_enabled=0` keeps chat data from Gemini; `chat_with_nova` does not check it | runbook says `GEMINI_API_KEY` unset is the real lock | — |
| — | SQL used category `display` for `nova_enabled` (default `assistant`) | changed to `assistant` | — |

Checklist text: the guardian recommended three more changes. They are in **patch 0006** (still for you to
apply; `git apply --check` passes):
- check 5 names the cycle clearing `run_requested_at` as allowed bookkeeping;
- new check 13: agent code on core pages is read-only, never uses `get_solo` / `get_or_create`, and
  cannot break a page;
- new check 14: console snapshot views are limited to the user's accounts and active schedules.
