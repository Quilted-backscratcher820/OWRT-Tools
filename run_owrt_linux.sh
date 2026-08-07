#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
exec sh "$ROOT/support/run_gui.sh" "$@"
