# Runbook: one staging week for the Reconciliation Agent (level 0)

The goal is to run the agent for a week against a copy of production, in a separate staging
environment, with every outbound message switched off. The agent stays at **autonomy level 0**.
It observes and rehearses; it never applies anything.

A person runs every step below. **The agent never writes any of the settings in steps 1 and 3**; they are
core settings (`SystemSetting`), and the gate refuses agent writes to core tables.

Related runbook: `docs/agent/runbook_real_data.md` (golden check and audit on a restored copy).

---

## 0. What you need

- A staging Railway environment, separate from production, with its own PostgreSQL database.
  **Nothing in staging may point at the production database.**
- The latest production backup (`pg_dump -Fc` file).
- `psql` and `pg_restore` on the machine you run the steps from.
- The staging database URL: `export STAGING_URL=postgres://…/staging`.
- The names and emails of the people testing on staging (for the digest and for access, steps 1 and 4).

## 1. First, before any service starts: file storage, variables, access

### 1a. What I found about file storage

- `FIREBASE_STORAGE_BUCKET` (`ad_monitor/settings.py:88`) decides where every uploaded file lives. When
  set, all FileField reads, writes and deletes go to that Firebase bucket
  (`core.firebase_storage.FirebaseStorage`, `settings.py:93-99`). When empty, files go to the local
  folder `MEDIA_ROOT` (`settings.py:81`, default `<app>/media`, which is lost on every Railway redeploy
  unless a volume is mounted). Firebase is initialised in `core/apps.py:14` with the `FIREBASE_*`
  service-account variables.
- A save never overwrites an existing file: the storage has `exists()`, so Django picks a new name.
- **A delete does reach the bucket**: `FirebaseStorage.delete` (`core/firebase_storage.py:68-71`) removes
  the blob.
- The restored database rows keep their **production file paths** (`Schedule.file`,
  `MonitoringData.file`, `TransmissionReport.file`, `ScheduleTemplate.file`). With staging's own bucket
  or local storage, **downloads of old files fail on staging. That is expected.**
- **With the production bucket configured, deleting on staging deletes the production file.** Core
  deletes stored files here:

| Where | file:line | What it deletes |
|---|---|---|
| `schedule_delete` | `core/views.py:1189` (delete at `:1222`) | the schedule's uploaded file |
| `monitoring_delete` | `core/views.py:1797` (delete at `:1814`) | the monitoring file |
| `monitoring_delete_group` | `core/views.py:1825` (delete at `:1914`) | the shared file of a multi-channel upload |
| `tc_delete` | `core/views.py:5123` (delete at `:5144`) | the TC file |
| `schedule_template_upload` | `core/views.py:1150` (delete at `:1165`) | the previous sample template, replaced on every upload |
| `monitoring_upload` (MapOnline) | `core/views.py:1418`, purge call at `:1534` → `core/maponline_cleanup.py:71` | **every MapOnline file older than 30 days**, automatically after any MapOnline upload |
| `purge_maponline` command | `core/management/commands/purge_maponline.py` → `core/maponline_cleanup.py:71` | the same 30-day purge, when someone runs it |
| `branding_upload` | `core/views.py:8440` (`os.remove` at `:8462`) | the previous logo file under the **local** `MEDIA_ROOT/branding` only (never the bucket) |

### 1b. Choose staging storage (one of two)

- **(A) Its own empty bucket:** a new Firebase project or bucket used only by staging. Set
  `FIREBASE_STORAGE_BUCKET` to it and the `FIREBASE_*` credentials to that project's service account.
- **(B) No bucket:** leave `FIREBASE_STORAGE_BUCKET` and every `FIREBASE_*` variable **unset**. Mount a
  Railway volume on the web service and set `MEDIA_ROOT` to its path (for example `/data/media`).

**Never the production bucket or the production Firebase credentials.**

### 1c. Environment variables: never copy these from production

Create the staging services' variables from this list, not by cloning production.

