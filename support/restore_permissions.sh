#!/bin/sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
MANIFEST="$ROOT/support/permissions.txt"

if [ ! -f "$MANIFEST" ]; then
	printf '%s\n' "未找到 Linux 权限清单：$MANIFEST" >&2
	exit 1
fi

restored=0
while IFS=' ' read -r mode relative; do
	case "$mode" in
	''|'#'*) continue ;;
	[0-7][0-7][0-7]) ;;
	*) printf '%s\n' "权限模式无效：$mode" >&2; exit 1 ;;
	esac
	case "$relative" in
	''|/*|../*|*/../*|*/..) printf '%s\n' "权限路径不安全：$relative" >&2; exit 1 ;;
	esac
	target=$ROOT/$relative
	if [ -e "$target" ]; then
		chmod "$mode" "$target"
		restored=$((restored + 1))
	fi
done < "$MANIFEST"

printf '已恢复 %s 个 Linux 启动文件权限。\n' "$restored"
