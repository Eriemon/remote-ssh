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

"$PYTHON_CMD" "$TOOL" discover "$@"
discover_rc=$?
if [ "$discover_rc" -eq 0 ]; then
  exit 0
fi
if [ "$discover_rc" -ne 3 ] && [ "$discover_rc" -ne 4 ]; then
  exit "$discover_rc"
fi

printf '\nNo enabled SSH server configuration was found.\n'
printf 'Add a server entry now? [Y/n]: '
IFS= read -r add_now || add_now=
case "$add_now" in
  n|N|no|NO|No)
    exit "$discover_rc"
    ;;
esac

"$PYTHON_CMD" "$TOOL" add-server --interactive "$@"
add_rc=$?
if [ "$add_rc" -ne 0 ]; then
  exit "$add_rc"
fi

printf '\n'
"$PYTHON_CMD" "$TOOL" discover "$@"
