# ARCHIVE.md — Legacy branch record

The repo had accumulated 30+ branches across **two unrelated histories**. During the
2026-07 cleanup, the live production app (formerly the branch
`claude/tc-converter-upload-issue-i8ns30`) was promoted to `main`, and `staging`
was created from it. See `WORKFLOW.md` for the ongoing branch/deploy model.

Every branch below is recorded here by its **tip commit SHA** so the work is traceable
even after the branch is deleted. To make these permanently recoverable, also create
git **tags** from your local machine (tag pushes are blocked in the CI/web environment) —
see `scripts/finish-cleanup.sh`.

## Recovering archived code
```bash
git fetch origin
git checkout <tip-sha>                 # inspect
git checkout -b recover/<name> <sha>   # resurrect as a branch
# or, if the tags were created:
git checkout archive/<name>
```

## Archived branches

| Branch | Tip commit | Subject |
|--------|-----------|---------|
| main (old lineage) | `9b18c82` | Update login.py |
| Finalized-System---2026.05.01 | `59aef1a` | Fix 'too many values to unpack' errors from brand_theme_map 3-tuple migration |
| Fixed-Summary-card-errors-2026.5.13 | `cf6ee53` | Upgrade UI: TC upload page, Marketing Officer portal, PWA install banner |
| claude/add-ad-validation-w6N0W | `c00e6fc` | Fix LMRB theme quick-pick showing cross-channel themes |
| claude/add-lmrb-tc-matching-4JnFh | `6146c54` | Fix LMRB candidates to include already-matched rows; add schedule details |
| claude/add-special-notes-section-iGKy5 | `e81835d` | Skip zero-spot rows in colored schedule export |
| claude/align-maponline-columns-AkgAK | `6ffce34` | Fix client-side TC PDF parser in upload.html (same 3 bugs as Python) |
| claude/brand-theme-schedule-matching-MFG60 | `2454875` | Update login.py |
| claude/document-database-setup-NVPcs | `262faa2` | Redesign schedule list with month-card grid and channel accordion |
| claude/filter-schedules-by-account-czMme | `79b39af` | Fix empty Matched LMRB sheet + add Programme + Not Aired section to TC detail |
| claude/fix-ad-verification-issues-nCw2L | `af8018a` | Reorder TC reconciliation: TC↔LMRB first, then LMRB→Schedule |
| claude/fix-lmrb-upload-monitoring-JkS6i | `3062425` | Mobile responsive improvements across all major pages |
| claude/fix-pending-verification-status-ELngP | `d0bbc7c` | Fix: heal orphaned is_matched=True rows in smart mode to prevent permanent Pending status |
| claude/fix-reports-dashboard-eQU8y | `719bf97` | fix: resolve Excel account_id param, PDF 500 error, and add dashboard charts |
| claude/intelligent-volta-Fy6TU | `0066152` | Replace all em/en dashes with hyphens across UI and codebase |
| claude/missed-spots-verification-plan-67KTO | `e9e04c7` | Fix empty brand for ITN-style sponsorship rows in pivot schedule |
| claude/multi-sheet-schedule-upload-TVvUh | `0426b71` | Fix PWA start_url to use main dashboard instead of officer-only route |
| claude/optimistic-ride-kced67 | `99b13bf` | TC Upload: hide already-uploaded schedules + fix back-to-top scroll |
| claude/parse-maponline-ads-IDhIn | `c48d7cd` | Update login.py |
| claude/pensive-brown-qrcxmw | `b2991fa` | TC ↔ LMRB (no schedule): upload TC directly from the tab |
| claude/program-mismatch-dashboard-hxJ0W | `9992d92` | feat: Transmission Certificate (TC) upload, reconciliation & Summary Sheet |
| claude/reconcile-tc-lmrb-data-ZmWvE | `c8a1eae` | Fix tc_pdf_convert: use empty string for FileField instead of None |
| claude/reconciliation-bonus-reports-TeA6p | `e465495` | Fix 500 on TC detail view and correct summary 3rd Party / Extra columns |
| claude/review-code-update-docs-DZS7F | `71a0a16` | feat: premium PDF redesign for reconciliation summary report |
| claude/tc-upload-summary-table-6Vp2r | `c84f30b` | Feature 3 & 5: Mark as Aired action, two-way notes, and unread badges |
| claude/whatsapp-production-mode-1D9Gf | `2fca515` | Show WhatsApp failure reason after officer create; fix test UI |
| modernize-dashboard-charts-V1 | `f3dedea` | feat: auto-stop verification after schedule period ends, show Reconcile Now |
| redesign-brand-mapping-filters--V3-2026.03.28 | `0777af1` | Exclude sponsorship keywords from Extra Aired and improve link modal |
| schedule-verification-auto-stop-V2 | `3c3bdd8` | feat: update login page branding — Ogilvy Nova + automation team credit |

> `main (old lineage)` is the original abandoned app (Streamlit→Django rewrite +
> early brand-theme matching). It shares **no common history** with the current app
> and was replaced wholesale. Kept here only for reference.
