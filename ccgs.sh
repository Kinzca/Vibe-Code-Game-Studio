#!/usr/bin/env sh
set -eu

CCGS_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CCGS_CLI="$CCGS_ROOT/.ccgs-core/scripts/ccgs_cli.py"

if [ ! -f "$CCGS_CLI" ]; then
  echo "VIBE_LAUNCHER_ERROR CLI_NOT_FOUND" >&2
  exit 2
fi

vibe_python_is_supported() {
  "$@" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    >/dev/null 2>&1
}

if [ -n "${CCGS_PYTHON:-}" ]; then
  if vibe_python_is_supported "$CCGS_PYTHON"; then
    exec "$CCGS_PYTHON" "$CCGS_CLI" "$@"
  fi
  echo "VIBE_LAUNCHER_ERROR PYTHON_NOT_FOUND" >&2
  exit 2
fi

if command -v python3 >/dev/null 2>&1 && vibe_python_is_supported python3; then
  exec python3 "$CCGS_CLI" "$@"
fi

if command -v python >/dev/null 2>&1 && vibe_python_is_supported python; then
  exec python "$CCGS_CLI" "$@"
fi

echo "VIBE_LAUNCHER_ERROR PYTHON_NOT_FOUND" >&2
exit 2
