# Nova tools review (Amendment A7)

Every function in `core/agent_tools.py` (protected core), classified by reading the code at
commit `5db10d2`, not by its name or docstring. A function is **writing** if it (or anything
it calls) creates, updates or deletes a database row.

Rule (A7): only functions classified read-only may be imported by `agent/` or `intake/`, and
writing functions are never called. **Phase 1 imports none of them.** Any future import must
also be pinned in `agent/tests/test_core_contract.py` (guardian checks 13 and 16).

## Writing (never call from the agent)

| Function | Line | What it writes |
|---|---|---|
| `propose_manual_match(schedule_row_id, lmrb_row_id, confirmed_by_user, user=None)` | `core/agent_tools.py:117` | Creates a `ManualMatch` (`:140`), sets `ScheduleRow.is_manual_matched` (`:145`) and `LMRBRow.is_manual_matched` (`:146`). Despite the name, it writes when `confirmed_by_user=True`. ManualMatch is T4 for the agent. |
| `propose_tc_lmrb_match(tc_row_id, lmrb_row_id, confirmed_by_user, user=None)` | `:458` | Creates a `ManualMatch` (`:481`), sets `TCRow.is_lmrb_confirmed`/`matched_lmrb` (`:487`) and `LMRBRow.is_manual_matched` (`:488`). |

## Read-only, safe to import

| Function | Line | Notes |
|---|---|---|
| `_parse_hms(value)` | `:32` | Pure helper |
| `get_schedule_row_context(schedule_row_id)` | `:47` | Reads one ScheduleRow |
| `check_brand_mapping(account_id, brand)` | `:104` | Reads BrandMapping |
| `validate_upload_selection(step, account_id, brand_hint_from_filename=None)` | `:152` | Pure advisory checks |
| `audit_brand_mapping(account_id, brand)` | `:184` | Reads BrandMapping / LMRB themes; returns warnings |
| `diagnose_unmatched_tc_spot(tc_row_id)` | `:237` | Reads TC row and mappings |
| `lookup_schedule(schedule_number_or_id)` | `:500` | Reads Schedule |
| `lookup_by_brand(brand, month=None)` | `:522` | Reads ScheduleRow |
| `open_summary_sheet(…)`, `open_schedule_detail(…)`, `open_mapping_page(…)` | `:543`, `:551`, `:562` | Return a navigation URL only |
| `generate_summary_report(…)` | `:568` | Returns a download URL only (no file is generated) |
| `propose_mapping_fix(account_id, brand, tc_theme, reasoning)` | `:581` | Returns a dict; writes nothing despite the name |
| `list_schedules(…)` | `:591` | Reads Schedule |

## Read-only, but NOT usable by the agent as-is

These write nothing, but calling them would break an agent rule:

| Function | Line | Why the agent must not use it |
|---|---|---|
| `search_lmrb_candidates(channel, date, planned_time, …)` | `:69` | Filters by channel and date only (`:77`), **not by account**, and returns locked rows (it flags them, `:95-96`). It fails R3 (full lock set) and client isolation, so it can't be a candidate source. |
| `list_unmatched_spots(account_id, channel, month, schedule_id=None, …)` | `:311` | Calls `build_summary_data(..., schedule_id=None)` when no schedule is given (`:327-328`), which counts superseded rows (A4 FORBIDDEN). Only usable with a real `schedule_id`. |
| `investigate_all_unmatched(…)` | `:381` | Uses both functions above (`:399`, `:408`, `:430`) and inherits both problems. |
| `open_summary_smart(brand, channel, month, account_id)` | `:617` | Matches channel and month with `icontains` on free text (`:634-637`). The agent must take channel/month from Schedule records only (R2, guardian check 3). |

## Ambiguity (A12)

None found: every function's effect is clear from its body. The only trap is naming:
`propose_manual_match` and `propose_tc_lmrb_match` **write**, while `propose_mapping_fix`
does **not**. The agent's own proposals live in `AgentProposal`, never in these functions.
