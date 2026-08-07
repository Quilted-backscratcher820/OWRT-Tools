"""Side-effecting OpenWrt project, configuration and build operations."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tarfile
import tempfile
from typing import Any

from .configuration import BUILD_SETTINGS_FILE, serialize_build_settings
from .logs import timestamp_log_text
from .models import BuildSpec, PluginSpec, PrebuiltPackageSpec, ProjectSpec, ScriptSpec
from .packages import PrebuiltPackageError, stage_prebuilt_package, verify_staged_package
from .scripts import ScriptError, stage_build_script, verify_staged_script
from .validation import (
    FORCED_CONFIG_FILE,
    ValidationError,
    apply_forced_config_text,
    build_config_text,
    load_forced_config,
    platform_key,
    require_branch,
    require_component,
    require_repository,
    source_name,
    validate_build_spec,
    validate_resolved_config,
)


LogCallback = Callable[[str], None]
StepCallback = Callable[[str], None]
CancelCallback = Callable[[], bool]
FinalConfigCallback = Callable[[], None]


class WorkflowError(RuntimeError):
    """A command or checked workflow condition failed."""


class OperationCancelled(WorkflowError):
    """The user cancelled the currently running operation."""


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def current_date() -> str:
    return datetime.now().strftime("%Y%m%d")


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    """Write user generated text atomically and with a known mode."""

    path.parent.mkdir(parents=True, exist_ok=True)
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


class CommandRunner:
    """Run a command without a shell while streaming merged stdout/stderr."""

    def __init__(self, log: LogCallback, cancelled: CancelCallback) -> None:
        self.log = log
        self.cancelled = cancelled
        self._process: subprocess.Popen[str] | None = None

    def cancel(self) -> None:
        process = self._process
        if process and process.poll() is None:
            process.terminate()

    def run(self, arguments: Iterable[str], cwd: Path, env: dict[str, str] | None = None) -> None:
        command = tuple(str(item) for item in arguments)
        if self.cancelled():
            raise OperationCancelled("操作已取消。")
        self.log("$ " + " ".join(command))
        merged_environment = os.environ.copy()
        if env:
            merged_environment.update(env)
        try:
            self._process = subprocess.Popen(
                command,
                cwd=cwd,
                env=merged_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            raise WorkflowError(f"无法启动命令：{exc}") from exc

        process = self._process
        assert process.stdout is not None
        reader = selectors.DefaultSelector()
        reader.register(process.stdout, selectors.EVENT_READ)
        cancelled = False
        try:
            while process.poll() is None:
                if self.cancelled() and not cancelled:
                    cancelled = True
                    self.log("[取消] 正在终止当前命令。")
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                for key, _ in reader.select(timeout=0.25):
                    line = key.fileobj.readline()
                    if line:
                        self.log(line.rstrip("\n"))
            for line in process.stdout:
                self.log(line.rstrip("\n"))
        finally:
            reader.close()
            process.stdout.close()
            result = process.wait()
            self._process = None
        if cancelled:
            raise OperationCancelled("操作已取消。")
        if result:
            raise WorkflowError(f"命令退出代码为 {result}：{' '.join(command)}")


class Workflow:
    """A serial build workflow rooted in one dedicated working directory."""

    PROJECTS_DIR = "projects"
    LOGS_DIR = "logs"
    TOOLCHAINS_DIR = "toolchains"
    INTERNAL_DIR = ".builder"
    METADATA_FILE = "project.json"

    def __init__(
        self,
        root: Path,
        log: LogCallback | None = None,
        step: StepCallback | None = None,
        cancelled: CancelCallback | None = None,
        final_config_started: FinalConfigCallback | None = None,
        source_root: Path | None = None,
        toolchain_root: Path | None = None,
        log_root: Path | None = None,
    ) -> None:
        self.root = root.resolve()
        self.source_root = (source_root or (self.root / self.PROJECTS_DIR)).expanduser().resolve()
        self._toolchain_root = (
            toolchain_root or (self.root / self.TOOLCHAINS_DIR)
        ).expanduser().resolve()
        self._log_root = (log_root or (self.root / self.LOGS_DIR)).expanduser().resolve()
        self._external_log = log or (lambda message: None)
        self.step = step or (lambda value: None)
        self.cancelled = cancelled or (lambda: False)
        self.final_config_started = final_config_started or (lambda: None)
        self._log_file: Any | None = None
        self.runner = CommandRunner(self._write, self.cancelled)
        self.output_directory: Path | None = None
        self.backup_output_directory: Path | None = None

    @property
    def projects_root(self) -> Path:
        return self.source_root

    @property
    def toolchains_root(self) -> Path:
        return self._toolchain_root

    @property
    def logs_root(self) -> Path:
        return self._log_root

    def _write(self, message: str) -> None:
        stamped = timestamp_log_text(message, preserve_existing=False)
        self._external_log(stamped)
        if self._log_file:
            self._log_file.write(stamped + "\n")
            self._log_file.flush()

    @contextmanager
    def operation_log(self, platform: str) -> Iterable[Path]:
        safe_platform = re.sub(r"[^A-Za-z0-9._-]+", "_", platform) or "operation"
        logs_root = self.logs_root
        try:
            logs_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkflowError(f"无法创建日志目录：{logs_root}: {exc}") from exc
        stem = f"log-{safe_platform}-{timestamp()}"
        log_path = logs_root / f"{stem}.txt"
        suffix = 1
        while log_path.exists():
            log_path = logs_root / f"{stem}-{suffix}.txt"
            suffix += 1
        try:
            handle = log_path.open("a", encoding="utf-8", buffering=1)
        except OSError as exc:
            raise WorkflowError(f"无法创建日志文件：{log_path}: {exc}") from exc
        with handle:
            self._log_file = handle
            self._write(f"[日志] {log_path}")
            try:
                yield log_path
            finally:
                self._log_file = None

    def _assert_not_cancelled(self) -> None:
        if self.cancelled():
            raise OperationCancelled("操作已取消。")

    def _project_metadata_path(self, directory: Path) -> Path:
        return directory / self.INTERNAL_DIR / self.METADATA_FILE

    def _feeds_date_path(self, directory: Path) -> Path:
        return directory / self.INTERNAL_DIR / "feeds-updated-date"

    def _refresh_feeds(self, directory: Path, date_key: str) -> None:
        feeds = directory / "scripts" / "feeds"
        if not feeds.is_file():
            raise WorkflowError("项目不是带 scripts/feeds 的 OpenWrt 源码树。")
        self.step("更新 feeds")
        self.runner.run((str(feeds), "update", "-a"), directory)
        self._assert_not_cancelled()
        self.step("安装 feeds")
        self.runner.run((str(feeds), "install", "-a"), directory)
        atomic_write(self._feeds_date_path(directory), date_key + "\n")

    def _feeds_need_refresh(self, directory: Path, date_key: str) -> bool:
        try:
            return self._feeds_date_path(directory).read_text(encoding="ascii").strip() != date_key
        except OSError:
            return True

    def _forced_config(self) -> tuple[str, ...]:
        try:
            return load_forced_config(self.root / "support" / FORCED_CONFIG_FILE)
        except ValidationError as exc:
            raise WorkflowError(str(exc)) from exc

    def _apply_forced_config(self, directory: Path, forced_config: tuple[str, ...]) -> None:
        config = directory / ".config"
        try:
            content = config.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkflowError(f"无法读取待混入的 .config：{exc}") from exc
        atomic_write(config, apply_forced_config_text(content, forced_config))

    def _read_project(self, directory: Path) -> ProjectSpec:
        metadata_path = self._project_metadata_path(directory)
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            return ProjectSpec(
                name=require_component(str(metadata["name"]), "项目名"),
                repository=require_repository(str(metadata["repository"])),
                branch=require_branch(str(metadata["branch"])),
                directory=directory,
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorkflowError(f"项目元数据无效：{metadata_path}: {exc}") from exc

    def list_projects(self) -> list[ProjectSpec]:
        if not self.projects_root.is_dir():
            return []
        projects: list[ProjectSpec] = []
        for directory in sorted(self.projects_root.iterdir()):
            if directory.is_dir() and (directory / ".git").is_dir():
                try:
                    projects.append(self._read_project(directory))
                except WorkflowError:
                    continue
        return projects

    def get_project(self, name: str) -> ProjectSpec:
        name = require_component(name, "项目名")
        directory = self.projects_root / name
        if not directory.is_dir():
            raise WorkflowError(f"项目不存在：{name}")
        return self._read_project(directory)

    def clone_project(self, repository: str, branch: str, name: str = "") -> ProjectSpec:
        repository = require_repository(repository)
        branch = require_branch(branch)
        project_name = require_component(name, "项目名") if name.strip() else source_name(repository)
        directory = self.projects_root / project_name
        self.projects_root.mkdir(parents=True, exist_ok=True)
        if directory.exists():
            raise WorkflowError(f"项目目录已存在，不能覆盖：{directory}")
        try:
            with self.operation_log(project_name):
                self.step("浅克隆项目")
                self.runner.run(
                    ("git", "clone", "--depth", "1", "--single-branch", "--branch", branch, repository, str(directory)),
                    self.root,
                )
                self._assert_not_cancelled()
                if not (directory / "scripts" / "feeds").is_file():
                    raise WorkflowError("克隆结果不是带 scripts/feeds 的 OpenWrt 源码树。")
                self._refresh_feeds(directory, current_date())
                metadata = {"name": project_name, "repository": repository, "branch": branch}
                atomic_write(self._project_metadata_path(directory), json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
                self._write("[完成] 项目克隆和 feeds 更新完成。")
        except Exception:
            if directory.exists() and not self._project_metadata_path(directory).is_file():
                shutil.rmtree(directory)
            raise
        return ProjectSpec(project_name, repository, branch, directory)

    @staticmethod
    def _makefile_declares(path: Path, package_name: str) -> bool:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return bool(re.search(rf"^\s*PKG_NAME\s*:?=\s*{re.escape(package_name)}\s*$", content, re.MULTILINE))

    def _package_candidates(self, root: Path, package_name: str) -> list[Path]:
        matches: list[Path] = []
        for makefile in root.rglob("Makefile"):
            if ".git" in makefile.parts:
                continue
            parent = makefile.parent
            if parent.name == package_name or self._makefile_declares(makefile, package_name):
                matches.append(parent)
        return matches

    def _remove_duplicate_packages(self, project: ProjectSpec, package_name: str, keep: Path) -> None:
        keep = keep.resolve()
        project_root = project.directory.expanduser().resolve()
        roots = (project.directory / "package", project.directory / "feeds")
        duplicates: list[Path] = []
        for root in roots:
            if root.is_dir():
                duplicates.extend(self._package_candidates(root, package_name))
        safe_duplicates: set[Path] = set()
        for item in duplicates:
            candidate = item.resolve()
            if candidate.is_relative_to(project_root):
                safe_duplicates.add(candidate)
            else:
                self._write(f"[去重] 跳过项目目录之外的插件目录：{candidate}")
        for candidate in sorted(safe_duplicates, key=lambda item: len(item.parts), reverse=True):
            if candidate == keep or keep.is_relative_to(candidate):
                continue
            self._write(f"[去重] 删除同名插件目录：{candidate}")
            shutil.rmtree(candidate)

    def _unique_package_candidate(
        self,
        root: Path,
        package_name: str,
        *,
        required: bool,
    ) -> Path | None:
        candidates = self._package_candidates(root, package_name)
        if not candidates:
            if required:
                raise WorkflowError(f"插件仓库中未找到 {package_name} 的 Makefile。")
            return None
        if len(candidates) > 1:
            joined = "；".join(str(path.relative_to(root)) for path in candidates)
            raise WorkflowError(f"插件 {package_name} 匹配多个目录，请指定唯一插件名：{joined}")
        return candidates[0]

    def _resolve_plugin_packages(
        self,
        root: Path,
        requested_names: tuple[str, ...],
    ) -> tuple[tuple[str, Path], ...]:
        resolved: dict[str, Path] = {}
        requested = set(requested_names)
        for requested_name in requested_names:
            requested_path = self._unique_package_candidate(
                root, requested_name, required=True
            )
            assert requested_path is not None
            if requested_name.startswith("luci-app-"):
                companion_name = requested_name.removeprefix("luci-app-")
                companion_path = self._unique_package_candidate(
                    root, companion_name, required=False
                )
                pair = ((companion_name, companion_path), (requested_name, requested_path))
            else:
                companion_name = f"luci-app-{requested_name}"
                companion_path = self._unique_package_candidate(
                    root, companion_name, required=False
                )
                pair = ((requested_name, requested_path), (companion_name, companion_path))
            for package_name, package_path in pair:
                if package_path is not None:
                    resolved.setdefault(package_name, package_path)
            if companion_path is not None and companion_name not in requested:
                self._write(
                    f"[插件] {requested_name} 自动包含配套包：{companion_name}"
                )
        return tuple(resolved.items())

    def _install_plugins(
        self,
        project: ProjectSpec,
        plugins: tuple[PluginSpec, ...],
    ) -> tuple[PluginSpec, ...]:
        if not plugins:
            return ()
        package_root = project.directory / "package" / "custom"
        staging_root = project.directory / self.INTERNAL_DIR / "staging"
        package_root.mkdir(parents=True, exist_ok=True)
        staging_root.mkdir(parents=True, exist_ok=True)
        installed: list[PluginSpec] = []
        for index, plugin in enumerate(plugins, 1):
            self._assert_not_cancelled()
            repository = require_repository(plugin.repository, "插件项目地址")
            branch = require_branch(plugin.branch, "插件分支")
            stage = staging_root / f"plugin-{index}-{source_name(repository)}"
            if stage.exists():
                shutil.rmtree(stage)
            try:
                self.step(f"下载自定义插件 {index}/{len(plugins)}")
                self.runner.run(
                    ("git", "clone", "--depth", "1", "--single-branch", "--branch", branch, repository, str(stage)),
                    project.directory,
                )
                resolved = self._resolve_plugin_packages(stage, plugin.package_names)
                for package_name, source in resolved:
                    destination = package_root / package_name
                    if destination.exists():
                        self._write(f"[插件] 替换受本工具管理的插件：{destination}")
                        shutil.rmtree(destination)
                    shutil.copytree(source, destination, symlinks=True)
                    self._remove_duplicate_packages(project, package_name, destination)
                    self._write(f"[插件] 已安装 {package_name}：{repository}@{branch}")
                installed.append(
                    PluginSpec(repository, branch, tuple(name for name, _source in resolved))
                )
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        return tuple(installed)

    def _deduplicate_installed_plugins(
        self,
        project: ProjectSpec,
        plugins: tuple[PluginSpec, ...],
    ) -> None:
        for plugin in plugins:
            for package_name in plugin.package_names:
                keep = project.directory / "package" / "custom" / package_name
                if keep.is_dir():
                    self._remove_duplicate_packages(project, package_name, keep)

    def _write_settings_package(self, project: ProjectSpec, spec: BuildSpec) -> None:
        """Put defaults in a tiny package instead of editing upstream source files."""

        package = project.directory / "package" / "custom" / "settings"
        defaults = package / "files" / "etc" / "uci-defaults" / "99-builder-settings"
        build_id_file = package / "files" / "etc" / "owrt-tools-build-id"
        makefile = """include $(TOPDIR)/rules.mk

