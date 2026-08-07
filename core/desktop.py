"""Open output directories on native Linux and WSL."""

from __future__ import annotations

import os
import platform
from pathlib import Path
import shutil
import subprocess

from PySide6.QtCore import QProcess, QUrl
from PySide6.QtGui import QDesktopServices


def _is_wsl() -> bool:
    return bool(os.environ.get("WSL_INTEROP") or os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in platform.release().lower()


def open_directory(directory: Path) -> None:
    """Open a directory and raise an actionable error if that is unavailable."""

    directory = directory.expanduser().resolve()
    if _is_wsl():
        converter = shutil.which("wslpath")
        explorer = shutil.which("explorer.exe") or "/mnt/c/Windows/explorer.exe"
        if not converter or not Path(explorer).exists():
            raise OSError("WSL 中未找到 wslpath 或 Windows 资源管理器")
        converted = subprocess.run([converter, "-w", str(directory)], capture_output=True, text=True, check=False)
        if converted.returncode or not converted.stdout.strip():
            raise OSError(converted.stderr.strip() or "无法转换 WSL 路径")
        started, _pid = QProcess.startDetached(explorer, [converted.stdout.strip()])
        if not started:
            raise OSError(f"无法打开目录：{directory}")
        return
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
        raise OSError(f"无法打开目录：{directory}")
