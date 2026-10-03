#!/usr/bin/env bash
# Off-Mac safety net for unpushed work: one git bundle with every local branch
# and tag, written to ~/khayrkhoh-backups/ with the date in its name, then
# verified with `git bundle verify` and by listing its branch heads.
#
#   scripts/backup_repo.sh              # → ~/khayrkhoh-backups/volunteers-2026-10-03-1512.bundle
#   BACKUP_DIR=/Volumes/USB scripts/backup_repo.sh
#
# Restore anywhere: `git clone ~/khayrkhoh-backups/volunteers-<ts>.bundle volunteers`
# (or `git fetch <bundle> 'refs/heads/*:refs/remotes/bundle/*'` in an existing clone).
# Only committed work is included — the script warns about uncommitted changes.
# Copy the bundle off this Mac (USB / cloud drive) to make it a real backup.
set -euo pipefail

cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
DIR="${BACKUP_DIR:-$HOME/khayrkhoh-backups}"
TS="$(date +%Y-%m-%d-%H%M)"
OUT="$DIR/volunteers-$TS.bundle"
mkdir -p "$DIR"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "WARNING: uncommitted changes are NOT in the bundle:"
  git status --short | head -20
fi

git bundle create "$OUT" --branches --tags
git bundle verify "$OUT" >/dev/null
echo "== verified: $OUT ($(du -h "$OUT" | cut -f1))"

# Every local branch tip must be in the bundle at the same commit.
missing=0
while read -r sha ref; do
  if ! git bundle list-heads "$OUT" | grep -q "^$sha $ref\$"; then
    echo "MISSING in bundle: $ref $sha"; missing=1
  fi
done < <(git for-each-ref --format='%(objectname) %(refname)' refs/heads)
[[ $missing -eq 0 ]] || { echo "FAIL: bundle incomplete"; exit 1; }

git bundle list-heads "$OUT" | grep refs/heads | while read -r sha ref; do
  printf '   %-32s %s\n' "${ref#refs/heads/}" "${sha:0:7}"
done
echo "OK: $(git bundle list-heads "$OUT" | grep -c refs/heads) branch(es) in the bundle."
