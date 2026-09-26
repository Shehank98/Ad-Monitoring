# Runbook: golden check and core audit on a restored copy of production

These steps close Phase 1 exit rules A11.3 (golden check on real data, PostgreSQL) and
A11.4 (core audit on real data). Run them on a **disposable restored copy**, never on the
production database. Both commands only read the data: every run is inside a transaction
that is always rolled back, and the audit sets `SET TRANSACTION READ ONLY`.

## 0. Prerequisites

- PostgreSQL 14+ client tools (`pg_dump`, `pg_restore`, `psql`)
- This branch checked out, `pip install -r requirements.txt`
- A shell with these set (replace the placeholders):

```bash
export SECRET_KEY=any-local-value
export PROD_URL='postgres://USER:PASSWORD@PROD_HOST:PORT/PROD_DB'          # read access is enough
export COPY_URL='postgres://USER:PASSWORD@LOCAL_HOST:5432/admon_restore'   # disposable
```

## 1. Restore a copy

```bash
pg_dump --format=custom --no-owner --no-acl "$PROD_URL" --file admon_prod.dump
createdb --maintenance-db="${COPY_URL%/*}/postgres" admon_restore
pg_restore --no-owner --no-acl --dbname="$COPY_URL" admon_prod.dump
```

Apply this branch's migrations to the **copy** only. This adds the `agent_*` and
`intake_*` tables and changes nothing in core tables:

```bash
DATABASE_URL="$COPY_URL" python manage.py migrate --no-input
DATABASE_URL="$COPY_URL" python manage.py showmigrations agent intake
```

## 2. Core audit (A11.4)

```bash
DATABASE_URL="$COPY_URL" python manage.py agent_core_audit
```

This writes `docs/agent/core_audit_<YYYYMMDD>.md` and prints the headline table. Commit
that file. It is the real-data report; the committed `core_audit_20260925_SYNTHETIC.md` is
only a sample. Headline figures to send to finance:
- wildcard tc_theme on commercial brands
- scopes with superseded schedules
- ManualMatch rows that lost their lock
- LOCK_ORPHANED

## 3. Golden check (A11.3)

Write the golden list: 3–6 already-authorised scopes. Include at least one with a revised
schedule and one with several schedules. Channel and month must be copied **exactly** as
they appear in the Summary page (the command refuses strings that don't match a stored
Schedule).

```bash
cat > golden_scopes.json <<'JSON'
[
  {"account": "Client name as stored", "channel": "Sirasa TV",  "month": "August 2026"},
  {"account": 12,                      "channel": "TV - Hiru TV", "month": "August 2026"}
]
JSON
```

`account` can be the Account id or its exact name.

**3a. Baseline** (what the Summary page shows per selected schedule):

```bash
DATABASE_URL="$COPY_URL" python manage.py agent_golden_check snapshot \
  --scopes golden_scopes.json --out golden_baseline.json
```

**3b. Idempotent check: the exit gate.** It must print `Result: MATCH`. On a mismatch the
command exits non-zero; stop and send me the report (A12).

```bash
DATABASE_URL="$COPY_URL" python manage.py agent_golden_check verify --mode idempotent \
  --scopes golden_scopes.json --report docs/agent/golden_idempotent_$(date +%Y%m%d).md
```

**3c. Rebuild check: report only.** It clears engine-set state (manual locks untouched)
and re-runs the agent's steps, still inside a rolled-back transaction. It refuses to run
unless `AGENT_DISPOSABLE_DB=1`. Any difference is stop-and-ask, not a failure.

```bash
AGENT_DISPOSABLE_DB=1 DATABASE_URL="$COPY_URL" python manage.py agent_golden_check verify \
  --mode rebuild --scopes golden_scopes.json --baseline golden_baseline.json \
  --report docs/agent/golden_rebuild_$(date +%Y%m%d).md
```

## 4. Confirm nothing changed

```bash
psql "$COPY_URL" -c "select count(*), sum(is_matched::int) from core_schedulerow;"
psql "$COPY_URL" -c "select count(*), sum(is_lmrb_confirmed::int) from core_tcrow;"
```

Run these before step 2 and after step 3c; the numbers must be identical.

## 4b. Labelled diagnosis eval (Phase 3, S2c)

Use a backup restored from **before** the problems in your labels were fixed, so diagnose
can still see them.

1. Write the label CSV (one row per real cause you know of; UTF-8):

   ```
   account,channel,month,schedule_number,cause_code,brand,duration,as_of,note
   Keells,Sirasa TV,January 2025,101,NO_TC_MAPPING,Nexus,30,2025-02-03,tc_theme was blank
   Keells,Derana TV,January 2025,201,no_issue,,,2025-02-03,checked; all fine
   ```

   - `channel` and `month` must match the Schedule **exactly** (copy them from the Summary Sheet).
   - `cause_code`: a diagnose code (NO_TC_MAPPING, CHANNEL_VARIANT, TC_NOT_LINKED,
     WILDCARD_TC_THEME_COMMERCIAL, MANUAL_LOCK_LOST, DUPLICATE_ACTIVE_NUMBER, SCHEDULE_LOCKED,
     TC_NO_ROWS, LMRB_THEME_NO_ROWS, SPONSORSHIP_NOT_RUN, …), `no_issue` or `other`.
   - Any bad row refuses the whole file, with the line number.

2. Run the eval on the copy (never on production; it refuses without the flag):

   ```bash
   AGENT_DISPOSABLE_DB=1 DATABASE_URL="$COPY_URL" python manage.py agent_diagnose_labelled labels.csv
   ```

   It runs readiness + diagnose only (no engine, no dry run) inside read-only transactions
   and writes `docs/agent/labelled_eval_<date>.md` (per code TP / FP / misses, precision,
   recall). Exit criterion 3 is the Overall recall.

3. To store the same labels on **production** findings (criterion 2, precision), an admin runs
   `python manage.py agent_label_scopes labels.csv --actor you@company.lk`. It writes only the
   agent findings ledger, one logged action per row.

## 5. Send back

- `docs/agent/core_audit_<date>.md`
- `docs/agent/golden_idempotent_<date>.md` and `golden_rebuild_<date>.md`
- the golden list you used
- `docs/agent/labelled_eval_<date>.md` (if you ran 4b)

Then drop the copy: `dropdb --maintenance-db="${COPY_URL%/*}/postgres" admon_restore`.
