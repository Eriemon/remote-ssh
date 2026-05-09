#!/usr/bin/env sh
set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TOOL="$SCRIPT_DIR/../../remote_ssh.py"
PYTHON_CMD=${PYTHON_CMD:-}
if [ -z "$PYTHON_CMD" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD=python3
  else
    PYTHON_CMD=python
  fi
fi

"$PYTHON_CMD" "$TOOL" configure --interactive "$@"
