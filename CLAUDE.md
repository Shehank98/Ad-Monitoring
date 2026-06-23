# CLAUDE.md — Ad-Monitoring Project Reference

This file is the single source of truth for Claude (and any developer) when reading,
maintaining, or extending this codebase. Read it fully before making any change.

---

## 1. Project Overview

**Ad-Monitoring** is a Django web application used by a media agency (Phoenix O & M)
to verify that TV advertisements actually aired as planned.

Three independent data sources are reconciled:
| Source | What it is | Model |
|--------|-----------|-------|
| **Schedule** | Planned ad spots uploaded by the Planner | `Schedule` / `ScheduleRow` |
| **LMRB / MapOnline** | Independent 3rd-party monitoring data | `MonitoringData` / `LMRBRow` |
| **TC (Transmission Certificate)** | Channel's own record of what aired | `TransmissionReport` / `TCRow` |

---

## 2. Tech Stack

- **Backend:** Django 5.1 (Python 3.11.9)
- **Database:** SQLite (dev) / PostgreSQL (prod via `DATABASE_URL`)
- **PDF:** ReportLab (primary), FPDF (legacy Streamlit view only)
- **Excel:** openpyxl (export), xlrd + pandas (import)
- **Fuzzy matching:** fuzzywuzzy + python-Levenshtein
- **Static files:** WhiteNoise
- **Deployment:** Gunicorn on Railway (`Procfile`)
- **Auth:** Custom `accounts.User` model with role-based access

---

## 3. Startup / Deployment

```
# Procfile (runs on every deploy):
python manage.py migrate --no-input
python manage.py collectstatic --no-input --clear
python manage.py ensure_superadmin
gunicorn ad_monitor.wsgi --bind 0.0.0.0:$PORT --workers 2 --timeout 120
```

**Key environment variables** (loaded via `django-environ` from `.env`):
```
SECRET_KEY
DEBUG                        # bool, default False
DATABASE_URL                 # default sqlite:///db.sqlite3
ALLOWED_HOSTS                # list
CSRF_TRUSTED_ORIGINS         # list
ALLOWED_EMAIL_DOMAIN         # restrict signup to one domain
SUPER_ADMIN_EMAILS           # auto-promote on login
```

**Upload limits:** 50 MB (`DATA_UPLOAD_MAX_MEMORY_SIZE`, `FILE_UPLOAD_MAX_MEMORY_SIZE`)
**Timezone:** `Asia/Colombo`
**Static:** `/static/` → `staticfiles/`
**Media:** `/media/` → `media/`

---

## 4. User Roles

Managed by `@role_required(['role1', 'role2'])` decorator in `accounts/decorators.py`.

| Role | Access |
|------|--------|
| `super_admin` | Everything |
| `admin` | Everything |
| `team_head` | Read-only + approvals |
| `planner` | Upload schedules |
| `operations` | Upload monitoring data |

Unauthenticated → redirect to `/auth/login/`.
Wrong role → HTTP 403 (renders `403.html`), not a redirect.

---

## 5. URL Routes (all under `/dashboard/` prefix)

