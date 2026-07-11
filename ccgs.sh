#!/usr/bin/env sh
set -eu

CCGS_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CCGS_CLI="$CCGS_ROOT/.ccgs-core/scripts/ccgs_cli.py"

if [ ! -f "$CCGS_CLI" ]; then
  echo "CCGS error: CLI not found at $CCGS_CLI." >&2
  exit 2
fi

if [ -n "${CCGS_PYTHON:-}" ]; then
  exec "$CCGS_PYTHON" "$CCGS_CLI" "$@"
fi

if command -v python3 >/dev/null 2>&1; then
  exec python3 "$CCGS_CLI" "$@"
fi

if command -v python >/dev/null 2>&1; then
  exec python "$CCGS_CLI" "$@"
fi

echo "CCGS error: Python 3.10+ was not found. Set CCGS_PYTHON to a Python executable." >&2
exit 2
