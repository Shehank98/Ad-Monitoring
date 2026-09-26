# Runbook: one staging week for the Reconciliation Agent (level 0)

The goal is to run the agent for a week against a copy of production, in a separate staging
environment, with every outbound message switched off. The agent stays at **autonomy level 0**.
It observes and rehearses; it never applies anything.

A person runs every step below. **The agent never writes any of the settings in step 2**; they are
core settings (`SystemSetting`), and the gate refuses agent writes to core tables.

Related runbook: `docs/agent/runbook_real_data.md` (golden check and audit on a restored copy).

---

## 0. What you need

- A staging Railway environment, separate from production, with its own PostgreSQL database.
  **Nothing in staging may point at the production database.**
- The latest production backup (`pg_dump -Fc` file).
- `psql` and `pg_restore` on the machine you run the steps from.
- The staging database URL: `export STAGING_URL=postgres://…/staging`.
- The names and emails of the people testing on staging (for the digest, step 3).

## 1. Restore the production backup into staging PostgreSQL

**Do not start any staging service yet** (web, cron-agent, cron-audit, cron-intake). They must stay
stopped until step 2 is done.

```bash
# empty the staging database, then restore
psql "$STAGING_URL" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
pg_restore --no-owner --no-privileges -d "$STAGING_URL" production_YYYYMMDD.dump
```

## 2. Switch off every outbound message, before any service starts

Core can reach the outside world in these ways. All of them are set **in the restored staging
database or in the staging service variables**, never in production.

### 2a. SystemSetting keys (in the database)

| Key | Used by | Set to | Why that value is safe |
|---|---|---|---|
| `whatsapp_enabled` | `core/whatsapp.py::_get_config` (`send_template`, `send_text`), `core/views.py::whatsapp_test` (:11311), missed-spot and reconcile-done alerts (`core/views.py:10147`, `:10212`), TC reminders (`:11421`), replies to incoming messages (`:11516`) | `0` | every WhatsApp send checks it first and returns without sending |
| `whatsapp_access_token` | same | `` (empty) | second lock: without a token nothing can be sent, even if someone switches WhatsApp back on |
| `whatsapp_phone_number_id` | same | `` (empty) | same |
| `whatsapp_test_number` | `core/whatsapp.py::_resolve_to` | `` (empty) | leave empty; it only redirects messages, it does not stop them |
| `email_enabled` | `core/views.py::_notify_missed_spots_email` (:10239) and the agent digest (`agent/digest.py`) | `0` | core email returns at once; the digest is only logged (NotificationLog `via='log'`) |
| `email_host` | same | `` (empty) | second lock: no SMTP host |
| `email_host_password` | same | `` (empty) | second lock: no SMTP login |
| `nova_enabled` | Nova chat widget (`core/agent_chat.py`, sends chat text to Gemini) | `0` | hides the chat, so no client data goes to Gemini from chat |

Other WhatsApp keys (`whatsapp_app_base_url`, `whatsapp_company_name`, `whatsapp_webhook_verify_token`)
do not send anything and can stay as they are.

**Either** a person sets them on **System Settings** (`/dashboard/settings/`, super admin; start
only the staging web service for this, with no cron services and no variables from 2b), **or**
run this one-off SQL against the staging database before starting anything:

Save this as `staging_outbound_off.sql`. It **inserts or updates** each key, because a missing row reads as the code default, and for `nova_enabled` that default is on. On an existing row it changes only the value. (Tested on a copy: afterwards `get_setting_int('whatsapp_enabled')`, `get_setting_int('email_enabled')` and `get_setting('nova_enabled')` all read 0.)

