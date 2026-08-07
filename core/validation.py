"""Input validation and deterministic OpenWrt configuration generation."""

from __future__ import annotations

from collections.abc import Iterable
from ipaddress import IPv4Address
from pathlib import Path
import re
from urllib.parse import urlparse

from .models import BuildSpec, PluginSpec, PrebuiltPackageSpec, ScriptSpec


class ValidationError(ValueError):
    """The user supplied an unsafe or incomplete build setting."""


_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_CONFIG = re.compile(
    r'^(?:CONFIG_[A-Za-z0-9_-]+=(?:y|m|n|"[^"\n]*"|[^\s]+)|'
    r"# CONFIG_[A-Za-z0-9_-]+ is not set)$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BUILD_ID = re.compile(r"^OWRT-Tools-\d{8}-\d{6}$")
_PREBUILT_SUFFIXES = frozenset({".apk", ".ipk"})
FORCED_CONFIG_FILE = "forced_config.txt"


def require_component(value: str, label: str) -> str:
    value = value.strip()
    if not _COMPONENT.fullmatch(value):
        raise ValidationError(f"{label}只能包含字母、数字、点、下划线和连字符。")
    return value


def require_branch(value: str, label: str = "分支名") -> str:
    value = value.strip()
    if not _BRANCH.fullmatch(value) or ".." in value or value.startswith("-"):
        raise ValidationError(f"{label}格式不安全。")
    return value


def require_repository(value: str, label: str = "项目地址") -> str:
    value = value.strip()
    parsed = urlparse(value)
    is_ssh = re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9._/-]+(?:\.git)?", value)
    if parsed.scheme not in {"https", "http", "ssh", "git"} and not is_ssh:
        raise ValidationError(f"{label}必须是 HTTPS、SSH 或 Git 地址。")
    if any(character.isspace() for character in value) or "\x00" in value:
        raise ValidationError(f"{label}不能包含空白字符或空字节。")
    if parsed.scheme and (not parsed.netloc or parsed.username or parsed.password):
        raise ValidationError(f"{label}必须是不含凭据的仓库地址。")
    return value


def source_name(repository: str) -> str:
    """Derive a stable local name from a normal Git URL."""

    cleaned = repository.rstrip("/")
    name = cleaned.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return require_component(name, "项目名")


def split_platform(value: str) -> tuple[str, str | None]:
    """Accept target/subtarget or target_subtarget notation."""

    text = value.strip().replace("-", "_")
    if not text:
        raise ValidationError("平台不能为空。")
    if "/" in text:
        parts = text.split("/")
        if len(parts) != 2:
            raise ValidationError("平台应为 target/subtarget 或 target_subtarget。")
        target, subtarget = parts
    elif "_" in text:
        target, subtarget = text.split("_", 1)
    else:
        target, subtarget = text, None
    target = require_component(target, "目标平台").replace("-", "_")
    if subtarget:
        subtarget = require_component(subtarget, "子平台").replace("-", "_")
    return target, subtarget


def platform_key(value: str) -> str:
    target, subtarget = split_platform(value)
    return f"{target}_{subtarget}" if subtarget else target


def validate_build_spec(spec: BuildSpec) -> BuildSpec:
    target, subtarget = split_platform(spec.platform)
    del target, subtarget
    if not spec.devices:
        raise ValidationError("至少输入一个设备名称。")
    devices = tuple(require_component(device, "设备名") for device in spec.devices)
    if len(set(devices)) != len(devices):
        raise ValidationError("设备名称不能重复。")
    require_component(spec.hostname, "主机名")
    validate_build_id(spec.build_id)
    try:
        address = IPv4Address(spec.ip_address.strip())
    except ValueError as exc:
        raise ValidationError("IP 地址必须是有效 IPv4 地址。") from exc
    if address.is_multicast or address.is_unspecified or address.is_loopback:
        raise ValidationError("IP 地址不能是组播、未指定或回环地址。")
    if not spec.wifi_ssid.strip() or "\n" in spec.wifi_ssid or "\x00" in spec.wifi_ssid:
        raise ValidationError("WiFi 名称不能为空，且不能包含换行或空字节。")
    if not spec.wifi_password.strip() or len(spec.wifi_password) < 8 or "\n" in spec.wifi_password or "\x00" in spec.wifi_password:
        raise ValidationError("WiFi 密码至少需要 8 个字符，且不能包含换行或空字节。")
    if not 1 <= spec.backup_retention <= 100:
        raise ValidationError("固件备份留存数必须是 1-100 的整数。")
    plugin_package_names: set[str] = set()
    for plugin in spec.plugins:
        validate_plugin_spec(plugin)
        for package_name in plugin.package_names:
            if package_name in plugin_package_names:
                raise ValidationError(f"插件名不能跨项目重复：{package_name}")
            plugin_package_names.add(package_name)
    package_names: set[str] = set()
    package_digests: set[str] = set()
    for package in spec.prebuilt_packages:
        validate_prebuilt_package_spec(package)
        if package.filename in package_names or package.sha256 in package_digests:
            raise ValidationError("预编译软件包不能重复。")
        package_names.add(package.filename)
        package_digests.add(package.sha256)
    if spec.custom_script is not None:
        validate_script_spec(spec.custom_script)
    validate_extra_config(spec.extra_config)
    return spec


