"""Local dependency, permission and network checks."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
import os
import shutil
import subprocess
import time
from typing import Callable

from .models import EnvironmentCheck, EnvironmentReport
from .validation import FORCED_CONFIG_FILE, ValidationError, load_forced_config
from .wsl import inspect_wsl_path


LogCallback = Callable[[str], None]
CancelCallback = Callable[[], bool]


class EnvironmentProbe:
    """Probe the exact local prerequisites without mutating the system."""

    NETWORK_TARGETS = (
        ("Google", "https://www.google.com/"),
        ("YouTube", "https://www.youtube.com/"),
        ("GitHub", "https://github.com/"),
    )

    def __init__(
        self,
        root: Path,
        log: LogCallback | None = None,
        cancelled: CancelCallback | None = None,
    ) -> None:
        self.root = root
        self.log = log or (lambda message: None)
        self.cancelled = cancelled or (lambda: False)

    @property
    def dependency_file(self) -> Path:
        return self.root / "support" / "dependencies.txt"

    def dependency_names(self) -> tuple[str, ...]:
        if not self.dependency_file.is_file():
            raise FileNotFoundError(f"缺少依赖清单：{self.dependency_file}")
        content = self.dependency_file.read_text(encoding="utf-8-sig")
        names = tuple(
            token
            for raw in content.replace("\r", "").splitlines()
            for token in raw.split("#", 1)[0].split()
        )
        if not names:
            raise ValueError("support/dependencies.txt 为空。")
        return names

    @staticmethod
    def _package_installed(package: str) -> bool:
        if shutil.which("dpkg-query"):
            result = subprocess.run(
                ["dpkg-query", "-W", "-f=${Status}", package],
                capture_output=True,
                text=True,
                check=False,
            )
            return result.returncode == 0 and result.stdout.strip() == "install ok installed"
        return shutil.which(package) is not None

    def _dependency_check(self) -> EnvironmentCheck:
        try:
            missing: list[str] = []
            for entry in self.dependency_names():
                if self.cancelled():
                    return EnvironmentCheck("检测状态", False, "检测已取消")
                if not any(self._package_installed(candidate) for candidate in entry.split("|")):
                    missing.append(entry)
        except (OSError, ValueError) as exc:
            return EnvironmentCheck("编译依赖", False, str(exc))
        if missing:
            return EnvironmentCheck("编译依赖", False, "缺少：" + " ".join(missing))
        return EnvironmentCheck("编译依赖", True, "support/dependencies.txt 中的全部软件包已安装")

    def _runtime_check(self) -> EnvironmentCheck:
        missing = [name for name in ("git", "curl", "make", "python3") if not shutil.which(name)]
        if missing:
            return EnvironmentCheck("运行工具", False, "缺少命令：" + " ".join(missing))
        try:
            import_module("PySide6")
        except (ImportError, OSError) as exc:
            return EnvironmentCheck("运行工具", False, f"无法导入 PySide6：{exc}")
        try:
            load_forced_config(self.root / "support" / FORCED_CONFIG_FILE)
        except ValidationError as exc:
            return EnvironmentCheck("运行工具", False, str(exc))
        return EnvironmentCheck(
            "运行工具",
            True,
            "Python、PySide6、git、curl、make 和强制配置清单可用",
        )

    def _permission_check(self) -> EnvironmentCheck:
        if os.name != "posix":
            return EnvironmentCheck("运行权限", False, "仅支持 Linux 或 WSL。")
        if os.geteuid() == 0:
            return EnvironmentCheck("运行权限", False, "请使用普通用户运行，OpenWrt 不支持 root 编译。")
        try:
            for directory in (self.root, self.root / "projects", self.root / "logs"):
                directory.mkdir(parents=True, exist_ok=True)
                test_file = directory / ".builder-write-test"
                test_file.write_text("ok", encoding="ascii")
                test_file.unlink()
        except OSError as exc:
            return EnvironmentCheck("运行权限", False, f"工作目录不可写：{exc}")
        return EnvironmentCheck("运行权限", True, "普通用户可写入工作目录")

    def _wsl_path_check(self) -> EnvironmentCheck:
        status = inspect_wsl_path()
        return EnvironmentCheck("WSL PATH", status.ok, status.detail)

    def _network_check(self, name: str, url: str) -> EnvironmentCheck:
        self.log(f"[检测] {name}: {url}")
        try:
            process = subprocess.Popen(
                [
                    "curl",
                    "--fail",
                    "--location",
                    "--silent",
                    "--show-error",
                    "--output",
                    os.devnull,
                    "--connect-timeout",
                    "6",
                    "--max-time",
                    "15",
                    "--write-out",
                    "%{http_code}",
                    url,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            return EnvironmentCheck(name, False, f"curl 无法执行：{exc}")
        deadline = time.monotonic() + 16
        while process.poll() is None:
            if self.cancelled():
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    process.wait()
                return EnvironmentCheck(name, False, "检测已取消")
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                return EnvironmentCheck(name, False, "连接超时")
            time.sleep(0.1)
        stdout, stderr = process.communicate()
        code = stdout.strip() or "无响应"
        if process.returncode != 0:
            message = stderr.strip() or f"HTTP {code}"
            return EnvironmentCheck(name, False, message)
        return EnvironmentCheck(name, True, f"HTTP {code}")

    def inspect(self) -> EnvironmentReport:
        if self.cancelled():
            return EnvironmentReport((EnvironmentCheck("检测状态", False, "检测已取消"),))
        checks: list[EnvironmentCheck] = []
        for check_factory in (
            self._dependency_check,
            self._runtime_check,
            self._wsl_path_check,
            self._permission_check,
        ):
            check = check_factory()
            checks.append(check)
            if self.cancelled():
                checks.append(EnvironmentCheck("检测状态", False, "检测已取消"))
                return EnvironmentReport(tuple(checks))
        for name, url in self.NETWORK_TARGETS:
            if self.cancelled():
                checks.append(EnvironmentCheck("检测状态", False, "检测已取消"))
                break
            check = self._network_check(name, url)
            checks.append(check)
            if check.detail == "检测已取消":
                break
        return EnvironmentReport(tuple(checks))
