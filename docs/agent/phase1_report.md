# Phase 1 report: Foundation

Branch `claude/design-system-extraction-vnav5h`. Phase 1 base `a4df7a3`. Brief + Amendment 01
+ the owner's answers 1–4 and plan changes P1–P6.

## Exit rules (Amendment A11)

| # | Rule | Status |
|---|---|---|
| 1 | Test baseline (A2) | **Met.** Full suite 259/259 on SQLite and on PostgreSQL 16 (168 core + 91 agent/intake). |
| 2 | `test_core_contract` passes | **Met.** 29 pinned signatures plus a source scan that fails on any unpinned core import. |
| 3 | Golden check matches exactly on the listed scopes, on PostgreSQL | **Met on synthetic scopes only** (a revised schedule and a two-schedule scope). **Open on real data**: you run it per `runbook_real_data.md`. |
| 4 | `agent_core_audit` ran and its report is committed | **Met on synthetic data** (`core_audit_20260925_SYNTHETIC.md`). **Open on real data** (same runbook). |
| 5 | `nova_tools_review.md` written | **Met.** |
| 6 | No protected path, nothing under `core/` | **Met.** See the guardian verdict below. |

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
| SQLite, full suite | 259 tests, OK (2 skipped: PostgreSQL-only lock tests) |
| PostgreSQL 16, full suite | 259 tests, OK (7 skipped: SQLite-only fallback/refusal tests); advisory-lock contention and release-on-rollback pass |
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

_To be filled from the guardian's report._

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
