# Phase 1 follow-ups: the guardian's 7 non-blocking notes

From the Phase 1 numbers-guardian review (range `a4df7a3...527ae8d`). Each note has one
decision: **fixed in 1.1**, **assigned to a named phase**, or **won't fix** (with the reason).
Notes that affect the correctness of `reconcile_scope`, validate, gate or locks were fixed now.

| # | Note | Affects reconcile/validate/gate/locks? | Decision |
|---|---|---|---|
| 1 | AgentAction stores summary hashes, not values | Yes (audit trail of reconcile) | **Fixed in 1.1** |
| 2 | `reconcile_scope` has no default actor | Yes (every action must have an actor, A9) | **Fixed in 1.1** |
| 3 | Service-user command writes `accounts.User` without an AgentAction | No | **Fixed in 1.1** (owner decision 6) |
| 4 | `AgentAuthorisation` uses `on_delete=PROTECT` | No (nothing writes it before Phase 5) | **Assigned to Phase 5** |
| 5 | Standalone TC↔LMRB path has no V2/V3 | Yes (validate) | **Fixed in 1.1** (V2); V3 **won't fix** (reason below) |
| 6 | T3 trusts the caller's `exact_value` flag | Yes (gate) | **Fixed in 1.1** |
| 7 | CI push filter leaves out `docs/agent/**` | No | **Won't fix** (reason below; triggers changed by item 9) |

## Details

**1. Full summaries on the AgentAction (fixed).** `reconcile_scope` now stores the
per-schedule summaries on the action: `before.summaries` and `after.summaries` (keyed by
schedule id), next to the existing sha256 values. Amendment proposals also carry the full
authorised and current summaries. Test: `test_action_stores_full_before_and_after_summaries`.

**2. Default actor (fixed).** When no actor is passed on a real run, `reconcile_scope` uses
the service user through `service.require_service_user()`. That call asserts
`role == 'operations'`. If the user is missing or the role is wrong, it logs an error, sets
the Heartbeat alert and raises `ServiceUserError`, so nothing runs. Dry runs write no
AgentAction and need no actor. Tests: `test_standalone_path_runs_v2_and_logs_service_user` and
`test_action_stores_full_before_and_after_summaries` (actor = service user).

**3. Service-user writes (fixed per owner decision 6).**
- The person-run `agent_ensure_service_user` creates no AgentAction and prints every change.
- The system re-sync `sync_service_user()` (for `agent_cycle`):
  - only adds accounts;
  - records a `service_user_account_sync` AgentAction with before/after account ids;
  - never changes role, password or is_active, and never touches another user;
  - stops with a Heartbeat alert if the role is wrong.
- Guardian check 18 covers these rules. Tests: `ServiceUserSyncTest` (5 tests).

**4. `on_delete=PROTECT` on AgentAuthorisation (Phase 5).** Nothing creates an
AgentAuthorisation before Phase 5, so today no core delete can hit `ProtectedError`. Before
authorisations are written, Phase 5 must decide what should happen when a person deletes
a Schedule that has an agent authorisation. Options: SET_NULL plus the stored
schedule_number, or a pre-delete warning in the agent UI. Core delete views stay unchanged
either way.

**5. Standalone path checks (V2 fixed, V3 won't fix).** The standalone path now counts
multi-flag LMRB rows over the whole channel before and after `reconcile_tc_lmrb`. A V2
failure rolls back and sets NEEDS_HUMAN, as in the schedule path. The standalone scope has
no schedule period, so the count covers the whole channel, which is wider than needed.
V3 is **not applicable**: it checks that `TransmissionReport.channel`/`month` byte-match the
linked Schedule, and a standalone scope has no Schedule by definition. The standalone
scope's own strings come from the TransmissionReport (brief rule). Test:
`test_standalone_path_runs_v2_and_logs_service_user`.

**6. The gate checks for wildcards itself (fixed).** `gate.allowed(..., values=[...])` and
`gate.perform(..., values=[...])` now take the values the write would store. For T3 the gate
refuses the write when `values` is missing, or when any value or pipe-part contains `*`,
whatever the caller's `exact_value` flag says. No T3 write exists yet (Phase 4), so no caller
changes. Test: `test_wildcard_is_never_auto_applied`.

**7. CI and docs (won't fix).** Docs cannot change a test result, so leaving
`docs/agent/**` out of the CI trigger paths is intentional. Item 9 replaced the path filter
anyway: CI now runs on every `pull_request` and on pushes to this branch, with
`permissions: contents: read` and no secrets.

## New follow-ups found in 1.1

| # | Item | Phase |
|---|---|---|
| F1 | **Partial authorisation.** A scope becomes AUTHORISED only when every active schedule is authorised. With one of two schedules authorised, `reconcile_scope` still runs the engines, and `run_scope` works on the whole scope. The authorised schedule's numbers could then move from the agent's own run. V5 explains that change (agent_action) and an amendment proposal is opened, so nothing goes unrecorded. Phase 5 must still decide whether a partly authorised scope may run at all. | Phase 5 |
| F2 | **Fingerprint breadth.** The fingerprint is per account, and `match_results_run_max` is per scope. An edit to another channel's mapping in the same account also "explains" a change here. That errs towards explained, never towards a silent change, because the diff is saved in AgentRun and in the proposal. Narrow it per scope once Phase 3 has real-data volumes. | Phase 3 |
| F3 | **Amendment proposals have no UI.** They are stored as `AgentProposal(kind='amendment', tier=T4)` with the full before/after summaries and the fingerprint diff. | Phase 5 (with authorisation) |
| F4 | **The `agent_cycle` caller for `sync_service_user()`** does not exist yet. The function and its tests are ready. | Phase 2 |