```
/                                         → dashboard
/users/                                   → user_list
/users/create/                            → create_user
/users/<id>/edit/                         → edit_user
/accounts/                                → account_list
/channels/                                → channel_list

/schedules/                               → schedule_list
/schedules/upload/                        → schedule_upload
/schedules/detect/                        → schedule_detect           (AJAX POST)
/schedules/<pk>/download/                 → schedule_download
/schedules/<pk>/delete/                   → schedule_delete

/brand-mappings/                          → brand_mapping_list
/brand-mappings/options/                  → brand_mapping_options     (AJAX: account options)
/brand-mappings/channels/                 → brand_mapping_channels    (AJAX: channels for account)
/brand-mappings/months/                   → brand_mapping_months      (AJAX: months for channel)
/brand-mappings/schedules/                → brand_mapping_schedules   (AJAX: schedules for month)

/monitoring/                              → monitoring_list
/monitoring/upload/                       → monitoring_upload
/monitoring/detect/                       → monitoring_detect         (AJAX POST)
/monitoring/<pk>/download/                → monitoring_download
/monitoring/<pk>/delete/                  → monitoring_delete
/monitoring/group/<group_id>/delete/      → monitoring_delete_group   (POST)
/monitoring/matched-lmrb-excel/           → matched_lmrb_excel        (Excel download)

/monitor/                                 → monitoring_dashboard      (analytics tabs)
/monitor/pdf/                             → monitoring_pdf            (missed ad PDF)
/monitor/analytics/                       → analytics_full            (full analytics view)

/tc/                                      → tc_list
/tc/upload/                               → tc_upload
/tc/detect/                               → tc_detect                 (AJAX POST)
/tc/preview/                              → tc_preview                (preview parsed TC rows)
/tc/upload-parsed/                        → tc_upload_parsed          (submit previewed rows)
/tc/<pk>/delete/                          → tc_delete                 (POST)
/tc/reconcile/                            → tc_reconcile              (GET or POST)
/tc/detail/                               → tc_three_way              (three-way detail view)
/tc/pdf-convert/                          → tc_pdf_convert            (PDF TC file → Excel preview)

/tc/lmrb-match/                           → tc_lmrb_match             (standalone TC↔LMRB, no schedule)
/tc/lmrb-match/run/                       → tc_lmrb_match_run         (POST: auto-match / reset)
/tc/lmrb-match/candidates/                → tc_lmrb_match_candidates  (AJAX: LMRB pool for a TC row)
/tc/lmrb-match/assign/                    → tc_lmrb_match_assign      (POST: manual pair)
/tc/lmrb-match/remove/<pk>/               → tc_lmrb_match_remove      (POST: unmatch + unlock)
/tc/lmrb-match/download/                  → tc_lmrb_match_download    (Excel: 3 sheets)

/summary/                                 → summary_report            (GET=view, POST=save meta)
/summary/excel/                           → summary_excel
/summary/pdf/                             → summary_pdf

/sponsorship/reconcile/                   → sponsorship_reconcile     (auto-match sponsorship)
/sponsorship/candidates/                  → sponsorship_candidates    (AJAX: candidate LMRB rows)
/sponsorship/assign/                      → sponsorship_assign        (POST: manual assign)
/sponsorship/reset/                       → sponsorship_reset         (POST: reset assignments)
/sponsorship/unmatched-rows/              → sponsorship_unmatched_rows

/manual/                                  → manual_reconciliation     (manual match UI)
/manual/match/                            → manual_match_create       (POST: create ManualMatch)
/manual/dematch/<pk>/                     → manual_dematch            (POST: remove ManualMatch)

/commercial/candidates/                   → commercial_candidates     (AJAX: LMRB pool for commercial)
/commercial/unmatched-rows/               → commercial_unmatched_rows
/commercial/assign/                       → commercial_assign         (POST: manual commercial assign)

/settings/                                → system_settings           (super_admin only)
/settings/branding/                       → branding_upload           (POST: logo/branding upload)

/db-tools/                                → db_tools                  (admin diagnostics)

/admin-export/                            → admin_export              (super_admin data export)
```

Also: `/auth/`, `/verify/` (legacy Streamlit-style verification tool).

---

## 6. Data Models

### `Schedule`
One record per uploaded Excel file.
```
account (FK → Account)
channel (str)             ← MUST match ScheduleRow.channel exactly
month   (str)             ← format: "January 2025"  (auto-detected)
schedule_number (str)     ← entered by planner
version (int)             ← auto-incremented per (account, channel)
start_date, end_date      ← auto-detected from Date column
row_count, file, original_filename, uploaded_by, uploaded_at
```

### `ScheduleRow`
One row per ad in the schedule file.
```
schedule, account, channel, month
brand (str)               ← matches BrandMapping.brand
programme (str)
date (DateField)
start_time, end_time (str, "HH:MM:SS")
duration (int, seconds)
ad_type (str)             ← stored as 'COMMERCIAL BENEFITS' or 'SPONSORSHIP'
is_matched (bool)         ← row-level lock; True = claimed by commercial engine
matched_lmrb (FK → LMRBRow)
matched_at
is_manual_matched (bool)  ← permanently locked; set when a ManualMatch references this row
```

> **CRITICAL:** `ad_type` is stored as `'COMMERCIAL BENEFITS'` or `'SPONSORSHIP'`.
> During parsing, `'SPONSORSHIP BENEFITS'` (the value in client schedule files) is
> automatically normalised to `'SPONSORSHIP'`.  Any other value is silently skipped.

### `MonitoringData`
One record per channel per uploaded monitoring file.
Multi-channel uploads share the same `file_group_id` (UUID).
```
account, data_type ('maponline' | 'mediawatch')
channel, start_date, end_date
file, original_filename, row_count
file_group_id (UUID str)  ← shared across channel-split records
uploaded_by, uploaded_at
```

