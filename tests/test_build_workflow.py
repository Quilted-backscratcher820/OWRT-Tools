from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.configuration import BUILD_SETTINGS_FILE, load_build_config
from core.models import BuildSpec, ProjectSpec
from core.workflow import Workflow


class BuildWorkflowTests(unittest.TestCase):
    def test_custom_script_build_id_and_custom_log_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_dir = root / "project"
            (project_dir / "scripts").mkdir(parents=True)
            (project_dir / "scripts" / "feeds").write_text("#!/bin/sh\n", encoding="ascii")
            status_js = (
                project_dir
                / "feeds"
                / "luci"
                / "modules"
                / "luci-mod-status"
                / "htdocs"
                / "10_system.js"
            )
            status_js.parent.mkdir(parents=True)
            status_js.write_text("version = (luciversion || '');\n", encoding="utf-8")
            (project_dir / "Makefile").write_text(
                """.DEFAULT_GOAL := all
.PHONY: defconfig download clean all
defconfig:
	@test -f .config
download:
	@:
clean:
	@:
all:
	@mkdir -p bin/targets/fixture
	@touch bin/targets/fixture/firmware.bin
""",
                encoding="utf-8",
            )
            source = root / "Settings.sh"
            source.write_bytes(
                b"#!/bin/bash\r\n"
                b"printf '%s\\n' \"$WRT_MARK|$WRT_DATE|$WRT_NAME\" > script-env.txt\r\n"
                b"printf 'CONFIG_PACKAGE_scripted=y\\n' >> .config\r\n"
            )
            project = ProjectSpec(
                "fixture", "https://github.com/example/fixture.git", "main", project_dir
            )
            log_root = root / "custom-logs"
            workflow = Workflow(root, log_root=log_root)
            script = workflow.stage_build_script(project, source)
            spec = BuildSpec(
                platform="x86_64",
                devices=("generic",),
                hostname="OWRT",
                ip_address="192.168.1.1",
                wifi_ssid="OWRT",
                wifi_password="12345678",
                custom_script=script,
                build_id="OWRT-Tools-20990102-030405",
                backup_enabled=False,
            )
            with patch("core.workflow.timestamp", return_value="20990102-030405"):
                workflow.build(project, spec)
                workflow.build(project, spec)

            self.assertEqual(
                (project_dir / "script-env.txt").read_text(encoding="utf-8").strip(),
                "OWRT-Tools|20990102-030405|OWRT",
            )
            status_text = status_js.read_text(encoding="utf-8")
            self.assertEqual(status_text.count("OWRT-Tools-20990102-030405"), 1)
            build_id_file = (
                project_dir
                / "package"
                / "custom"
                / "settings"
                / "files"
                / "etc"
                / "owrt-tools-build-id"
            )
            self.assertEqual(
                build_id_file.read_text(encoding="ascii").strip(),
                "OWRT-Tools-20990102-030405",
            )
            logs = sorted(log_root.glob("log-x86_64-*.txt"))
            self.assertEqual(len(logs), 2)
            for line in logs[-1].read_text(encoding="utf-8").splitlines():
                self.assertRegex(line, r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} ")

    def test_build_automatically_applies_latest_toolchain_and_saves_new_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_dir = root / "projects" / "fixture"
            (project_dir / "scripts").mkdir(parents=True)
            (project_dir / "scripts" / "feeds").write_text("#!/bin/sh\n", encoding="ascii")
            (project_dir / "staging_dir" / "toolchain-x").mkdir(parents=True)
            marker = project_dir / "staging_dir" / "toolchain-x" / "marker"
            marker.write_text("from-archive", encoding="ascii")
            (project_dir / "Makefile").write_text(
                """.DEFAULT_GOAL := all
.PHONY: defconfig download clean all
defconfig:
	@test -f clean.marker
	@test -f .config
download:
	@:
clean:
	@touch clean.marker
all:
	@mkdir -p bin/targets/fixture
	@touch bin/targets/fixture/firmware.bin
""",
                encoding="utf-8",
            )
            project = ProjectSpec("fixture", "https://github.com/example/fixture.git", "main", project_dir)
            toolchain_root = root / "saved-toolchains"
            workflow = Workflow(root, toolchain_root=toolchain_root)
            first_manifest = workflow.save_toolchain(project, "x86_64")
            marker.write_text("changed-before-build", encoding="ascii")
            spec = BuildSpec(
                platform="x86_64",
                devices=("generic",),
                hostname="OWRT",
                ip_address="192.168.1.1",
                wifi_ssid="OWRT",
                wifi_password="password8",
                backup_enabled=False,
            )
            output = Workflow(root, toolchain_root=toolchain_root).build(project, spec)
            self.assertTrue((output / "fixture" / "firmware.bin").is_file())
            self.assertEqual(marker.read_text(encoding="ascii"), "from-archive")
            manifests = sorted(toolchain_root.glob("fixture--x86_64-*.json"))
            self.assertEqual(len(manifests), 2)
            self.assertTrue(first_manifest.is_file())
            log = sorted((root / "logs").glob("log-x86_64-*.txt"))[-1].read_text(encoding="utf-8")
            self.assertIn("自动应用最新匹配工具链", log)

    def test_build_generates_and_validates_both_config_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_dir = root / "projects" / "fixture"
            (project_dir / "scripts").mkdir(parents=True)
            (project_dir / "scripts" / "feeds").write_text("#!/bin/sh\n", encoding="ascii")
            (project_dir / "Makefile").write_text(
                """.DEFAULT_GOAL := all
.PHONY: defconfig download clean all
defconfig:
\t@test -f clean.marker
\t@test -f .config
download:
\t@:
clean:
\t@touch clean.marker
all:
\t@mkdir -p bin/targets/fixture
\t@touch bin/targets/fixture/firmware.bin
""",
                encoding="ascii",
            )
            project = ProjectSpec("fixture", "https://github.com/example/fixture.git", "main", project_dir)
            spec = BuildSpec(
                platform="x86_64",
                devices=("generic",),
                hostname="fixture",
                ip_address="192.168.8.1",
                wifi_ssid="fixture-wifi",
                wifi_password="fixture-password",
                extra_config="CONFIG_PACKAGE_alpha=y\nCONFIG_PACKAGE_beta=y\n",
            )
            output = Workflow(root).build(project, spec)
            self.assertTrue((output / "fixture" / "firmware.bin").is_file())
            self.assertTrue((project_dir / "clean.marker").is_file())
            configs = next((project_dir / ".builder" / "configs").iterdir())
            self.assertTrue((configs / "initial.config").is_file())
            self.assertTrue((configs / "initial-validated.config").is_file())
            self.assertTrue((configs / "final.config").is_file())
            metadata = configs / BUILD_SETTINGS_FILE
            self.assertTrue(metadata.is_file())
            self.assertEqual(metadata.stat().st_mode & 0o777, 0o600)
            self.assertEqual(load_build_config(metadata).hostname, spec.hostname)
            latest_metadata = project_dir / ".builder" / BUILD_SETTINGS_FILE
            self.assertEqual(latest_metadata.stat().st_mode & 0o777, 0o600)
            settings = project_dir / "package" / "custom" / "settings" / "files" / "etc" / "uci-defaults" / "99-builder-settings"
            self.assertTrue(settings.is_file())
            settings_makefile = project_dir / "package" / "custom" / "settings" / "Makefile"
            self.assertIn("define Build/Compile\nendef", settings_makefile.read_text(encoding="utf-8"))
            build_log = next((root / "logs").glob("log-x86_64-*.txt")).read_text(
                encoding="utf-8"
            )
            self.assertIn("WiFi 密码：fixture-password", build_log)
            self.assertIn("常规配置：2 行；SHA256：", build_log)
            self.assertNotIn("[输入] 常规配置：CONFIG_PACKAGE", build_log)
            backups = list((root / "backup_firmware").glob("x86_64-*"))
            self.assertEqual(len(backups), 1)
            self.assertTrue((backups[0] / "SHA256SUMS").is_file())
            self.assertTrue((backups[0] / "targets" / "fixture" / "firmware.bin").is_file())
            with patch("core.workflow.timestamp", return_value="20990101-000001"):
                Workflow(root).build(project, spec)
            self.assertTrue((project_dir / "clean.marker").is_file())
            logs = sorted((root / "logs").glob("log-x86_64-*.txt"))
            self.assertIn("每次编译前执行 make clean", logs[-1].read_text(encoding="utf-8"))
