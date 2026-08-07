#!/bin/sh
set -u

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
DEPENDENCIES="$ROOT/support/dependencies.txt"
RUNTIME_REQUIREMENTS="$ROOT/support/requirements.txt"
CHECKER="$ROOT/support/check_requirements.py"

if [ ! -f "$DEPENDENCIES" ]; then
	echo "未找到依赖列表：$DEPENDENCIES" >&2
	exit 1
fi
if ! command -v apt-get >/dev/null 2>&1; then
	echo "未找到 apt-get，本工具只支持 Debian/Ubuntu/WSL 的自动安装。" >&2
	exit 1
fi

if [ "$(id -u)" -ne 0 ] && ! command -v sudo >/dev/null 2>&1; then
	echo "安装依赖需要管理员权限，且未找到 sudo。" >&2
	exit 1
fi

run_as_root() {
	if [ "$(id -u)" -eq 0 ]; then
		"$@"
	else
		sudo "$@"
	fi
}

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

package_installed() {
	package=$1
	[ "$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)" = "install ok installed" ]
}

resolve_package() {
	entry=$1
	old_ifs=$IFS
	IFS='|'
	# shellcheck disable=SC2086
	set -- $entry
	IFS=$old_ifs
	for package do
		if package_installed "$package"; then
			printf '%s\n' "$package"
			return 0
		fi
	done
	for package do
		if apt-cache show "$package" >/dev/null 2>&1; then
			printf '%s\n' "$package"
			return 0
		fi
	done
	return 1
}

set --
while IFS= read -r entry; do
	package=$(resolve_package "$entry" || true)
	if [ -z "$package" ]; then
		echo "依赖没有可用的软件包候选：$entry" >&2
		exit 1
	fi
	set -- "$@" "$package"
done <<EOF
$(tr -d '\r' < "$DEPENDENCIES" | awk '!/^[[:space:]]*#/ {for (i = 1; i <= NF; i++) print $i}')
EOF
if [ "$#" -eq 0 ]; then
	echo "依赖列表为空。" >&2
	exit 1
fi

# First-install policy: refresh, fully upgrade, install the integrated list, then clean.
status=0
if run_as_root apt-get update -y && run_as_root apt-get full-upgrade -y; then
	run_as_root apt-get install -y "$@" || status=1
	run_as_root apt-get autoremove --purge -y || status=1
	run_as_root apt-get autoclean -y || status=1
	run_as_root apt-get clean -y || status=1
else
	status=1
fi

PYTHON=$(find_system_python || true)
if [ -z "$PYTHON" ]; then
	echo "系统依赖安装完成后仍未找到 python3。" >&2
	status=1
elif ! "$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
	echo "Python 版本过低，需要 Python 3.10 或更高版本。" >&2
	status=1
elif ! "$PYTHON" -c 'import PySide6' >/dev/null 2>&1; then
	echo "安装 PySide6 到系统 Python..."
	if "$PYTHON" -m pip install --help 2>/dev/null | grep -q -- '--break-system-packages'; then
		run_as_root env PIP_ROOT_USER_ACTION=ignore "$PYTHON" -m pip install --break-system-packages -r "$RUNTIME_REQUIREMENTS" || status=1
	else
		run_as_root env PIP_ROOT_USER_ACTION=ignore "$PYTHON" -m pip install -r "$RUNTIME_REQUIREMENTS" || status=1
	fi
fi
if [ -n "$PYTHON" ] && ! "$PYTHON" "$CHECKER"; then
	status=1
fi
if ! sh "$ROOT/support/restore_permissions.sh"; then
	status=1
fi
if [ "$status" -eq 0 ]; then
	echo "依赖安装和运行时检查完成。"
else
	echo "依赖安装或运行时检查未通过。" >&2
fi
exit "$status"