### `LMRBRow`
Master table of all monitoring observations. Deduplicated on upload.
```
account, channel, date (DateField)
advt_theme (str)          ← matches BrandMapping.theme
advt_time  (str)          ← "HH:MM:SS"
duration   (int, seconds)
source ('maponline' | 'mediawatch')

# Extended LMRB columns (may be empty):
product_group, advertiser, product, ads
program (aired programme name), prog_time
ad_pos, tot_ads, brk_no, pos_in_brk, ads_in_brk
lng, cost (Decimal), day

batch_id (UUID, nullable) ← groups rows from the same upload batch
dedup_key (str, unique)   ← sha256 of full identity (see Section 12)
is_matched (bool)         ← row-level lock for commercial matching
matched_schedule (FK → ScheduleRow)
matched_at, uploaded_at
is_sponsorship_matched (bool) ← row-level lock for sponsorship matching
is_manual_matched (bool)  ← permanently locked; set when a ManualMatch references this row
```

### `BrandMapping`
The bridge between all three data sources. One row per brand→theme pairing.
```
account (FK)
brand    (str)   ← as it appears in Schedule file
theme    (str)   ← as it appears in LMRB / MapOnline (Advt_Theme column)
tc_theme (str)   ← as it appears in TC file; pipe-separated for multiple themes
                    e.g. "Theme A|Theme B|Theme C"
                    blank = TC reconciliation skips this brand entirely
duration (int, optional) ← if set, mapping only applies when duration matches exactly
```

> **Rules:**
> - `theme` is used by the Schedule↔LMRB engine (`verification/engine.py`)
> - `tc_theme` is used by the TC reconciliation engine (`verification/tc_engine.py`)
> - Multiple TC themes: separate with `|` — `tc_themes_list` property splits on pipe
> - If `tc_theme` is blank, that brand is skipped during TC reconciliation
> - If `duration` is None, the mapping matches any duration
> - Matching is always **case-insensitive + strip whitespace** (`_normalize`)
> - **Wildcard suffix `*`:** If `theme` ends with `*`, the engine treats it as a
>   prefix match — any LMRB `Advt_Theme` that *starts with* the prefix (before the `*`)
>   will match.  Use this when a campaign has multiple LMRB theme variants that share
>   a common prefix, e.g. `Ai National Expo 2025*` matches
>   `Ai National Expo 2025_1 (30)(Sin)`, `Ai National Expo 2025_3 (30)(Sin)`, etc.
>   This avoids creating a separate mapping row for every theme variant.
> - **Wildcard `*` suffix is also supported for `tc_theme`** — if a `tc_theme` ends
>   with `*`, TC reconciliation treats it as a prefix match. Any TCRow whose tc_theme
>   starts with the prefix (before `*`) resolves to this brand.
>   Example: `"Unlimited Fiber - 20 Sec*"` matches `"Unlimited Fiber - 20 Sec"` and
>   `"Unlimited Fiber - 20 Sec extra"`. Pipe-separated values and wildcard can be
>   combined: `"Theme A|Theme B*"`.

### `TransmissionReport`
One record per TC file upload.
```
account, channel, month, schedule (FK, optional link)
file, original_filename, row_count
start_date, end_date    ← auto-detected from TC file
uploaded_by, uploaded_at
```

### `TCRow`
One row per spot in the TC file. Reconciliation state stored here.
```
account, tc_report (FK), channel, date
programme, tc_theme (str), duration (int), aired_time (str)
dedup_key (unique)        ← sha256(tc|account_id|channel|date|aired_time|tc_theme|dur)[:32]

is_schedule_matched (bool)   ← True if matched to a ScheduleRow
matched_schedule (FK → ScheduleRow)
is_lmrb_confirmed (bool)     ← True if cross-checked with an LMRBRow (±5 sec)
matched_lmrb (FK → LMRBRow)
is_extra (bool)              ← True if no matching ScheduleRow found
```

### `SummaryReportMeta`
User-editable fields for the printed summary. Unique per (account, channel, month).
```
supplier_invoice_no, po_no, invoice_no, notes
prepared_by, checked_by, authorised_by
created_at, updated_at
```

### `ManualMatch`
A manually confirmed linkage created by operations staff via `/dashboard/manual/`.
```
account, channel, month
match_mode: 'schedule_lmrb' | '3way' | 'tc_lmrb'
schedule_row (OneToOne → ScheduleRow, nullable)
tc_row       (OneToOne → TCRow, nullable)
lmrb_row     (OneToOne → LMRBRow, required)
note         (text, optional)
matched_at, matched_by (FK → User)
```

**Workflow:**
1. Operations user opens `/dashboard/manual/`.
2. Selects rows from the relevant panels, optionally adds a note, clicks "Match".
3. All referenced rows are locked (`is_manual_matched=True`) — the automatic engine skips them.
4. De-matching (`/manual/dematch/<pk>/`) removes the record and unlocks all rows.

