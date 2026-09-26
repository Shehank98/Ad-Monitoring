# Phase 2 report: agent UI and email TC intake (off / suggest)

Branch `claude/design-system-extraction-vnav5h`. Built to the approved plan
(`/root/.claude/plans/concurrent-giggling-tower.md`) with owner answers Q1–Q7 and changes
C1–C10. **Built and tested on synthetic data only. Nothing is deployed.**

> **Confirm must not be enabled in production until media storage is confirmed.**
> Core saves files through Django default storage (`ad_monitor/settings.py:80-99`). If
> `FIREBASE_STORAGE_BUCKET` is set, files go to Firebase Storage. If it is not, they go to
> `FileSystemStorage` at `MEDIA_ROOT`, and a redeploy loses them unless `MEDIA_ROOT` is on a
> Railway volume. Intake itself keeps attachment bytes in the database, so the code does not
> depend on the answer. The admin Confirm step writes the TC file exactly as core does.

## What was built

| Area | Where | Notes |
|---|---|---|
| Prompt | `intake/prompts/tc_intake_system.md` | Your text, saved verbatim (version 1.0.0). The runner reads the version header into `LlmCall.prompt_version`. |
| Gate by actor kind (Q6) | `agent/gate.py` | One write path, `gate.perform(actor_kind=…)`. Every write logs an `AgentAction` with the real actor, `actor_kind`, and `human_confirmed` for admin writes. See the table below. |
| Models | `agent/migrations/0004`, `intake/migrations/0002` | `AgentConfig`: modes off and suggest only; a stored `auto` is migrated to `off` and rejected by `clean()`/`save()`; new fields `intake_fetch_enabled` and `min_brand_overlap` (default 0.6). `AgentAction`: `actor_kind`, `human_confirmed`. `AllowedSender` rebuilt: `email_or_domain`, `channel_hint`, `accounts` (empty = all), `active`, `note`. `InboundAttachment`: bytes stored in `content` (BinaryField), plus `duplicate_of`, `rule_verdict`, `llm_verdict`, `evidence`, purge fields. New reason codes: `duplicate_active_number`, `schedule_locked`, `too_large`, `foreign_brands`, `llm_disagrees`, `duplicate_attachment`, `unsupported_type`. |
| Mailboxes (Q1) | `intake/mailbox.py` | `ImapMailbox`: `EXAMINE` + `UID SEARCH SINCE` + `UID FETCH BODY.PEEK[]` only; password or XOAUTH2 login; a guard refuses every other command. `GraphMailbox`: GET only, and only under `/users/{INTAKE_GRAPH_MAILBOX}/`, including `@odata.nextLink`. `INTAKE_MAILBOX_TYPE` picks one. Both are tested with fake transports only; neither has run against a real mailbox. |
| Fetch | `intake/cron/fetch.py`, `fetch_tc_emails` | Idempotent by `message_id` (plus the IMAP UID) within the `INTAKE_FETCH_SINCE_DAYS` window. Attachment rules: seen sha256 → `duplicate`/`duplicate_attachment` with a link to the original (C7); not xlsx/xls/pdf → `unsupported_type` (C8); over 25 MB → `too_large`, no bytes kept; unknown sender → `unknown_sender`. Protected by the intake-wide lock (C9). |
| Tools | `intake/cron/tools.py` | Read-only. `detect_tc` returns a summary only (C5), and its temp file is always deleted. PDFs are read by the heuristic converter and by Gemini (with `_tc_channel_prompt`), and the two readings are compared. `find_schedules` searches the sender's allowed accounts, active schedules only; the channel is used only to search, via `_lmrb_channel_q`. `get_schedule`. `check_brand_overlap` uses the engine's own `_build_reverse_tc_theme_map` + `_brands_for_tc_theme` (Q7). |
| Rules R and combiner (C2) | `intake/cron/rules.py` | `evaluate()` follows the prompt's steps 1–5. `combine()` applies the C2 table; the LLM can only downgrade. |
| LLM (C1) | `intake/llm/{schemas,provider}.py`, `intake/cron/runner.py` | Tools are exactly `detect_tc, find_schedules, get_schedule, check_brand_overlap, submit_decision`. One attachment per run, at most 15 tool calls. `submit_decision` is validated with pydantic and has no confidence field. A schedule id that no tool returned is rejected. The model comes from `ANTHROPIC_MODEL`, and `tool_choice` is `auto` (forced tool choice returns a 400 on some current models). A daily token cap, provider errors and refusals all switch to rules only. Every call is logged in `LlmCall`. |
| Confirm (C4) | `intake/confirm.py` | Web, admin, POST only. Refusal codes: `schedule_frozen`, `schedule_locked`, `duplicate_active_number`, `date_out_of_range` (unless the second tick box is set; the tick is logged). Channel, month and schedule are copied from the Schedule. The A6 collision guard runs in a savepoint: if any existing TC row would be replaced, everything rolls back (no report, no file) and a `tc_link` proposal is created. The file is saved through default storage only after the guard passes. Then `ScopeState.needs_run`; Confirm never reconciles. |
| Retention (C6) | `intake_purge_content --days 90` | Clears bytes and bodies of finished items; keeps sha256, metadata and evidence. One AgentAction per run. |
| Sender import (Q2) | `intake_import_senders <csv> --actor <admin>` | One human AgentAction per row. |
| UI | `templates/agent/*`, `agent/views.py`, `intake/views.py` | Tab bar inside `_base.html`. **Overview**: audit card from the latest `AgentRun(kind='audit')` (admin report page), grouped baseline card, TC Inbox counts, AgentAction feed. **Scopes**: unchanged. **TC Inbox**: list and detail at `/dashboard/agent/inbox/`, with the rule and assistant verdicts side by side. **Activity**: the AgentAction log. **Settings**: config, senders, per-client overrides. Access: admin = super_admin/admin; users (team_head/planner/operations) see only their own clients; channel_officer gets 403. |
| Railway (not deployed) | `railway/cron-intake.json`, `railway/cron-audit.json` | NIXPACKS, the same as `railway.json`. Intake runs every 5 minutes; the audit runs at `30 20 * * *` (02:00 Colombo) followed by the purge. Both use restart policy NEVER. |
| Guardian patch | `docs/agent/patches/0004_guardian_phase2.diff` | New check 5, plus checks 19–21. Apply after 0003; tested with `git apply --check` on a copy. |

