#!/usr/bin/env bash
set -euo pipefail

HUB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "$HUB_DIR/bin/hub.py" sync
python3 "$HUB_DIR/bin/hub.py" build
