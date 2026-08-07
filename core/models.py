"""Data exchanged by the UI and the build workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def new_build_id(now: datetime | None = None) -> str:
    """Create the immutable identifier embedded into one build."""

    return f"OWRT-Tools-{(now or datetime.now()).strftime('%Y%m%d-%H%M%S')}"


@dataclass(frozen=True)
class ProjectSpec:
    """A checked-out OpenWrt source tree."""

    name: str
    repository: str
    branch: str
    directory: Path


@dataclass(frozen=True)
class PluginSpec:
    """One package (or package directory) selected from a Git repository."""

    repository: str
    branch: str
    package_names: tuple[str, ...]


@dataclass(frozen=True)
class PrebuiltPackageSpec:
    """A locally staged IPK or APK to embed through the generated package."""

    filename: str
    sha256: str


@dataclass(frozen=True)
class ScriptSpec:
    """A converted and syntax-checked script staged below one project."""

    filename: str
    sha256: str


@dataclass(frozen=True)
class BuildSpec:
    """All source-level settings required for a build."""

    platform: str
    devices: tuple[str, ...]
    hostname: str
    ip_address: str
    wifi_ssid: str
    wifi_password: str
    extra_config: str = ""
    plugins: tuple[PluginSpec, ...] = field(default_factory=tuple)
    prebuilt_packages: tuple[PrebuiltPackageSpec, ...] = field(default_factory=tuple)
    custom_script: ScriptSpec | None = None
    build_id: str = field(default_factory=new_build_id)
    build_directory: Path | None = None
    backup_enabled: bool = True
    backup_retention: int = 3
    backup_directory: Path | None = None


@dataclass(frozen=True)
class EnvironmentCheck:
    """A single prerequisite result for display and gating."""

    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class EnvironmentReport:
    """The full environment gate report."""

    checks: tuple[EnvironmentCheck, ...]

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)
