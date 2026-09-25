SYNTHETIC DATA: this report was produced on a synthetic test database, not on production.

# SYNTHETIC DATA — Core audit 2026-09-25

Read-only audit (Amendment A10). Database: postgresql. Accounts: 4. The transaction was set READ ONLY (PostgreSQL) and always rolled back.

## Headline

| Issue | Rows | Accounts | Months |
|---|---:|---:|---:|
| Wildcard tc_theme on commercial brands (commercial summary counts them as 0) | 1 | 1 | 1 |
| Scopes with superseded schedules (rows counted when no schedule is selected) | 2 | 2 | 1 |
| Duplicate active schedule numbers (keep_both) | 1 | 1 | 1 |
| Mixed-width schedule numbers (text ordering risk) | 1 | 1 | 1 |
| Locked schedules (is_locked=True) | 1 | 1 | 1 |
| ManualMatch rows whose referenced rows lost is_manual_matched | 1 | 1 | 1 |
| TC rows schedule-matched without mapping evidence (time belt) | 0 | 0 | 0 |
| Channel strings differing only by case or whitespace | 1 | 1 | 0 |
| LMRB rows with more than one lock flag | 1 | 1 | 1 |
| TransmissionReports with schedule=None in scopes that have schedules | 1 | 1 | 1 |
| LOCK_ORPHANED: lock flag set with no record behind it | 4 | 1 | 1 |

## Wildcard tc_theme on commercial brands (commercial summary counts them as 0)

| account | month | channel | brand | example_ids | tc_theme |
|---|---|---|---|---|---|
| Dialog (synthetic) | January 2025 | Sirasa TV | HBB | [3] | HBB* |

## Scopes with superseded schedules (rows counted when no schedule is selected)

| account | month | channel | count | rows_in_scope | example_ids |
|---|---|---|---|---|---|
| Keells (synthetic) | January 2025 | Sirasa TV | 1 | 5 | [1, 2] |
| Elephant House (synthetic) | January 2025 | Sirasa TV | 1 | 1 | [5, 6, 7] |

## Duplicate active schedule numbers (keep_both)

| account | month | channel | schedule_number | example_ids |
|---|---|---|---|---|
| Elephant House (synthetic) | January 2025 | Sirasa TV | 301 | [6, 5] |

## Mixed-width schedule numbers (text ordering risk)

| account | month | channel | numbers |
|---|---|---|---|
| Elephant House (synthetic) | January 2025 | Sirasa TV | ['301', '99'] |

## Locked schedules (is_locked=True)

| account | month | channel | count | example_ids |
|---|---|---|---|---|
| Elephant House (synthetic) | January 2025 | Sirasa TV | 1 | [7] |

## ManualMatch rows whose referenced rows lost is_manual_matched

| account | month | count | example_ids |
|---|---|---|---|
| Cargills (synthetic) | January 2025 | 1 | [1] |

## TC rows schedule-matched without mapping evidence (time belt)

None found.

## Channel strings differing only by case or whitespace

| account | variants |
|---|---|
| Elephant House (synthetic) | ['SIRASA TV', 'Sirasa TV'] |

## LMRB rows with more than one lock flag

| account | month | count | example_ids |
|---|---|---|---|
| Cargills (synthetic) | January 2025 | 1 | [12] |

## TransmissionReports with schedule=None in scopes that have schedules

| account | month | channel | example_ids |
|---|---|---|---|
| Elephant House (synthetic) | January 2025 | Sirasa TV | [4] |

## LOCK_ORPHANED: lock flag set with no record behind it

| sub_code | model | account | month | count | example_ids |
|---|---|---|---|---|---|
| TC_LMRB | LMRBRow | Cargills (synthetic) | January 2025 | 1 | [10] |
| SPONSORSHIP | LMRBRow | Cargills (synthetic) | January 2025 | 2 | [11, 12] |
| COMMERCIAL | LMRBRow | Cargills (synthetic) | January 2025 | 1 | [12] |