**Match modes:**
- `schedule_lmrb` — links a ScheduleRow to an LMRBRow (no TC required)
- `3way` — links ScheduleRow + TCRow + LMRBRow
- `tc_lmrb` — links a TCRow to an LMRBRow only (no corresponding schedule row)

> **CRITICAL:** Once `is_manual_matched=True`, neither the automatic matching engine
> nor TC reconciliation will touch those rows. They survive `mode='reset'`.

### `SponsorshipLmrbAssignment`
Tracks the LMRB row assigned to a SPONSORSHIP ScheduleRow.
```
account (FK)
schedule_row (OneToOne → ScheduleRow, ad_type='SPONSORSHIP')
lmrb_row     (OneToOne → LMRBRow)
match_type: 'auto' | 'manual'
matched_at, matched_by (FK → User, nullable)
```

Created by:
- `verification/sponsorship_engine.py` `reconcile_sponsorship()` → `match_type='auto'`
- Operations user via sponsorship picker in the Summary Sheet → `match_type='manual'`

Once created, `LMRBRow.is_sponsorship_matched=True` acts as a permanent lock.

### `TcLmrbMatch`
A stored TC ↔ LMRB match made **without any schedule** (the "LMRB cut of TC" path).
Use when a channel sends a TC but no booking schedule exists for that period, so the
Summary Sheet reconciliation cannot run.
```
account (FK)
channel, month            ← scope (taken from the TransmissionReport)
tc_row   (OneToOne → TCRow)
lmrb_row (OneToOne → LMRBRow)
match_type: 'auto' | 'manual'
matched_at, matched_by (FK → User, nullable)
```

Created by `verification/tc_lmrb_engine.py`:
- `reconcile_tc_lmrb()` → `match_type='auto'` (greedy, one-to-one: same date +
  duration + air-time within `tc_lmrb_time_tolerance`, AND brand agreement via
  BrandMapping)
- Operations user via the "Find LMRB" picker on `/dashboard/tc/lmrb-match/` → `'manual'`

> **CRITICAL — global lock:** once matched, `LMRBRow.is_tc_lmrb_matched=True` and
> `TCRow.is_tc_lmrb_matched=True`. The commercial engine (`engine.py`), sponsorship
> engine, and the schedule-based TC engine (`tc_engine.py`) all skip LMRB rows where
> `is_tc_lmrb_matched=True`, so a row claimed here can never be reused elsewhere.
> Removing the `TcLmrbMatch` (unmatch / reset) clears both flags.
> **Brand mapping (no schedule needed):** like reconcile_tc Step 1, the TC `tc_theme`
> resolves to a brand via `BrandMapping.tc_theme` and the LMRB `advt_theme` must
> resolve to the *same* brand via `BrandMapping.theme` — so only brand-consistent
> spots are paired. When a `tc_theme` has no mapping, it falls back to time-only.

### `SummaryReportMeta`
User-editable fields for the printed summary. Unique per (account, channel, month).
```
supplier_invoice_no, po_no, invoice_no, notes
prepared_by, checked_by, authorised_by
created_at, updated_at
```

### `MatchResult`
Persisted outcome of Schedule↔LMRB matching. One row per ScheduleRow processed.
```
status: 'matched' | 'programme_mismatch' | 'late_telecast' | 'not_aired' | 'no_mapping' | 'manual_match'
brand, programme, scheduled_date, planned_start, planned_end, duration
theme, aired_date, air_time, source
schedule_row (FK), lmrb_row (FK)
run_at
```

### `SystemSetting`
Key-value store for site-wide configuration, editable at `/dashboard/settings/` (super_admin only).
Auto-created with defaults on first page visit. Read via module helpers:
```python
get_setting(key, default='')        # returns str
get_setting_int(key, default=0)     # returns int
get_setting_list(key)               # returns list[str] (comma-split)
```

| Key | Default | Description |
|-----|---------|-------------|
| `tc_lmrb_time_tolerance` | `5` | Max seconds between TC aired time and LMRB advt time for cross-check |
| `tc_extra_theme_aliases` | `` | Extra column names for TC_Theme (comma-separated) |
| `tc_extra_time_aliases` | `` | Extra column names for Aired_Time (comma-separated) |
| `tc_extra_date_aliases` | `` | Extra column names for TC Date (comma-separated) |
| `tc_extra_duration_aliases` | `` | Extra column names for TC Duration (comma-separated) |
| `tc_extra_programme_aliases` | `` | Extra column names for TC Programme (comma-separated) |
| `lmrb_extra_theme_aliases` | `` | Extra column names for LMRB Advt_Theme (comma-separated) |
| `lmrb_extra_time_aliases` | `` | Extra column names for LMRB Advt_time (comma-separated) |
| `lmrb_extra_duration_aliases` | `` | Extra column names for LMRB Duration (comma-separated) |
| `lmrb_extra_date_aliases` | `` | Extra column names for LMRB Date (comma-separated) |
| `lmrb_sponsorship_keywords` | `-BB,Com Break,DJ,-Extro,-Intro,-LLogo,Tag,Time Check,-Tr` | Keywords that classify an LMRB row as Sponsorship |

