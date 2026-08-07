"""WSL distribution configuration checks for OpenWrt builds."""

from __future__ import annotations

from configparser import ConfigParser, Error as ConfigParserError
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


WSL_CONF_PATH = Path("/etc/wsl.conf")
RESTART_HINT = "请在 Windows CMD 或 PowerShell 中运行 wsl --shutdown，然后重新启动 WSL。"
_WINDOWS_PATH = re.compile(r"^/mnt/[A-Za-z](?:/|$)")
_SECTION = re.compile(r"^\s*\[([^]]+)\]\s*$")
_OPTION = re.compile(r"^(\s*)appendWindowsPath\s*=.*$", re.IGNORECASE)


@dataclass(frozen=True)
class WslPathStatus:
    """The persistent WSL PATH setting and the current-session state."""

    ok: bool
    detail: str
    changed: bool = False
    requires_restart: bool = False


def running_in_wsl() -> bool:
    if os.environ.get("WSL_INTEROP") or os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        release = Path("/proc/version").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "microsoft" in release.lower() or "microsoft-standard" in release.lower()


def windows_path_entries(path_value: str | None = None) -> tuple[str, ...]:
    value = os.environ.get("PATH", "") if path_value is None else path_value
    return tuple(entry for entry in value.split(os.pathsep) if _WINDOWS_PATH.match(entry))


def sanitize_build_path(
    path_value: str | None = None,
    *,
    wsl: bool | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Remove inherited Windows PATH entries before running a build command.

    ``/etc/wsl.conf`` prevents new Windows PATH entries after a WSL restart,
    but an already-running session can still contain them.  Build recipes may
    interpolate PATH without quoting, so removing those entries is a second,
    process-local safety net.  Broken PATH conversions can leave fragments
    containing shell metacharacters; those fragments are removed as well.
    """

    value = os.environ.get("PATH", "") if path_value is None else path_value
    if not (running_in_wsl() if wsl is None else wsl):
        return value, ()
    entries = value.split(os.pathsep)
    removed = tuple(
        entry
        for entry in entries
        if _WINDOWS_PATH.match(entry)
        or any(character in entry for character in "()\t\r\n")
    )
    if not removed:
        return value, ()
    kept = tuple(entry for entry in entries if entry not in removed)
    return os.pathsep.join(kept), removed


def _append_windows_path_value(content: str) -> str | None:
    parser = ConfigParser(interpolation=None, strict=False)
    parser.optionxform = lambda optionstr: optionstr.lower()
    try:
        parser.read_string(content or "")
    except ConfigParserError as exc:
        raise ValueError(f"/etc/wsl.conf 格式无法解析：{exc}") from exc
    section_name = next(
        (name for name in parser.sections() if name.strip().lower() == "interop"),
        None,
    )
    if section_name is None:
        return None
    value = parser[section_name].get("appendwindowspath")
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError("/etc/wsl.conf 的 appendWindowsPath 必须是 true 或 false。")
    return normalized


def _disable_append_windows_path(content: str) -> str:
    lines = content.splitlines()
    interop_start: int | None = None
    interop_end = len(lines)
    for index, line in enumerate(lines):
        match = _SECTION.match(line)
        if not match:
            continue
        if match.group(1).strip().lower() == "interop":
            interop_start = index
            continue
        if interop_start is not None:
            interop_end = index
            break
    if interop_start is None:
        suffix = "" if not content or content.endswith("\n") else "\n"
        return f"{content}{suffix}[interop]\nappendWindowsPath=false\n"
    for index in range(interop_start + 1, interop_end):
        match = _OPTION.match(lines[index])
        if match:
            lines[index] = f"{match.group(1)}appendWindowsPath=false"
            return "\n".join(lines) + "\n"
    lines.insert(interop_end, "appendWindowsPath=false")
    return "\n".join(lines) + "\n"


def _write_config(path: Path, content: str) -> None:
    """Write wsl.conf directly when possible, otherwise through sudo install."""

    temporary: Path | None = None
    try:
        # /etc/wsl.conf is a distribution-level system file. Always use sudo
        # for the real WSL path so the startup behavior is explicit and auditable.
        if path != WSL_CONF_PATH and os.access(path.parent, os.W_OK):
            descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            os.close(descriptor)
            temporary = Path(name)
            temporary.write_text(content, encoding="utf-8")
            temporary.chmod(0o644)
            temporary.replace(path)
            return
        sudo = shutil.which("sudo")
        if sudo is None:
            raise OSError("当前用户无权写入 /etc/wsl.conf，且未找到 sudo。")
        descriptor, name = tempfile.mkstemp(prefix="owrt-tools-wsl.")
        os.close(descriptor)
        temporary = Path(name)
        temporary.write_text(content, encoding="utf-8")
        try:
            result = subprocess.run(
                [sudo, "install", "-m", "644", str(temporary), str(path)],
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            raise OSError("sudo install 超时。") from exc
        if result.returncode != 0:
            raise OSError(f"sudo install 返回退出码 {result.returncode}。")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def inspect_wsl_path(
    config_path: Path = WSL_CONF_PATH,
    *,
    path_value: str | None = None,
    wsl: bool | None = None,
) -> WslPathStatus:
    """Ensure appendWindowsPath=false and report whether WSL must be restarted."""

    if not (running_in_wsl() if wsl is None else wsl):
        return WslPathStatus(True, "当前不是 WSL 环境，无需修改 WSL PATH。")
    try:
        content = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
        value = _append_windows_path_value(content)
    except (OSError, ValueError) as exc:
        return WslPathStatus(False, str(exc))
    mixed = windows_path_entries(path_value)
    if value == "false":
        if mixed:
            joined = "、".join(mixed[:3])
            suffix = " 等" if len(mixed) > 3 else ""
            return WslPathStatus(
                False,
                f"appendWindowsPath=false 已写入，但当前 PATH 仍包含 Windows 路径：{joined}{suffix}；{RESTART_HINT}",
                requires_restart=True,
            )
        return WslPathStatus(True, "appendWindowsPath=false 已生效，当前 PATH 未混入 Windows 路径。")
    try:
        updated = _disable_append_windows_path(content)
        _write_config(config_path, updated)
    except (OSError, ValueError) as exc:
        return WslPathStatus(
            False,
            f"无法自动写入 {config_path}：{exc}。请手动加入 [interop] appendWindowsPath=false。",
        )
    return WslPathStatus(
        False,
        f"已将 appendWindowsPath=false 写入 {config_path}；{RESTART_HINT}",
        changed=True,
        requires_restart=True,
    )
