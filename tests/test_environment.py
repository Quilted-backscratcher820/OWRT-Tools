from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.environment import EnvironmentProbe
from core.models import EnvironmentCheck


class EnvironmentTests(unittest.TestCase):
    def test_dependency_candidates_and_main_domains(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "support").mkdir()
            (root / "support" / "dependencies.txt").write_text("one|two\n", encoding="ascii")
            probe = EnvironmentProbe(root)
            self.assertEqual(probe.dependency_names(), ("one|two",))
            with patch.object(EnvironmentProbe, "_package_installed", side_effect=lambda name: name == "two"):
                self.assertTrue(probe._dependency_check().ok)
        self.assertEqual(EnvironmentProbe.NETWORK_TARGETS[0][1], "https://www.google.com/")
        self.assertEqual(EnvironmentProbe.NETWORK_TARGETS[1][1], "https://www.youtube.com/")

    def test_ubuntu_freetype_package_alias_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "support").mkdir()
            (root / "support" / "dependencies.txt").write_text(
                "libfreetype6-dev|libfreetype-dev\n", encoding="ascii"
            )
            probe = EnvironmentProbe(root)
            with patch.object(EnvironmentProbe, "_package_installed", side_effect=lambda name: name == "libfreetype-dev"):
                self.assertTrue(probe._dependency_check().ok)

    def test_cancelled_probe_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "support").mkdir()
            (root / "support" / "dependencies.txt").write_text("one\n", encoding="ascii")
            report = EnvironmentProbe(root, cancelled=lambda: True).inspect()
            self.assertFalse(report.ready)
            self.assertEqual(report.checks[0].detail, "检测已取消")

    def test_cancellation_between_base_checks_stops_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = {"cancelled": False}
            calls: list[str] = []
            probe = EnvironmentProbe(Path(temporary), cancelled=lambda: state["cancelled"])

            def dependency() -> EnvironmentCheck:
                calls.append("dependency")
                state["cancelled"] = True
                return EnvironmentCheck("编译依赖", True, "ok")

            def runtime() -> EnvironmentCheck:
                calls.append("runtime")
                return EnvironmentCheck("运行工具", True, "ok")

            def permission() -> EnvironmentCheck:
                calls.append("permission")
                return EnvironmentCheck("运行权限", True, "ok")

            probe._dependency_check = dependency  # type: ignore[method-assign]
            probe._runtime_check = runtime  # type: ignore[method-assign]
            probe._permission_check = permission  # type: ignore[method-assign]
            report = probe.inspect()
            self.assertEqual(calls, ["dependency"])
            self.assertFalse(report.ready)