> **Note:** Column alias settings extend the built-in lists — they do not replace them.
> Add new aliases here via the settings page instead of editing code.

---

## 7. Schedule ↔ LMRB Matching Engine (`verification/engine.py`)

### Entry points
```python
run_scope(account_id, channel, month, mode='smart')
auto_run_all_for_account(account_id)
```

### Algorithm (four passes — all schedule rows complete each pass before the next)

**Pass 1 — Exact Match (same date, in-window):**
- For each ScheduleRow, find LMRBRow candidates: same channel + date + duration + theme (via BrandMapping)
- Candidate's `advt_time` falls within planned `[start_time, end_time]` window → **Matched**
- Lock both rows (`is_matched=True`)

**Pass 2 — Programme Mismatch (same date, just after window):**
- Still-unmatched ScheduleRows; same date + theme + duration
- Candidate's `advt_time` is AFTER `end_time` AND within 10 minutes (600s) of it → **Programme Mismatch**
- Lock both rows

**Pass 3 — Late Telecast (different date):**
- Still-unmatched ScheduleRows; any date where `aired_date > scheduled_date`
- Same theme + duration + `advt_time` falls within planned time window → **Late Telecast**
- Lock both rows

**Pass 4 — Not Aired:**
- Any ScheduleRow still unmatched after Passes 1–3 → **Not Aired**
- No BrandMapping for brand → **No Brand Mapping** (stored as `not_aired` / `no_mapping`)

**Extra Aired:** LMRBRows not consumed in any pass for the scope.

### Multi-schedule rules
- **Rule 10:** Multiple Schedule records per (account, channel, month) are processed in ascending `schedule_number` order. A single shared LMRB pool means a row consumed by schedule #101 cannot be claimed by #102.
- **Rule 12:** If multiple uploads share the same `schedule_number`, only the latest version (highest `version` field) is used.
- **Rule 8 (LMRB date cap):** Schedule rows with dates beyond the latest available LMRB date are left unprocessed (`is_matched=False`). They are auto-picked up when new LMRB data is uploaded.

### Row-level locking
- `ScheduleRow.is_matched = True` and `LMRBRow.is_matched = True` after a match
- `ScheduleRow.is_manual_matched = True` / `LMRBRow.is_manual_matched = True` for ManualMatch rows — these are **never** cleared by `mode='reset'`
- `mode='smart'`: queries `is_matched=False` and `is_manual_matched=False` — never double-counts
- `mode='reset'`: clears `is_matched` flags + MatchResult records (except `status='manual_match'`), then full re-run

---

## 8. TC Reconciliation Engine (`verification/tc_engine.py`)

### Entry point
```python
reconcile_tc(account_id, channel, month, mode='smart')
# Returns: {'matched': int, 'extra': int, 'lmrb_confirmed': int}
```

### Algorithm

**Step 1 — Build theme map:**
Uses `BrandMapping.tc_theme` (not `theme`) to map brands to TC themes.
Only brands with a non-empty `tc_theme` are included.

**Step 2 — TC ↔ Schedule matching (one-to-one, greedy):**
- Available TCRows indexed by `(normalized_tc_theme, duration)`
- For each ScheduleRow in date order:
  - Look up tc_themes via BrandMapping
  - Find earliest TCRow with matching (theme, duration) where **TCRow.date ≥ ScheduleRow.date** (late-aired rule)
  - Set `TCRow.is_schedule_matched = True`, link `matched_schedule`
- Remaining TCRows in pool → `is_extra = True`

**Step 3 — TC ↔ LMRB cross-check (configurable tolerance):**
- For each matched + extra TCRow, find an LMRBRow with:
  - Same channel, date, duration
  - `|aired_time_secs − advt_time_secs| ≤ tolerance` (default 5s, configurable via `SystemSetting.tc_lmrb_time_tolerance`)
  - Not already used (one-to-one)
- Set `TCRow.is_lmrb_confirmed = True`, link `matched_lmrb`

> **To adjust the tolerance:** Go to `/dashboard/settings/` and change **TC–LMRB Time Tolerance (seconds)**.
> This avoids a code change when TC and LMRB timestamps differ by more than 5 seconds.

