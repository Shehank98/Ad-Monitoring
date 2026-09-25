# AGENT BUILD BRIEF: AMENDMENT 01
Based on Phase 0 discovery at commit 445fb8a.

Precedence (highest first):
1. this amendment
2. docs/agent/AGENT_BUILD_BRIEF.md
3. CLAUDE.md
Facts come from docs/agent/phase0_discovery.md. The code is the truth for behaviour.

## A1 Locations and tooling
- The brief lives at docs/agent/AGENT_BUILD_BRIEF.md.
- Tests use the Django runner only. Replace every mention of pytest with:
  - all tests:     python manage.py test
  - agent only:    python manage.py test agent intake
  - live-LLM evals: tag with @tag('eval'); excluded by default (--exclude-tag=eval)
- Railway:
  - The web service keeps the root railway.json unchanged.
  - New services use railway/<service>.json.
  - Do not reconcile the Procfile and railway.json; flag the difference only.
- Add CI: .github/workflows/agent-ci.yml, a new file.
  - Run the full suite on a Postgres service container.
  - Include the advisory-lock tests.

## A2 Test baseline
- If the pre-phase fixture fix was committed, the full suite must pass 168/168.
- Otherwise docs/agent/test_baseline.txt lists the 10 known failures. Every phase must
  show no new failures and no growth in that list.
- Never edit core/tests.py.

## A3 Numbers: the code is the truth
- Never state, derive or re-implement Summary semantics from CLAUDE.md section 9.
- Read numbers only from build_summary_data(..., schedule_id=sid).
- Show only the columns it returns, with these labels:
  - Planned
  - Aired
  - 3rd Party (TC confirmed by LMRB)
  - Extra
  - Missed
- Avg 30s is not shown or calculated anywhere, including the covering note.
- Period sponsorship: show it as a separate section, built from coverage(ps).
  Never merge it into the sponsorship totals.
- Replace the validator checks from the brief with these:
  V1 Arithmetic consistency with the code path used.
     - Mapped commercial rows:
         extra  == max(0, third_party - planned)
         missed == max(0, planned - third_party)
         aired  >= third_party
     - Unmapped rows (no tc_theme for that duration):
         third_party == 0
         missed == max(0, planned - aired)
       Also raise the finding NO_TC_MAPPING.
     - Any other combination: VALIDATION_FAIL, stop the scope and send it to a person.
     - These references are what the code does; the agent does not redefine them
       (tc_engine.py :866-934).
  V2 Lock integrity, measured against the previous run.
     - Count LMRBRows in the scope with more than one of: is_matched,
       is_sponsorship_matched, is_manual_matched, is_tc_lmrb_matched.
     - Record the count as a baseline.
     - Halt only if an agent run increases it. Rows that were already double-flagged
       are reported, not repaired.
  V3 Every TransmissionReport linked to a schedule has channel and month byte-equal
     to that schedule.
  V4 Active set sanity. Any of these puts the scope into NEEDS_HUMAN:
     - two non-superseded schedules share a schedule_number
     - a schedule in the scope has is_locked=True (the agent freezes the whole scope,
       because run_scope works on the whole scope)
     - schedule numbers in the scope have different string widths (ordering risk);
       this one is a warning finding
  V5 Every change in numbers since the last snapshot is explained by logged
     AgentActions or new uploads.
- Remove the check "3rd Party >= Aired" from all code, the UI and all tests.

## A4 Per-schedule execution is mandatory
- Active schedules: use verification.engine.active_schedule_ids(), plus makeup
  schedules exactly as the engine includes them. Never re-implement Rule 12.
- reconcile_scope(scope_id), run under ScopeLock and inside transaction.atomic():
  1. run_scope(account_id, channel, month, 'smart'), once per scope.
     - Catch the ValueError for "no commercial rows", record it and continue.
     - Convert the DataFrames it returns into counts.
  2. For each active schedule, in engine order:
       reconcile_tc(..., mode='smart', schedule_id=sid)
       reconcile_sponsorship(..., mode='smart', schedule_id=sid)
  3. reconcile_period_sponsorship(ps) for each PeriodSponsorship in the scope,
     loaded by id.
  4. build_summary_data(..., schedule_id=sid), stored as one SummarySnapshot per
     schedule.
