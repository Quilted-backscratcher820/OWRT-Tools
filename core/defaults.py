"""Directly apply GUI network defaults to an OpenWrt source tree."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile

from .validation import split_platform


class SourceDefaultsError(ValueError):
    """A supported source location for a requested default is unavailable."""


@dataclass(frozen=True)
class SourceDefaultsResult:
    """Source files used for direct default-setting changes."""

    config_generate: Path
    wireless_files: tuple[Path, ...]
    luci_files: tuple[Path, ...]


@dataclass(frozen=True)
class _PendingChange:
    path: Path
    original: str
    updated: str
    mode: int
    label: str


_DEFAULT_IP = re.compile(
    r"(?P<prefix>\$\{ipaddr:-[\"'])(?:\d{1,3}\.){3}\d{1,3}(?P<suffix>[\"']\})"
)
_DEFAULT_HOSTNAME = re.compile(r"(?P<prefix>\.hostname=)'[^'\n]*'")
_BASE_SSID = re.compile(r"(?m)(?P<prefix>^\s*BASE_SSID=)'[^\n]*'")
_BASE_WORD = re.compile(r"(?m)(?P<prefix>^\s*BASE_WORD=)'[^\n]*'")
_UCODE_SSID = re.compile(r"(?m)(?P<prefix>\.ssid=)'[^\n]*'")
_UCODE_KEY = re.compile(r"(?m)(?P<prefix>\.key=)'[^\n]*'")
_LUCI_RECONNECT_IP = re.compile(
    r"(?P<prefix>ui\.awaitReconnect\([^;\n]*?['\"])(?:\d{1,3}\.){3}\d{1,3}"
    r"(?P<suffix>['\"])"
)


def _atomic_write(path: Path, content: str, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SourceDefaultsError(f"无法读取{label}：{path}: {exc}") from exc


def _queue_change(
    changes: list[_PendingChange],
    path: Path,
    original: str,
    updated: str,
    label: str,
) -> None:
    if updated != original:
        try:
            mode = path.stat().st_mode & 0o777
        except OSError as exc:
            raise SourceDefaultsError(f"无法读取{label}权限：{path}: {exc}") from exc
        changes.append(_PendingChange(path, original, updated, mode, label))


def _commit_changes(changes: list[_PendingChange]) -> None:
    applied: list[_PendingChange] = []
    try:
        for change in changes:
            _atomic_write(change.path, change.updated, change.mode)
            applied.append(change)
    except OSError as exc:
        rollback_errors: list[str] = []
        for change in reversed(applied):
            try:
                _atomic_write(change.path, change.original, change.mode)
            except OSError as rollback_exc:
                rollback_errors.append(f"{change.path}: {rollback_exc}")
        detail = f"；回滚失败：{'；'.join(rollback_errors)}" if rollback_errors else ""
        label = changes[len(applied)].label if len(applied) < len(changes) else "默认设置"
        path = changes[len(applied)].path if len(applied) < len(changes) else "未知文件"
        raise SourceDefaultsError(f"无法写入{label}：{path}: {exc}{detail}") from exc


def _shell_single_quoted(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _ucode_single_quoted(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _replace_required(
    content: str,
    pattern: re.Pattern[str],
    replacement: Callable[[re.Match[str]], str],
    label: str,
    path: Path,
) -> str:
    updated, count = pattern.subn(replacement, content)
    if count == 0:
        raise SourceDefaultsError(f"{label}修改位置不存在：{path}")
    return updated


def _apply_config_generate(
    project_root: Path,
    hostname: str,
    ip_address: str,
    changes: list[_PendingChange],
) -> Path:
    path = project_root / "package" / "base-files" / "files" / "bin" / "config_generate"
    if not path.is_file():
        raise SourceDefaultsError(f"默认网络配置文件不存在：{path}")
    original = _read(path, "默认网络配置")
    updated = _replace_required(
        original,
        _DEFAULT_IP,
        lambda match: f"{match.group('prefix')}{ip_address}{match.group('suffix')}",
        "LAN IP",
        path,
    )
    updated = _replace_required(
        updated,
        _DEFAULT_HOSTNAME,
        lambda match: f"{match.group('prefix')}'{hostname}'",
        "主机名",
        path,
    )
    _queue_change(changes, path, original, updated, "默认网络配置")
    return path


def _wireless_candidates(project_root: Path, platform: str) -> tuple[Path, ...]:
    target, subtarget = split_platform(platform)
    target_root = project_root / "target" / "linux" / target
    roots = [target_root]
    if subtarget:
        roots.insert(0, target_root / subtarget)
    candidates: list[Path] = []
    for root in roots:
        if root.is_dir():
            candidates.extend(root.rglob("*set-wireless.sh"))
    return tuple(sorted(dict.fromkeys(path for path in candidates if path.is_file())))


def _apply_target_wireless(
    paths: tuple[Path, ...],
    wifi_ssid: str,
    wifi_password: str,
    changes: list[_PendingChange],
) -> tuple[Path, ...]:
    modified: list[Path] = []
    for path in paths:
        original = _read(path, "平台 WiFi 默认配置")
        if not _BASE_SSID.search(original) and not _BASE_WORD.search(original):
            continue
        updated = _replace_required(
            original,
            _BASE_SSID,
            lambda match: f"{match.group('prefix')}{_shell_single_quoted(wifi_ssid)}",
            "WiFi 账号",
            path,
        )
        updated = _replace_required(
            updated,
            _BASE_WORD,
            lambda match: f"{match.group('prefix')}{_shell_single_quoted(wifi_password)}",
            "WiFi 密码",
            path,
        )
        _queue_change(changes, path, original, updated, "平台 WiFi 默认配置")
        modified.append(path)
    return tuple(modified)


def _apply_generic_wireless(
    project_root: Path,
    wifi_ssid: str,
    wifi_password: str,
    changes: list[_PendingChange],
) -> tuple[Path, ...]:
    path = (
        project_root
        / "package"
        / "network"
        / "config"
        / "wifi-scripts"
        / "files"
        / "lib"
        / "wifi"
        / "mac80211.uc"
    )
    if not path.is_file():
        raise SourceDefaultsError(f"WiFi 默认配置文件不存在：{path}")
    original = _read(path, "通用 WiFi 默认配置")
    updated = _replace_required(
        original,
        _UCODE_SSID,
        lambda match: f"{match.group('prefix')}{_ucode_single_quoted(wifi_ssid)}",
        "WiFi 账号",
        path,
    )
    updated = _replace_required(
        updated,
        _UCODE_KEY,
        lambda match: f"{match.group('prefix')}{_ucode_single_quoted(wifi_password)}",
        "WiFi 密码",
        path,
    )
    _queue_change(changes, path, original, updated, "通用 WiFi 默认配置")
    return (path,)


def _apply_luci_reconnect(
    project_root: Path,
    ip_address: str,
    changes: list[_PendingChange],
) -> tuple[Path, ...]:
    root = project_root / "feeds" / "luci" / "modules" / "luci-mod-system"
    if not root.is_dir():
        return ()
    modified: list[Path] = []
    for path in sorted(root.rglob("flash.js")):
        original = _read(path, "LuCI 重连地址")
        updated = _LUCI_RECONNECT_IP.sub(
            lambda match: f"{match.group('prefix')}{ip_address}{match.group('suffix')}",
            original,
        )
        if updated != original:
            _queue_change(changes, path, original, updated, "LuCI 重连地址")
            modified.append(path)
    return tuple(modified)


def apply_source_defaults(
    project_root: Path,
    *,
    platform: str,
    hostname: str,
    ip_address: str,
    wifi_ssid: str,
    wifi_password: str,
) -> SourceDefaultsResult:
    """Apply defaults using the source locations used by OpenWRT-CI Settings.sh."""

    project_root = project_root.expanduser().resolve()
    changes: list[_PendingChange] = []
    config_generate = _apply_config_generate(project_root, hostname, ip_address, changes)
    wireless = _apply_target_wireless(
        _wireless_candidates(project_root, platform),
        wifi_ssid,
        wifi_password,
        changes,
    )
    if not wireless:
        wireless = _apply_generic_wireless(
            project_root, wifi_ssid, wifi_password, changes
        )
    luci_files = _apply_luci_reconnect(project_root, ip_address, changes)
    _commit_changes(changes)
    return SourceDefaultsResult(config_generate, wireless, luci_files)