### Normalization helpers
```python
def _normalize(s): return str(s).lower().strip() if s else ''
def _time_to_secs(t): # "HH:MM:SS" → int seconds since midnight
```

---

## 8b. Sponsorship Reconciliation Engine (`verification/sponsorship_engine.py`)

Handles SPONSORSHIP ScheduleRows separately from commercial matching.

### Entry points
```python
reconcile_sponsorship(account_id, channel, month)
# Returns: {'auto_matched': int, 'already_matched': int, 'unmatched': int}
```

### Algorithm

**Step 1 — Auto matching:**
- Find all unmatched SPONSORSHIP ScheduleRows for scope
- Find all leftover (unmatched) LMRBRows for scope
- Greedy one-to-one match by theme (via BrandMapping) + duration
- Creates `SponsorshipLmrbAssignment(match_type='auto')`; sets `LMRBRow.is_sponsorship_matched=True`

**Step 2 — Manual assignment (via UI):**
- Operations user visits the Summary Sheet sponsorship panel
- Selects an LMRBRow from the unmatched pool and assigns it to a SPONSORSHIP ScheduleRow
- Creates `SponsorshipLmrbAssignment(match_type='manual')` via `/sponsorship/assign/`

**Reset:** `/sponsorship/reset/` deletes all assignments for the scope and unlocks rows.

> Sponsorship matching uses `BrandMapping.theme` (same as commercial), NOT `tc_theme`.
> The sponsorship engine is independent of TC reconciliation.

---

## 9. Summary Report Column Definitions

**These definitions are business-critical. Do not change without approval.**

| Column | Formula | Source |
|--------|---------|--------|
| **Planned** | Count of ScheduleRows for this brand/duration | `ScheduleRow` |
| **Aired** | TCRows where `is_schedule_matched=True` AND `is_lmrb_confirmed=True` | TC ∩ LMRB |
| **3rd Party** | Total LMRBRow count for this brand/theme (independent monitoring count) | `LMRBRow` |
| **Extra** | `max(0, 3rd_Party − Planned)` — LMRB found more than planned | LMRB vs Plan |
| **Missed** | `max(0, Planned − 3rd_Party)` — LMRB found fewer than planned | LMRB vs Plan |
| **Avg 30s** | `Aired × Duration / 30` | computed |

Implemented in `build_summary_data()` in `verification/tc_engine.py`.

---

## 10. File Parsing — Column Detection

### TC file columns (`_parse_tc_rows` in `core/views.py`)

Detection is **case-insensitive** using `_find_col(df, *names)`.

| Standard name | Accepted aliases (any case) |
|---|---|
| `Channel` | `Station`, `CHANNEL`, `channel` |
| `Date` | `Aired Date`, `Prg Date`, `aired_date`, `AiredDate`, `Prg_Date` |
| `Programme` | `Program`, `Prg Name`, `PrgName`, `programme` |
| `TC_Theme` | `Advt_Theme`, `Advt_theme`, `Theme`, `theme`, `Product`, `Description`, `Ad Name`, `AdName`, `Ad_Name` |
| `Duration` | `Dur`, `Seconds`, `Ad Dur`, `Duration_Sec` |
| `Aired_Time` | `Advt_Time`, `Advt_time`, `advt_Time`, `Time`, `Aired Time`, `Ad Start`, `AdTime`, `AiredTime` |

> **If `TC_Theme` or `Aired_Time` cannot be found, the entire row is skipped.**
> This was the bug that caused "0 matched" — always add new aliases here when a
> client sends a differently-named TC file.

### Schedule file columns
Detected by `_detect_schedule_meta(df)`. Requires a `Date` column (exact, after strip).
Month is auto-set: `dates.min().strftime('%B %Y')` → e.g. `"January 2025"`.

### LMRB / MapOnline columns
Handled by `_parse_lmrb_rows()`. MapOnline renames: `Theme→Advt_Theme`, `Prg Date→Date`,
`Ad Dur→Dur`, `Ad Start→Advt_time`. MediaWatch builds date from `Dd/Mn/Yr` columns.

---

## 11. `_find_col` Helper

```python
def _find_col(df, *names):
    """Case-insensitive column finder. Returns actual column name in df, or None."""
    lower_map = {c.lower().strip(): c for c in df.columns}
    for n in names:
        actual = lower_map.get(n.lower().strip())
        if actual is not None:
            return actual
    return None
```

Use this whenever looking up a column in a user-uploaded DataFrame.
**Never use `'ColName' in df.columns` directly** — it will miss capitalisation variants.

---

## 12. Deduplication Keys

