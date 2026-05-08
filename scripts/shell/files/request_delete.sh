#!/usr/bin/env sh
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -z "${PYTHON_CMD:-}" ]; then
  if command -v python3 >/dev/null 2>&1; then PYTHON_CMD=python3; else PYTHON_CMD=python; fi
fi
exec "$PYTHON_CMD" "$SCRIPT_DIR/../../remote_ssh.py" request-delete "$@"
