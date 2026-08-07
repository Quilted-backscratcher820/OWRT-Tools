from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from core.models import BuildSpec
from core.workflow import Workflow


class BackupTests(unittest.TestCase):
    def test_backup_name_manifest_and_disabled_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "targets"
            output.mkdir()
            (output / "firmware.bin").write_bytes(b"firmware")
            snapshot = root / "config"
            snapshot.mkdir()
            (snapshot / "final.config").write_text("CONFIG_TARGET_x=y\n", encoding="ascii")
            log = root / "build.log"
            log.write_text("build\n", encoding="ascii")
            spec = BuildSpec(
                platform="mediatek/filogic",
                devices=("demo",),
                hostname="demo",
                ip_address="192.168.1.1",
                wifi_ssid="demo",
                wifi_password="password8",
                backup_directory=root / "archives",
                backup_retention=1,
            )
            workflow = Workflow(root)
            destination = workflow._backup_firmware(
                spec,
                output,
                snapshot,
                log,
                "20260806-090000",
            )
            assert destination is not None
            self.assertEqual(destination.name, "mediatek_filogic-20260806-090000")
            self.assertTrue((destination / "SHA256SUMS").is_file())
            self.assertTrue((destination / "targets" / "firmware.bin").is_file())
            firmware_sum = hashlib.sha256(b"firmware").hexdigest()
            sums = (destination / "SHA256SUMS").read_text(encoding="ascii")
            self.assertIn(f"{firmware_sum}  targets/firmware.bin", sums)
            disabled = BuildSpec(
                platform="mediatek/filogic",
                devices=("demo",),
                hostname="demo",
                ip_address="192.168.1.1",
                wifi_ssid="demo",
                wifi_password="password8",
                backup_enabled=False,
            )
            self.assertIsNone(workflow._backup_firmware(disabled, output, snapshot, log, "later"))
