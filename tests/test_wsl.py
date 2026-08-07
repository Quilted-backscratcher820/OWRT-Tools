from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.wsl import _write_config, inspect_wsl_path, sanitize_build_path


class WslPathTests(unittest.TestCase):
    def test_system_wsl_conf_is_written_through_sudo(self) -> None:
        with (
            patch("core.wsl.shutil.which", return_value="/usr/bin/sudo"),
            patch("core.wsl.subprocess.run") as run,
        ):
            run.return_value.returncode = 0
            _write_config(Path("/etc/wsl.conf"), "[interop]\nappendWindowsPath=false\n")
            command = run.call_args.args[0]
            self.assertEqual(command[:3], ["/usr/bin/sudo", "install", "-m"])
            self.assertEqual(command[-1], "/etc/wsl.conf")

    def test_missing_setting_is_added_and_requires_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "wsl.conf"
            config.write_text("[user]\ndefault=builder\n", encoding="ascii")
            status = inspect_wsl_path(
                config,
                path_value="/usr/bin:/mnt/c/Windows/System32",
                wsl=True,
            )
            self.assertFalse(status.ok)
            self.assertTrue(status.changed)
            self.assertTrue(status.requires_restart)
            self.assertIn("[interop]\nappendWindowsPath=false", config.read_text())
            restarted = inspect_wsl_path(config, path_value="/usr/bin", wsl=True)
            self.assertTrue(restarted.ok)

    def test_true_setting_is_replaced_without_discarding_other_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "wsl.conf"
            config.write_text(
                "# keep this comment\n"
                "[interop]\n"
                "enabled=true\n"
                "appendWindowsPath=true\n"
                "[automount]\n"
                "enabled=false\n",
                encoding="ascii",
            )
            status = inspect_wsl_path(config, path_value="/usr/bin", wsl=True)
            self.assertFalse(status.ok)
            self.assertTrue(status.changed)
            content = config.read_text(encoding="ascii")
            self.assertIn("enabled=true", content)
            self.assertIn("appendWindowsPath=false", content)
            self.assertIn("[automount]", content)

    def test_non_wsl_environment_is_not_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            status = inspect_wsl_path(Path(temporary) / "wsl.conf", wsl=False)
            self.assertTrue(status.ok)
            self.assertFalse(status.changed)

    def test_current_windows_path_requires_restart_after_existing_false_setting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "wsl.conf"
            config.write_text("[interop]\nappendWindowsPath=false\n", encoding="ascii")
            status = inspect_wsl_path(
                config,
                path_value="/usr/bin:/mnt/d/Program Files/Git/bin",
                wsl=True,
            )
            self.assertFalse(status.ok)
            self.assertFalse(status.changed)
            self.assertTrue(status.requires_restart)
            self.assertIn("wsl --shutdown", status.detail)

    def test_build_path_removes_windows_entries_and_shell_fragments(self) -> None:
        sanitized, removed = sanitize_build_path(
            "/usr/bin:/mnt/c/Program Files (x86)/dotnet:/mnt/d/Tools:(x86)/broken",
            wsl=True,
        )
        self.assertEqual(sanitized, "/usr/bin")
        self.assertEqual(
            removed,
            (
                "/mnt/c/Program Files (x86)/dotnet",
                "/mnt/d/Tools",
                "(x86)/broken",
            ),
        )

    def test_build_path_is_unchanged_outside_wsl(self) -> None:
        value = "/usr/bin:/mnt/c/Program Files (x86)/dotnet"
        self.assertEqual(sanitize_build_path(value, wsl=False), (value, ()))