### Gate rules by actor kind (Q6, as built)

| actor_kind | Kill switch | Other conditions | Used by |
|---|---|---|---|
| `agent` | yes, re-read immediately before the write | tier rules | reconcile, service-user sync |
| `intake_runner` | yes | `tc_intake_mode == 'suggest'` | intake decisions |
| `intake_fetch` | no | `intake_fetch_enabled` and ≥ 1 active AllowedSender | mail fetch |
| `human` | no | actor role super_admin/admin; every per-action condition True | Confirm, Ignore/Reject, Settings, senders, overrides, sender import |
| **`intake_retention`** (addition) | no | none | `intake_purge_content` only: clears stored bytes and bodies of finished intake items |

**Addition to your Q6 table, for you to confirm:** `intake_retention`. Retention must keep
running after fetch or the agent is switched off. If it ran under `intake_fetch`, switching
fetch off would stop the 90-day purge, and stored email content would be kept indefinitely.
It writes only intake tables.

## Checks done before building (C3, Q7, parity)
- **C3, several TCs per schedule: allowed.** `reconcile_tc` (`tc_engine.py:314`) and
  `build_summary_data` (`:896`, `:928`) filter TC rows on `tc_report__schedule_id`, so every
  report linked to the schedule counts. Test `test_second_tc_for_same_schedule_is_counted`
  confirms two TCs, then runs `reconcile_tc(schedule_id)` in a rolled-back transaction:
  both rows are matched.
- **Q7:** the engine's resolver is used directly (both functions were already pinned). Test
  `test_overlap_uses_engine_wildcard_and_pipe_resolution` covers `*` and `|`.
- **Parity with `tc_pdf_convert`** (`ParityWithTcPdfConvertTest`): the same parsed rows give
  the same TCRow tuples (date, time, theme, duration, programme). Differences are asserted
  as expected, not hidden:

  | | core `tc_pdf_convert` | intake Confirm |
  |---|---|---|
  | month | from the TC dates | copied from the Schedule |
  | schedule link | none | the chosen Schedule |
  | file | `''` (none stored) | the original file, via default storage |
  | same-key rows | deleted before insert, with no check | collision guard: full rollback + proposal |