**LMRBRow** — includes break position fields so two spots in the same break at the
same time (different position) are stored as distinct rows:
```python
raw = (f'{account_id}|{channel}|{date}|{advt_time}|{advt_theme}|{dur}'
       f'|{brk_no or ""}|{pos_in_brk or ""}|{advertiser or ""}|{product or ""}')
key = sha256(raw.encode()).hexdigest()[:32]
```

**TCRow:** (prefixed with `tc|` to separate keyspace)
```python
raw = f'tc|{account_id}|{channel}|{date}|{aired_time}|{tc_theme}|{dur}'
key = sha256(raw.encode()).hexdigest()[:32]
```

> **Why different?** LMRB files include break-position metadata; TC files do not.
> The extra fields in LMRBRow's key prevent collapsing two ads in the same break.
> TC's simpler key means re-uploading a TC file with the same spot always replaces
> the existing row rather than inserting a duplicate.

Re-uploading the same file replaces existing rows (old row deleted, new row inserted).
For LMRBRow, re-upload also unlocks the previously matched ScheduleRow.

---

## 13. Channel & Month Must Match Exactly

The reconciliation queries are:
```python
ScheduleRow.objects.filter(account_id=..., channel=channel, month=month)
TCRow.objects.filter(account_id=..., channel=channel, tc_report__month=month)
LMRBRow.objects.filter(account_id=..., channel=channel, date__range=(d_min, d_max))
```

`channel` and `month` are **plain string equality** — no normalization is applied at
query time. "Sirasa TV" ≠ "sirasa tv" ≠ "Sirasa TV " (trailing space).

**Best practice:** On the TC upload form, always select the linked Schedule first.
The `scheduleSelected()` JS function will copy the exact `channel` and `month`
strings from the Schedule record into the form fields, guaranteeing a match.

---

## 14. TC Upload Form UX (templates/tc/upload.html)

```
1. Select Account → filters the schedule dropdown
2. Select Linked Schedule (optional but recommended)
   → JS auto-fills Channel and Month from data-channel / data-month attrs
3. Upload TC File
   → JS AJAX to /tc/detect/ → auto-fills Channel (if blank) + Month (if blank)
   → Month format: "January 2025" (converted from YYYY-MM-DD start_date)
4. Submit
```

**Schedule dropdown shows:** `Account | Channel | Month | #ScheduleNumber`
(Previously showed `v{version}` — this was wrong and has been fixed.)

---

## 15. PDF Reports

### Missed Ad PDF (`/monitor/pdf/`)
Function: `_build_missed_ad_pdf()` in `core/views.py`
- Landscape A4, ReportLab
- Shows: Not Aired, No Brand Mapping, Programme Mismatch, Late Telecast rows
- Columns: Brand, Duration, Programme, Planned Date, Planned Start, Planned End, Aired Date, Aired Time, Status

### Reconciliation PDF (`/summary/pdf/`)
Function: `summary_pdf()` in `core/views.py`
- **Page 1:** Summary tables (Commercial Benefits + Sponsorship Benefits) with all metrics
- **Page 2+:** Matched LMRB report — all `LMRBRow` records confirmed by TC (i.e.,
  `tc_confirmations__isnull=False`) with full columns:
  Date, Time, Theme, Duration, Programme, Prog Time, Advertiser, Product,
  Ad Pos, Brk No, Pos in Brk, Ads in Brk, Day, Cost

---

## 16. Common Bugs & How to Avoid Them

| Symptom | Root cause | Fix |
|---------|-----------|-----|
| "0 matched, 0 extra, 0 LMRB-confirmed" | TC column names have different capitalisation — `_parse_tc_rows` couldn't find `TC_Theme` or `Aired_Time` → all rows skipped | Add extra aliases via `/dashboard/settings/` (TC Theme / Aired Time aliases) without touching code |
| "No brand mapping" in TC reconciliation | `BrandMapping.tc_theme` field is blank | Fill in `tc_theme` in the Brand Mappings admin |
| Summary shows all zeros | Channel or month mismatch between TC and Schedule | Use linked schedule in TC upload to auto-fill exact values |
| Duplicate rows after re-upload | Old dedup keys not deleted | This is handled automatically — re-upload replaces rows |
| Schedule rows missing from report | `ad_type` column in Excel is not `'COMMERCIAL BENEFITS'` or `'SPONSORSHIP BENEFITS'` | Parser normalises `'SPONSORSHIP BENEFITS'` → `'SPONSORSHIP'`; any other value is silently skipped |
| LMRB count wrong in 3rd Party column | BrandMapping.theme doesn't match LMRBRow.advt_theme | Check exact spelling in brand mappings; matching is case-insensitive but must otherwise match |
| TC reconciliation matches wrong theme variant | `tc_theme` contains one value but TC file has multiple variant names | Use pipe-separated values in `tc_theme` field, e.g. `Theme A\|Theme B` |
| Manual match prevents automatic re-matching | `is_manual_matched=True` rows are permanently skipped | To undo, use `/dashboard/manual/` → de-match. `mode='reset'` does NOT clear manual matches |
| Sponsorship "Aired" shows zero | Sponsorship LMRB assignments not run yet | Visit the Summary Sheet sponsorship panel and click "Auto-reconcile" or assign manually |
| TC–LMRB confirmed count too low | Time tolerance too tight — TC and LMRB timestamps differ by more than 5s | Increase **TC–LMRB Time Tolerance** in `/dashboard/settings/` |
| TC theme variant not matching (e.g. "Brand extra") | tc_theme exact-match misses variant suffixes | Add `*` suffix to `tc_theme` in Brand Mappings, e.g. `"Unlimited Fiber - 20 Sec*"` |

