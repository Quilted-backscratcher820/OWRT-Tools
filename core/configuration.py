"""Import OpenWrt configuration files and persist restorable build settings."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
from typing import Any

from .models import BuildSpec, PluginSpec, PrebuiltPackageSpec, ScriptSpec, new_build_id
from .validation import ValidationError, platform_key, validate_build_spec


BUILD_SETTINGS_FILE = "build-settings.json"
BUILD_SETTINGS_FORMAT = "openwrt-build-settings"
BUILD_SETTINGS_VERSION = 1
MAX_CONFIG_SIZE = 16 * 1024 * 1024

_ASSIGNMENT = re.compile(r'^CONFIG_[A-Za-z0-9_-]+=(?:y|m|n|"[^"\n]*"|[^\s]+)$')
_NOT_SET = re.compile(r"^# CONFIG_[A-Za-z0-9_-]+ is not set$")
_ENABLED_TARGET = re.compile(r"^CONFIG_TARGET_([A-Za-z0-9_]+)=y$")
_TARGET_BOARD = re.compile(r'^CONFIG_TARGET_BOARD="([A-Za-z0-9._-]+)"$')
_TARGET_SUBTARGET = re.compile(r'^CONFIG_TARGET_SUBTARGET="([A-Za-z0-9._-]+)"$')
_DEVICE_TARGET = re.compile(
    r"^CONFIG_TARGET_DEVICE_([A-Za-z0-9_]+)_DEVICE_([A-Za-z0-9._-]+)=y$"
)
_BUILTIN_LINES = {
    "CONFIG_PACKAGE_luci=y",
    "CONFIG_LUCI_LANG_zh_Hans=y",
    "CONFIG_PACKAGE_builder-settings=y",
    "CONFIG_PACKAGE_builder-prebuilt=y",
}
_NON_PLATFORM_TARGETS = {
    "ALL_PROFILES",
    "MULTI_PROFILE",
    "PER_DEVICE_ROOTFS",
}


class ConfigurationError(ValueError):
    """The selected configuration cannot be imported safely."""


@dataclass(frozen=True)
class ImportedBuildConfig:
    """Fields recovered from a normal .config and optional tool metadata."""

    platform: str
    devices: tuple[str, ...]
    extra_config: str
    hostname: str | None = None
    ip_address: str | None = None
    wifi_ssid: str | None = None
    wifi_password: str | None = None
    plugins: tuple[PluginSpec, ...] | None = None
    prebuilt_packages: tuple[PrebuiltPackageSpec, ...] | None = None
    custom_script: ScriptSpec | None = None
    build_id: str | None = None
    backup_enabled: bool | None = None
    backup_retention: int | None = None
    backup_directory: Path | None = None
    metadata_path: Path | None = None


def serialize_build_settings(spec: BuildSpec) -> str:
    """Serialize all GUI-managed build fields for a later exact import."""

    validate_build_spec(spec)
    document = {
        "format": BUILD_SETTINGS_FORMAT,
        "version": BUILD_SETTINGS_VERSION,
        "platform": spec.platform,
        "devices": list(spec.devices),
        "hostname": spec.hostname,
        "ip_address": spec.ip_address,
        "wifi": {
            "ssid": spec.wifi_ssid,
            "password": spec.wifi_password,
        },
        "extra_config": spec.extra_config,
        "plugins": [
            {
                "repository": plugin.repository,
                "branch": plugin.branch,
                "package_names": list(plugin.package_names),
            }
            for plugin in spec.plugins
        ],
        "prebuilt_packages": [
            {"filename": package.filename, "sha256": package.sha256}
            for package in spec.prebuilt_packages
        ],
        "custom_script": (
            {"filename": spec.custom_script.filename, "sha256": spec.custom_script.sha256}
            if spec.custom_script
            else None
        ),
        "build_id": spec.build_id,
        "backup": {
            "enabled": spec.backup_enabled,
            "retention": spec.backup_retention,
            "directory": str(spec.backup_directory) if spec.backup_directory else None,
        },
    }
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label}必须是对象。")
    return value


def _require_string(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str):
        raise ConfigurationError(f"{label}必须是文本。")
    return value


def _require_strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError(f"{label}必须是文本列表。")
    return tuple(value)


def _read_metadata(path: Path) -> BuildSpec:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"无法读取工具配置元数据：{path}: {exc}") from exc
    root = _require_mapping(document, "工具配置")
    if root.get("format") != BUILD_SETTINGS_FORMAT or root.get("version") != BUILD_SETTINGS_VERSION:
        raise ConfigurationError("工具配置格式或版本不受支持。")
    wifi = _require_mapping(root.get("wifi"), "WiFi 配置")
    backup = _require_mapping(root.get("backup"), "备份配置")
    enabled = backup.get("enabled")
    retention = backup.get("retention")
    directory = backup.get("directory")
    if not isinstance(enabled, bool):
        raise ConfigurationError("备份启用状态必须是布尔值。")
    if not isinstance(retention, int) or isinstance(retention, bool):
        raise ConfigurationError("备份留存数必须是整数。")
    if directory is not None and not isinstance(directory, str):
        raise ConfigurationError("备份目录必须是文本或 null。")
    raw_plugins = root.get("plugins")
    if not isinstance(raw_plugins, list):
        raise ConfigurationError("插件配置必须是列表。")
    plugins: list[PluginSpec] = []
    for index, item in enumerate(raw_plugins, 1):
        plugin = _require_mapping(item, f"插件 {index}")
        plugins.append(
            PluginSpec(
                repository=_require_string(plugin, "repository", f"插件 {index} 项目地址"),
                branch=_require_string(plugin, "branch", f"插件 {index} 分支"),
                package_names=_require_strings(plugin.get("package_names"), f"插件 {index} 名称"),
            )
        )
    raw_prebuilt = root.get("prebuilt_packages", [])
    if not isinstance(raw_prebuilt, list):
        raise ConfigurationError("预编译软件包配置必须是列表。")
    prebuilt_packages: list[PrebuiltPackageSpec] = []
    for index, item in enumerate(raw_prebuilt, 1):
        package = _require_mapping(item, f"预编译软件包 {index}")
        prebuilt_packages.append(
            PrebuiltPackageSpec(
                filename=_require_string(package, "filename", f"预编译软件包 {index} 文件名"),
                sha256=_require_string(package, "sha256", f"预编译软件包 {index} SHA-256"),
            )
        )
    raw_script = root.get("custom_script")
    custom_script: ScriptSpec | None = None
    if raw_script is not None:
        script = _require_mapping(raw_script, "自定义脚本")
        custom_script = ScriptSpec(
            filename=_require_string(script, "filename", "自定义脚本文件名"),
            sha256=_require_string(script, "sha256", "自定义脚本 SHA-256"),
        )
    raw_build_id = root.get("build_id")
    if raw_build_id is not None and not isinstance(raw_build_id, str):
        raise ConfigurationError("编译标识必须是文本。")
    spec = BuildSpec(
        platform=_require_string(root, "platform", "平台"),
        devices=_require_strings(root.get("devices"), "设备"),
        hostname=_require_string(root, "hostname", "主机名"),
        ip_address=_require_string(root, "ip_address", "IP 地址"),
        wifi_ssid=_require_string(wifi, "ssid", "WiFi 账号"),
        wifi_password=_require_string(wifi, "password", "WiFi 密码"),
        extra_config=_require_string(root, "extra_config", "常规配置"),
        plugins=tuple(plugins),
        prebuilt_packages=tuple(prebuilt_packages),
        custom_script=custom_script,
        build_id=raw_build_id or new_build_id(),
        backup_enabled=enabled,
        backup_retention=retention,
        backup_directory=Path(directory).expanduser() if directory else None,
    )
    try:
        return validate_build_spec(spec)
    except ValidationError as exc:
        raise ConfigurationError(f"工具配置内容无效：{exc}") from exc


def _read_text(path: Path) -> str:
    try:
        size = path.stat().st_size
        if size > MAX_CONFIG_SIZE:
            raise ConfigurationError("配置文件超过 16 MiB，拒绝导入。")
        return path.read_text(encoding="utf-8")
    except ConfigurationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(f"无法读取配置文件：{path}: {exc}") from exc


def _parse_openwrt_config(text: str) -> tuple[str, tuple[str, ...], str]:
    settings = [
        line.strip()
        for line in text.splitlines()
        if _ASSIGNMENT.fullmatch(line.strip()) or _NOT_SET.fullmatch(line.strip())
    ]
    board = next(
        (match.group(1) for line in settings if (match := _TARGET_BOARD.fullmatch(line))),
        None,
    )
    subtarget = next(
        (match.group(1) for line in settings if (match := _TARGET_SUBTARGET.fullmatch(line))),
        None,
    )
    device_rows = [match.groups() for line in settings if (match := _DEVICE_TARGET.fullmatch(line))]
    if device_rows:
        prefixes = {prefix for prefix, _device in device_rows}
        if len(prefixes) != 1:
            raise ConfigurationError("配置中包含多个目标平台的设备选择。")
        platform = next(iter(prefixes))
        devices = tuple(dict.fromkeys(device for _prefix, device in device_rows))
    elif board:
        platform = f"{board}_{subtarget}" if subtarget else board
        devices = ()
    else:
        candidates = [
            match.group(1)
            for line in settings
            if (match := _ENABLED_TARGET.fullmatch(line))
            and not match.group(1).startswith(("DEVICE_", "PROFILE_"))
            and match.group(1) not in _NON_PLATFORM_TARGETS
        ]
        if not candidates:
            raise ConfigurationError("配置中未找到已启用的 OpenWrt 目标平台。")
        platform = max(candidates, key=len)
        devices = ()
    target_parts = platform.split("_", 1)
    managed_targets = {platform, target_parts[0]}
    extra: list[str] = []
    for line in settings:
        target = _ENABLED_TARGET.fullmatch(line)
        if line in _BUILTIN_LINES or line == "CONFIG_TARGET_MULTI_PROFILE=y":
            continue
        if _DEVICE_TARGET.fullmatch(line):
            continue
        if target and target.group(1) in managed_targets:
            continue
        if line.startswith("CONFIG_TARGET_PROFILE="):
            continue
        extra.append(line)
    return platform, devices, "\n".join(dict.fromkeys(extra)) + ("\n" if extra else "")


def _project_root_for_config(path: Path) -> Path | None:
    if path.name == ".config":
        return path.parent
    for parent in path.parents:
        if parent.name == ".builder":
            return parent.parent
    return None


def _read_generated_defaults(config_path: Path) -> dict[str, str]:
    project_root = _project_root_for_config(config_path)
    if project_root is None:
        return {}
    defaults = (
        project_root
        / "package"
        / "custom"
        / "settings"
        / "files"
        / "etc"
        / "uci-defaults"
        / "99-builder-settings"
    )
    if not defaults.is_file():
        return {}
    fields = {
        "system.@system[0].hostname": "hostname",
        "network.lan.ipaddr": "ip_address",
        "wireless.default_radio0.ssid": "wifi_ssid",
        "wireless.default_radio0.key": "wifi_password",
    }
    values: dict[str, str] = {}
    try:
        lines = defaults.read_text(encoding="utf-8").splitlines()
        for raw in lines:
            arguments = shlex.split(raw, comments=True)
            if arguments[:3] != ["uci", "-q", "set"] or len(arguments) != 4:
                continue
            key, separator, value = arguments[3].partition("=")
            if separator and key in fields:
                values[fields[key]] = value
    except (OSError, UnicodeError, ValueError):
        return {}
    return values


def _metadata_candidates(config_path: Path) -> tuple[Path, ...]:
    candidates = [config_path.with_name(BUILD_SETTINGS_FILE)]
    project_root = _project_root_for_config(config_path)
    if project_root is not None:
        candidates.append(project_root / ".builder" / BUILD_SETTINGS_FILE)
    return tuple(dict.fromkeys(candidates))


def load_build_config(path: Path) -> ImportedBuildConfig:
    """Load a normal .config or a tool-generated build-settings JSON file."""

    path = path.expanduser().resolve()
    if not path.is_file():
        raise ConfigurationError(f"配置文件不存在：{path}")
    if path.suffix.lower() == ".json":
        spec = _read_metadata(path)
        return ImportedBuildConfig(
            platform=spec.platform,
            devices=spec.devices,
            extra_config=spec.extra_config,
            hostname=spec.hostname,
            ip_address=spec.ip_address,
            wifi_ssid=spec.wifi_ssid,
            wifi_password=spec.wifi_password,
            plugins=spec.plugins,
            prebuilt_packages=spec.prebuilt_packages,
            custom_script=spec.custom_script,
            build_id=spec.build_id,
            backup_enabled=spec.backup_enabled,
            backup_retention=spec.backup_retention,
            backup_directory=spec.backup_directory,
            metadata_path=path,
        )
    platform, devices, extra_config = _parse_openwrt_config(_read_text(path))
    for metadata_path in _metadata_candidates(path):
        if not metadata_path.is_file():
            continue
        spec = _read_metadata(metadata_path)
        if platform_key(spec.platform) != platform_key(platform) or set(spec.devices) != set(devices):
            raise ConfigurationError(f"工具配置元数据与所选 .config 不匹配：{metadata_path}")
        return ImportedBuildConfig(
            platform=spec.platform,
            devices=spec.devices,
            extra_config=spec.extra_config,
            hostname=spec.hostname,
            ip_address=spec.ip_address,
            wifi_ssid=spec.wifi_ssid,
            wifi_password=spec.wifi_password,
            plugins=spec.plugins,
            prebuilt_packages=spec.prebuilt_packages,
            custom_script=spec.custom_script,
            build_id=spec.build_id,
            backup_enabled=spec.backup_enabled,
            backup_retention=spec.backup_retention,
            backup_directory=spec.backup_directory,
            metadata_path=metadata_path,
        )
    defaults = _read_generated_defaults(path)
    return ImportedBuildConfig(
        platform=platform,
        devices=devices,
        extra_config=extra_config,
        hostname=defaults.get("hostname"),
        ip_address=defaults.get("ip_address"),
        wifi_ssid=defaults.get("wifi_ssid"),
        wifi_password=defaults.get("wifi_password"),
    )


def load_complete_project_config(project_directory: Path) -> ImportedBuildConfig | None:
    """Load a project's complete .config, or return None when it is incomplete."""

    path = project_directory.expanduser().resolve() / ".config"
    if not path.is_file():
        return None
    try:
        imported = load_build_config(path)
    except ConfigurationError:
        return None
    if not imported.devices:
        return None
    return imported
