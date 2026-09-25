# CLAUDE.md vs code: discrepancies

Per the brief §3: the **code is the truth for behaviour**. Nothing here was "fixed". Each
entry says what CLAUDE.md claims, what the code does, and what it means for the agent.
Found during Phase 0 at commit `445fb8a`. Severity reflects the risk to correct numbers (G1).

| # | Severity | Topic | CLAUDE.md says | Code does | Agent impact |
|---|---|---|---|---|---|
| D1 | **High** | Aired definition (§9) | `is_schedule_matched=True AND is_lmrb_confirmed=True` | Any `is_lmrb_confirmed` TCRow whose tc_theme maps to the brand, schedule-matched or not, **plus** ManualMatch count (`verification/tc_engine.py:886-915`) | `validate` must not assume Aired ≤ Planned or Aired ≤ 3rd Party. Use `build_summary_data` output as-is. |
| D2 | **High** | 3rd Party definition (§9) | Total raw LMRBRow count for the brand/theme | Same TC∩LMRB-confirmed count as Aired, without manual matches (`tc_engine.py:918-930`); `0` when unmapped (`:880`). Sponsorship: `third_party = aired` (`:1111`). | "3rd Party ≥ Aired" is false when ManualMatches exist. Missed/Extra derive from this TC-based count. |
| D3 | **High** | Wildcard `*` in `tc_theme` (§6, §16) | Supported: prefix match in TC reconciliation | Supported in `reconcile_tc` matching (`tc_engine.py:187-211`, `:560-575`) and in the sponsorship summary (`:1087-1091`), but **not** in the commercial summary: `tc_theme__iexact='prefix*'` (`:894-897`, `:924-927`). A brand mapped only by wildcard shows Aired = 0 / 3rd Party = 0 / Missed = Planned. | The agent must not propose wildcards as a fix for commercial Aired (they won't change the Summary). Diagnose should flag brands whose only tc_theme values are wildcards. |
| D4 | **High** | Mapping required for Aired (§9) | Time-window overlap is never sufficient; the fallback was removed | Removed from `build_summary_data` (`tc_engine.py:953-971`) **but still present in `reconcile_tc` Step 3b** (`tc_engine.py:605-679`). Unmapped, LMRB-confirmed TC rows get `is_schedule_matched=True` against a slot by time. Step 1 also confirms unmapped TC rows against LMRB by time only (`:405-408`). | TCRow flags can't be used as evidence of brand attribution. The agent should read `build_summary_data` and treat `is_schedule_matched` from Step 3b as unattributed. |
| D5 | **High** | Rule 12, active version (§7) | Only the highest version per schedule_number is used | Applied only in `run_scope` (`verification/engine.py:253-273`). `reconcile_tc` (`tc_engine.py:280-300`), `build_summary_data` without `schedule_id` (`:840-846`), `reconcile_sponsorship` (`verification/sponsorship_engine.py:124-129`) and `import_from_schedule` (`verification/period_sponsorship_engine.py:196-198`) read all ScheduleRows of the scope, including superseded versions (rows are kept, `core/views.py:680-689`). | Planned can double-count after a "replace" upload unless the summary is built per schedule. The agent should always call `build_summary_data(..., schedule_id=active_id)` and flag scopes with superseded versions. |
| D6 | Medium | `Schedule.is_superseded` | Not documented | Set by the replace upload (`core/views.py:690`) but ignored by Rule 12, which uses version only. `keep_both` (`core/views.py:660`) leaves two non-superseded schedules with one number; Rule 12 drops the older silently. | Agent `get_active_schedules` must reuse `active_schedule_ids` (brief §5), and should report `keep_both` duplicates as a finding. |
| D7 | Medium | Rule 10 ordering (§7) | Ascending schedule_number | String ordering on a `CharField` (`core/models.py:106`, `engine.py:263-264`): `"99"` after `"101"` | Tests and golden checks should use same-width numbers. Flag mixed widths. |
| D8 | Medium | Channel exact match (§13) | Plain string equality, no normalisation | Case-insensitive in most engine queries, plus a "TYPE - Name" prefix strip: `_lmrb_channel_q` (`engine.py:54-64`, `tc_engine.py:53-58`), `channel__iexact` in `reconcile_tc` (`tc_engine.py:292`, `:307`), `lmrb_candidates` (`sponsorship_engine.py:248`), `reconcile_tc_lmrb` (`tc_lmrb_engine.py:95-113`). `build_summary_data`'s ScheduleRow/TCRow queries **are** exact (`tc_engine.py:841`, `:889`). | R2 still holds for the agent (copy exact strings). But a TC stored with different case would be reconciled yet **not counted** in the summary. Validate byte-equality (brief §9) is essential. |
| D9 | Medium | Commercial pool vs sponsorship lock (§6 "full lock set") | Every consumer excludes all four flags | `run_scope`'s pool excludes `is_manual_matched`, `is_tc_lmrb_matched` and (smart) `is_matched`, but **keeps `is_sponsorship_matched` rows** (`engine.py:617-630`). They're only skipped when listing Extras (`engine.py:765`). `match_ads` has no check for them. | A sponsorship-locked LMRB row can also be claimed commercially. The agent's own candidate queries must still exclude all four (R3). Report as a core finding. |
| D10 | Medium | TC ↔ Schedule algorithm (§8) | Greedy by (tc_theme, duration), earliest date, then LMRB cross-check | Order is reversed (LMRB confirmation first), ±2 h proximity pass, time-belt fallback, sponsorship tags confirmed ignoring duration (`tc_engine.py:349-679`) | Diagnose rules must follow the code, not §8. |
| D11 | Medium | Avg 30s (§9, §15) | Column `Aired × Duration / 30` on the summary/PDF | Not computed by `build_summary_data` (docstring only, `tc_engine.py:790`). The PDF comment says "Avg 30s removed" (`core/views.py:6637`). | The agent covering note must not quote Avg 30s unless it computes it itself as display-only. Ask whether the column is still wanted. |
| D12 | Medium | ManualMatch permanence (§6: "survive reset") | `is_manual_matched` is never cleared by engines | True for engines. But the schedule "replace" upload clears `ScheduleRow.is_manual_matched` (`core/views.py:684-687`), documented as a bug by `ScheduleSupersedeBugTest` (`core/tests.py:1741`). A TC re-upload **deletes TCRows by dedup key** (`core/views.py:4395`), and `ManualMatch.tc_row` is `on_delete=CASCADE` (`core/models.py:520-523`), so the ManualMatch is deleted. | The intake must check `TCRow.manual_match` on colliding dedup keys before any upload, and turn it into a proposal. |
| D13 | Medium | TC upload copies channel/month from the schedule (§13, §14) | JS copies them, guaranteeing a match | The server trusts POST text (`core/views.py:4945-4946`); `schedule` is optional (`:5002-5007`). | The agent's upload wrapper must set `channel`/`month` from the Schedule (brief R2), which is stronger than the view. |
| D14 | Medium | PDF TC reading (brief §11) | "PDFs are read twice and compared" | Not implemented. `tc_upload` uses only the heuristic converter (`core/views.py:4962-4986`). `tc_pdf_convert` / `tc_lmrb_upload` use Gemini with heuristic *fallback* (`core/views.py:4741-4768`). | The `pdf_disagreement` check is new agent code (call both converters). |
| D15 | Low | Sponsorship engine return keys (§8b) | `{'auto_matched','already_matched','unmatched'}` | `{'assigned','total_spon_rows','already_assigned'}` (`sponsorship_engine.py:226-230`) | Wrapper uses the real keys. |
| D16 | Low | Roles (§4) | 5 roles | 6 roles, incl. `channel_officer` "Marketing Officer" (`accounts/models.py:20-27`). Access also via `User.clients` materialised into `user.accounts` (`accounts/models.py:47-49`). | UI role mapping: treat `channel_officer` as a "User" with no agent pages, or exclude. Needs a decision. |
| D17 | Low | Django version (§2) | 5.1 | `requirements.txt` pins `Django>=5.2` (resolves 5.2.17) | None |
| D18 | Low | Deployment (§3) | Procfile runs migrate/collectstatic/ensure_superadmin/gunicorn | Procfile also runs `purge_maponline`. `railway.json` `startCommand` (used by Railway) omits it. | Phase 7 must reconcile the two. The brief's `railway/*.json` is actually `railway.json` at the root. |
| D19 | Low | `Schedule.is_locked` | Not documented | Comment says it prevents auto-verification re-runs (`core/models.py:117-120`), but no engine reads it | The agent could honour it as an extra freeze signal (read-only). Needs a decision. |
| D20 | Info | Existing agent | Not documented (§18 file map) | `core/agent_chat.py` (Gemini "Nova" chat) and `core/agent_tools.py` (tools incl. `propose_manual_match`, `propose_mapping_fix`) with tests `core/tests.py:3001-3346` | Decide reuse and naming before Phase 1. |
| D21 | Info | Tests | Regression guards protect Rules 10/12 | 10 of 168 tests fail today: the fixture creates `source="maponline"` rows (`core/tests.py:115-139`), which `run_scope` now ignores (`engine.py:619`). Rule 10 and Rule 12 guards are among them. | Blocks the Phase 1 exit rule "all existing tests pass". Needs your decision (see phase0_discovery.md Q11). **Resolved** by decision D1, commit `bbb2963` (fixture default now `mediawatch`; 168/168 pass). |
| D22 | Info | CLAUDE.md §18 counts | "12 classes" / "88 routes" | `core/models.py` has more model classes (e.g. PeriodSponsorship, TcLmrbMatch, TcLmrbThemeMap, Client, SpotNote…) | Documentation only |
| D23 | Medium | TC re-upload vs TcLmrbMatch (found in Phase 1, A6.3) | Removing a TcLmrbMatch clears both `is_tc_lmrb_matched` flags (§6) | `TcLmrbMatch.tc_row` is `on_delete=CASCADE` (`core/models.py:700-702`). A TC re-upload deletes TCRows by dedup key (`core/views.py:4395`), which silently deletes the TcLmrbMatch **without** clearing `LMRBRow.is_tc_lmrb_matched`. The LMRB row stays locked with no record behind it, and no engine will ever use it again. | The collision guard (A6) must treat a TCRow with a `tc_lmrb_match` as protected. Measured by the audit and diagnoser as `LOCK_ORPHANED` / `TC_LMRB`. |
| D24 | Low | Django `Meta.ordering` + `.distinct()` (found in Phase 1) | — | Schedule, TransmissionReport, MonitoringData order by `-uploaded_at`; ScheduleRow by `date, start_time`. `values_list(...).distinct()` without `.order_by()` silently adds the ordering column to `SELECT DISTINCT`, returning one row per upload / per date instead of one per key. Agent code always calls `.order_by()` first. Core code that relies on `.distinct()` (e.g. `auto_run_all_for_account`, `core/views.py` channel/month pickers) may return duplicates; most callers wrap the result in `set()` or `sorted(set(...))`. | Information only. Full call-site analysis below. |

## D24 — `.distinct()` call-site analysis (Phase 1.1, read-only)

**Cause.** On a queryset whose model has `Meta.ordering`, `values()/values_list(...).distinct()`
without an explicit `.order_by()` adds the ordering columns to `SELECT DISTINCT`. The result then
has one row per ordering value instead of one per key. Verified from the generated SQL, e.g.
`ScheduleRow…values_list('month').distinct()` →
`SELECT DISTINCT month, date, start_time … ORDER BY date, start_time`.
Call sites that end with `.order_by(<selected fields>)`, wrap the result in `set()`/a dict, or use
`.count()` (which clears ordering) are unaffected.

**Summary numbers: not affected.** The only `.distinct()` inside `build_summary_data` is
`verification/tc_engine.py:1019` (`spon_programmes`). It ends with `.order_by('programme')`, and
the SQL is `SELECT DISTINCT programme … ORDER BY 1`.

All 36 call sites in `core/` and `verification/` (excluding tests):

| # | Call site | Model (ordering) | Safe because / effect | Class |
|---|---|---|---|---|
| 1 | `core/agent_tools.py:221` | LMRBRow (date, advt_time) | Duplicates removed by the `seen` set | List only, no effect |
| 2 | `core/views.py:2114` | BrandMapping | `.count()` clears ordering; "n brands deleted" message | Count, **unaffected** |
| 3 | `core/views.py:2164` | ScheduleRow | Explicit `.order_by('brand')` | Dropdown, unaffected |
| 4 | `core/views.py:2383` | LMRBRow | Collected into sets | List only, no effect |
| 5 | `core/views.py:2432` | TCRow | Collected into sets | List only, no effect |
| 6 | `core/views.py:2442` | TCRow | Collected into sets | List only, no effect |
| 7 | `core/views.py:2470` | ScheduleRow (date, start_time) | `.append()` into a list: a brand's products can be listed repeatedly in the Quick Map picker data | **List only, visible duplicates** |
| 8 | `core/views.py:2956` | ScheduleRow | Explicit `.order_by('brand')`; per-brand counts are separate `filter().count()` calls | Count, unaffected |
| 9 | `core/views.py:2978` | ScheduleRow | Explicit `.order_by('brand')` | Count, unaffected |
| 10 | `core/views.py:3060` | LMRBRow | `if t not in map` dedups | List only, no effect |
| 11 | `core/views.py:3826` | ScheduleRow | Explicit `.order_by('channel')` | Dropdown, unaffected |
| 12 | `core/views.py:3832` | ScheduleRow | Explicit `.order_by('month')` | Dropdown, unaffected |
| 13 | `core/views.py:5045` | ChannelOfficer | Used in `channel__in=` | List only, no effect |
| 14 | `core/views.py:5194` | TCRow | Explicit `.order_by('tc_theme')` | List, unaffected |
| 15 | `core/views.py:5199` | BrandMapping | Only tested for emptiness | No effect |
| 16 | `core/views.py:6458` | TCRow | Wrapped in `set()` | No effect |
| 17 | `core/views.py:6465` | TCRow | Wrapped in `set()` | No effect |
| 18 | `core/views.py:9009` | ScheduleRow | Explicit `.order_by('channel')` | Dropdown, unaffected |
| 19 | `core/views.py:9015` | ScheduleRow | Explicit `.order_by('month')` | Dropdown, unaffected |
| 20 | `core/views.py:9150` | ScheduleRow | Explicit `.order_by('channel')` | Dropdown, unaffected |
| 21 | `core/views.py:9157` | ScheduleRow | Explicit `.order_by('month')` | Dropdown, unaffected |
| 22 | `core/views.py:9856` | LMRBRow (date, advt_time) | `lmrb_themes` repeats a theme per (date, time); `total`, `covered` and `pct` on **Admin Analytics → mapping coverage** are inflated and the % skewed | **Count (Admin Analytics only)** |
| 23 | `core/views.py:9958` | Schedule (-uploaded_at) | DB Tools "delete duplicate schedules": combos repeat, but `keeper_ids` is a set | No effect |
| 24 | `core/views.py:10291` | MatchResult (-run_at, brand, scheduled_date) | `_notify_missed_spots_email` loops over scopes that repeat per (run_at, brand, date): **the not-aired email for one scope can be sent many times** per upload | **Count/behaviour (emails)** |
| 25 | `core/views.py:10565` | TCRow | Wrapped in `set()` | No effect |
| 26 | `core/views.py:10905` | User (full rows, not `values`) | Full-row DISTINCT is correct | Not affected |
| 27 | `core/views.py:11645` | TCRow | Wrapped in `set()` | No effect |
| 28 | `verification/engine.py:855` | ScheduleRow | Later `sorted(set(overlap))` | No effect |
| 29 | `verification/engine.py:860` | LMRBRow | Dict comprehension | No effect |
| 30 | `verification/engine.py:870` | ScheduleRow (date, start_time) | `auto_run_all_for_account` runs `run_scope(..., 'smart')` **once per distinct (month, date, start_time)** instead of once per month. It runs in the upload background thread. Smart mode makes repeats mostly no-ops, but each repeat deletes and re-creates MatchResult rows for still-unmatched rows | **Unclear** (numbers expected unchanged; heavy repeated work; races widen) |
| 31 | `verification/tc_engine.py:358` | ScheduleRow | Set comprehension | No effect |
| 32 | `verification/tc_engine.py:1019` | ScheduleRow | Explicit `.order_by('programme')` — **inside build_summary_data** | Summary, **unaffected** |
| 33 | `verification/tc_lmrb_engine.py:426` | TcLmrbMatch | Added to a set | No effect |
| 34 | `verification/tc_lmrb_engine.py:430` | TransmissionReport | Added to a set | No effect |
| 35 | `verification/views.py:749` | Schedule | Explicit `.order_by('month')` | Dropdown, unaffected |
| 36 | `verification/views.py:1036` | MatchResult | Explicit `.order_by('channel', 'month')` | List, unaffected |

**For engineering (not the agent project):** #24 (repeated not-aired emails), #30 (repeated
`run_scope` in the upload thread), #22 (Admin Analytics coverage %), #7 (duplicate picker entries).
The fix in each case is to put `.order_by()` before `.values…distinct()`. Core was not changed.

## D25 — Makeup schedules and billing (pending finance)

| Severity | Topic | Code does | Agent behaviour |
|---|---|---|---|
| Medium (billing rule pending) | Where makeup spots are reported | `run_scope` includes makeup schedules (linked by `parent_schedule`) in the **parent** scope's commercial run (`verification/engine.py:276-295`, `:648-650`). `reconcile_tc(schedule_id=…)` and `build_summary_data(schedule_id=…)` include only the rows of that schedule. Makeup rows are added to TC reconciliation only when `schedule_id` is None (`tc_engine.py:292-300`), which the agent never uses (A4). | Mirrors core: per-schedule calls only. Each makeup schedule is reconciled and reported in **its own** scope's per-schedule loop (test: `agent/tests/test_makeup.py`). Finding `MAKEUP_LINKED` (info) on the parent and each makeup schedule; `ScheduleStatus.makeup_linked`. Drafts (Phase 5) will say "Makeup spots for this schedule are reported under schedule <number>." |

**Open with finance:** should makeup spots be billed under the parent schedule's report or under
the makeup schedule's own report? Until they decide, the agent follows core.