---

## 17. Adding a New Feature — Checklist

1. **New model field?** → Add migration, update `__str__`, update any admin registration in `core/admin.py`
2. **New URL?** → Add to `core/urls.py`; follow existing pattern `path('section/action/', views.fn, name='name')`
3. **New column in TC/LMRB parsing?** → Prefer adding the alias via `/dashboard/settings/` (no code change needed). If a built-in alias is required, add it to `_ci_rename()` in `_parse_tc_rows()` or the LMRB parser; always use `_find_col()`, never raw string lookup
4. **New summary metric?** → Update `build_summary_data()` in `verification/tc_engine.py`; update both `summary_report.html` template and `summary_excel()` export; update `summary_pdf()` PDF
5. **New role restriction?** → Apply `@role_required([...])` decorator
6. **New file upload?** → Always: detect metadata, create header record, parse rows with dedup key, handle re-upload (delete old + insert new)
7. **Anything that touches channel or month strings?** → Treat as an exact-match primary key; never transform case or strip after storage
8. **New system-wide configuration?** → Add entry to `SETTING_DEFAULTS` in `core/models.py`; read via `get_setting()` / `get_setting_int()` / `get_setting_list()` helpers — never query `SystemSetting` directly
9. **New locking behaviour?** → Follow the `is_matched` / `is_manual_matched` pattern; document which engine sets and clears the flag, and whether `mode='reset'` should clear it

---

## 18. Key File Map

| File | Purpose |
|------|---------|
| `core/models.py` | All database models (12 classes: Account, Channel, Schedule, ScheduleRow, MonitoringData, LMRBRow, BrandMapping, TransmissionReport, TCRow, ManualMatch, SponsorshipLmrbAssignment, SummaryReportMeta, MatchResult, SystemSetting) |
| `core/views.py` | All upload, detect, list, dashboard, PDF, Excel, manual, sponsorship, settings views |
| `core/urls.py` | All URL routes (88 routes) |
| `core/forms.py` | AccountForm, ChannelForm, upload forms |
| `verification/engine.py` | Schedule ↔ LMRB four-pass matching engine |
| `verification/tc_engine.py` | TC ↔ Schedule + TC ↔ LMRB reconciliation + summary data builder |
| `verification/tc_lmrb_engine.py` | Standalone TC ↔ LMRB reconciliation (no schedule); stores `TcLmrbMatch`, locks both rows |
| `verification/sponsorship_engine.py` | SPONSORSHIP ScheduleRow ↔ LMRBRow assignment engine |
| `verification/processing.py` | Low-level helpers: normalize, lmrb_fingerprint, four-pass match_ads algorithm |
| `verification/schedule_converter.py` | Pivot schedule format detection/conversion |
| `verification/views.py` | Legacy verification tool UI + Excel export |
| `verification/tc_converters/dispatch.py` | Router for channel-specific PDF TC parsers |
| `verification/tc_converters/generic.py` | Heuristic PDF TC parser (fallback) |
| `verification/tc_converters/sirasa_tv.py` | Sirasa TV specific PDF TC parser |
| `accounts/decorators.py` | `role_required` access control decorator |
| `accounts/models.py` | Custom User model with `role` field and `CAN_CREATE` hierarchy |
| `templates/summary/report.html` | Summary sheet HTML report template |
| `templates/tc/upload.html` | TC file upload form (schedule auto-fill JS here) |
| `templates/schedules/upload.html` | Schedule upload form |
| `templates/monitoring/upload.html` | LMRB/MapOnline upload form |
| `ad_monitor/settings.py` | Django settings (reads from `.env`) |
| `CLAUDE.md` | **This file** |