def validate_build_id(value: str) -> str:
    if not _BUILD_ID.fullmatch(value):
        raise ValidationError("编译标识必须为 OWRT-Tools-时间戳。")
    return value


def validate_plugin_spec(spec: PluginSpec) -> PluginSpec:
    require_repository(spec.repository, "插件项目地址")
    require_branch(spec.branch, "插件分支")
    if not spec.package_names:
        raise ValidationError("至少输入一个插件名。")
    names = tuple(require_component(name, "插件名") for name in spec.package_names)
    if len(set(names)) != len(names):
        raise ValidationError("同一插件项目中的插件名不能重复。")
    return spec


def validate_prebuilt_package_spec(spec: PrebuiltPackageSpec) -> PrebuiltPackageSpec:
    """Validate metadata for a package stored below .builder/prebuilt only."""

    filename = spec.filename
    if Path(filename).name != filename or not _COMPONENT.fullmatch(filename):
        raise ValidationError("预编译软件包文件名不安全。")
    if Path(filename).suffix not in _PREBUILT_SUFFIXES:
        raise ValidationError("预编译软件包只支持 .ipk 或 .apk 文件。")
    if not _SHA256.fullmatch(spec.sha256):
        raise ValidationError("预编译软件包 SHA-256 格式无效。")
    return spec


def validate_script_spec(spec: ScriptSpec) -> ScriptSpec:
    if Path(spec.filename).name != spec.filename or not _COMPONENT.fullmatch(spec.filename):
        raise ValidationError("自定义脚本文件名不安全。")
    if Path(spec.filename).suffix.lower() != ".sh":
        raise ValidationError("自定义脚本只支持 .sh 文件。")
    if not _SHA256.fullmatch(spec.sha256):
        raise ValidationError("自定义脚本 SHA-256 格式无效。")
    return spec