| Variable | Reaches | Staging value |
|---|---|---|
| `DATABASE_URL` | the database | `$STAGING_URL` (the staging database only) |
| `SECRET_KEY` | sessions, password-reset tokens | a **new** staging-only value |
| `FIREBASE_STORAGE_BUCKET` | Firebase Storage | staging bucket (1b A) or **unset** (1b B) |
| `FIREBASE_TYPE`, `FIREBASE_PROJECT_ID`, `FIREBASE_PRIVATE_KEY_ID`, `FIREBASE_PRIVATE_KEY`, `FIREBASE_CLIENT_EMAIL`, `FIREBASE_CLIENT_ID`, `FIREBASE_AUTH_URI`, `FIREBASE_TOKEN_URI`, `FIREBASE_CLIENT_X509_CERT_URL` | Firebase (Google) | the staging project's service account (1b A) or **unset** (1b B) |
| `MEDIA_ROOT` | local disk | the staging volume path (1b B), otherwise unset |
| `GEMINI_API_KEY` | Google Gemini: PDF TC conversion and the Nova chat send file and chat content to Google | **unset**. This is the real lock for Nova chat |
| `GEMINI_TC_MODEL` | (model name only) | unset (default) |
| `ANTHROPIC_API_KEY` | Anthropic API (TC intake assistant) | **unset** |
| `ANTHROPIC_MODEL`, `AGENT_LLM_DAILY_TOKEN_CAP` | (intake assistant) | **unset** |
| `INTAKE_MAILBOX_TYPE`, `INTAKE_IMAP_HOST`, `INTAKE_IMAP_PORT`, `INTAKE_IMAP_USER`, `INTAKE_IMAP_PASSWORD`, `INTAKE_IMAP_AUTH`, `INTAKE_IMAP_OAUTH_TOKEN`, `INTAKE_GRAPH_TENANT_ID`, `INTAKE_GRAPH_CLIENT_ID`, `INTAKE_GRAPH_CLIENT_SECRET`, `INTAKE_GRAPH_MAILBOX`, `INTAKE_MAILBOX_FOLDER`, `INTAKE_FETCH_SINCE_DAYS` | the TC mailbox | **unset** (no cron-intake this week) |
| `SUPER_ADMIN_EMAIL`, `SUPER_ADMIN_PASSWORD`, `SUPER_ADMIN_NAME` | `ensure_superadmin` runs on **every** web deploy (Procfile) and creates or promotes this user to super_admin and resets the password | **one named staging tester** and a staging-only password; never a production person's password |
| `SUPER_ADMIN_EMAILS` | (read into settings; not used by the Django app today) | **unset** |
| `ALLOWED_HOSTS` | which host names the app answers | **only the staging domain** (step 1d) |
| `CSRF_TRUSTED_ORIGINS` | form posts | `https://<staging domain>` only |
| `ALLOWED_EMAIL_DOMAIN` | user creation | same as production (harmless) |
| `DEBUG` | error pages | `False` |
| `AGENT_DISPOSABLE_DB` | — | **never** on a service (step 5) |

Email and WhatsApp credentials are **not** environment variables. They are SystemSetting rows in the
restored database and are switched off in step 3.

### 1d. Access: only named testers

1. **A separate domain.** Give the staging web service its own domain (for example
   `ad-monitoring-staging.up.railway.app`). Set `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` to it only.
   Never add the production domain.
2. **Only named testers can log in.** After the restore (step 2) and before starting the web service,
   a person runs this one-off SQL. It deactivates every restored user except the named testers and
   drops all restored sessions:

```sql
-- Staging only. Replace the emails with the named testers.
BEGIN;
UPDATE accounts_user SET is_active = false
 WHERE lower(email) NOT IN ('tester1@company.lk', 'tester2@company.lk',
                            'reconciliation-agent@agent.invalid');   -- the agent's service user
DELETE FROM django_session;
SELECT email, role, is_active FROM accounts_user WHERE is_active ORDER BY email;
COMMIT;
```

   The agent's service user (`reconciliation-agent@agent.invalid`) stays active; it cannot log in (no
   usable password). Email is off on staging (step 3), so password-reset mails do not arrive: set each
   tester's password with a one-off `python manage.py changepassword tester1@company.lk`. The
   `SUPER_ADMIN_EMAIL` user is created or re-activated on every web deploy (1c), so make that one of the
   named testers.

## 2. Restore the production backup into staging PostgreSQL

**Do not start any staging service yet** (web, cron-agent, cron-audit, cron-intake). They must stay
stopped until step 3 is done.

```bash
# empty the staging database, then restore
psql "$STAGING_URL" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
pg_restore --no-owner --no-privileges -d "$STAGING_URL" production_YYYYMMDD.dump
```

