#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
CHECKER="$ROOT/support/check_requirements.py"
SETUP="$ROOT/support/system_setup.sh"

find_system_python() {
	candidate=$(command -v python3 2>/dev/null || true)
	if [ -n "$candidate" ] && "$candidate" -c \
		'import sys; raise SystemExit(sys.prefix != sys.base_prefix)' >/dev/null 2>&1; then
		printf '%s\n' "$candidate"
		return 0
	fi
	if [ -x /usr/bin/python3 ]; then
		printf '%s\n' /usr/bin/python3
		return 0
	fi
	return 1
}

PYTHON=$(find_system_python || true)

if [ -z "$PYTHON" ] || ! "$PYTHON" "$CHECKER" >/dev/null 2>&1; then
	printf '%s\n' "首次运行：检查并安装 support/dependencies.txt 中的编译依赖。"
	sh "$SETUP"
fi

PYTHON=$(find_system_python || true)
if [ -z "$PYTHON" ] || ! "$PYTHON" "$CHECKER"; then
	printf '%s\n' "依赖安装完成后检查仍未通过。" >&2
	exit 1
fi
cd "$ROOT"
exec "$PYTHON" -m core "$@"
