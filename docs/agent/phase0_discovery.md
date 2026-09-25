# Phase 0 — Discovery report

Read-only investigation for `AGENT_BUILD_BRIEF.md`. No code was written or changed.
All references are `file:line` at commit `445fb8a` (branch `claude/design-system-extraction-vnav5h`).

> Note: at the time of Phase 0 the brief lived at the repo root. It has since been moved
> to `docs/agent/AGENT_BUILD_BRIEF.md` (Amendment 01, A1).

## Summary of what needs your decision

1. **10 existing tests fail today, before any agent code** (Q11). The Phase 1 exit rule
   "all existing tests pass" can't be met without changing those tests, which the brief forbids.
2. **`build_summary_data` does not compute what CLAUDE.md §9 says** (Q2). 3rd Party is a
   TC∩LMRB count, not a raw LMRB count, Aired includes unscheduled TC spots, Avg 30s is not
   returned, and wildcard `tc_theme` values are not expanded for commercial rows.
3. **`reconcile_tc` still has a time-belt fallback** that links unmapped, LMRB-confirmed TC
   spots to schedule slots by time (Q2). The Summary Sheet doesn't credit them, but TCRow
   state does record them.
4. **Superseded schedule versions are only excluded by `run_scope`** (Q4). `reconcile_tc`,
   `build_summary_data` (without `schedule_id`) and `reconcile_sponsorship` read every row in the scope.
5. **Channel matching is case-insensitive in most engines** (Q1, Q7), unlike CLAUDE.md §13.
6. **A TC re-upload deletes TCRows by dedup key, and the delete cascades to ManualMatch** (Q5, Q7).
7. **A Gemini-based "Nova" agent already exists in `core/`** (Q10). The brief plans a new
   Anthropic-based agent.
8. **Question 5 recommendation: option (a)**, calling the existing helpers directly.

Everything is also listed in `docs/agent/discrepancies.md`.

---

## Q1. Engine signatures and return values

| Function | Signature | Returns | `mode`? | `schedule_id`? |
|---|---|---|---|---|
| `run_scope` | `verification/engine.py:452` `run_scope(account_id, channel, month, mode='smart')` | Tuple `((matched_df, prog_mis_df, late_df, not_aired_df, extra_df), total_sch)` (`engine.py:842`); pandas DataFrames, **not JSON-safe**. Raises `ValueError` when the scope has no COMMERCIAL rows (`engine.py:573-577`). Returns empty frames early in smart mode when nothing new (`engine.py:686-694`). | yes | **no** |
| `auto_run_all_for_account` | `engine.py:845` `(account_id)` | `list[{'channel','month','ok','error'?}]` (`engine.py:866-885`). Runs `run_scope(..., 'smart')` for every scope whose schedule channel overlaps LMRB channels **case-insensitively** (`engine.py:861-863`). | no (always smart) | no |
| `reconcile_tc` | `verification/tc_engine.py:217` `(account_id, channel, month, mode='smart', schedule_id=None)`, decorated `@transaction.atomic` (`tc_engine.py:216`) | `{'matched','extra','lmrb_confirmed'}` (`tc_engine.py:758-762`) | yes | yes: restricts ScheduleRows to that schedule (`tc_engine.py:282-283`) and TCRows to its TC report (`tc_engine.py:310-314`) |
| `build_summary_data` | `tc_engine.py:767` `(account_id, channel, month, schedule_id=None)` | `{'commercial': [...], 'commercial_total': {...}, 'sponsorship': [{'programme','rows','subtotal'}], 'sponsorship_total': {...}}` (`tc_engine.py:1158-1163`). Row keys: `product, dur, planned, aired, missed, extra, third_party` (plus `status`/`available_lmrb` on sponsorship rows). | n/a | yes (`tc_engine.py:840-846`) |
| `reconcile_sponsorship` | `verification/sponsorship_engine.py:108` `(account_id, channel, month, mode='smart', schedule_id=None)` | `{'assigned','total_spon_rows','already_assigned'}` (`sponsorship_engine.py:226-230`). **CLAUDE.md §8b documents different keys** (`auto_matched/already_matched/unmatched`). `mode='reset'` calls `reset_sponsorship` (`:118-119`). | yes | yes (`:128-129`) |
| `reset_sponsorship` | `sponsorship_engine.py:325` `(account_id, channel, month)` | `{'deleted','lmrb_unlocked'}` (`:346`). Destructive, T4 for the agent. | no | no |
| `reconcile_period_sponsorship` | `verification/period_sponsorship_engine.py:116` `(ps: PeriodSponsorship, user=None)`, `@transaction.atomic` (`:115`) | `coverage(ps)` dict (`:136`, `:156-181`). Takes a **model instance, not IDs**. The agent wrapper must load `PeriodSponsorship` by id. | no | no |
| `import_from_schedule` | `period_sponsorship_engine.py:184` `(account_id, channel, month, user=None)`, `@transaction.atomic` (`:183`) | `{'created','groups'}` (`:244`). T4 for the agent (the brief lists importing period sponsorships as human only). | no | no |
| `reconcile_tc_lmrb` | `verification/tc_lmrb_engine.py:119` `(account_id, channel, month, mode='smart', user=None)`, `@transaction.atomic` (`:118`) | `{'matched','unmatched_tc','unmatched_lmrb'}` (`:279-283`) | yes | no |

