from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from core.configuration import (
    ConfigurationError,
    load_build_config,
    load_complete_project_config,
    serialize_build_settings,
)
from core.models import BuildSpec, PluginSpec, PrebuiltPackageSpec, ScriptSpec
from core.validation import build_config_text


class ConfigurationImportTests(unittest.TestCase):
    def test_complete_project_config_requires_selected_device(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".config").write_text(
                "CONFIG_TARGET_x86=y\nCONFIG_TARGET_x86_64=y\n",
                encoding="ascii",
            )
            self.assertIsNone(load_complete_project_config(project))
            (project / ".config").write_text(
                "CONFIG_TARGET_x86=y\n"
                "CONFIG_TARGET_x86_64=y\n"
                "CONFIG_TARGET_DEVICE_x86_64_DEVICE_generic=y\n",
                encoding="ascii",
            )
            imported = load_complete_project_config(project)
            self.assertIsNotNone(imported)
            assert imported is not None
            self.assertEqual(imported.devices, ("generic",))

    def test_plain_config_and_generated_defaults_are_imported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            config = project / ".config"
            content = """# generated configuration
CONFIG_TARGET_qualcommax=y
CONFIG_TARGET_qualcommax_ipq60xx=y
CONFIG_TARGET_BOARD="qualcommax"
CONFIG_TARGET_SUBTARGET="ipq60xx"
CONFIG_TARGET_DEVICE_qualcommax_ipq60xx_DEVICE_redmi-ax5=y
CONFIG_TARGET_MULTI_PROFILE=y
CONFIG_PACKAGE_luci=y
CONFIG_PACKAGE_htop=y
# CONFIG_PACKAGE_nano is not set
"""
            config.write_text(content, encoding="utf-8")
            defaults = (
                project
                / "package"
                / "custom"
                / "settings"
                / "files"
                / "etc"
                / "uci-defaults"
                / "99-builder-settings"
            )
            defaults.parent.mkdir(parents=True)
            defaults.write_text(
                """#!/bin/sh
uci -q set system.@system[0].hostname=router
uci -q set network.lan.ipaddr=192.168.9.1
uci -q set wireless.default_radio0.ssid='Home WiFi'
uci -q set wireless.default_radio0.key='password 123'
""",
                encoding="utf-8",
            )
            imported = load_build_config(config)
            self.assertEqual(imported.platform, "qualcommax_ipq60xx")
            self.assertEqual(imported.devices, ("redmi-ax5",))
            self.assertEqual(imported.hostname, "router")
            self.assertEqual(imported.ip_address, "192.168.9.1")
            self.assertEqual(imported.wifi_ssid, "Home WiFi")
            self.assertEqual(imported.wifi_password, "password 123")
            self.assertIn("CONFIG_PACKAGE_htop=y", imported.extra_config)
            self.assertIn("# CONFIG_PACKAGE_nano is not set", imported.extra_config)
            self.assertNotIn("CONFIG_TARGET_qualcommax=y", imported.extra_config)
            text_import = project / "saved-config.txt"
            text_import.write_text(content, encoding="utf-8")
            imported_text = load_build_config(text_import)
            self.assertEqual(imported_text.platform, "qualcommax_ipq60xx")
            self.assertEqual(imported_text.devices, ("redmi-ax5",))

    def test_tool_metadata_round_trip_and_mismatch_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = BuildSpec(
                platform="mediatek/filogic",
                devices=("xiaomi_mi_router_wr30u",),
                hostname="router",
                ip_address="192.168.31.1",
                wifi_ssid="Home WiFi",
                wifi_password="password 123",
                extra_config="CONFIG_PACKAGE_htop=y\n",
                plugins=(
                    PluginSpec(
                        "https://github.com/example/plugin.git",
                        "main",
                        ("luci-app-example",),
                    ),
                ),
                prebuilt_packages=(
                    PrebuiltPackageSpec(
                        "0123456789abcdef-demo.ipk",
                        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                    ),
                ),
                custom_script=ScriptSpec(
                    "0123456789abcdef-Settings.sh",
                    "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
                ),
                build_id="OWRT-Tools-20990102-030405",
                backup_enabled=True,
                backup_retention=5,
                backup_directory=root / "firmware",
            )
            config = root / "final.config"
            config.write_text(build_config_text(spec), encoding="utf-8")
            metadata = root / "build-settings.json"
            metadata.write_text(serialize_build_settings(spec), encoding="utf-8")
            imported = load_build_config(config)
            self.assertEqual(imported.platform, spec.platform)
            self.assertEqual(imported.devices, spec.devices)
            self.assertEqual(imported.plugins, spec.plugins)
            self.assertEqual(imported.prebuilt_packages, spec.prebuilt_packages)
            self.assertEqual(imported.custom_script, spec.custom_script)
            self.assertEqual(imported.build_id, spec.build_id)
            self.assertEqual(imported.backup_retention, 5)
            self.assertEqual(imported.metadata_path, metadata)
            self.assertEqual(load_build_config(metadata).wifi_password, spec.wifi_password)

            config.write_text(
                build_config_text(
                    BuildSpec(
                        platform="x86_64",
                        devices=("generic",),
                        hostname="router",
                        ip_address="192.168.1.1",
                        wifi_ssid="wifi",
                        wifi_password="password8",
                    )
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "不匹配"):
                load_build_config(config)
