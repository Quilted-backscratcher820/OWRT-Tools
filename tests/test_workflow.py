from __future__ import annotations

import json
import io
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from core.models import BuildSpec, PluginSpec, ProjectSpec
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

    def test_installed_plugin_is_a_materialized_directory(self) -> None:
        class CloneRunner:
            def __init__(self, source: Path) -> None:
                self.source = source

            def run(self, arguments: tuple[str, ...], cwd: Path, env: object = None) -> None:
                del cwd, env
                shutil.copytree(self.source, Path(arguments[-1]), symlinks=True)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            package = repository / "demo"
            package.mkdir(parents=True)
            (package / "Makefile").write_text("PKG_NAME:=demo\n", encoding="ascii")
            (package / "payload").write_text("copied", encoding="ascii")
            (package / "payload-link").symlink_to("payload")
            project_dir = root / "project"
            project = ProjectSpec(
                "fixture",
                "https://github.com/example/fixture.git",
                "main",
                project_dir,
            )
            workflow = Workflow(root)
            workflow.runner = CloneRunner(repository)  # type: ignore[assignment]
            installed = workflow._install_plugins(
                project,
                (
                    PluginSpec(
                        "https://github.com/example/plugins.git",
                        "main",
                        ("demo",),
                    ),
                ),
            )
            destination = project_dir / "package" / "custom" / "demo"
            self.assertEqual(installed[0].package_names, ("demo",))
            self.assertTrue(destination.is_dir())
            self.assertFalse(destination.is_symlink())
            self.assertTrue((destination / "payload-link").is_file())
            self.assertFalse((destination / "payload-link").is_symlink())
            self.assertEqual((destination / "payload-link").read_text(), "copied")
            outside = root / "outside"
            outside.write_text("private", encoding="ascii")
            (package / "outside-link").symlink_to(outside)
            with self.assertRaisesRegex(WorkflowError, "指向项目之外"):
                workflow._install_plugins(
                    project,
                    (
                        PluginSpec(
                            "https://github.com/example/plugins.git",
                            "main",
                            ("demo",),
                        ),
                    ),
                )
            self.assertTrue((destination / "payload-link").is_file())
            self.assertEqual((destination / "payload-link").read_text(), "copied")

    def test_plugin_deduplication_is_restored_when_install_transaction_fails(self) -> None:
        class CloneRunner:
            def __init__(self, source: Path) -> None:
                self.source = source

            def run(self, arguments: tuple[str, ...], cwd: Path, env: object = None) -> None:
                del cwd, env
                shutil.copytree(self.source, Path(arguments[-1]), symlinks=True)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            for name in ("demo", "demo-two"):
                package = repository / name
                package.mkdir(parents=True)
                (package / "Makefile").write_text(f"PKG_NAME:={name}\n", encoding="ascii")
            project_dir = root / "project"
            for name in ("demo", "demo-two"):
                duplicate = project_dir / "feeds" / "packages" / name
                duplicate.mkdir(parents=True)
                (duplicate / "original").write_text(name, encoding="ascii")
            project = ProjectSpec(
                "fixture", "https://github.com/example/fixture.git", "main", project_dir
            )
            workflow = Workflow(root)
            workflow.runner = CloneRunner(repository)  # type: ignore[assignment]
            real_dedup = workflow._remove_duplicate_packages
            calls = 0

            def fail_after_first_dedup(
                selected_project: ProjectSpec,
                package_name: str,
                keep: Path,
                preserve_root: Path | None = None,
            ) -> list[tuple[Path, Path]]:
                nonlocal calls
                calls += 1
                moved = real_dedup(selected_project, package_name, keep, preserve_root)
                if calls == 2:
                    raise WorkflowError("模拟插件安装事务失败")
                return moved

            with patch.object(
                workflow,
                "_remove_duplicate_packages",
                side_effect=fail_after_first_dedup,
            ), self.assertRaisesRegex(WorkflowError, "模拟插件安装事务失败"):
                workflow._install_plugins(
                    project,
                    (
                        PluginSpec("https://github.com/example/one.git", "main", ("demo",)),
                        PluginSpec("https://github.com/example/two.git", "main", ("demo-two",)),
                    ),
                )
            for name in ("demo", "demo-two"):
                self.assertTrue((project_dir / "feeds" / "packages" / name / "original").is_file())
                self.assertFalse((project_dir / "package" / "custom" / name).exists())

    def test_tool_managed_readonly_tree_can_be_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary) / "managed"
            child = tree / "readonly"
            child.mkdir(parents=True)
            (child / "payload").write_text("data", encoding="ascii")
            child.chmod(0o500)
            tree.chmod(0o500)
            Workflow._remove_path(tree)
            self.assertFalse(tree.exists())

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

    def test_toolchain_cache_restores_ccache_and_openwrt_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "source"
            ccache = source_dir / ".ccache"
            ccache.mkdir(parents=True)
            (ccache / "cache-entry").write_text("cached", encoding="ascii")
            stamp = source_dir / "staging_dir" / "host" / "stamp"
            stamp.mkdir(parents=True)
            stamp_file = stamp / ".host_ready"
            stamp_file.write_text("ready", encoding="ascii")
            old_time = 1_000_000_000
            os.utime(stamp_file, (old_time, old_time))
            toolchain = source_dir / "staging_dir" / "toolchain-test"
            toolchain.mkdir(parents=True)
            (toolchain / "marker").write_text("cached-toolchain", encoding="ascii")
            project = ProjectSpec(
                "fixture", "https://github.com/example/fixture.git", "main", source_dir
            )
            manifest = Workflow(root).save_toolchain(project, "x86_64")
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
            with tarfile.open(manifest.parent / metadata["archive"], "r:gz") as archive:
                names = archive.getnames()
            self.assertIn(".ccache/cache-entry", names)
            self.assertIn("staging_dir/host/stamp/.host_ready", names)

            target_dir = root / "target"
            target = ProjectSpec(
                "fixture", "https://github.com/example/fixture.git", "main", target_dir
            )
            Workflow(root).apply_toolchain(target, "x86_64", manifest)
            self.assertEqual(
                (target_dir / ".ccache" / "cache-entry").read_text(encoding="ascii"),
                "cached",
            )
            self.assertEqual(
                (target_dir / "staging_dir" / "toolchain-test" / "marker").read_text(
                    encoding="ascii"
                ),
                "cached-toolchain",
            )
            self.assertEqual((target_dir / "tmp" / ".build").read_text(), "1\n")
            self.assertGreater(
                (target_dir / "staging_dir" / "host" / "stamp" / ".host_ready").stat().st_mtime,
                old_time,
            )

    def test_toolchain_archive_hash_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_dir = root / "projects" / "fixture"
            source_dir = project_dir / "staging_dir" / "toolchain-test"
            source_dir.mkdir(parents=True)
            (source_dir / "marker").write_text("original", encoding="ascii")
            project = ProjectSpec("fixture", "https://github.com/example/fixture.git", "main", project_dir)
            workflow = Workflow(root)
            manifest = workflow.save_toolchain(project, "x86_64")
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
            archive = manifest.parent / metadata["archive"]
            archive.write_bytes(archive.read_bytes() + b"tampered")
            with self.assertRaisesRegex(WorkflowError, "SHA-256 校验失败"):
                workflow.apply_toolchain(project, "x86_64", manifest)

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
            config = project_dir / "package" / "base-files" / "files" / "bin" / "config_generate"
            config.parent.mkdir(parents=True)
            config.write_text(
                "lan) ipad=${ipaddr:-\"192.168.1.1\"} ;;\n"
                "uci -q set system.@system[-1].hostname='OpenWrt'\n",
                encoding="ascii",
            )
            wireless = (
                project_dir
                / "package"
                / "network"
                / "config"
                / "wifi-scripts"
                / "files"
                / "lib"
                / "wifi"
                / "mac80211.uc"
            )
            wireless.parent.mkdir(parents=True)
            wireless.write_text(
                "set ${si}.ssid='OpenWrt'\nset ${si}.key='password8'\n",
                encoding="ascii",
            )
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
