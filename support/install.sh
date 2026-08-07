#!/bin/sh
set -u

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
STATUS=${1:?missing status file}
sh "$ROOT/support/system_setup.sh"
RESULT=$?
printf '%s\n' "$RESULT" > "$STATUS"
exit "$RESULT"