## 3. Switch off every outbound message, before any service starts

Core can reach the outside world in these ways. All of them are set **in the restored staging
database or in the staging service variables**, never in production.

### 3a. SystemSetting keys (in the database)

| Key | Used by | Set to | Why that value is safe |
|---|---|---|---|
| `whatsapp_enabled` | `core/whatsapp.py::_get_config` (`send_template`, `send_text`), `core/views.py::whatsapp_test` (:11311), missed-spot and reconcile-done alerts (`core/views.py:10147`, `:10212`), TC reminders (`:11421`), replies to incoming messages (`:11516`) | `0` | every WhatsApp send checks it first and returns without sending |
| `whatsapp_access_token` | same | `` (empty) | second lock: without a token nothing can be sent, even if someone switches WhatsApp back on |
| `whatsapp_phone_number_id` | same | `` (empty) | same |
| `whatsapp_test_number` | `core/whatsapp.py::_resolve_to` | `` (empty) | leave empty; it only redirects messages, it does not stop them |
| `email_enabled` | `core/views.py::_notify_missed_spots_email` (:10239) and the agent digest (`agent/digest.py`) | `0` | core email returns at once; the digest is only logged (NotificationLog `via='log'`) |
| `email_host` | same | `` (empty) | second lock: no SMTP host |
| `email_host_password` | same | `` (empty) | second lock: no SMTP login |
| `nova_enabled` | Nova chat widget (`core/context_processors.py::branding` shows or hides it) | `0` | hides the chat button only. **It is not the lock:** `chat_with_nova` (`core/agent_chat.py:328-365`) does not check it. The real lock is `GEMINI_API_KEY` unset (step 1c) |

Other WhatsApp keys (`whatsapp_app_base_url`, `whatsapp_company_name`, `whatsapp_webhook_verify_token`)
do not send anything and can stay as they are.

**Either** a person sets them on **System Settings** (`/dashboard/settings/`, super admin; start
only the staging web service for this, with no cron services, and with the variables exactly as step 1 lists them), **or**
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
  ('nova_enabled',             '0', 'Nova chat enabled',        'staging: off', 'assistant', now())
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
a **staging-only** SMTP account on System Settings, **after** step 4 has limited the recipients. Core's
missed-spot emails would then also go out. They go to the users who can see each account, so either
switch email back off or accept that the restored users will receive them.

### 3b. Environment variables

All environment variables are set in step 1c, before any service starts. Check them again now:
the web service you start for step 4 must have exactly those values.

## 4. Digest recipients: named staging testers only

The restored database contains every production admin. The digest must reach only the named testers.

1. Start only the staging **web** service (no cron services yet).
2. Make sure each tester has an active **admin** or **super admin** user on staging. The digest never
   goes to other roles.
3. **Agent Settings** (`/dashboard/agent/config/`) → **Digest recipients**: select exactly the named
   testers and save. The change is logged in Activity (a human action).

Why this matters: when the list is empty, the digest goes to **every** active super admin and admin.

## 5. Migrations, service user, golden check, audit

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

## 6. Create the two cron services

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

## 7. Agent Settings

On `/dashboard/agent/config/` (admin):

| Setting | Value |
|---|---|
| Enabled | **on** |
| Autonomy level | **0** |
| Intake fetch enabled | **off** |
| TC intake mode | **off** |
| Intake Gemini enabled | **off** |
| Digest recipients | the named testers (step 4) |
| Shadow window | 01:00–05:00, budget 1800 s (defaults) |

Save once; the change is logged in Activity. The sidebar card (once patch 0007 is applied) then
shows **Running · level 0**. The first cycle observes all scopes at the next 15-minute tick. An admin
can press **Run cycle** to mark a run as wanted; it still runs at the next tick, not in the browser.

## 8. Daily checklist for the staging week

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
| 13 | No outbound message left staging | nothing received by clients, channels or production users | ask the testers; `SELECT key, value FROM core_systemsetting WHERE key IN (…)` from step 3a still shows everything off |

At the end of the week, send back: the Agent Overview screenshots, the last nightly audit report,
the digests, and your notes on checks 3, 8 and 9.

## 9. Stop

To stop the agent at any time: **Pause** on the sidebar card, or Agent Settings → Enabled off. Both
are logged. Deleting the `cron-agent` service also stops it. Nothing the agent did needs undoing:
at level 0 it never changed core data.
