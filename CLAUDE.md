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
Wrong role → redirect to `/dashboard/` with error message.

---

## 5. URL Routes (all under `/dashboard/` prefix)

```
/                                  → dashboard
/users/                            → user_list
/users/create/                     → create_user
/users/<id>/edit/                  → edit_user
/accounts/                         → account_list
/channels/                         → channel_list

/schedules/                        → schedule_list
/schedules/upload/                 → schedule_upload
/schedules/detect/                 → schedule_detect        (AJAX POST)
/schedules/<pk>/download/          → schedule_download
/schedules/<pk>/delete/            → schedule_delete

/brand-mappings/                   → brand_mapping_list

/monitoring/                       → monitoring_list
/monitoring/upload/                → monitoring_upload
/monitoring/detect/                → monitoring_detect      (AJAX POST)
/monitoring/<pk>/download/         → monitoring_download
/monitoring/<pk>/delete/           → monitoring_delete
/monitoring/lmrb-delete/           → lmrb_delete_range      (POST)

/monitor/                          → monitoring_dashboard   (7-tab analytics)
/monitor/pdf/                      → monitoring_pdf         (missed ad PDF)

/tc/                               → tc_list
/tc/upload/                        → tc_upload
/tc/detect/                        → tc_detect              (AJAX POST)
/tc/<pk>/delete/                   → tc_delete              (POST)
/tc/reconcile/                     → tc_reconcile           (GET or POST)

/summary/                          → summary_report         (GET=view, POST=save meta)
/summary/excel/                    → summary_excel
/summary/pdf/                      → summary_pdf
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
ad_type (str)             ← EXACTLY 'COMMERCIAL BENEFITS' or 'SPONSORSHIP'
is_matched (bool)         ← row-level lock; True = claimed by engine
matched_lmrb (FK → LMRBRow)
matched_at
```

> **CRITICAL:** `ad_type` must be exactly `'COMMERCIAL BENEFITS'` or `'SPONSORSHIP'`.
> Any other value is silently skipped during parsing and never appears in reports.

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