PKG_NAME:=builder-settings
PKG_RELEASE:=1

include $(INCLUDE_DIR)/package.mk

define Package/builder-settings
  SECTION:=base
  CATEGORY:=Base system
  TITLE:=OpenWrt Builder defaults
endef

define Build/Compile
endef

define Package/builder-settings/install
\t$(INSTALL_DIR) $(1)/etc/uci-defaults
\t$(INSTALL_BIN) ./files/etc/uci-defaults/99-builder-settings $(1)/etc/uci-defaults/99-builder-settings
\t$(INSTALL_DATA) ./files/etc/owrt-tools-build-id $(1)/etc/owrt-tools-build-id
endef

$(eval $(call BuildPackage,builder-settings))
"""
        import shlex

        script = "\n".join(
            (
                "#!/bin/sh",
                f"uci -q set system.@system[0].hostname={shlex.quote(spec.hostname)}",
                f"uci -q set network.lan.ipaddr={shlex.quote(spec.ip_address)}",
                f"uci -q set wireless.default_radio0.ssid={shlex.quote(spec.wifi_ssid)}",
                "uci -q set wireless.default_radio0.encryption='psk2'",
                f"uci -q set wireless.default_radio0.key={shlex.quote(spec.wifi_password)}",
                "uci -q commit system",
                "uci -q commit network",
                "uci -q commit wireless",
                "exit 0",
                "",
            )
        )
        atomic_write(package / "Makefile", makefile)
        atomic_write(defaults, script, mode=0o755)
        atomic_write(build_id_file, spec.build_id + "\n")

    def stage_prebuilt_package(self, project: ProjectSpec, source: Path) -> PrebuiltPackageSpec:
        """Copy one selected archive into the selected project's private storage."""

        project_root = project.directory.expanduser().resolve()
        staging = (project_root / self.INTERNAL_DIR / "prebuilt").resolve()
        if not staging.is_relative_to(project_root):
            raise WorkflowError("预编译软件包目录不能位于项目目录之外。")
        try:
            package = stage_prebuilt_package(source, staging)
        except PrebuiltPackageError as exc:
            raise WorkflowError(str(exc)) from exc
        self._write(f"[预编译包] 已导入：{package.filename}；SHA256：{package.sha256}")
        return package

    def stage_build_script(self, project: ProjectSpec, source: Path) -> ScriptSpec:
        """Prepare one selected script below the project's private storage."""

        project_root = project.directory.expanduser().resolve()
        staging = (project_root / self.INTERNAL_DIR / "scripts").resolve()
        if not staging.is_relative_to(project_root):
            raise WorkflowError("自定义脚本目录不能位于项目目录之外。")
        try:
            script = stage_build_script(source, staging)
        except ScriptError as exc:
            raise WorkflowError(str(exc)) from exc
        self._write(
            f"[脚本] 已完成 dos2unix、bash -n 和执行权限设置：{script.filename}；"
            f"SHA256：{script.sha256}"
        )
        return script

    def _run_custom_script(self, project: ProjectSpec, spec: BuildSpec) -> None:
        if spec.custom_script is None:
            return
        staging = project.directory / self.INTERNAL_DIR / "scripts"
        try:
            script = verify_staged_script(staging, spec.custom_script)
        except ScriptError as exc:
            raise WorkflowError(str(exc)) from exc
        self.step("执行自定义脚本")
        self._write(f"[脚本] 开始执行：{script}")
        environment = {
            "GITHUB_WORKSPACE": str(project.directory),
            "WRT_CONFIG": "",
            "WRT_DATE": spec.build_id.removeprefix("OWRT-Tools-"),
            "WRT_IP": spec.ip_address,
            "WRT_MARK": "OWRT-Tools",
            "WRT_NAME": spec.hostname,
            "WRT_PACKAGE": spec.extra_config,
            "WRT_SSID": spec.wifi_ssid,
            "WRT_TARGET": platform_key(spec.platform),
            "WRT_THEME": "bootstrap",
            "WRT_WORD": spec.wifi_password,
        }
        self.runner.run(("bash", str(script)), project.directory, env=environment)
        self._write("[脚本] 自定义脚本执行完成。")

    def _write_build_identifier(self, project: ProjectSpec, build_id: str) -> None:
        """Append the build ID to LuCI's firmware-version display."""

        status_root = project.directory / "feeds" / "luci" / "modules" / "luci-mod-status"
        candidates = sorted(status_root.rglob("10_system.js")) if status_root.is_dir() else []
        pattern = re.compile(
            r"\(luciversion\s*\|\|\s*''\)"
            r"(?:\s*\+\s*\(\s*' / OWRT-Tools-\d{8}-\d{6}'\s*\))*"
        )
        changed = 0
        for path in candidates:
            try:
                content = path.read_text(encoding="utf-8")
                updated, replacements = pattern.subn(
                    f"(luciversion || '') + (' / {build_id}')", content
                )
                if replacements:
                    atomic_write(path, updated, mode=path.stat().st_mode & 0o777)
                    changed += replacements
            except (OSError, UnicodeError) as exc:
                raise WorkflowError(f"无法写入编译标识：{path}: {exc}") from exc
        if changed:
            self._write(f"[标识] 已写入 LuCI 固件版本：{build_id}")
        else:
            self._write(
                f"[标识] 未找到 LuCI 版本显示位置，"
                f"已仅写入 /etc/owrt-tools-build-id：{build_id}"
            )

    def _write_prebuilt_package(self, project: ProjectSpec, spec: BuildSpec) -> None:
        """Generate the small package that copies staged IPK/APK data into the rootfs."""

        package = project.directory / "package" / "custom" / "builder-prebuilt"
        marker = package / ".owrt-tool-managed"
        if not spec.prebuilt_packages:
            if marker.is_file():
                shutil.rmtree(package)
                self._write("[预编译包] 已移除不再使用的集成包。")
            return
        if package.exists() and (package.is_symlink() or not marker.is_file()):
            raise WorkflowError(f"预编译软件包目录已被其他内容占用：{package}")
        if package.exists():
            shutil.rmtree(package)

        staged_root = project.directory / self.INTERNAL_DIR / "prebuilt"
        files = package / "files"
        package.mkdir(parents=True, exist_ok=True)
        files.mkdir()
        try:
            for entry in spec.prebuilt_packages:
                try:
                    source = verify_staged_package(staged_root, entry)
                except PrebuiltPackageError as exc:
                    raise WorkflowError(str(exc)) from exc
                shutil.copy2(source, files / entry.filename)
            makefile = """include $(TOPDIR)/rules.mk

PKG_NAME:=builder-prebuilt
PKG_RELEASE:=1
PKG_INSTALL:=1

include $(INCLUDE_DIR)/package.mk

define Package/builder-prebuilt
  SECTION:=base
  CATEGORY:=Base system
  TITLE:=Staged prebuilt package data
endef

define Build/Compile
\tset -eu; \\
\t$(INSTALL_DIR) $(PKG_INSTALL_DIR); \\
\tfor package in ./files/*.apk; do \\
\t\t[ -f "$$package" ] || continue; \\
\t\t[ -x "$(STAGING_DIR_HOST)/bin/apk" ] || { echo "APK 集成需要 staging_dir/host/bin/apk" >&2; exit 1; }; \\
\t\t$(STAGING_DIR_HOST)/bin/apk extract --allow-untrusted --destination $(PKG_INSTALL_DIR) "$$package"; \\
\tdone; \\
\tfor package in ./files/*.ipk; do \\
\t\t[ -f "$$package" ] || continue; \\
\t\tmember="$$(ar t "$$package" | sed -n '/^data\\.tar\\./{p;q;}')"; \\
\t\t[ -n "$$member" ] || { echo "IPK 缺少 data.tar.*：$$package" >&2; exit 1; }; \\
\t\tcase "$$member" in \\
\t\t\tdata.tar.gz) ar p "$$package" "$$member" | tar -xzf - -C $(PKG_INSTALL_DIR) ;; \\
\t\t\tdata.tar.xz) ar p "$$package" "$$member" | tar -xJf - -C $(PKG_INSTALL_DIR) ;; \\
\t\t\tdata.tar.bz2|data.tar.bz) ar p "$$package" "$$member" | tar -xjf - -C $(PKG_INSTALL_DIR) ;; \\
\t\t\tdata.tar.zst) ar p "$$package" "$$member" | tar --zstd -xf - -C $(PKG_INSTALL_DIR) ;; \\
\t\t\tdata.tar) ar p "$$package" "$$member" | tar -xf - -C $(PKG_INSTALL_DIR) ;; \\
\t\t\t*) echo "不支持的 IPK 数据格式：$$member" >&2; exit 1 ;; \\
\t\tesac; \\
\tdone
endef

define Package/builder-prebuilt/install
\t$(CP) $(PKG_INSTALL_DIR)/. $(1)/
endef

$(eval $(call BuildPackage,builder-prebuilt))
"""
            atomic_write(package / "Makefile", makefile)
            atomic_write(marker, "managed by owrt_tool\n")
        except WorkflowError:
            if package.exists():
                shutil.rmtree(package)
            raise
        except OSError as exc:
            if package.exists():
                shutil.rmtree(package)
            raise WorkflowError(f"无法生成预编译软件包集成包：{exc}") from exc
        self._write(f"[预编译包] 已准备 {len(spec.prebuilt_packages)} 个软件包用于固件集成。")

    def _write_initial_config(
        self,
        project: ProjectSpec,
        spec: BuildSpec,
        stamp: str,
        forced_config: tuple[str, ...],
    ) -> Path:
        config = project.directory / ".config"
        self.step("生成初始配置")
        atomic_write(config, build_config_text(spec, forced_config))
        copy = project.directory / self.INTERNAL_DIR / "configs" / stamp / "initial.config"
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config, copy)
        metadata = serialize_build_settings(spec)
        atomic_write(copy.parent / BUILD_SETTINGS_FILE, metadata, mode=0o600)
        atomic_write(project.directory / self.INTERNAL_DIR / BUILD_SETTINGS_FILE, metadata, mode=0o600)
        return copy

    def _validate_initial_config(
        self,
        project: ProjectSpec,
        spec: BuildSpec,
        stamp: str,
        forced_config: tuple[str, ...],
        jobs: str,
    ) -> Path:
        self.step("校验初始配置")
        self.runner.run(("make", "defconfig", f"-j{jobs}"), project.directory)
        config = project.directory / ".config"
        validate_resolved_config(config, spec, forced_config)
        copy = project.directory / self.INTERNAL_DIR / "configs" / stamp / "initial-validated.config"
        shutil.copy2(config, copy)
        self._write(f"[校验] 初始配置有效：{copy}")
        return copy

    def _backup_firmware(
        self,
        spec: BuildSpec,
        output: Path,
        snapshot: Path,
        log_path: Path,
        stamp: str,
    ) -> Path | None:
        if not spec.backup_enabled:
            self._write("[备份] 已禁用固件备份。")
            return None
        output = output.expanduser().resolve()
        snapshot = snapshot.expanduser().resolve()
        backup_root = (spec.backup_directory or self.root / "backup_firmware").expanduser().resolve()
        if backup_root == output or backup_root.is_relative_to(output):
            raise WorkflowError("固件备份目录不能位于固件输出目录内。")
        backup_root.mkdir(parents=True, exist_ok=True)
        destination = backup_root / f"{platform_key(spec.platform)}-{stamp}"
        suffix = 1
        while destination.exists():
            destination = backup_root / f"{platform_key(spec.platform)}-{stamp}-{suffix}"
            suffix += 1
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=backup_root)
        )
        try:
            shutil.copytree(output, temporary / "targets")
            shutil.copytree(snapshot, temporary / "config")
            shutil.copy2(log_path, temporary / log_path.name)
            sums: list[str] = []
            for path in sorted(temporary.rglob("*")):
                if path.is_file():
                    digest = hashlib.sha256()
                    with path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    sums.append(f"{digest.hexdigest()}  {path.relative_to(temporary)}")
            atomic_write(temporary / "SHA256SUMS", "\n".join(sums) + "\n")
            temporary.replace(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        self.backup_output_directory = destination
        self._write(f"[备份] 固件已保存：{destination}")
        candidates = sorted(
            (path for path in backup_root.iterdir() if path.is_dir() and path.name.startswith(f"{platform_key(spec.platform)}-")),
            key=lambda path: path.name,
            reverse=True,
        )
        for old in candidates[spec.backup_retention :]:
            shutil.rmtree(old)
            self._write(f"[备份] 按留存数删除旧备份：{old}")
        return destination

    def build(self, project: ProjectSpec, spec: BuildSpec) -> Path:
        """Download custom packages, validate config, then build one source tree."""

        validate_build_spec(spec)
        forced_config = self._forced_config()
        build_directory = (spec.build_directory or project.directory).expanduser().resolve()
        project = replace(project, directory=build_directory)
        if not (project.directory / "scripts" / "feeds").is_file():
            raise WorkflowError("项目不是可编译的 OpenWrt 源码树。")
        base_stamp = timestamp()
        spec = replace(spec, build_id=f"OWRT-Tools-{base_stamp}")
        stamp = base_stamp
        suffix = 1
        configs_root = project.directory / self.INTERNAL_DIR / "configs"
        while (configs_root / stamp).exists():
            stamp = f"{base_stamp}-{suffix}"
            suffix += 1
        jobs = str(max(1, os.cpu_count() or 1))
        with self.operation_log(platform_key(spec.platform)) as log_path:
            manifest = self.latest_toolchain_manifest(project, spec.platform)
            if manifest is not None:
                self._write(f"[工具链] 自动应用最新匹配工具链：{manifest.name}")
                self._apply_toolchain(project, spec.platform, manifest)
            else:
                self._write("[工具链] 未找到匹配工具链，本次使用源码树现有工具链。")
            self._write(f"[项目] {project.name} {project.repository}@{project.branch}")
            self._write(f"[平台] {spec.platform}; 设备：{' '.join(spec.devices)}")
            self._write(f"[标识] 编译标识：{spec.build_id}")
            self._write(
                f"[输入] 主机名：{spec.hostname}; IP：{spec.ip_address}; WiFi：{spec.wifi_ssid}; "
                f"WiFi 密码：{spec.wifi_password}"
            )
            for plugin in spec.plugins:
                self._write(
                    f"[输入] 插件：{plugin.repository}@{plugin.branch}; "
                    f"名称：{' '.join(plugin.package_names)}"
                )
            config_lines = [line for line in spec.extra_config.splitlines() if line.strip()]
            if config_lines:
                config_digest = hashlib.sha256(spec.extra_config.encode("utf-8")).hexdigest()
                self._write(
                    f"[输入] 常规配置：{len(config_lines)} 行；SHA256：{config_digest}"
                )
            self._write(
                f"[配置] 强制混入 {len(forced_config)} 项："
                + " ".join(forced_config)
            )
            for package in spec.prebuilt_packages:
                self._write(f"[输入] 预编译包：{package.filename}；SHA256：{package.sha256}")
            resolved_plugins = self._install_plugins(project, spec.plugins)
            spec = replace(spec, plugins=resolved_plugins)
            self._assert_not_cancelled()
            self.step("写入默认网络设置")
            self._write_settings_package(project, spec)
            self.step("准备预编译软件包")
            self._write_prebuilt_package(project, spec)
            self._write_initial_config(project, spec, stamp, forced_config)
            self._run_custom_script(project, spec)
            self._apply_forced_config(project.directory, forced_config)
            self._validate_initial_config(project, spec, stamp, forced_config, jobs)
            self._assert_not_cancelled()
            self.step("清理编译目录")
            self._write("[构建] 初始 make defconfig 完成，执行 make clean。")
            self.runner.run(("make", "clean", f"-j{jobs}"), project.directory)
            date_key = current_date()
            if self._feeds_need_refresh(project.directory, date_key):
                self._write(f"[feeds] 日期变更或缺少记录，更新当日 feeds：{date_key}")
                self._refresh_feeds(project.directory, date_key)
                self._deduplicate_installed_plugins(project, spec.plugins)
            else:
                self._write(f"[feeds] 当日已更新，跳过重复更新：{date_key}")
            self._apply_forced_config(project.directory, forced_config)
            self._write_build_identifier(project, spec.build_id)
            self.step("生成最终配置")
            self.final_config_started()
            self.runner.run(("make", "defconfig", f"-j{jobs}"), project.directory)
            validate_resolved_config(project.directory / ".config", spec, forced_config)
            final_copy = project.directory / self.INTERNAL_DIR / "configs" / stamp / "final.config"
            shutil.copy2(project.directory / ".config", final_copy)
            self._write(f"[配置] 最终配置：{final_copy}")
            self.step("下载编译依赖")
            self.runner.run(("make", "download", f"-j{jobs}"), project.directory)
            self._assert_not_cancelled()
            self.step("编译固件")
            try:
                self.runner.run(("make", f"-j{jobs}"), project.directory)
            except OperationCancelled:
                raise
            except WorkflowError as exc:
                self._write(f"[重试] 并行编译失败：{exc}")
                self.runner.run(("make", "-j1", "V=s"), project.directory)
            output = project.directory / "bin" / "targets"
            if not output.is_dir():
                raise WorkflowError("编译结束但未找到 bin/targets 输出目录。")
            self.output_directory = output
            snapshot = project.directory / self.INTERNAL_DIR / "configs" / stamp
            self._backup_firmware(spec, output, snapshot, log_path, stamp)
            try:
                self._save_toolchain(project, spec.platform)
            except OperationCancelled:
                raise
            except (WorkflowError, OSError, tarfile.TarError) as exc:
                self._write(f"[工具链] 自动保存跳过：{exc}")
            self.step("编译完成")
            self._write(f"[完成] 固件输出目录：{output}")
            return output

    @staticmethod
    def _toolchain_entries(project: ProjectSpec) -> list[Path]:
        project_root = project.directory.expanduser().resolve()
        staging = (project_root / "staging_dir").resolve()
        if not staging.is_relative_to(project_root):
            raise WorkflowError("staging_dir 不能指向项目目录之外。")
        if not staging.is_dir():
            return []
        return [
            path
            for path in staging.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and (path.name.startswith("toolchain-") or path.name in {"host", "hostpkg"})
        ]

    def _toolchain_key(self, project: ProjectSpec, platform: str) -> str:
        return f"{project.name}--{platform_key(platform)}"

    def list_toolchains(self) -> list[Path]:
        if not self.toolchains_root.is_dir():
            return []
        return sorted(self.toolchains_root.glob("*.json"), reverse=True)

    def latest_toolchain_manifest(self, project: ProjectSpec, platform: str) -> Path | None:
        """Return the newest timestamped manifest for exactly one project/platform pair."""

        if not self.toolchains_root.is_dir():
            return None
        key = self._toolchain_key(project, platform)
        timestamped: list[tuple[tuple[str, int], Path]] = []
        pattern = re.compile(
            rf"^{re.escape(key)}-(\d{{8}}-\d{{6}})(?:-(\d+))?\.json$"
        )
        for candidate in self.toolchains_root.glob(f"{key}-*.json"):
            match = pattern.fullmatch(candidate.name)
            if match:
                timestamped.append(((match.group(1), int(match.group(2) or 0)), candidate))
        if timestamped:
            return max(timestamped, key=lambda item: item[0])[1]
        legacy = self.toolchains_root / f"{key}.json"
        return legacy if legacy.is_file() else None

    def _save_toolchain(self, project: ProjectSpec, platform: str) -> Path:
        key = self._toolchain_key(project, platform)
        entries = self._toolchain_entries(project)
        if not entries:
            raise WorkflowError("未找到 staging_dir/toolchain-*；请至少成功编译一次。")
        self.toolchains_root.mkdir(parents=True, exist_ok=True)
        base_stamp = timestamp()
        base = f"{key}-{base_stamp}"
        archive = self.toolchains_root / f"{base}.tar.gz"
        manifest = self.toolchains_root / f"{base}.json"
        suffix = 1
        while archive.exists() or manifest.exists():
            archive = self.toolchains_root / f"{base}-{suffix}.tar.gz"
            manifest = self.toolchains_root / f"{base}-{suffix}.json"
            suffix += 1
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{archive.name}.", suffix=".tmp", dir=self.toolchains_root
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            self.step("保存工具链")
            self._write(f"[工具链] 归档：{archive}")
            with tarfile.open(temporary, "w:gz") as handle:
                for entry in entries:
                    self._assert_not_cancelled()
                    handle.add(entry, arcname=str(Path("staging_dir") / entry.name), recursive=True)
            temporary.replace(archive)
            metadata = {
                "project_name": project.name,
                "repository": project.repository,
                "branch": project.branch,
                "platform": platform_key(platform),
                "archive": archive.name,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            atomic_write(manifest, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
            self._write("[完成] 工具链保存完成。")
        finally:
            temporary.unlink(missing_ok=True)
        return manifest

    def save_toolchain(self, project: ProjectSpec, platform: str) -> Path:
        with self.operation_log(f"toolchain-{platform_key(platform)}"):
            return self._save_toolchain(project, platform)

    @staticmethod
    def _safe_link_target(member: tarfile.TarInfo) -> bool:
        raw_target = member.linkname
        target = Path(raw_target)
        if not raw_target or "\x00" in raw_target or target.is_absolute():
            return False
        candidates = [target]
        if member.issym():
            candidates.insert(0, Path(member.name).parent / target)
        for candidate in candidates:
            parts: list[str] = []
            for part in candidate.parts:
                if part in {"", "."}:
                    continue
                if part == "..":
                    if not parts:
                        break
                    parts.pop()
                else:
                    parts.append(part)
            else:
                if parts and parts[0] == "staging_dir":
                    return True
        return False

    @staticmethod
    def _safe_extract(handle: tarfile.TarFile, destination: Path) -> None:
        for member in handle.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "staging_dir":
                raise WorkflowError(f"工具链归档包含不安全路径：{member.name}")
            if member.issym() or member.islnk():
                if not Workflow._safe_link_target(member):
                    raise WorkflowError(f"工具链归档包含不安全链接：{member.name}")
                continue
            if not member.isdir() and not member.isreg():
                raise WorkflowError(f"工具链归档包含不支持的文件类型：{member.name}")
        for member in handle.getmembers():
            handle.extract(member, destination)

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path)

    def _apply_toolchain(self, project: ProjectSpec, platform: str, manifest: Path) -> None:
        manifest = manifest.expanduser().resolve()
        toolchains_root = self.toolchains_root.expanduser().resolve()
        if not manifest.is_relative_to(toolchains_root):
            raise WorkflowError("工具链清单必须位于工具链目录内。")
        try:
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
            expected_project = metadata["project_name"]
            expected_platform = metadata["platform"]
            archive_name = metadata["archive"]
            if not all(isinstance(value, str) for value in (expected_project, expected_platform, archive_name)):
                raise TypeError("工具链项目、平台和归档名必须是文本")
            if Path(archive_name).name != archive_name:
                raise WorkflowError("工具链归档路径必须位于工具链目录内。")
            archive = (manifest.parent / archive_name).resolve()
            if not archive.is_relative_to(toolchains_root):
                raise WorkflowError("工具链归档必须位于工具链目录内。")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorkflowError(f"工具链清单无效：{exc}") from exc
        if expected_project != project.name or expected_platform != platform_key(platform):
            raise WorkflowError("工具链项目名或平台名不匹配，已拒绝应用。")
        if not archive.is_file():
            raise WorkflowError(f"工具链归档不存在：{archive}")
        self.step("校验工具链")
        with tempfile.TemporaryDirectory(prefix="builder-toolchain-", dir=self.root) as temporary:
            temporary_path = Path(temporary)
            with tarfile.open(archive, "r:gz") as handle:
                self._safe_extract(handle, temporary_path)
            staged = temporary_path / "staging_dir"
            entries = list(staged.iterdir()) if staged.is_dir() else []
            invalid_entries = [
                path
                for path in entries
                if not path.is_dir() or path.is_symlink()
                or (not path.name.startswith("toolchain-") and path.name not in {"host", "hostpkg"})
            ]
            if invalid_entries:
                joined = "、".join(path.name for path in invalid_entries)
                raise WorkflowError(f"工具链归档包含不允许的目录：{joined}")
            if not entries:
                raise WorkflowError("工具链归档不包含 staging_dir 内容。")
            target_staging = project.directory / "staging_dir"
            if target_staging.is_symlink():
                raise WorkflowError("项目 staging_dir 不能是符号链接。")
            target_staging.mkdir(parents=True, exist_ok=True)
            backup = project.directory / self.INTERNAL_DIR / "toolchains" / timestamp()
            suffix = 1
            while backup.exists():
                backup = project.directory / self.INTERNAL_DIR / "toolchains" / f"{timestamp()}-{suffix}"
                suffix += 1
            self.step("应用工具链")
            moved: list[tuple[Path, Path]] = []
            touched: list[Path] = []
            try:
                for entry in entries:
                    self._assert_not_cancelled()
                    target = target_staging / entry.name
                    if os.path.lexists(target):
                        backup.mkdir(parents=True, exist_ok=True)
                        old = backup / entry.name
                        shutil.move(str(target), str(old))
                        moved.append((target, old))
                        self._write(f"[工具链] 已备份旧目录：{old}")
                    touched.append(target)
                    shutil.copytree(entry, target, symlinks=False)
                    self._write(f"[工具链] 已应用：{target}")
            except Exception:
                for target in reversed(touched):
                    if os.path.lexists(target):
                        self._remove_path(target)
                for target, old in reversed(moved):
                    if os.path.lexists(old):
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(old), str(target))
                raise
        self._write("[完成] 工具链应用完成。")

    def apply_toolchain(self, project: ProjectSpec, platform: str, manifest: Path) -> None:
        with self.operation_log(f"toolchain-{platform_key(platform)}"):
            self._apply_toolchain(project, platform, manifest)