- `read_rows` (intake's read-only row filter) is tested to keep exactly the rows core
  `_parse_tc_rows` keeps, aliases included (`test_read_rows_matches_core_parser`).
- New read-only core imports pinned in `test_core_contract.py`: `_tc_channel_prompt`,
  `_safe_str`, `_safe_int`, `_safe_date`. `TcLmrbThemeMap` was already allowed.

## Environment variables (new)
| Variable | Service | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | cron-intake | LLM |
| `ANTHROPIC_MODEL` | cron-intake | Model id. Unset = rules only (everything goes to review). |
| `AGENT_LLM_DAILY_TOKEN_CAP` | cron-intake | Overrides `AgentConfig.llm_daily_token_cap` (200,000). |
| `INTAKE_MAILBOX_TYPE` | cron-intake | `imap` or `graph` |
| `INTAKE_MAILBOX_FOLDER` | cron-intake | Default INBOX / inbox |
| `INTAKE_FETCH_SINCE_DAYS` | cron-intake | Default 14 |
| `INTAKE_IMAP_HOST`, `INTAKE_IMAP_PORT`, `INTAKE_IMAP_USER`, `INTAKE_IMAP_PASSWORD`, `INTAKE_IMAP_AUTH` (password \| xoauth2), `INTAKE_IMAP_OAUTH_TOKEN` | cron-intake | IMAP |
| `INTAKE_GRAPH_TENANT_ID`, `INTAKE_GRAPH_CLIENT_ID`, `INTAKE_GRAPH_CLIENT_SECRET`, `INTAKE_GRAPH_MAILBOX` | cron-intake | Graph |

Shared with the cron services: `DATABASE_URL`, `SECRET_KEY`, `GEMINI_API_KEY`,
`GEMINI_TC_MODEL`. `FIREBASE_STORAGE_BUCKET` is not needed on cron, because cron never
writes media.

## Synthetic walk-through (screens in `docs/agent/screens/`)
I copied the synthetic database (`synthetic_ui`) and loaded `docs/agent/synthetic/seed_intake.py`
(FakeMailbox, FakeProvider) with six emails:

| Email | Result |
|---|---|
| Clean Keells TC | Proposed → Confirmed in the browser: 4 rows uploaded to #101 v2, with the exact channel/month copied from the Schedule |
| Forward of the same file | `duplicate` / `duplicate_attachment` |
| "Ignore your previous instructions…" | `needs_review` / `suspicious_instruction` |
| File that matches several clients' schedules | `needs_review` / `multiple_schedules` |
| Rate card (.docx) | `ignored` / `unsupported_type` |
| Unknown sender | `needs_review` / `unknown_sender` |

The walk-through found three UI bugs, all fixed with tests:
- an empty reason filter hid rows;
- the overview activity feed still read the old AuditLog fields;
- flash messages showed twice.

Screens: `p2_overview`, `p2_inbox`, `p2_inbox_proposed`, `p2_inbox_confirmed`,
`p2_inbox_injection`, `p2_settings`, `p2_activity`.

## Results
| Run | Run | Passed | Skipped | Failed |
|---|---:|---:|---:|---:|
| SQLite, full suite (`--exclude-tag=eval`) | 379 | 377 | 2 | 0 |
| PostgreSQL 16, full suite (`--exclude-tag=eval`) | 379 | 372 | 7 | 0 |

- Phase 1.2 ended at 291 tests, so Phase 2 adds 88: the intake app, the new agent gate and UI tests, and 3 tests added after the guardian review.
- The skips are the same database-specific lock and dry-run tests as before: 2 on SQLite, 7 on PostgreSQL.
- The 4 `eval` tests are excluded by the tag, so they are not counted.
- `makemigrations agent intake --check`: no changes. New migrations are `agent/0004_phase2` (with a data step that turns any stored `auto` mode into `off`) and `intake/0002_phase2`.
- Golden `verify --mode idempotent` on the synthetic PostgreSQL data, after migrating it: **MATCH** on #101, #201 and #202. The report is unchanged.
- The UI walk-through used a copy of that database, so the golden data was not touched.

## Guardian review
Range `598d681..8dfae08`; the reviewer used the current 8-check file, plus your checks 19–21 by hand.

**PASS on all 8 checks. Checks 19, 20 and 21 also pass, and the proposed check 5 passes with one gap (fixed below).**
At review time the SQLite suite gave 376 run, 374 passed, 2 skipped, 0 failed, and golden idempotent gave MATCH.
No `.claude/**` file changed in the range.

| Guardian item | Action |
|---|---|
| Proposed check 5 gap: the Django admin allowed unlogged edits to AgentConfig, AccountOverride and AllowedSender | **Fixed.** Those admin screens are now read-only; changes go through Agent Settings, which logs them. |
| `intake_retention` / `intake_fetch` could pass the kill switch for any target | **Fixed.** The gate refuses any `intake_*` actor kind whose target is not an `intake.*` table (`test_intake_kinds_cannot_write_core_tables`). |
| `intake_purge_content --days` had no lower bound | **Fixed.** The minimum is 7 (`test_days_lower_bound`). |
| Note 1: a scope authorised the legacy way (`SummaryReportMeta.authorised_by`) was not treated as frozen | **Fixed.** Confirm refuses it with `schedule_frozen`, and `get_schedule` reports it as authorised (`test_legacy_authorised_scope_is_frozen`). |
| Note 2: race between the schedule checks and the write | Open, for Phase 5 (together with authorisation). The window is milliseconds, and the admin sees the result. |
| Note 3: the collision path's attachment status update is not logged | Accepted. The `tc_link` AgentProposal records the collision with its evidence. |
| Note 4: **Gemini receives the full PDF** on the cron side, as it already does in core `tc_pdf_convert` | **For you to know.** C5 limits apply to the intake LLM (Anthropic), not to the existing Gemini PDF reader. Unset `GEMINI_API_KEY` on cron-intake if PDFs must not go to Gemini; PDFs then stay needs_review. |
| Note 5: a file saying "Sirasa TV" does not find a schedule stored as "TV - Sirasa TV" | Accepted as safe: the item goes to `no_schedule` for review. Use `AllowedSender.channel_hint` with the stored form to narrow the search. |
| Note 6: items left in needs_review (unknown sender, too large) are never purged | Open, for you to decide. Option: purge needs_review items older than N days too. |
| Note 7: the `intake/0002` migration assumes empty intake tables | True everywhere today; the Phase 1 tables were never filled. Staging has the Phase 1 tables, which are empty. |
| Note 8: the cron commands are chained with `&&` | Intentional for intake (no runner after a failed fetch). For the audit, a failed audit skips that night's purge; I can change it to `;` if you prefer. |
| Note 9: Settings allows autonomy level 1–3 before Phase 4 | Every change is logged. There is no level-3 writer yet, and the gate already refuses T2/T4. I can cap the form at 1 until Phase 4 if you prefer. |

## Open items for you
1. **Media storage.** Confirm that `FIREBASE_STORAGE_BUCKET` is set, or that `MEDIA_ROOT` is on a volume, before Confirm is used in production.
2. **`intake_retention` actor kind.** It is an addition to your Q6 table; please confirm (see above).
3. **Mail fetch logs one AgentAction per run**, including empty runs. At every 5 minutes that is about 288 a day. Options: log only runs that stored something, or keep it for heartbeat visibility (Heartbeat is not wired yet).
4. **The sidebar link to the TC Inbox** needs a `templates/base.html` change, which is protected. The inbox is reachable from the agent tab bar. I can ship the sidebar link as patch `0005`.
5. **The eval set** (`intake/tests/test_eval.py`, tag `eval`) was not run: this environment has no API key. Run it by hand with `--tag=eval` once `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` are set.
6. **Real mailbox clients** are tested only through fake transports. The first live fetch should run against your dedicated mailbox with `intake_fetch_enabled` on and mode `off` (list only).
7. **Apply patches 0002, 0003 and 0004**, in that order, when you are ready.

Stopped before deployment.

---

# Phase 2.1

Owner items 1–9. Phase 3 exists as a plan only.

| # | Item | Built | Tests |
|---|---|---|---|
| 1 | LLM with no final decision | `tool_choice` stays `auto`, and extended thinking is not enabled. If a turn ends without `submit_decision`, the runner sends one follow-up. If there is still none, the result is rules only, NEEDS_REVIEW, reason **`llm_no_decision`**. The same applies to `submit_decision` called twice, text after it, or an invalid schema. Text *before* the call is allowed. With `llm_no_decision`, the reason is always `llm_no_decision`; the rules' own verdict and reason are kept in the note and the evidence. | `NoDecisionTest` (6): text-only ending, rescue after the follow-up, duplicate submit, text after submit, invalid schema, text before submit |
| 2 | Confirm concurrency | `_scope_guard`: ScopeLock for the schedule's scope inside `transaction.atomic()` (PostgreSQL advisory xact lock, or the ScopeLockRow elsewhere, always released). Inside it the Schedule is re-read with `select_for_update` and every refusal is re-checked: frozen, legacy authorised, locked, duplicate active number, date window. A busy lock gives **`scope_busy`**: "The agent is working on this scope. Try again in a minute." | `ConfirmConcurrencyTest`: lock contention (SQLite row lock and a second PostgreSQL connection), authorised between page load and POST, locked between page load and POST |
| 3 | Debounce | `debounce_hit` also counts `TransmissionReport.uploaded_at`, which includes a Confirm upload. | `DebounceTest` |
| 4 | Gemini flag | `AgentConfig.intake_gemini_enabled` (default off). Gemini runs only when the flag is on and `GEMINI_API_KEY` is set (`tools.gemini_allowed()`). Confirm uses the same rule, so an admin confirms the reading that was reviewed. The flag is on the Settings page, and changes are logged. | `GeminiFlagTest` (3) |
| 5 | Retention | `intake_retention` was approved as built. The purge also moves needs_review items older than `--review-days` (default 180, minimum 30) to **`expired` / `retention_expired`** and clears their content. New command `agent_nightly` runs audit, then purge; each step always runs, and the command exits non-zero if either failed. `railway/cron-audit.json` now runs `python manage.py agent_nightly`. | `RetentionNightlyTest` (3) |
| 6 | Logging and heartbeat | Fetch logs an AgentAction only when it stored something (`intake_fetch`) or failed (`intake_fetch_failed`). Fetch, runner and nightly each update Heartbeat: `last_ok_at`, `last_error_at`, `last_error`, `counts`. A new `gate.check()` lets fetch stop before reading mail. Agent Overview has a **Health** card; fetch shows as *stale* after 20 minutes without a good run while fetch is on. | `HeartbeatTest` (3), plus the updated fetch idempotency test |
| 7 | Autonomy cap | `AgentConfig.clean()` and the Settings form reject levels above 0: "Levels above 0 unlock in Phase 4." | `AutonomyCapTest` |
| 8 | Nav patch | `docs/agent/patches/0005_base_nav_inbox.diff` adds a "TC Inbox" link to the Reconciliation Agent group. The whole group is already hidden from channel_officer. Applying 0001 and then 0005 to the current `base.html` passes `git apply --check`. `base.html` itself is not edited. | — |
| 9 | Eval set | 27 cases in `intake/tests/eval_cases.py`, criteria in `docs/agent/eval_criteria.md`. The live test (tag `eval`) prints a table per case and asserts the four criteria. `EvalCasesTest` runs offline in every suite and checks each case's rules verdict. | 1 offline test; the live run is pending an API key |

Rules change found while building the eval set: when **no candidate is eligible**, only
candidates whose brands appear in the file now decide the reason. Before, a Keells file on
Sirasa TV whose schedule was locked came back as `multiple_schedules`, because an
unrelated Cargills schedule on the same channel also counted. It now says
`schedule_locked`. This can only change *which* needs_review reason is given; it can never
produce a proposal.

## About the "400 on forced tool_choice" (item 1)

**I did not see this error myself.** No real API call has been made from this environment
(there is no API key here). I chose `tool_choice: auto` because of the Claude API reference
bundled with my tooling. It lists this error for the newest models: Claude Fable 5.1,
Claude Mythos 5.1 and Claude Opus 5.5. It gives the error text as:

> `tool_choice: type "tool" and "any" are not supported for this model.`

Because I haven't observed it, please treat the text and the model list as documented, not
observed. The first live eval run will show whether your `ANTHROPIC_MODEL` accepts forced
tool use. The runner doesn't depend on it either way.

## Results (Phase 2.1)
| Run | Run | Passed | Skipped | Failed |
|---|---:|---:|---:|---:|
| SQLite, full suite (`--exclude-tag=eval`) | 400 | 398 | 2 | 0 |
| PostgreSQL 16, full suite (`--exclude-tag=eval`) | 400 | 393 | 7 | 0 |

Phase 2 ended at 379 tests, so Phase 2.1 adds 21: 20 in `test_phase2_1.py` and the
offline eval-case test. The skips are the same database-specific lock tests as before.
The live eval tests are excluded by the tag.

- `makemigrations agent intake --check`: no changes (new migrations `agent/0005_phase2_1` and `intake/0003_phase2_1`).
- Golden `verify --mode idempotent` on the synthetic PostgreSQL data, after migrating it: **MATCH** (#101, #201, #202).

## Guardian review (Phase 2.1)
GUARDIAN21_PLACEHOLDER
