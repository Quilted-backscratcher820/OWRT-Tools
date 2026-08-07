from __future__ import annotations

import json
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from core.models import BuildSpec, ProjectSpec
from core.validation import build_config_text
from core.workflow import OperationCancelled, Workflow, WorkflowError


FORCED_CONFIG_TEXT = (
    Path(__file__).parents[1] / "support" / "forced_config.txt"
).read_text(encoding="utf-8")


class WorkflowTests(unittest.TestCase):
    def test_custom_source_root_is_used_for_project_listing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "selected-source-root"
            project_dir = source_root / "fixture"
            (project_dir / ".builder").mkdir(parents=True)
            (project_dir / ".git").mkdir()
            (project_dir / ".builder" / "project.json").write_text(
                json.dumps(
                    {
                        "name": "fixture",
                        "repository": "https://github.com/example/fixture.git",
                        "branch": "main",
                    }
                ),
                encoding="utf-8",
            )
            workflow = Workflow(root, source_root=source_root)
            projects = workflow.list_projects()
            self.assertEqual([project.name for project in projects], ["fixture"])
            self.assertEqual(projects[0].directory, project_dir)

    def test_plugin_candidates_match_directory_or_pkg_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "luci-app-test"
            package.mkdir()
            (package / "Makefile").write_text("PKG_NAME:=different-name\n", encoding="utf-8")
            alternate = root / "anything"
            alternate.mkdir()
            (alternate / "Makefile").write_text("PKG_NAME:=named-package\n", encoding="utf-8")
            workflow = Workflow(root)
            self.assertEqual(workflow._package_candidates(root, "luci-app-test"), [package])
            self.assertEqual(workflow._package_candidates(root, "named-package"), [alternate])

    def test_core_plugin_automatically_includes_matching_luci_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            core = root / "axonhub"
            luci = root / "luci-app-axonhub"
            core.mkdir()
            luci.mkdir()
            (core / "Makefile").write_text("PKG_NAME:=axonhub\n", encoding="utf-8")
            (luci / "Makefile").write_text(
                "PKG_NAME:=luci-app-axonhub\n",
                encoding="utf-8",
            )
            messages: list[str] = []
            resolved = Workflow(root, log=messages.append)._resolve_plugin_packages(
                root,
                ("axonhub",),
            )
            self.assertEqual([name for name, _path in resolved], ["axonhub", "luci-app-axonhub"])
            self.assertTrue(any("自动包含配套包" in message for message in messages))

    def test_toolchain_manifest_rejects_project_or_platform_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_dir = root / "projects" / "openwrt"
            (project_dir / "staging_dir" / "toolchain-test").mkdir(parents=True)
            (project_dir / "staging_dir" / "toolchain-test" / "marker").write_text("ok", encoding="ascii")
            project = ProjectSpec("openwrt", "https://github.com/openwrt/openwrt.git", "main", project_dir)
            workflow = Workflow(root)
            manifest = workflow.save_toolchain(project, "x86_64")
            second_manifest = workflow.save_toolchain(project, "x86_64")
            data = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(data["project_name"], "openwrt")
            self.assertRegex(manifest.name, r"^openwrt--x86_64-\d{8}-\d{6}(?:-\d+)?\.json$")
            self.assertRegex(second_manifest.name, r"^openwrt--x86_64-\d{8}-\d{6}(?:-\d+)?\.json$")
            self.assertEqual(workflow.latest_toolchain_manifest(project, "x86_64"), second_manifest)
            with self.assertRaises(WorkflowError):
                workflow.apply_toolchain(project, "mediatek_filogic", manifest)

    def test_toolchain_archive_cannot_escape_staging_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "unsafe.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                source = Path(temporary) / "file"
                source.write_text("x", encoding="ascii")
                archive.add(source, arcname="../escape")
            with tarfile.open(archive_path, "r:gz") as archive:
                with self.assertRaises(WorkflowError):
                    Workflow._safe_extract(archive, Path(temporary) / "out")

    def test_toolchain_manifest_cannot_escape_archive_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_dir = root / "projects" / "fixture"
            project_dir.mkdir(parents=True)
            outside = root / "outside.tar.gz"
            with tarfile.open(outside, "w:gz") as archive:
                source = root / "payload"
                source.write_text("x", encoding="ascii")
                archive.add(source, arcname="staging_dir/toolchain-x/file")
            toolchains = root / "toolchains"
            toolchains.mkdir()
            manifest = toolchains / "fixture--x86_64.json"
            manifest.write_text(
                json.dumps(
                    {
                        "project_name": "fixture",
                        "platform": "x86_64",
                        "archive": "../outside.tar.gz",
                    }
                ),
                encoding="utf-8",
            )
            project = ProjectSpec("fixture", "https://github.com/example/fixture.git", "main", project_dir)
            with self.assertRaisesRegex(WorkflowError, "位于工具链目录内"):
                Workflow(root).apply_toolchain(project, "x86_64", manifest)

    def test_toolchain_archive_rejects_special_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "special.tar.gz"
            fifo = root / "fifo"
            os.mkfifo(fifo)
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(fifo, arcname="staging_dir/toolchain-test/fifo")
            with tarfile.open(archive_path, "r:gz") as archive:
                with self.assertRaises(WorkflowError):
                    Workflow._safe_extract(archive, root / "out")

    def test_toolchain_archive_allows_links_that_stay_inside_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "links.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                data = b"ok"
                target = tarfile.TarInfo("staging_dir/toolchain-test/file")
                target.size = len(data)
                archive.addfile(target, io.BytesIO(data))
                link = tarfile.TarInfo("staging_dir/toolchain-test/link")
                link.type = tarfile.SYMTYPE
                link.linkname = "file"
                archive.addfile(link)
            with tarfile.open(archive_path, "r:gz") as archive:
                Workflow._safe_extract(archive, root / "out")
            link_path = root / "out" / "staging_dir" / "toolchain-test" / "link"
            self.assertTrue(link_path.is_symlink())
            self.assertEqual(link_path.read_text(encoding="ascii"), "ok")

    def test_cancelled_toolchain_apply_restores_previous_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "source"
            (source_dir / "staging_dir" / "toolchain-test").mkdir(parents=True)
            (source_dir / "staging_dir" / "host").mkdir()
            (source_dir / "staging_dir" / "toolchain-test" / "marker").write_text("new")
            (source_dir / "staging_dir" / "host" / "marker").write_text("new")
            project = ProjectSpec("fixture", "https://github.com/example/fixture.git", "main", source_dir)
            manifest = Workflow(root).save_toolchain(project, "x86_64")

            target_dir = root / "target"
            (target_dir / "staging_dir" / "toolchain-test").mkdir(parents=True)
            (target_dir / "staging_dir" / "host").mkdir()
            (target_dir / "staging_dir" / "toolchain-test" / "marker").write_text("old")
            (target_dir / "staging_dir" / "host" / "marker").write_text("old")
            calls = 0

            def cancelled() -> bool:
                nonlocal calls
                calls += 1
                return calls > 1

            target = ProjectSpec("fixture", project.repository, project.branch, target_dir)
            with self.assertRaises(OperationCancelled):
                Workflow(root, cancelled=cancelled).apply_toolchain(target, "x86_64", manifest)
            self.assertEqual(
                (target_dir / "staging_dir" / "toolchain-test" / "marker").read_text(), "old"
            )
            self.assertEqual(
                (target_dir / "staging_dir" / "host" / "marker").read_text(), "old"
            )

    def test_failed_clone_removes_partial_directory(self) -> None:
        class FailingRunner:
            def run(self, arguments: tuple[str, ...], cwd: Path, env: object = None) -> None:
                del cwd, env
                target = Path(arguments[-1])
                (target / ".git").mkdir(parents=True)
                raise WorkflowError("模拟克隆失败")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = Workflow(root)
            workflow.runner = FailingRunner()  # type: ignore[assignment]
            with self.assertRaisesRegex(WorkflowError, "模拟克隆失败"):
                workflow.clone_project("https://github.com/example/project.git", "main")
            self.assertFalse((root / "projects" / "project").exists())

    def test_cancelled_parallel_build_does_not_retry_serially(self) -> None:
        class CancelledRunner:
            def __init__(self) -> None:
                self.commands: list[tuple[str, ...]] = []

            def run(self, arguments: tuple[str, ...], cwd: Path, env: object = None) -> None:
                del env
                command = tuple(arguments)
                self.commands.append(command)
                if command[:2] == ("make", "defconfig"):
                    (cwd / ".config").write_text(
                        build_config_text(spec, FORCED_CONFIG_TEXT.splitlines()),
                        encoding="utf-8",
                    )
                if command and command[0] == "make" and len(command) == 2 and command[1].startswith("-j"):
                    raise OperationCancelled("操作已取消。")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "support").mkdir()
            (root / "support" / "forced_config.txt").write_text(
                FORCED_CONFIG_TEXT,
                encoding="utf-8",
            )
            project_dir = root / "project"
            (project_dir / "scripts").mkdir(parents=True)
            (project_dir / "scripts" / "feeds").write_text("", encoding="ascii")
            project = ProjectSpec("fixture", "https://github.com/example/fixture.git", "main", project_dir)
            spec = BuildSpec(
                platform="x86_64",
                devices=("generic",),
                hostname="fixture",
                ip_address="192.168.2.1",
                wifi_ssid="wifi",
                wifi_password="password8",
                backup_enabled=False,
            )
            runner = CancelledRunner()
            workflow = Workflow(root)
            workflow.runner = runner  # type: ignore[assignment]
            with self.assertRaises(OperationCancelled), patch("core.workflow.os.cpu_count", return_value=2):
                workflow.build(project, spec)
            self.assertIn(("make", "defconfig", "-j2"), runner.commands)
            self.assertIn(("make", "clean", "-j2"), runner.commands)
            self.assertNotIn(("make", "-j1", "V=s"), runner.commands)
