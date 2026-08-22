#!/usr/bin/env bash
set -euo pipefail

HUB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

sync_status=0
python3 "$HUB_DIR/bin/hub.py" sync || sync_status=$?

build_status=0
if (( sync_status == 0 )); then
  python3 "$HUB_DIR/bin/hub.py" build || build_status=$?
else
  LLM_OPS_HUB_SYNC_FAILED=1 python3 "$HUB_DIR/bin/hub.py" build || build_status=$?
fi

if (( build_status != 0 )); then
  exit "$build_status"
fi
exit "$sync_status"
