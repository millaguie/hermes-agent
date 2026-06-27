#!/usr/bin/env bash
# Carry the postgres-kanban fork forward to a new upstream Hermes release.
#
# The fork's whole design goal is to be cheap to rebase: ~2000 lines live in
# our own new files (hermes_cli/kanban_store_*.py + tests) that NEVER conflict,
# and the footprint inside upstream files is 3 additive touches (the backend
# chokepoint in hermes_cli/kanban_db.py, plus pyproject.toml / Dockerfile).
# git rerere (enabled below) memorises how you resolved each conflict so the
# next bump replays it automatically.
#
# Usage:
#   ./update-fork.sh                # rebase onto the latest upstream release tag
#   ./update-fork.sh v2026.7.3      # rebase onto a specific upstream tag/ref
#
# Requires: a clean working tree, `upstream` remote, `uv`. Set
# HERMES_TEST_POSTGRES_DSN to also run the postgres contract + stress gate.
set -euo pipefail

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"

git config rerere.enabled true
git config rerere.autoupdate true

if [[ -n "$(git status --porcelain)" ]]; then
  echo "ERROR: working tree not clean. Commit or stash first." >&2
  exit 1
fi

echo "==> fetching $UPSTREAM_REMOTE"
git fetch --tags "$UPSTREAM_REMOTE"

# Resolve the target release. Default: newest vYYYY.M.D tag on upstream.
if [[ $# -ge 1 ]]; then
  NEW_BASE="$1"
else
  NEW_BASE="$(git tag --list 'v2026.*' --sort=-creatordate | head -1)"
fi
if [[ -z "$NEW_BASE" ]]; then
  echo "ERROR: could not resolve a target upstream ref." >&2
  exit 1
fi

# Our current base = the common ancestor of our branch and the new release
# (our branch is old-base + our commits; the new release descends from old-base
# on upstream's line, so merge-base gives exactly the old base).
OLD_BASE="$(git merge-base HEAD "$NEW_BASE")"
NEW_BASE_SHA="$(git rev-parse "${NEW_BASE}^{commit}")"

if [[ "$OLD_BASE" == "$NEW_BASE_SHA" ]]; then
  echo "Already based on $NEW_BASE. Nothing to do."
  exit 0
fi

echo "==> rebasing $BRANCH"
echo "    old base: $OLD_BASE"
echo "    new base: $NEW_BASE ($NEW_BASE_SHA)"
BACKUP="${BRANCH}-backup-$(git rev-parse --short HEAD)"
git branch -f "$BACKUP"
echo "    backup ref: $BACKUP"

# Auto-resolve uv.lock by regeneration rather than hand-merging the hash blob.
if ! git rebase --onto "$NEW_BASE_SHA" "$OLD_BASE"; then
  while true; do
    conflicts="$(git diff --name-only --diff-filter=U || true)"
    if [[ "$conflicts" == "uv.lock" ]]; then
      echo "==> auto-resolving uv.lock (regenerate)"
      git checkout --theirs uv.lock 2>/dev/null || true
      uv lock
      git add uv.lock
      git rebase --continue || true
      [[ -d .git/rebase-merge || -d .git/rebase-apply ]] || break
      continue
    fi
    echo "" >&2
    echo "Manual conflict resolution needed in:" >&2
    echo "$conflicts" | sed 's/^/  /' >&2
    echo "Resolve, 'git add', then 'git rebase --continue'. Re-run this script's" >&2
    echo "validation block afterwards, or 'git rebase --abort' to bail (backup: $BACKUP)." >&2
    exit 2
  done
fi

echo "==> validation gate"
python -m py_compile hermes_cli/kanban_db.py hermes_cli/kanban_store_*.py
uv sync --extra dev --extra postgres >/dev/null
uv run pytest tests/hermes_cli/test_kanban_store_*.py tests/hermes_cli/test_kanban_db.py -q

if [[ -n "${HERMES_TEST_POSTGRES_DSN:-}" ]]; then
  echo "==> postgres contract + (optional) stress harness"
  uv run pytest tests/hermes_cli/test_kanban_store_postgres_local.py -q
  if [[ -f scripts/stress_pg.py ]]; then
    uv run python scripts/stress_pg.py
  fi
else
  echo "    (HERMES_TEST_POSTGRES_DSN unset -> skipped postgres-live gate)"
fi

echo ""
echo "OK. $BRANCH rebased onto $NEW_BASE. Review, then:"
echo "    git push --force-with-lease origin $BRANCH"
echo "    git branch -D $BACKUP   # once you're happy"
