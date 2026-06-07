#!/usr/bin/env bash
# Audit Google Takeout → dashboard HTML local
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAKEOUT="${1:-/home/bill/Images/Reseau social export/Googlefinito/Takeout}"

python3 "$SCRIPT_DIR/google-takeout-audit.py" "$TAKEOUT" "${@:2}"
OUT="$TAKEOUT/audit-dashboard.html"
if [[ -f "$OUT" ]]; then
  xdg-open "$OUT" >/dev/null 2>&1 &
fi