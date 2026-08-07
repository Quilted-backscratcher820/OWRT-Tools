from __future__ import annotations

from pathlib import Path
import unittest


class EntrypointTests(unittest.TestCase):
    root = Path(__file__).resolve().parent.parent

    def test_wsl_cmd_delegates_to_bridge_and_linux_launcher(self) -> None:
        entrypoint = (self.root / "run_owrt_windows_wsl2.cmd").read_text(encoding="utf-8-sig")
        self.assertIn('set "CORE_TOOL_WSL_SCRIPT=run_owrt_linux.sh"', entrypoint)
        self.assertIn('call "%~dp0support\\wsl_bridge.cmd" %*', entrypoint)

        bridge = (self.root / "support" / "wsl_bridge.cmd").read_text(encoding="utf-8-sig")
        for expected in (
            "\\\\wsl.localhost\\",
            "\\\\wsl$\\",
            "wsl.exe -d %DISTRO% --cd",
            "wsl.exe --cd",
            "CORE_TOOL_NO_PAUSE",
            "CORE_TOOL_WSL_DISTRO",
            "if not defined CORE_TOOL_WSL_DISTRO set \"DISTRO=%%A\"",
        ):
            self.assertIn(expected, bridge)
        self.assertIn("不支持 Windows 原生路径", bridge)

    def test_linux_permissions_use_a_safe_manifest(self) -> None:
        manifest = (self.root / "support" / "permissions.txt").read_text(encoding="ascii")
        self.assertIn("755 run_owrt_linux.sh", manifest)
        self.assertIn("755 support/system_setup.sh", manifest)
        restore = (self.root / "support" / "restore_permissions.sh").read_text(encoding="utf-8")
        self.assertIn('MANIFEST="$ROOT/support/permissions.txt"', restore)
        self.assertIn("权限路径不安全", restore)