- FORBIDDEN: calling reconcile_tc, reconcile_sponsorship or build_summary_data without
  schedule_id (they would include superseded rows).
- The standalone path (reconcile_tc_lmrb) is used only for scopes with no active
  schedule.
- A TransmissionReport with schedule=None in a scope that has schedules:
  - scope state WAITING_INPUTS, reason tc_not_linked
  - a T4 proposal telling the admin to re-upload the TC with the linked schedule
  - the agent never edits TransmissionReport.schedule
- The report unit is the schedule number:
  - Authorisation is per schedule: AgentAuthorisation(schedule, snapshot_sha256).
  - SummaryReportMeta stays per scope and is edited only in the core Summary page.
  - When a scope has more than one schedule, show the notice:
    "Header fields are shared by all schedules in this scope."
- Golden check baseline: build_summary_data(schedule_id=sid) for each active schedule.
  This is what the Summary page shows when that schedule is selected.
  reconcile_scope on a copy of the database must reproduce it exactly.

## A5 Evidence and safety rules
- Attribution evidence is only a BrandMapping resolution or a ManualMatch.
  - Never use TCRow.is_schedule_matched or matched_schedule as evidence; the
    time-belt fallback (tc_engine.py:605-679) sets them without a mapping.
- Commercial tc_theme wildcards count 0 in build_summary_data:
  - Never propose a wildcard tc_theme for a commercial brand.
  - For theme variants, propose appending exact values with "|".
  - Wildcards may only ever be proposed for BrandMapping.theme or sponsorship,
    and always at T4.
- Channel strings: keep R2 (exact strings from the Schedule).
- All agent engine calls, real and dry-run, run inside transaction.atomic(), so a
  crash in run_scope never leaves partial writes.
- Dry runs:
  - PostgreSQL only, under ScopeLock, and short.
  - On SQLite, dry_run() raises unless the test settings say otherwise.
- Debounce: skip a scope if any Schedule.uploaded_at or MonitoringData.uploaded_at for
  its account is within AgentConfig.upload_debounce_minutes (default 10). This avoids
  racing the core background thread.
- New diagnoser findings (information only, never auto-fixed):
  - WILDCARD_TC_THEME_COMMERCIAL
  - CHANNEL_VARIANT: the same account has channel strings that differ only by case or
    whitespace across Schedule, LMRBRow and TransmissionReport
  - SCHEDULE_NUMBER_WIDTH
  - DUPLICATE_ACTIVE_NUMBER
  - SCHEDULE_LOCKED
  - MANUAL_LOCK_LOST: a ManualMatch whose referenced rows have is_manual_matched=False
  - TIME_BELT_UNATTRIBUTED: TC rows schedule-matched without a mapping
  - SUPERSEDED_ROWS_PRESENT

## A6 Email TC intake
- Reuse option (a). Import:
  - core.views: _find_col, _detect_tc_meta, _parse_tc_rows
  - verification.tc_converters.dispatch: get_converter
  - verification.tc_converters.gemini_ai: is_configured, parse_pdf, GeminiError
- agent/tests/test_core_contract.py imports each one and checks it with
  inspect.signature. It must fail loudly if core renames or changes any of them.
