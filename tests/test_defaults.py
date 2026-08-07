from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.defaults import SourceDefaultsError, apply_source_defaults


def write_base_defaults(root: Path) -> Path:
    config = root / "package" / "base-files" / "files" / "bin" / "config_generate"
    config.parent.mkdir(parents=True)
    config.write_text(
        "lan) ipad=${ipaddr:-\"192.168.1.1\"} ;;\n"
        "netm=${netmask:-\"255.255.0.0\"}\n"
        "uci -q set system.@system[-1].hostname='OpenWrt'\n",
        encoding="utf-8",
    )
    return config


class SourceDefaultsTests(unittest.TestCase):
    def test_target_defaults_are_directly_replaced_and_repeatable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = write_base_defaults(root)
            target = (
                root
                / "target"
                / "linux"
                / "mediatek"
                / "filogic"
                / "base-files"
                / "etc"
                / "uci-defaults"
                / "99-set-wireless.sh"
            )
            target.parent.mkdir(parents=True)
            target.write_text(
                "BASE_SSID='OpenWrt'\nBASE_WORD='password8'\n",
                encoding="utf-8",
            )
            generic = (
                root
                / "package"
                / "network"
                / "config"
                / "wifi-scripts"
                / "files"
                / "lib"
                / "wifi"
                / "mac80211.uc"
            )
            generic.parent.mkdir(parents=True)
            generic.write_text("set radio.ssid='OpenWrt'\nset radio.key='password8'\n")
            flash = (
                root
                / "feeds"
                / "luci"
                / "modules"
                / "luci-mod-system"
                / "htdocs"
                / "flash.js"
            )
            flash.parent.mkdir(parents=True)
            flash.write_text(
                "ui.awaitReconnect('192.168.1.1', 'openwrt.lan');\n"
                "ui.awaitReconnect(window.location.host, '192.168.1.1');\n",
                encoding="utf-8",
            )

            result = apply_source_defaults(
                root,
                platform="mediatek_filogic",
                hostname="router-one",
                ip_address="192.168.9.1",
                wifi_ssid="Home's WiFi",
                wifi_password="pass'word8",
            )
            self.assertEqual(result.wireless_files, (target,))
            self.assertIn('${ipaddr:-"192.168.9.1"}', config.read_text())
            self.assertIn('${netmask:-"255.255.255.0"}', config.read_text())
            self.assertIn(".hostname='router-one'", config.read_text())
            target_text = target.read_text(encoding="utf-8")
            self.assertIn("BASE_SSID='Home'\"'\"'s WiFi'", target_text)
            self.assertIn("BASE_WORD='pass'\"'\"'word8'", target_text)
            self.assertIn(".ssid='OpenWrt'", generic.read_text())
            self.assertEqual(flash.read_text().count("192.168.9.1"), 2)

            apply_source_defaults(
                root,
                platform="mediatek_filogic",
                hostname="router-two",
                ip_address="10.0.0.1",
                wifi_ssid="Second",
                wifi_password="abcdefgh",
            )
            self.assertIn('${ipaddr:-"10.0.0.1"}', config.read_text())
            self.assertNotIn("192.168.9.1", config.read_text())
            self.assertIn("BASE_SSID='Second'", target.read_text())
            self.assertEqual(flash.read_text().count("10.0.0.1"), 2)

    def test_generic_ucode_fallback_is_copied_as_direct_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_base_defaults(root)
            generic = (
                root
                / "package"
                / "network"
                / "config"
                / "wifi-scripts"
                / "files"
                / "lib"
                / "wifi"
                / "mac80211.uc"
            )
            generic.parent.mkdir(parents=True)
            generic.write_text(
                "set ${si}.ssid='${defaults?.ssid || 'OpenWrt'}'\n"
                "set ${si}.key='${defaults?.key || 'password8'}'\n",
                encoding="utf-8",
            )
            result = apply_source_defaults(
                root,
                platform="x86_64",
                hostname="x86-router",
                ip_address="172.16.0.1",
                wifi_ssid="Cafe's WiFi",
                wifi_password=r"pass\word8",
            )
            self.assertEqual(result.wireless_files, (generic,))
            text = generic.read_text(encoding="utf-8")
            self.assertIn(r".ssid='Cafe\'s WiFi'", text)
            self.assertIn(r".key='pass\\word8'", text)

    def test_missing_config_generate_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(SourceDefaultsError, "默认网络配置文件不存在"):
                apply_source_defaults(
                    Path(temporary),
                    platform="x86_64",
                    hostname="router",
                    ip_address="192.168.1.1",
                    wifi_ssid="wifi",
                    wifi_password="password8",
                )

    def test_validation_and_write_failures_do_not_leave_partial_source_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = write_base_defaults(root)
            original = config.read_text(encoding="utf-8")
            with self.assertRaisesRegex(SourceDefaultsError, "WiFi 默认配置文件不存在"):
                apply_source_defaults(
                    root,
                    platform="x86_64",
                    hostname="router",
                    ip_address="10.0.0.1",
                    wifi_ssid="wifi",
                    wifi_password="password8",
                )
            self.assertEqual(config.read_text(encoding="utf-8"), original)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = write_base_defaults(root)
            wireless = (
                root
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
            config_original = config.read_text(encoding="utf-8")
            wireless_original = wireless.read_text(encoding="utf-8")
            import core.defaults as defaults

            real_write = defaults._atomic_write
            calls = 0

            def fail_second(path: Path, content: str, mode: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("模拟写入失败")
                real_write(path, content, mode)

            with patch("core.defaults._atomic_write", side_effect=fail_second):
                with self.assertRaisesRegex(SourceDefaultsError, "模拟写入失败"):
                    apply_source_defaults(
                        root,
                        platform="x86_64",
                        hostname="router",
                        ip_address="10.0.0.1",
                        wifi_ssid="wifi",
                        wifi_password="password8",
                    )
            self.assertEqual(config.read_text(encoding="utf-8"), config_original)
            self.assertEqual(wireless.read_text(encoding="utf-8"), wireless_original)
