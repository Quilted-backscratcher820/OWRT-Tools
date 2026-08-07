from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from core.models import BuildSpec, ProjectSpec
from core.packages import PrebuiltPackageError, package_sha256, stage_prebuilt_package, verify_staged_package
from core.workflow import Workflow, WorkflowError


class PrebuiltPackageTests(unittest.TestCase):
    def test_stage_is_hashed_and_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "my package.apk"
            source.write_bytes(b"apk fixture")
            staged = root / "project" / ".builder" / "prebuilt"
            package = stage_prebuilt_package(source, staged)
            self.assertTrue(package.filename.endswith("-my_package.apk"))
            self.assertEqual(package.sha256, package_sha256(staged / package.filename))
            (staged / package.filename).write_bytes(b"changed")
            with self.assertRaisesRegex(PrebuiltPackageError, "校验失败"):
                verify_staged_package(staged, package)
            upper = root / "other.IPK"
            upper.write_bytes(b"ipk fixture")
            upper_package = stage_prebuilt_package(upper, staged)
            self.assertTrue(upper_package.filename.endswith("-other.ipk"))

    def test_only_ipk_and_apk_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "package.bin"
            source.write_bytes(b"data")
            with self.assertRaisesRegex(PrebuiltPackageError, "只支持"):
                stage_prebuilt_package(source, root / "staged")
            package = root / "package.ipk"
            package.write_bytes(b"data")
            real_staging = root / "real-staging"
            real_staging.mkdir()
            linked_staging = root / "linked-staging"
            linked_staging.symlink_to(real_staging, target_is_directory=True)
            with self.assertRaisesRegex(PrebuiltPackageError, "暂存目录不能是符号链接"):
                stage_prebuilt_package(package, linked_staging)

    def test_generated_wrapper_contains_data_only_extractors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_dir = root / "project"
            source = root / "package.ipk"
            source.write_bytes(b"ipk fixture")
            project = ProjectSpec(
                "fixture",
                "https://github.com/example/fixture.git",
                "main",
                project_dir,
            )
            workflow = Workflow(root)
            package = workflow.stage_prebuilt_package(project, source)
            spec = BuildSpec(
                platform="x86_64",
                devices=("generic",),
                hostname="OWRT",
                ip_address="192.168.1.1",
                wifi_ssid="OWRT",
                wifi_password="password8",
                prebuilt_packages=(package,),
                backup_enabled=False,
            )
            workflow._write_prebuilt_package(project, spec)
            makefile = (project_dir / "package" / "custom" / "builder-prebuilt" / "Makefile").read_text(
                encoding="utf-8"
            )
            self.assertIn("apk extract --allow-untrusted --destination", makefile)
            self.assertIn("ar p", makefile)
            staged_copy = (
                project_dir
                / "package"
                / "custom"
                / "builder-prebuilt"
                / "files"
                / package.filename
            )
            self.assertTrue(staged_copy.is_file())
            with self.assertRaisesRegex(WorkflowError, "校验失败"):
                workflow._write_prebuilt_package(
                    project,
                    replace(spec, prebuilt_packages=(replace(package, sha256="0" * 64),)),
                )
            self.assertTrue(
                (project_dir / "package" / "custom" / "builder-prebuilt" / "Makefile").is_file()
            )