dedup_key (str, unique)   ← sha256(account_id|channel|date|advt_time|advt_theme|dur)[:32]
is_matched (bool)         ← row-level lock
matched_schedule (FK → ScheduleRow)
matched_at, uploaded_at
```

### `BrandMapping`
The bridge between all three data sources. One row per brand→theme pairing.
```
account (FK)
brand    (str)   ← as it appears in Schedule file
theme    (str)   ← as it appears in LMRB / MapOnline (Advt_Theme column)
tc_theme (str)   ← as it appears in TC file (blank = TC reconciliation skips this brand)
duration (int, optional) ← if set, mapping only applies when duration matches exactly
```

> **Rules:**
> - `theme` is used by the Schedule↔LMRB engine (`verification/engine.py`)
> - `tc_theme` is used by the TC reconciliation engine (`verification/tc_engine.py`)
> - If `tc_theme` is blank, that brand is skipped during TC reconciliation
> - If `duration` is None, the mapping matches any duration
> - Matching is always **case-insensitive + strip whitespace** (`_normalize`)

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

### `MatchResult`
Persisted outcome of Schedule↔LMRB matching. One row per ScheduleRow processed.
```
status: 'matched' | 'programme_mismatch' | 'late_telecast' | 'not_aired' | 'no_mapping'
brand, programme, scheduled_date, planned_start, planned_end, duration
theme, aired_date, air_time, source
schedule_row (FK), lmrb_row (FK)
run_at
```

---

## 7. Schedule ↔ LMRB Matching Engine (`verification/engine.py`)

### Entry points
```python
run_scope(account_id, channel, month, mode='smart')
auto_run_all_for_account(account_id)
```

### Algorithm (two passes)
**Pass 1 — Same date:**
- For each ScheduleRow, find LMRBRow candidates: same channel + date + duration + theme (via BrandMapping)
- If any candidate's `advt_time` is within `[start_time, end_time]` window → **Matched**
- If candidate exists but time is outside window → **Programme Mismatch**

**Pass 2 — Different date:**
- For still-unmatched ScheduleRows, search any date
- Found → **Late Telecast**
- Not found → **Not Aired**
- No BrandMapping entry → **No Brand Mapping**

**Extra Aired:** LMRBRows not consumed in either pass.

### Row-level locking
- `ScheduleRow.is_matched = True` and `LMRBRow.is_matched = True` after a match
- `mode='smart'`: queries `is_matched=False` only — never double-counts
- `mode='reset'`: clears all flags + MatchResult for scope, then full re-run

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

**Step 3 — TC ↔ LMRB cross-check (±5 seconds):**
- For each matched + extra TCRow, find an LMRBRow with:
  - Same channel, date, duration
  - `|aired_time_secs − advt_time_secs| ≤ 5`
  - Not already used (one-to-one)
- Set `TCRow.is_lmrb_confirmed = True`, link `matched_lmrb`

### Normalization helpers
```python
def _normalize(s): return str(s).lower().strip() if s else ''
def _time_to_secs(t): # "HH:MM:SS" → int seconds since midnight
```

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

**LMRBRow:**
```python
raw = f'{account_id}|{channel}|{date}|{advt_time}|{advt_theme}|{dur}'
key = sha256(raw.encode()).hexdigest()[:32]
```

**TCRow:** (prefixed with `tc|` to separate keyspace)
```python
raw = f'tc|{account_id}|{channel}|{date}|{aired_time}|{tc_theme}|{dur}'
key = sha256(raw.encode()).hexdigest()[:32]
```

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
| "0 matched, 0 extra, 0 LMRB-confirmed" | TC column names have different capitalisation — `_parse_tc_rows` couldn't find `TC_Theme` or `Aired_Time` → all rows skipped | Add new aliases to `_ci_rename` calls inside `_parse_tc_rows` |
| "No brand mapping" in TC reconciliation | `BrandMapping.tc_theme` field is blank | Fill in `tc_theme` in the Brand Mappings admin |
| Summary shows all zeros | Channel or month mismatch between TC and Schedule | Use linked schedule in TC upload to auto-fill exact values |
| Duplicate rows after re-upload | Old dedup keys not deleted | This is handled automatically — re-upload replaces rows |
| Schedule rows missing from report | `ad_type` value is not exactly `'COMMERCIAL BENEFITS'` or `'SPONSORSHIP'` | Fix the uploaded Excel file |
| LMRB count wrong in 3rd Party column | BrandMapping.theme doesn't match LMRBRow.advt_theme | Check exact spelling in brand mappings; matching is case-insensitive but must otherwise match |

---

## 17. Adding a New Feature — Checklist

1. **New model field?** → Add migration, update `__str__`, update any admin registration in `core/admin.py`
2. **New URL?** → Add to `core/urls.py`; follow existing pattern `path('section/action/', views.fn, name='name')`
3. **New column in TC/LMRB parsing?** → Add alias to `_ci_rename()` in `_parse_tc_rows()` or the LMRB parser; use `_find_col()`, never raw string lookup
4. **New summary metric?** → Update `build_summary_data()` in `verification/tc_engine.py`; update both `summary_report.html` template and `summary_excel()` export; update `summary_pdf()` PDF
5. **New role restriction?** → Apply `@role_required([...])` decorator
6. **New file upload?** → Always: detect metadata, create header record, parse rows with dedup key, handle re-upload (delete old + insert new)
7. **Anything that touches channel or month strings?** → Treat as an exact-match primary key; never transform case or strip after storage

---

## 18. Key File Map

| File | Purpose |
|------|---------|
| `core/models.py` | All 9 database models |
| `core/views.py` | All upload, detect, list, dashboard, PDF, Excel views |
| `core/urls.py` | All URL routes |
| `core/forms.py` | AccountForm, ChannelForm, upload forms |
| `verification/engine.py` | Schedule ↔ LMRB two-pass matching engine |
| `verification/tc_engine.py` | TC ↔ Schedule + TC ↔ LMRB reconciliation + summary data builder |
| `verification/processing.py` | Low-level helpers: normalize, lmrb_fingerprint, match_ads |
| `verification/views.py` | Legacy verification tool UI + Excel export |
| `accounts/decorators.py` | `role_required` access control decorator |
| `accounts/models.py` | Custom User model with `role` field |
| `templates/summary/report.html` | Summary sheet HTML report template |
| `templates/tc/upload.html` | TC file upload form (schedule auto-fill JS here) |
| `templates/schedules/upload.html` | Schedule upload form |
| `templates/monitoring/upload.html` | LMRB/MapOnline upload form |
| `ad_monitor/settings.py` | Django settings (reads from `.env`) |
| `CLAUDE.md` | **This file** |