Related reusable helpers: `active_schedule_ids(account_id, channel, month)` (`engine.py:298`),
`diagnose_scope(account_id, channel, month)` (`engine.py:315`),
`sponsorship_engine.lmrb_candidates` (`sponsorship_engine.py:233`),
`tc_lmrb_engine.scope_date_range` (`tc_lmrb_engine.py:76`).

The order a person uses today:
- Schedule and MediaWatch uploads trigger `auto_run_all_for_account` in a background thread (Q6).
- TC reconciliation is a manual POST to `tc_reconcile` (`core/views.py:5155-5185`), which calls `reconcile_tc(..., mode=mode, schedule_id=sid)`.
- Sponsorship and period sponsorship run from their own buttons.

The brief's `reconcile_scope` order (run_scope → reconcile_tc → reconcile_sponsorship →
reconcile_period_sponsorship) is consistent with this.

---

## Q2. What `build_summary_data` actually computes vs CLAUDE.md §9

Source: `verification/tc_engine.py:767-1163`.

**Commercial rows** (one per `(brand, duration)` of `ad_type='COMMERCIAL BENEFITS'` ScheduleRows, `:849-855`):

| Column | Actual code | CLAUDE.md §9 | Differs? |
|---|---|---|---|
| Planned | Count of ScheduleRows in scope (`channel=` exact, `month=` exact), optionally `schedule_id` (`:840-855`). **No active-version filter**: superseded versions' rows are counted when `schedule_id` is None. | Count of ScheduleRows | Partly: superseded rows included |
| Aired | If the brand has no `tc_theme` for that duration: `aired = ManualMatch count` (`:866-883`). Otherwise TCRows with `is_lmrb_confirmed=True` whose `tc_theme__iexact` is one of the brand's tc_themes and same duration (`:886-901`), **plus** ManualMatch count (`:903-913`). **`is_schedule_matched` is not required**: unscheduled/extra TC spots count as Aired. | `is_schedule_matched=True AND is_lmrb_confirmed=True` | **Yes** |
| 3rd Party | The same TC∩LMRB-confirmed count as Aired, **without** manual matches (`:918-930`); `0` when there is no tc_theme mapping (`:880`). | "Total LMRBRow count for this brand/theme" | **Yes** (TC-based, not LMRB-based) |
| Extra | `max(0, third_party − planned)` (`:934`); `0` when there is no mapping | same formula | No |
| Missed | `max(0, planned − third_party)` (`:932`); `max(0, planned − manual)` with no mapping (`:878`) | same formula | No (but note: Missed uses 3rd Party, so manual matches don't reduce Missed when a mapping exists) |
| Avg 30s | **Not returned.** The docstring lists `avg_30` (`:790`) but no code computes it. The PDF removed the column (`core/views.py:6637`, comment "Avg 30s removed"). | `Aired × Duration / 30` | **Yes** |

**Wildcard `tc_theme`:** `_build_tc_theme_map` keeps the `*` (`tc_engine.py:119-134`). The commercial
Aired and 3rd Party queries use `tc_theme__iexact=t` (`:894-897`, `:924-927`). A mapping of
`"Unlimited Fiber*"` therefore matches only a TC theme literally equal to `unlimited fiber*`.
Wildcard tc_themes count **0** in commercial rows. The sponsorship section does handle `*` with
`istartswith` (`:1087-1091`). This contradicts CLAUDE.md §6 and §16. There is no test covering a
wildcard tc_theme in the commercial summary.

**Consequences for the agent's `validate`:**
- "3rd Party ≥ Aired" is **not** an invariant: `aired = third_party + manual`.
- `Extra` and `Missed` should be checked against the `third_party` and `planned` values the function returns.

**Sponsorship rows** (per programme, then `(brand, duration)`, `:1017-1143`):
- `aired` = unique LMRB ids from the union of:
  - (a) TCRows schedule-matched to a SPONSORSHIP row and LMRB-confirmed, filtered by duration
  - (b) `SponsorshipLmrbAssignment` rows
  - (c) any LMRB-confirmed TCRow whose tc_theme matches the brand at **any** duration, with wildcard support (`:1063-1095`)
- `third_party = aired` (`:1111`); `missed`/`extra` are computed against `aired`.
- `PeriodSponsorship` counts are **not** part of `build_summary_data` at all.

**`reconcile_tc` itself** (`tc_engine.py:217-763`) also differs from CLAUDE.md §8:
- Step order is TC↔LMRB **first** (`:378`), then confirmed TC → Schedule (`:450`), then unconfirmed TC → Schedule by tc_theme (`:546`), then a time-belt fallback, then Extra.
- Step 1 lets a TC row whose tc_theme has **no mapping** confirm against any LMRB row by time alone (`:405-408`, "may still confirm by time alone").
- Step 2 has a ±2 h "proximity" pass (`:511-527`).
- **Step 3b "time-belt fallback"** (`tc_engine.py:605-679`) links LMRB-confirmed TC rows *with no brand match* to a remaining commercial ScheduleRow purely on date + time window + duration. This sets `TCRow.is_schedule_matched=True` for spots with no mapping evidence. `build_summary_data` does not credit them (it filters by the brand's tc_themes), but views that read `is_schedule_matched` / `matched_schedule` will. This is the pattern CLAUDE.md §9 says was removed. It was only removed from the summary.
- Sponsorship tags are confirmed ignoring duration (`:349-376`).
- The TC↔LMRB index excludes `is_manual_matched` and `is_tc_lmrb_matched` LMRB rows, but **not** `is_matched` or `is_sponsorship_matched` (`:329-335`). TC confirmation is a cross-check, not a claim, so this is by design, but it is not the "full lock set".

---

## Q3. Are the engines safe inside `transaction.atomic()` + rollback?

**Yes, with caveats.** For a dry run, wrap everything in one outer
`transaction.atomic()` and force a rollback (`transaction.set_rollback(True)`, or raise a
private exception).

| Check | Finding |
|---|---|
| Threads | None inside `verification/*.py`. Threads are only started by **views** (`core/views.py:778`, `:1058`, `:1543`). |
| Emails / WhatsApp | None inside engines. Only views call `_notify_*` (`core/views.py:785-789`, `:1551-1555`, `:5183-5185`). |
| File writes | None in the engines listed in Q1. |
| `on_commit` hooks / signals | None in the repo (`grep` for `on_commit`, `post_save`, `receiver(` finds nothing outside comments). |
| Nested commits | `reconcile_tc`, `reconcile_period_sponsorship`, `import_from_schedule`, `reconcile_tc_lmrb` are `@transaction.atomic`. Inside an outer atomic block they become savepoints and roll back with it. `run_scope` has **no** atomic wrapper, so it can partially write if it crashes. Its "heal orphaned rows" step (`engine.py:579-611`) is proof this happened before. Wrapping it in the agent's atomic block fixes that for agent runs. No engine calls `commit()` or uses `autocommit`. |
| Caching | No Django cache use. `get_setting*` reads the DB on each call (`core/models.py:1469-1489`), so it rolls back too. |
| Side output | `print()` statements (`tc_engine.py:341,347,704,756`; `tc_lmrb_engine.py:274`). Harmless. |
| DB locks during dry run | A long dry run holds row locks and writes. On **SQLite** this blocks every other writer (including the upload threads) until rollback ("database is locked"). On Postgres the conflict is limited to touched rows. Dry runs should run only under the agent's scope lock and ideally only on Postgres. |
| Other writes to be aware of | `run_scope` writes `MatchResult` (delete + bulk_create, `engine.py:811-840`). `reconcile_tc` bulk-updates TCRow flags. Both roll back cleanly. |

---

## Q4. Rule 12 (active version) and Rule 10 (ordering)

- **Implementation:** `_active_schedules_for_scope(account_id, channel, month)` (`engine.py:253-273`)
  orders `Schedule` by `('schedule_number', '-version')` and keeps the first per
  `schedule_number`. Public wrapper: `active_schedule_ids(...)` (`engine.py:298-300`). **Reusable.**
- **Makeup schedules** (`parent_schedule` FK, `_makeup_schedules_for_scope`, `engine.py:276-295`)
  are appended after the active list and share the pool (`engine.py:648-650`).
- **Rule 10 ordering** is string ordering, because `schedule_number` is a `CharField`
  (`core/models.py:106`). `"99"` sorts after `"101"`. It matches Rule 10 only when numbers
  share a width.
- **`Schedule.is_superseded` exists** (`core/models.py:116`). It is set by the "replace" upload
  path (`core/views.py:678-691`) but **is not read by `_active_schedules_for_scope`**, which
  relies on version alone. With `duplicate_action='keep_both'` (`core/views.py:660`), two
  non-superseded schedules share a number and Rule 12 silently ignores the older one.
- **Only `run_scope` applies Rule 12.** These query every ScheduleRow in the scope, including
  superseded versions' rows (which are kept in the DB, `core/views.py:680-689`):
  - `reconcile_tc` (`tc_engine.py:280-300`)
  - `build_summary_data` when `schedule_id` is None (`tc_engine.py:840-846`)
  - `reconcile_sponsorship` (`sponsorship_engine.py:124-129`)
  - `import_from_schedule` (`period_sponsorship_engine.py:196-198`)
- **Per-schedule summary is possible** (brief §5): `build_summary_data` and `reconcile_tc`
  already accept `schedule_id`. The Summary view auto-selects the schedule when only one
  exists (`core/views.py:6331-6336`).
- `Schedule.is_locked` (`core/models.py:120`) "prevents the auto-verification engine from
  re-running" per its comment, but no engine reads it. Only a view toggles it
  (`core/views.py:1256`) and `verification/views.py:366-369` displays it.

---

## Q5. TC parsing, PDF conversion, preview and upload

| Piece | Location | Callable without HTTP? |
|---|---|---|
| Column finder | `_find_col(df, *names)` `core/views.py:4277` | Yes (pure) |
| Metadata detection | `_detect_tc_meta(df)` `core/views.py:4287` → `{channel,start_date,end_date,row_count}` | Yes (pure) |
| Row parse + upsert | `_parse_tc_rows(df, account, tc_report)` `core/views.py:4314-4400`: reads alias settings via `get_setting_list` (`:4333-4337`), forces `channel = tc_report.channel` (`:4347-4349`), builds dedup keys, **deletes existing TCRows with the same dedup key** then `bulk_create` (`:4395-4399`) | Yes. Needs a saved `TransmissionReport`. |
| Excel read | `pd.read_excel(tc_file, header=0)` inline in `tc_upload` (`core/views.py:4996-5000`) | Trivial to replicate |
| PDF, channel-specific heuristic | `verification/tc_converters/dispatch.py:29` `get_converter(channel)` → `.parse_pdf(path)` | Yes |
| PDF, Gemini | `verification/tc_converters/gemini_ai.py:84` `is_configured()`, `:106` `parse_pdf(pdf_path, channel='', extra_instructions='')`; raises `GeminiError` (`:68`) | Yes |
| Upload view (Excel/PDF) | `tc_upload` `core/views.py:4920-5040`, `@login_required @role_required([...all six roles])`. **Takes `channel` and `month` from POST text**, not from the linked schedule (`:4945-4946`; the JS copies them client-side). **The PDF path uses only the heuristic converter, not Gemini** (`:4962-4986`). Does not trigger reconciliation. | View only |
| Preview | `tc_preview` `core/views.py:4404` (AJAX, no DB write) | View only |
| Upload of client-parsed rows | `tc_upload_parsed` `core/views.py:4543` (rows as JSON) | View only |
| Gemini-first PDF page | `tc_pdf_convert` `core/views.py:4716` (Gemini then heuristic, `:4741-4768`) | View only |
| Standalone TC (no schedule) | `tc_lmrb_upload` `core/views.py:5768` | View only |

The logic the upload views add around the helpers is about 20 lines:
- read the file
- pick the PDF engine
- `TransmissionReport.objects.create(...)`
- `_parse_tc_rows`
- update `row_count`

**The brief's "PDFs are read twice and compared" does not exist today.** There is either
Gemini-with-fallback or heuristic-only, never both compared. The `pdf_disagreement` reason code
would need new agent-side code that calls both converters.

### Recommendation for Q5: **(a) call the existing helper functions directly**

- `_find_col`, `_detect_tc_meta`, `_parse_tc_rows`, `get_converter(...).parse_pdf` and
  `gemini_ai.parse_pdf` are plain functions with no `request` dependency. Importing them from
  `core.views` / `verification.tc_converters` needs no core edits.
- The agent's `tc_upload(attachment_id, schedule_id)` can then:
  - create the `TransmissionReport` with `channel=schedule.channel`, `month=schedule.month`, `schedule=schedule` (satisfying R2 byte-for-byte, which is *stronger* than today's view, which trusts POST text)
  - call `_parse_tc_rows`
  - run both PDF engines when it wants to detect disagreement
- **(b)** (calling `tc_upload` in-process) is worse:
  - it re-introduces typed channel/month
  - its PDF path skips Gemini
  - it redirects instead of returning counts
  - errors surface only as flash messages
- **(c)** isn't needed.

Caveats to accept:
1. These are private (`_`-prefixed) functions. A future core refactor could rename them, so pin
   them with a small agent test that imports each one.
2. The TC dedup key has no `tc_report` component (`core/models.py:484-486`). Uploading a spot
   that already exists in another report **deletes the old TCRow** (with its match state) and
   re-creates it under the new report.
3. That delete **cascades to `ManualMatch`** (`ManualMatch.tc_row` is `on_delete=CASCADE`,
   `core/models.py:520-523`). The intake must check `TCRow.manual_match` for colliding keys
   first and turn such uploads into a proposal (`tc_already_exists` / `conflict`).

---

## Q6. How MediaWatch upload triggers engine runs; avoiding races

- `monitoring_upload` (`core/views.py:1418`): for MediaWatch it starts
  `threading.Thread(target=_run_verification_then_email, args=(account.id,), daemon=True)`
  (`core/views.py:1541-1549`), then calls `_notify_missed_spots_whatsapp` synchronously (`:1551-1555`).
- `_run_verification_then_email` (`core/views.py:10401-10412`) runs
  `auto_run_all_for_account(account_id)` (every scope of the account, smart mode, no locking),
  then `_notify_missed_spots_email`.
- The same thread starts after `schedule_upload` (`core/views.py:776-789`) and
  `schedule_upload_multi` (`core/views.py:1056-1062`, engine only, no email).
- The thread takes no lock and records nothing, so the agent cannot observe it directly.

How the agent can avoid racing it without editing core:
1. **Debounce on upload timestamps.** Skip (defer) any scope whose account has a
   `MonitoringData.uploaded_at` or `Schedule.uploaded_at` (`core/models.py:139`, `:192`) newer
   than *N* minutes (the thread is short-lived). This is the brief's Phase 6 idea, and it works
   in cron mode too.
2. **Agent-vs-agent** conflicts are handled by `ScopeLock`. It can't stop the core thread.
3. **Why a race is mostly harmless:** both sides run in smart mode, and the row locks
   (`is_matched`) plus the one-shared-pool design make a second run a no-op. The real risk is
   the agent's **dry-run transaction** holding write locks while the thread writes. On SQLite
   that errors, on Postgres it waits. So: no dry runs within the debounce window, and keep dry
   runs short.
4. `auto_run_all_for_account` is never passed `mode='reset'`, so it cannot wipe agent-visible state.

---

## Q7. Lock flags that really exist

**LMRBRow** (`core/models.py:261-330`):
- `is_matched` (`:306`)
- `is_sponsorship_matched` (`:314`)
- `is_maponline_schedule_matched` (`:319`), MapOnline preview only
- `is_manual_matched` (`:322`)
- `is_tc_lmrb_matched` (`:328`)

Plus FKs `matched_schedule`, and reverse relations `manual_match`, `tc_confirmations`
(`TCRow.matched_lmrb`, `core/models.py:462-465`).

**TCRow** (`core/models.py:434-475`):
- `is_schedule_matched` (`:456`)
- `is_lmrb_confirmed` (`:461`)
- `is_extra` (`:466`)
- `is_tc_lmrb_matched` (`:471`)

**There is no `is_manual_matched` on TCRow.** Manual pinning is the reverse OneToOne
`manual_match` (`ManualMatch.tc_row`, `core/models.py:520-523`), queried as
`manual_match__isnull=True` (`tc_engine.py:269`, `:318`).

**ScheduleRow** (`core/models.py:201-250`):
- `is_matched` (`:227`)
- `is_maponline_matched` (`:238`)
- `is_manual_matched` (`:248`)

**How engines honour the full lock set (R3):**

| Consumer | Excludes |
|---|---|
| `run_scope` pool (`engine.py:617-630`) | `is_manual_matched`, `is_tc_lmrb_matched`, `is_matched` (smart); **`is_sponsorship_matched` rows stay in the pool** and are skipped only when listing Extras (`engine.py:765`) |
| `reconcile_sponsorship` / `lmrb_candidates` (`sponsorship_engine.py:152-180`, `:246-253`) | all four |
| `_free_lmrb_pool` (period) (`period_sponsorship_engine.py:97-112`) | all four |
| `reconcile_tc_lmrb` (`tc_lmrb_engine.py:167-179`) | all four |
| `reconcile_tc` TC↔LMRB index (`tc_engine.py:329-335`) | only `is_manual_matched`, `is_tc_lmrb_matched` (cross-check by design) |

Also note the supersede path clears `ScheduleRow.is_manual_matched`
(`core/views.py:684-687`). This is a known bug documented by `ScheduleSupersedeBugTest`
(`core/tests.py:1741`) and conflicts with R4.

---

## Q8. Roles, CAN_CREATE, account access and the service user

- Roles (`accounts/models.py:20-27`): `super_admin`, `admin`, `team_head`, `planner`,
  `operations`, **`channel_officer`** (label "Marketing Officer"). There are six, while CLAUDE.md §4 lists five.
- `ROLE_HIERARCHY` (`accounts/models.py:89`); `CAN_CREATE` (`accounts/models.py:91-95`):
  - super_admin → admin, team_head, planner, operations, channel_officer
  - admin → team_head, planner, operations, channel_officer
  - team_head → planner, operations
- `role_required` (`accounts/decorators.py:5-20`): unauthenticated → redirect, wrong role → 403 page.
- Access scoping:
  - `_is_admin` = super_admin or admin (`core/views.py:93-94`)
  - `_account_access(user, account_id)` / `_account_qs(user)` (`core/views.py:106-119`): admins see every Account, others only `user.accounts`
  - `User.clients` (M2M to `core.Client`, `accounts/models.py:49`) is "materialized" into `user.accounts` by the accounts views; access checks read `accounts` only
- Upload roles are open to all six roles (`core/views.py:97-100`).
- `summary_excel` and `summary_pdf` are `@login_required` + `_account_access` only
  (`core/views.py:7059-7075`, `:7117-7131`).

**Recommended service user:**
- role **`operations`** (lowest non-officer role; can't create users; can't reach `super_admin`-only pages such as `/dashboard/settings/`)
- with **every Account assigned in `user.accounts`**, created by an agent management command
- a sync step on each cycle, because new accounts are not auto-assigned

This is enough for in-process `summary_excel` / `summary_pdf` exports (brief §9). An `admin`
service user would be simpler (no sync) but would grant user-management and destructive views;
not recommended. `channel_officer` is too limited (`tc_lmrb_upload` excludes it,
`core/views.py:5766`).

---

## Q9. SystemSetting helpers and a write path for aliases

- Model: `SystemSetting` (`core/models.py:1409-1440`); defaults in `SETTING_DEFAULTS` (`core/models.py:956-…`).
- Readers: `get_setting` (`core/models.py:1469`), `get_setting_int` (`:1478`), `get_setting_list` (`:1486`).
- **There is no callable write helper.** The only write path is the `system_settings` view's
  POST loop (`core/views.py:8395-8404`), which iterates `SystemSetting.objects.all()` and saves
  values from `request.POST`. Missing rows are created by `_ensure_defaults()` (`core/views.py:8393`).
- Consequence (brief §8 T2): the "existing callable write path" precondition is **not met**, so
  `apply_alias` stays a **proposal** unless you approve one of:
  - (i) the agent writes `SystemSetting` directly (breaks R10's "never query directly")
  - (ii) a tiny `set_setting()` helper is added to `core/models.py` (protected file)
  - (iii) calling the view in-process as a `super_admin` user (defeats least privilege)

---

## Q10. Existing notification helpers

WhatsApp: `core/whatsapp.py`
- `send_template(to_number, template_name, params, ...)` `:48`
- `send_text(to_number, body)` `:117`
- `send_document(to_number, file_bytes, filename, ...)` `:182`
- `notify_missed_spots(officer_whatsapp, account_name, channel, ...)` `:239`
- `notify_tc_upload_reminder(officer_whatsapp, account_name, ...)` `:274`, directly usable for TC reminders
- `notify_reconciliation_done(ops_whatsapp, account_name, channel, ...)` `:298`
- `notify_new_user_created` `:322`, `notify_new_officer_created` `:355`, `send_welcome_registration` `:225`
- Config via `_get_config()` `:28`

View-level wrappers in `core/views.py` (they pick recipients):
- `_notify_missed_spots_whatsapp(account)` `:10147`
- `_notify_reconciliation_done_whatsapp(account_id, channel, month, result)` `:10212`
- `_notify_missed_spots_email(account_id)` `:10239`, which uses `django.core.mail.EmailMessage` (`:10252`, `:10387`)

**Existing "Nova" agent (not in the brief):**
- `core/agent_chat.py` is an investigation chat agent called "Nova" that uses **Gemini** over REST
  (`core/agent_chat.py:1-30`, `:245-248`).
- `core/agent_tools.py` holds deterministic tools, including `propose_manual_match` (`:117`),
  `propose_mapping_fix` (`:581`), `diagnose_unmatched_tc_spot` (`:237`) and
  `investigate_all_unmatched` (`:381`). Its docstring says the tools are shared with "the future
  background agent".
- Tests: `AgentToolsTest`, `MappingGuardianTest`, `NovaChatEndpointTest`, `NovaEnableToggleTest`
  (`core/tests.py:3001-3346`).
- The brief's new `agent/` app uses Anthropic. You should decide whether it reuses
  `core/agent_tools.py` (read-only tools only) or ignores it, and whether the two agents share the "Nova" name.

---

## Q11. Existing tests and how to run them

- One file: `core/tests.py`, 47 `TestCase` classes, **168 tests**. There is no pytest config.
- Run: `SECRET_KEY=test python manage.py test` (Django test runner, SQLite test DB when
  `DATABASE_URL` is unset). Requires `pip install -r requirements.txt`. Django resolves to
  **5.2** (`requirements.txt`: `Django>=5.2`; CLAUDE.md says 5.1).
- **Result today: 168 run, 10 FAIL, 0 ERROR (54 s):**
  - `LMRBSpotSingleUseTest.test_lmrb_row_consumed_by_first_schedule_row_only`
  - `MatchResultStatusTest.test_late_telecast_status_on_different_date`
  - `MatchResultStatusTest.test_matched_status_created_on_successful_match`
  - `MultiSchedulePriorityRule10Test.test_earlier_schedule_number_gets_lmrb_row_first`
  - `MultiSchedulePriorityRule10Test.test_later_schedule_gets_remaining_rows`
  - `RunScopeResetModeTest.test_reset_mode_clears_is_matched_flags`
  - `RunScopeSmartModeTest.test_smart_mode_creates_match_result`
  - `RunScopeSmartModeTest.test_smart_mode_matches_row_and_sets_is_matched`
  - `RunScopeSmartModeTest.test_smart_mode_skips_already_matched_lmrb_row`
  - `ScheduleVersioningRule12Test.test_only_highest_version_is_used`
- **Root cause:** the shared fixture `make_lmrb_row(..., source="maponline")`
  (`core/tests.py:115-139`) creates MapOnline rows. `run_scope` now reads only
  `source='mediawatch'` rows (`engine.py:617-620`; introduced around commits `6d00f37` /
  `e1c25c6`). All ten failures are `run_scope` tests that find an empty pool. The engine is
  behaving as designed; the tests are stale.
- **Consequence:** the Rule 10 and Rule 12 regression guards are effectively **not protecting
  anything right now**. The brief forbids modifying existing tests, and Phase 1 requires them to
  pass. You need to choose one:
  - (1) allow a one-line fixture fix (`source="mediawatch"`) in `core/tests.py`
  - (2) record the 10 as a known baseline that must not grow
- Key regression guards:
  - `UnmappedThemeNotAiredTest` (`core/tests.py:1908`), which passes
  - `ManualMatchPermanenceTest` (`:520`)
  - `SponsorshipDoubleClaimLockTest` (`:1252`)
  - `TCReconcileLateAiredRuleTest` (`:631`)
  - `TCReconcileTimeTolerance` (`:697`)
  - `ScheduleVersioningRule12Test` (`:853`, failing)
  - `MultiSchedulePriorityRule10Test` (`:789`, failing)
  - `AccountIsolationTest` (`:1665`)
  - `TcLmrbCandidatesFilterTest` (`:2832`)
  - `ScheduleSupersedeBugTest` (`:1741`), which documents the manual-lock bug in Q7
- Running the suite changed no files (`git status` clean afterwards).

---

## Q12. Base template, and Postgres advisory locks in tests

- **Base template:** `templates/base.html`. The sidebar nav groups are `<p class="nav-label">`
  sections (`templates/base.html:467`, `:489`, `:494`, `:499`, `:507`, `:518`, and "Admin" at `:527`).
  One "Agent" group would slot in before "Admin" (~`:526`). Per the brief, I'll ask before editing it.
- **DB setup:** `DATABASES = {'default': env.db('DATABASE_URL', default=sqlite)}`
  (`ad_monitor/settings.py:56-58`). With no `DATABASE_URL`, tests run on **SQLite**, which has
  no advisory locks. `psycopg2-binary` is installed (`requirements.txt:3`), so tests *can* run on
  Postgres by setting `DATABASE_URL` to a Postgres server. None is configured in this environment
  or in CI (there is no CI config in the repo).
- **Recommendation:**
  - `ScopeLock` = `pg_try_advisory_lock` when `connection.vendor == 'postgresql'`, else the `ScopeLockRow` table fallback
  - default tests exercise the fallback
  - one test module marked to run only when `DATABASE_URL` is Postgres covers the advisory path
- **Deployment note:** `Procfile` and `railway.json` differ. The Procfile also runs
  `purge_maponline` (`Procfile:1`), while `railway.json` `startCommand` does not. On Railway,
  `railway.json`'s `startCommand` takes precedence. The brief refers to `railway/*.json`, but the
  file is `railway.json` at the repo root.