```sql
-- Staging only. Run against $STAGING_URL, never production.
BEGIN;
INSERT INTO core_systemsetting (key, value, label, description, category, updated_at) VALUES
  ('whatsapp_enabled',         '0', 'WhatsApp enabled',         'staging: off', 'whatsapp', now()),
  ('whatsapp_access_token',    '',  'WhatsApp access token',    'staging: empty', 'whatsapp', now()),
  ('whatsapp_phone_number_id', '',  'WhatsApp phone number ID', 'staging: empty', 'whatsapp', now()),
  ('whatsapp_test_number',     '',  'WhatsApp test number',     'staging: empty', 'whatsapp', now()),
  ('email_enabled',            '0', 'Email enabled',            'staging: off', 'email', now()),
  ('email_host',               '',  'SMTP host',                'staging: empty', 'email', now()),
  ('email_host_password',      '',  'SMTP password',            'staging: empty', 'email', now()),
  ('nova_enabled',             '0', 'Nova chat enabled',        'staging: off', 'display', now())
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
SELECT key, value FROM core_systemsetting
 WHERE key IN ('whatsapp_enabled', 'whatsapp_access_token', 'whatsapp_phone_number_id', 'whatsapp_test_number',
               'email_enabled', 'email_host', 'email_host_password', 'nova_enabled')
 ORDER BY key;
COMMIT;
```

```bash
psql "$STAGING_URL" -f staging_outbound_off.sql
```

Expected result: `whatsapp_enabled = 0`, `email_enabled = 0`, `nova_enabled = 0`, and the token, phone id,
test number, host and password all empty.

**Want to see the digest email itself?** Leave `email_enabled = 0` for the first days (the digest text is
still saved in AgentRun and shown in the logs). To receive it later, a person sets `email_enabled = 1` and
a **staging-only** SMTP account on System Settings, **after** step 3 has limited the recipients. Core's
missed-spot emails would then also go out. They go to the users who can see each account, so either
switch email back off or accept that the restored users will receive them.

### 2b. Environment variables on the staging services

| Variable | Service | Staging value | Why |
|---|---|---|---|
| `GEMINI_API_KEY` | web | **unset** (unless you are testing PDF TC conversion on purpose) | PDF TC conversion and Nova chat send file or chat content to Google |
| `FIREBASE_STORAGE_BUCKET` | web, cron-* | **unset**, or a staging-only bucket | if set to the production bucket, staging uploads would write into production storage |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | cron-intake | **unset** | the intake assistant is not part of this week |
| `INTAKE_*` (mailbox) | cron-intake | **unset** | do not create cron-intake at all this week |
| `DATABASE_URL` | all | `$STAGING_URL` | never the production database |
| `AGENT_DISPOSABLE_DB` | **none** | never set on a service | see step 4 |

## 3. Digest recipients: named staging testers only

The restored database contains every production admin. The digest must reach only the named testers.

1. Start only the staging **web** service (no cron services yet).
2. Make sure each tester has an active **admin** or **super admin** user on staging. The digest never
   goes to other roles.
3. **Agent Settings** (`/dashboard/agent/config/`) → **Digest recipients**: select exactly the named
   testers and save. The change is logged in Activity (a human action).

Why this matters: when the list is empty, the digest goes to **every** active super admin and admin.

## 4. Migrations, service user, golden check, audit

Run these as one-off commands (Railway "run command" or a shell with the staging variables):

```bash
python manage.py migrate --noinput
python manage.py agent_ensure_service_user
```

Then follow `docs/agent/runbook_real_data.md` steps 2–4:

```bash
python manage.py agent_core_audit --output docs/agent/core_audit_staging.md
python manage.py agent_golden_check snapshot --scopes golden_scopes.json --out golden_baseline.json
python manage.py agent_golden_check verify --mode idempotent --scopes golden_scopes.json --report golden_idempotent.md
AGENT_DISPOSABLE_DB=1 python manage.py agent_golden_check verify --mode rebuild --scopes golden_scopes.json --baseline golden_baseline.json --report golden_rebuild.md
```

**`AGENT_DISPOSABLE_DB=1` is set only on that one command line** (the rebuild), and on
`agent_diagnose_labelled` if you run the labelled eval. **Never add it to any service's variables.**
Commands that need it refuse to run without it, and no service needs it.

Expected results: idempotent **MATCH**; rebuild report reviewed; the audit file written.

## 5. Create the two cron services

Create each one from its file, in the staging environment, from the same repository and branch as
the staging web service.

| Service | Config file | Start command | Schedule (UTC) | Restart |
|---|---|---|---|---|
| `cron-agent` | `railway/cron-agent.json` | `python manage.py agent_cycle` | `*/15 * * * *` (every 15 minutes) | NEVER |
| `cron-audit` | `railway/cron-audit.json` | `python manage.py agent_nightly` | `45 23 * * *` (05:15 Colombo, after the 01:00–05:00 window) | NEVER |

