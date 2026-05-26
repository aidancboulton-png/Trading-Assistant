#!/usr/bin/env bash
# Summit Shine — migrate code from Trading-Assistant feature branch into
# the new dedicated Summit-Shine-Cleaning-CO repo.
#
# Usage (one-liner from any terminal with git installed + GitHub auth):
#   curl -sSL https://raw.githubusercontent.com/aidancboulton-png/Trading-Assistant/claude/free-job-tracking-app-dYgpy/scripts/summit-shine-migrate.sh | bash

set -euo pipefail

SRC_REPO="https://github.com/aidancboulton-png/Trading-Assistant.git"
SRC_BRANCH="claude/free-job-tracking-app-dYgpy"
DST_REPO="https://github.com/aidancboulton-png/Summit-Shine-Cleaning-CO.git"

echo "→ Cloning Summit Shine source from Trading-Assistant ($SRC_BRANCH)..."
WORK=$(mktemp -d)
cd "$WORK"
git clone --quiet --depth 1 --branch "$SRC_BRANCH" "$SRC_REPO" src

echo "→ Staging Summit Shine for the new repo..."
mkdir new
cp -r src/summit_shine new/
cp src/render.yaml new/

cd new
git init --quiet
git add .
git -c commit.gpgsign=false commit --quiet -m "Initial commit — Summit Shine marketing site + job tracker"
git branch -M main
git remote add origin "$DST_REPO"

echo "→ Pushing to $DST_REPO ..."
git push -u origin main

echo ""
echo "✅ Done. Your new repo is populated."
echo ""
echo "Next: deploy on Render (one click) at:"
echo "   https://render.com/deploy?repo=https://github.com/aidancboulton-png/Summit-Shine-Cleaning-CO"