- upload_tc(attachment_id, schedule_id):
  1. Inside transaction.atomic(), create TransmissionReport with account, channel,
     month and schedule taken from the Schedule; start_date and end_date from
     _detect_tc_meta; uploaded_by = the service user; the file saved.
     Mirror every field that tc_upload populates (core/views.py:4920-5040).
  2. Collision guard. Before parsing, record the ids of existing TCRows in the
     account and channel that:
       - have a ManualMatch or a TcLmrbMatch, or
       - belong to a different tc_report
     Open a savepoint and run _parse_tc_rows. If any recorded id has disappeared,
     roll back the savepoint and create a proposal:
       - reason conflict, or tc_already_exists when it collides with the same
         schedule's report
     Otherwise update row_count and commit.
  3. Verify the on_delete behaviour of TcLmrbMatch.tc_row in Phase 1 and record it.
- PDF handling in detect_tc:
  - Run the heuristic converter from get_converter(schedule.channel).
  - If Gemini is configured, run it as well.
  - Compare the two readings on (date, aired_time, tc_theme, duration).
    Disagreeing rows are held with reason pdf_disagreement.
  - If Gemini is not configured, PDFs are suggest-only, never auto.
- Parity baseline for Phase 2: the tc_pdf_convert path (Gemini first), not tc_upload
  (which is heuristic only). Record the difference.
- Intake never reconciles. It marks the scope as needing a run, and the next agent
  cycle picks it up.

## A7 Nova and the LLM
- core/agent_chat.py and core/agent_tools.py are protected core.
- In Phase 1, write docs/agent/nova_tools_review.md classifying each function in
  agent_tools.py as read-only or writing.
  - Only functions classified read-only may be imported.
  - Writing functions are never called.
- UI name: "Reconciliation Agent". Nova remains the chat assistant.
  AgentConfig.enabled is independent of Nova's toggle.
- agent/llm/client.py sits behind a provider interface.
  - Default: Anthropic, with the model from ANTHROPIC_MODEL.
  - An optional Gemini adapter may be added. Do not wire it until approved.

## A8 Settings
- Aliases (T2) stay proposal-only. A proposal shows the exact SystemSetting key and the
  value to paste into /dashboard/settings/.
- The agent never writes SystemSetting.

## A9 Roles and service user
- Admin = super_admin, admin.
- User = team_head, planner, operations.
- channel_officer has no access to agent pages.
- Access scoping reuses core helpers by import: core.views._is_admin and _account_qs.
  Put the wrapper in agent/permissions.py.
- Service user:
  - management command agent_ensure_service_user creates a user with role operations,
    an unusable password and every Account assigned
  - agent_cycle re-syncs new accounts on every run
  - this user is the actor recorded on every AgentAction

## A10 Read-only core audit (Phase 1 deliverable)
- Command: python manage.py agent_core_audit. Strictly read-only, wrapped in a
  transaction that is always rolled back.
- Output: docs/agent/core_audit_<YYYYMMDD>.md, plus a card on Agent Overview.
- Report counts per account and month, with example ids, for:
  - BrandMappings with a wildcard tc_theme used by commercial brands, and their scopes
  - scopes with superseded schedules (whose rows would be counted if no schedule
    is selected)
  - duplicate active schedule numbers; mixed-width numbers; is_locked schedules
  - ManualMatch rows whose referenced rows lost is_manual_matched
  - TC rows schedule-matched without mapping evidence (time belt)
  - channel strings that differ only by case or whitespace
  - LMRBRows with more than one lock flag set
  - TransmissionReports with schedule=None in scopes that have schedules

## A11 Phase 1 exit (replaces the brief's exit rule)
All of the following must hold:
1. The test baseline (A2) holds.
2. test_core_contract passes.
3. agent_golden_check matches exactly for the scopes I list:
   - these must include at least one scope with a revised schedule and one with
     several schedules
   - the check runs on Postgres
4. agent_core_audit has run and its report is committed.
5. nova_tools_review.md is written.
6. git diff touches no protected paths (brief section 3), and nothing under core/.

## A12 Extra stop-and-ask triggers
Stop and ask me if:
- the golden check differs on any schedule
- build_summary_data returns a combination that V1 cannot classify
- the collision guard fires on real data
- any function in agent_tools.py is ambiguous (read-only or writing)
- the service user cannot export the Summary in-process