Variables for **both**: `DATABASE_URL` = `$STAGING_URL`, `SECRET_KEY` (the staging one), `DEBUG=False`,
`ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` (same as the staging web service), and optionally
`FIREBASE_STORAGE_BUCKET` (staging bucket or unset, never production). **Not needed and not to be set:**
`GEMINI_API_KEY`, `ANTHROPIC_*`, `INTAKE_*`, `AGENT_DISPOSABLE_DB`.

`cron-audit` writes its report to `/tmp/core_audit.md` inside the container and saves it as
`AgentRun(kind='audit')`. You read it on the Agent Overview (Existing-system audit → Full report).

Do **not** create `cron-intake` this week.

## 6. Agent Settings

On `/dashboard/agent/config/` (admin):

| Setting | Value |
|---|---|
| Enabled | **on** |
| Autonomy level | **0** |
| Intake fetch enabled | **off** |
| TC intake mode | **off** |
| Intake Gemini enabled | **off** |
| Digest recipients | the named testers (step 3) |
| Shadow window | 01:00–05:00, budget 1800 s (defaults) |

Save once; the change is logged in Activity. The sidebar card (once patch 0007 is applied) then
shows **Running · level 0**. The first cycle observes all scopes at the next 15-minute tick. An admin
can press **Run cycle** to mark a run as wanted; it still runs at the next tick, not in the browser.

## 7. Daily checklist for the staging week

Do this every morning after 07:30 Colombo (after the nightly audit and the digest).

| # | Check | Expected | Where to read it |
|---|---|---|---|
| 1 | The agent ran all night | Last cycle under 15 minutes ago; health OK | Sidebar card (patch 0007) or Agent Overview → **Health**, row "Agent cycle" (stale after 60 minutes) |
| 2 | No agent action inside the shadow window | **0** | Agent Overview → **Agent cycle** card, "Agent actions in windows"; nightly audit → **Agent effect**, column "Agent actions" |
| 3 | Core tables changed during the window | Only tables you can explain (people or core jobs) | Nightly audit → **Agent effect**, "Changed: reconciliation data" (look first) and "Changed: activity tables" |
| 4 | Pending windows | none | Nightly audit → Agent effect, "Status" column (`pending` = the nightly job could not get the lock; the next cycle closes it) |
| 5 | Shadow runs stayed within window and budget | "Shadow nights in budget" = nights so far | Agent Overview → **Agent cycle** card |
| 6 | The agent never blocked anyone | Yielded visits ≤ 2% of visits; no user complaints | Agent Overview → **Agent cycle** card, "Yielded visits" |
| 7 | Cycle duration | p95 under 600 s | Agent Overview → **Agent cycle** card, "Duration p95 / max" |
| 8 | Unexplained number changes | each one acknowledged with a root cause the same day | Review queue → **Agent findings**, rows `V5_UNEXPLAINED` (Acknowledge form); digest section "Unexplained number changes"; quality card "Unexplained changes (V5)" |
| 9 | New findings are right | label each new finding Correct / Incorrect / Unsure | Review queue → **Agent findings** (feedback buttons); quality card "Labelled precision" and "Mapping group precision" with n |
| 10 | Reconcile pending | note how many a person reconciled and how fast | Agent Overview → **Diagnosis quality**, "Reconcile pending" (open, reconciled by a person, median hours); scope page → **Pending effect** |
| 11 | Service user sees every client | no "cannot see … accounts" note | Agent Overview → **Health**, Agent cycle note; digest "Service user coverage" (fix: `python manage.py agent_ensure_service_user`) |
| 12 | Digest reached only the testers | only named testers | Activity; NotificationLog in Django admin, or AgentRun(kind='digest') detail |
| 13 | No outbound message left staging | nothing received by clients, channels or production users | ask the testers; `SELECT key, value FROM core_systemsetting WHERE key IN (…)` from step 2a still shows everything off |

At the end of the week, send back: the Agent Overview screenshots, the last nightly audit report,
the digests, and your notes on checks 3, 8 and 9.

## 8. Stop

To stop the agent at any time: **Pause** on the sidebar card, or Agent Settings → Enabled off. Both
are logged. Deleting the `cron-agent` service also stops it. Nothing the agent did needs undoing:
at level 0 it never changed core data.
