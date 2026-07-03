#!/usr/bin/env bash
set -euo pipefail

ROOT="${WINPATH_HUB_ROOT:-$HOME/winpath-hub}"
MIRROR="$ROOT/mirror.git"
REMOTE="git@github.com:pioootrek/win-path-6.git"

mkdir -p "$ROOT/cache" "$ROOT/logs" "$ROOT/public_releases"

if [[ ! -d "$MIRROR" ]]; then
  git clone --mirror "$REMOTE" "$MIRROR"
else
  git --git-dir="$MIRROR" fetch --prune origin '+refs/heads/*:refs/heads/*'
fi

python3 "$ROOT/bin/build_hub.py"
