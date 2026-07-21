# WORKFLOW.md — Branching, Testing & Deploy Runbook

This is the **operating manual** for how code moves from an idea to the live
Ad-Monitoring app. Read it before creating a branch or deploying. The goal is a
clean, production-ready repo that is easy to update and impossible to get lost in.

> Companion doc: `CLAUDE.md` describes *what the code does*. This file describes
> *how changes are made, tested, and shipped safely* — especially important
> because production holds **real client (Phoenix O & M) data**.

---

## 1. The three branches (and only three that live forever)

| Branch | Role | Railway environment | Who deploys to it |
|--------|------|---------------------|-------------------|
| `main` | **Production.** Exactly what live users see. | **Production** (real DB) | Auto-deploy on every push/merge to `main` |
| `staging` | **Test bench.** Where features are proven before going live. | **Staging** (separate throwaway DB) | Auto-deploy on every push/merge to `staging` |
| `feature/<name>` | **Short-lived.** One feature or fix. Deleted right after merge. | none | — |

**Rule of thumb:** if a branch is not `main`, `staging`, or an *active* `feature/*`
you are working on **right now**, it should not exist. No more branch graveyard.

---

## 2. The golden flow (every change follows this)

```
        create                merge PR              test on              merge PR
feature/x ─────► push ─────► into staging ─────► staging URL ─────► into main ─────► LIVE
   │                                                                                  │
   └──────────────────────── delete feature/x immediately after ─────────────────────┘
```

Commands:

```bash
# 1. Start from the latest production baseline
git checkout main
git pull origin main

# 2. Branch for ONE piece of work
git checkout -b feature/short-descriptive-name

# 3. Do the work (with Claude Code or by hand), commit, push
git push -u origin feature/short-descriptive-name

# 4. Open a PR: feature/x -> staging.  Merge it.
#    Railway auto-deploys the Staging environment.

# 5. TEST on the staging URL. Upload a real-shaped file, run a reconciliation,
#    click through the summary sheet. Confirm migrations applied cleanly.

# 6. Only when staging is proven good, open a PR: staging -> main. Merge it.
#    Railway auto-deploys Production. It is now live.

# 7. Delete the feature branch — locally and on GitHub.
git branch -d feature/short-descriptive-name
git push origin --delete feature/short-descriptive-name
```

> **Never push straight to `main`.** Production is protected (see §6). Every change
> reaches `main` through `staging` first. This is the single habit that keeps the
> repo clean and the client's data safe.

---

## 3. Railway environments

This project runs **two** Railway environments in one project:

| Environment | Tracks branch | Database | Notes |
|-------------|---------------|----------|-------|
| **Production** | `main` | Real production Postgres | The live app. Do not test here. |
| **Staging** | `staging` | **Separate** Postgres | Safe to break. Reset freely. |

**Staging must have its own environment variables** — never share the production
values. At minimum, staging needs its **own**:

```
DATABASE_URL            # separate DB — this is what protects real client data
SECRET_KEY              # different from prod
ALLOWED_HOSTS           # the staging URL
CSRF_TRUSTED_ORIGINS    # the staging URL
SUPER_ADMIN_EMAILS      # blank or a test address (so staging can't touch real admins)
GEMINI_API_KEY          # blank on staging unless you are specifically testing PDF AI
```

Everything else (`DEBUG=False`, `TIME_ZONE=Asia/Colombo`, upload limits) mirrors
production so staging behaves like the real thing.

### Optional: realistic staging data
To test against realistic (not empty) data, load a **copy** of the production DB
into staging periodically. Never point staging at the production `DATABASE_URL`.

---

## 4. Migrations — the real risk, handle with care

Every deploy runs, via `Procfile` / `railway.json`:

```
python manage.py migrate --no-input
python manage.py collectstatic --no-input --clear
python manage.py ensure_superadmin
gunicorn ...
```

That means **merging to `main` auto-applies migrations to the real client
database.** So:

- **Always test the migration on `staging` first.** A bad migration is the most
  likely way to damage production.
- Commit migration files with the code change that needs them (see `CLAUDE.md` §17).
- If a migration is destructive or renames/removes columns, note it in the PR and
  double-check it ran cleanly on staging before promoting to `main`.

---

## 5. Archived history (nothing was thrown away)

The repo previously accumulated 30+ branches across two unrelated histories. During
cleanup, every deleted branch was preserved as an **annotated tag** under the
`archive/` namespace before deletion, so no work is ever lost.

```bash
git tag -l "archive/*"                 # list everything archived
git checkout archive/<name>            # inspect an old branch's code
git checkout -b recover/<name> archive/<name>   # resurrect it as a branch if ever needed
```

Notable archives:
- `archive/legacy-main-*` — the original abandoned app lineage (pre-current-system).
- `archive/finalized-system-*`, `archive/fixed-summary-*` — dated milestone snapshots.

---

## 6. Guardrails (set once, protects forever)

1. **Branch protection on `main`** (GitHub → Settings → Branches → Add rule):
   - Require a pull request before merging.
   - Do not allow direct pushes / force pushes.
   This makes the `staging → main` flow mandatory, not just a convention.
2. **Consistent feature names:** `feature/<verb-noun>` e.g. `feature/fix-tc-timezone`,
   `feature/add-radio-channels`. Avoid random codenames.
3. **Delete on merge:** turn on GitHub's "Automatically delete head branches"
   (Settings → General) so merged feature branches vanish by themselves.

---

## 7. Quick reference

| I want to… | Do this |
|------------|---------|
| Start new work | `git checkout main && git pull && git checkout -b feature/x` |
| Test it safely | PR `feature/x → staging`, merge, open staging URL |
| Ship it live | PR `staging → main`, merge (auto-deploys production) |
| Clean up | delete `feature/x` on GitHub + locally |
| Find old code | `git tag -l "archive/*"` then checkout the tag |
| Undo a bad deploy | revert the merge commit on `main`, push (auto-redeploys) |

Keep it boring. Boring is production-ready.