def validate_extra_config(value: str) -> None:
    for number, raw in enumerate(value.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("# "):
            continue
        if not _CONFIG.fullmatch(line):
            raise ValidationError(f"常规配置第 {number} 行不是有效的 OpenWrt CONFIG 行。")


def config_line_symbol(line: str) -> str | None:
    """Return the CONFIG symbol represented by one normalized Kconfig line."""

    line = line.strip()
    if not _CONFIG.fullmatch(line):
        return None
    if line.startswith("# CONFIG_") and line.endswith(" is not set"):
        return line[2 : -len(" is not set")]
    return line.split("=", 1)[0]


def _normalized_config_lines(lines: Iterable[str], label: str) -> tuple[str, ...]:
    normalized: dict[str, str] = {}
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or (line.startswith("#") and not line.startswith("# CONFIG_")):
            continue
        symbol = config_line_symbol(line)
        if symbol is None:
            raise ValidationError(f"{label}第 {number} 行不是有效的 OpenWrt CONFIG 行。")
        normalized.pop(symbol, None)
        normalized[symbol] = line
    return tuple(normalized.values())


def load_forced_config(path: Path) -> tuple[str, ...]:
    """Load the hidden, manually maintained settings mixed into every build."""

    try:
        content = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValidationError(f"无法读取强制配置清单：{path}: {exc}") from exc
    lines = _normalized_config_lines(content.splitlines(), "强制配置清单")
    if not lines:
        raise ValidationError(f"强制配置清单为空：{path}")
    return lines


def without_config_symbols(value: str, hidden_lines: Iterable[str]) -> str:
    """Remove tool-managed symbols from text before exposing it to the GUI."""

    hidden = {
        symbol
        for line in hidden_lines
        if (symbol := config_line_symbol(line)) is not None
    }
    if not hidden:
        return value
    return "".join(
        raw
        for raw in value.splitlines(keepends=True)
        if config_line_symbol(raw) not in hidden
    )


def apply_forced_config_text(value: str, forced_config: Iterable[str]) -> str:
    """Remove conflicting copies and append authoritative settings once."""

    forced_lines = _normalized_config_lines(forced_config, "强制配置")
    forced_symbols = {
        symbol
        for line in forced_lines
        if (symbol := config_line_symbol(line)) is not None
    }
    retained = [
        raw
        for raw in value.splitlines()
        if config_line_symbol(raw) not in forced_symbols
    ]
    while retained and not retained[-1]:
        retained.pop()
    return "\n".join((*retained, *forced_lines)) + "\n"


def _merge_config_lines(*groups: Iterable[str]) -> tuple[str, ...]:
    merged: dict[str, str] = {}
    passthrough: list[str] = []
    for group in groups:
        for line in group:
            symbol = config_line_symbol(line)
            if symbol is None:
                passthrough.append(line.strip())
                continue
            merged.pop(symbol, None)
            merged[symbol] = line.strip()
    return tuple((*passthrough, *merged.values()))


def config_symbols(spec: BuildSpec) -> tuple[str, ...]:
    """Return the selectors that must survive make defconfig."""

    target, subtarget = split_platform(spec.platform)
    symbols = [f"CONFIG_TARGET_{target}"]
    if subtarget:
        symbols.append(f"CONFIG_TARGET_{target}_{subtarget}")
    device_prefix = f"CONFIG_TARGET_DEVICE_{target}"
    if subtarget:
        device_prefix += f"_{subtarget}"
    symbols.extend(f"{device_prefix}_DEVICE_{device}" for device in spec.devices)
    return tuple(symbols)


def plugin_config_symbols(spec: BuildSpec) -> tuple[str, ...]:
    """Return package selectors for all resolved custom plugin packages."""

    return tuple(
        dict.fromkeys(
            f"CONFIG_PACKAGE_{package_name}"
            for plugin in spec.plugins
            for package_name in plugin.package_names
        )
    )


def prebuilt_config_symbols(spec: BuildSpec) -> tuple[str, ...]:
    """Return the generated wrapper selector when archives were staged."""

    return ("CONFIG_PACKAGE_builder-prebuilt",) if spec.prebuilt_packages else ()


def build_config_text(spec: BuildSpec, forced_config: Iterable[str] = ()) -> str:
    """Create an initial .config without executing any shell fragment."""

    forced_lines = _normalized_config_lines(forced_config, "强制配置")
    managed_names = {
        line.split("=", 1)[0]
        for line in (
            "CONFIG_PACKAGE_luci=y",
            "CONFIG_LUCI_LANG_zh_Hans=y",
            "CONFIG_PACKAGE_builder-settings=y",
            *(f"{symbol}=y" for symbol in prebuilt_config_symbols(spec)),
            *(f"{symbol}=y" for symbol in config_symbols(spec)),
            *(f"{symbol}=y" for symbol in plugin_config_symbols(spec)),
        )
    }
    managed_names.update(config_symbols(spec))
    managed_names.update(plugin_config_symbols(spec))
    managed_names.update(prebuilt_config_symbols(spec))
    lines: list[str] = []
    for raw in spec.extra_config.splitlines():
        line = raw.strip()
        if not line:
            continue
        name = config_line_symbol(line)
        if name not in managed_names:
            lines.append(line)
    lines.extend(
        (
            "CONFIG_PACKAGE_luci=y",
            "CONFIG_LUCI_LANG_zh_Hans=y",
            "CONFIG_PACKAGE_builder-settings=y",
        )
    )
    lines.extend(f"{symbol}=y" for symbol in config_symbols(spec))
    if len(spec.devices) > 1:
        lines.append("CONFIG_TARGET_MULTI_PROFILE=y")
    lines.extend(f"{symbol}=y" for symbol in prebuilt_config_symbols(spec))
    lines.extend(f"{symbol}=y" for symbol in plugin_config_symbols(spec))
    return "\n".join(_merge_config_lines(lines, forced_lines)) + "\n"


def validate_resolved_config(
    path: Path,
    spec: BuildSpec,
    forced_config: Iterable[str] = (),
) -> None:
    """Fail when Kconfig removed an explicitly requested target or device."""

    text = path.read_text(encoding="utf-8", errors="replace")
    missing = [symbol for symbol in config_symbols(spec) if f"{symbol}=y" not in text]
    if missing:
        joined = "、".join(missing)
        raise ValidationError(f"make defconfig 后未保留所选平台或设备：{joined}")
    if "CONFIG_PACKAGE_builder-settings=y" not in text:
        raise ValidationError("自定义默认设置包未进入最终配置。")
    missing_plugins = [
        symbol for symbol in plugin_config_symbols(spec) if f"{symbol}=y" not in text
    ]
    if missing_plugins:
        joined = "、".join(missing_plugins)
        raise ValidationError(f"make defconfig 后未保留自定义插件：{joined}")
    missing_prebuilt = [
        symbol for symbol in prebuilt_config_symbols(spec) if f"{symbol}=y" not in text
    ]
    if missing_prebuilt:
        raise ValidationError("make defconfig 后未保留预编译软件包集成包。")
    settings = {line.strip() for line in text.splitlines()}
    missing_forced: list[str] = []
    for line in _normalized_config_lines(forced_config, "强制配置"):
        symbol = config_line_symbol(line)
        assert symbol is not None
        disabled = {f"{symbol}=n", f"# {symbol} is not set"}
        if line.endswith("=n") or line.startswith("# CONFIG_"):
            present = bool(settings & disabled)
        else:
            present = line in settings
        if not present:
            missing_forced.append(symbol)
    if missing_forced:
        raise ValidationError(
            "make defconfig 后未保留强制配置：" + "、".join(missing_forced)
        )
