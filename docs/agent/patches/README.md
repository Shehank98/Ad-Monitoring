# Patches for a person to apply

Files under `.claude/` (the guardian checklist and `settings.json`) and protected core
files are never edited by the Claude session. Changes to them ship here as patch files.
A person reviews each patch, then applies and commits it under their own name:

```bash
git apply --check docs/agent/patches/<file>.diff   # dry run
git apply docs/agent/patches/<file>.diff
git commit -am "apply <file>"                       # your own author name
```

The guardian (check 1, once patch 0003 is applied) looks up the commit author of every
`.claude/**` change. A change authored by the Claude session (`Claude <noreply@anthropic.com>`)
is a FAIL. When it can't tell who the author is, it reports "needs human confirmation".

| Patch | Changes | Status |
|---|---|---|
| `0001_base_nav_rename.diff` | `templates/base.html`: nav group renamed to "Reconciliation Agent" | for you to apply |
| `0002_claude_settings_deny.diff` | `.claude/settings.json`: deny `Edit(./.claude/**)` and `Write(./.claude/**)` | for you to apply |
| `0003_guardian_check1_claude_paths.diff` | `.claude/agents/numbers-guardian.md` check 1: `.claude/**` only through a patch a person applied, checked by commit author | for you to apply |
| `0004_guardian_phase2.diff` | `.claude/agents/numbers-guardian.md`: new check 5 (actor kinds), checks 19–21 (intake LLM tools, cron writes, LLM data). Apply **after** 0003 | for you to apply |
| `0005_base_nav_inbox.diff` | `templates/base.html`: "TC Inbox" link in the Reconciliation Agent nav group (hidden from channel_officer with the whole group). Apply **after** 0001 | for you to apply |
