#!/usr/bin/env bash
# Audit Google Takeout → dashboard HTML via serveur local (évite page vide en file://)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAKEOUT="${1:-/home/bill/Images/Reseau social export/Googlefinito/Takeout}"
PORT="${TAKEOUT_AUDIT_PORT:-8765}"

python3 "$SCRIPT_DIR/google-takeout-audit.py" "$TAKEOUT" "${@:2}"

OUT="$TAKEOUT/audit-dashboard.html"
if [[ ! -f "$OUT" ]]; then
  echo "Dashboard introuvable: $OUT" >&2
  exit 1
fi

# Tuer un ancien serveur sur le même port si c'est le nôtre
if command -v fuser >/dev/null 2>&1; then
  fuser -k "${PORT}/tcp" 2>/dev/null || true
fi

cd "$TAKEOUT"
python3 -m http.server "$PORT" >/dev/null 2>&1 &
SERVER_PID=$!
sleep 0.5

URL="http://127.0.0.1:${PORT}/audit-dashboard.html"
echo "→ Serveur local: $URL (PID $SERVER_PID)"
xdg-open "$URL" >/dev/null 2>&1 || true
echo "→ Ctrl+C pour arrêter le serveur, ou: kill $SERVER_PID"