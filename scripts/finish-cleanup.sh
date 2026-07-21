#!/usr/bin/env bash
#
# finish-cleanup.sh — run this ONCE from your own machine (not the web/CI session)
# to (1) create permanent archive tags for the legacy branches and (2) delete them.
#
# Tag pushes are blocked in the Claude web environment, so this last step is manual.
# Safe to run: it tags every branch BEFORE deleting it — nothing is lost.
#
# PREREQUISITE: confirm Railway's Production environment is deploying `main`
# and the app is healthy BEFORE running this (see WORKFLOW.md §3).

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
git fetch origin --prune

# Branches to archive + delete. (main, staging, and any active feature/* are NOT here.)
BRANCHES=(
  "Finalized-System---2026.05.01" "Fixed-Summary-card-errors-2026.5.13"
  "claude/add-ad-validation-w6N0W" "claude/add-lmrb-tc-matching-4JnFh"
  "claude/add-special-notes-section-iGKy5" "claude/align-maponline-columns-AkgAK"
  "claude/brand-theme-schedule-matching-MFG60" "claude/document-database-setup-NVPcs"
  "claude/filter-schedules-by-account-czMme" "claude/fix-ad-verification-issues-nCw2L"
  "claude/fix-lmrb-upload-monitoring-JkS6i" "claude/fix-pending-verification-status-ELngP"
  "claude/fix-reports-dashboard-eQU8y" "claude/intelligent-volta-Fy6TU"
  "claude/missed-spots-verification-plan-67KTO" "claude/multi-sheet-schedule-upload-TVvUh"
  "claude/optimistic-ride-kced67" "claude/parse-maponline-ads-IDhIn"
  "claude/pensive-brown-qrcxmw" "claude/program-mismatch-dashboard-hxJ0W"
  "claude/reconcile-tc-lmrb-data-ZmWvE" "claude/reconciliation-bonus-reports-TeA6p"
  "claude/review-code-update-docs-DZS7F" "claude/tc-upload-summary-table-6Vp2r"
  "claude/whatsapp-production-mode-1D9Gf" "modernize-dashboard-charts-V1"
  "redesign-brand-mapping-filters--V3-2026.03.28" "schedule-verification-auto-stop-V2"
)

# The branch that is CURRENTLY live production until you repoint Railway to main.
# Delete it only AFTER Railway Production tracks `main`. Uncomment to include:
# BRANCHES+=("claude/tc-converter-upload-issue-i8ns30")

echo ">> Creating archive tags..."
for b in "${BRANCHES[@]}"; do
  if git show-ref --verify --quiet "refs/remotes/origin/$b"; then
    tag="archive/${b//\//-}"
    git tag -a "$tag" "origin/$b" -m "Archived branch $b" 2>/dev/null || echo "   (tag $tag exists)"
  fi
done
git push origin --tags
echo ">> Tags pushed."

echo ">> Deleting legacy branches..."
for b in "${BRANCHES[@]}"; do
  git push origin --delete "$b" 2>/dev/null && echo "   deleted $b" || echo "   (already gone: $b)"
done

echo ">> Done. Remaining branches:"
git branch -r
