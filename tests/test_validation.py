from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from core.models import BuildSpec, PluginSpec, PrebuiltPackageSpec
from core.validation import (
    ValidationError,
    build_config_text,
    platform_key,
    split_platform,
    validate_build_spec,
    validate_resolved_config,
)


def valid_spec(**changes: object) -> BuildSpec:
    values: dict[str, object] = {
        "platform": "qualcommax/ipq60xx",
        "devices": ("redmi_ax5", "qihoo_360v6"),
        "hostname": "OpenWrt",
        "ip_address": "192.168.10.1",
        "wifi_ssid": "OpenWrt",
        "wifi_password": "password8",
        "extra_config": "CONFIG_PACKAGE_htop=y\n",
        "plugins": (PluginSpec("https://github.com/example/package.git", "main", ("luci-app-example",)),),
    }
    values.update(changes)
    return BuildSpec(**values)  # type: ignore[arg-type]


class ValidationTests(unittest.TestCase):
    def test_platform_forms_create_target_and_device_symbols(self) -> None:
        self.assertEqual(split_platform("mediatek_filogic"), ("mediatek", "filogic"))
        self.assertEqual(platform_key("qualcommax/ipq60xx"), "qualcommax_ipq60xx")
        config = build_config_text(valid_spec())
        self.assertIn("CONFIG_TARGET_qualcommax=y", config)
        self.assertIn("CONFIG_TARGET_qualcommax_ipq60xx=y", config)
        self.assertIn("CONFIG_TARGET_DEVICE_qualcommax_ipq60xx_DEVICE_redmi_ax5=y", config)
        self.assertIn("CONFIG_TARGET_MULTI_PROFILE=y", config)
        self.assertIn("CONFIG_PACKAGE_luci-app-example=y", config)

    def test_prebuilt_packages_select_the_generated_wrapper(self) -> None:
        package = PrebuiltPackageSpec(
            "0123456789abcdef-demo.apk",
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        )
        config = build_config_text(valid_spec(prebuilt_packages=(package,)))
        self.assertIn("CONFIG_PACKAGE_builder-prebuilt=y", config)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".config"
            path.write_text(config, encoding="ascii")
            validate_resolved_config(path, valid_spec(prebuilt_packages=(package,)))

    def test_invalid_wifi_and_extra_config_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            validate_build_spec(valid_spec(wifi_password="short"))
        with self.assertRaises(ValidationError):
            validate_build_spec(valid_spec(extra_config="rm -rf /"))
        validate_build_spec(valid_spec(extra_config="CONFIG_PACKAGE_luci-app-example=y\n"))
        validate_build_spec(
            valid_spec(extra_config='CONFIG_EXTRA_OPTIMIZATION="-fno-caller-saves -fno-plt"\n')
        )

    def test_managed_selectors_override_conflicting_extra_config(self) -> None:
        config = build_config_text(
            valid_spec(
                extra_config=(
                    "CONFIG_TARGET_qualcommax=n\n"
                    "# CONFIG_PACKAGE_luci-app-example is not set\n"
                    "CONFIG_PACKAGE_builder-settings=n\n"
                )
            )
        )
        self.assertNotIn("CONFIG_TARGET_qualcommax=n", config)
        self.assertNotIn("CONFIG_PACKAGE_luci-app-example is not set", config)
        self.assertNotIn("CONFIG_PACKAGE_builder-settings=n", config)
        self.assertIn("CONFIG_TARGET_qualcommax=y", config)
        self.assertTrue(config.endswith("CONFIG_PACKAGE_luci-app-example=y\n"))

    def test_defconfig_result_must_keep_requested_symbols(self) -> None:
        spec = valid_spec()
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / ".config"
            config.write_text(build_config_text(spec), encoding="utf-8")
            validate_resolved_config(config, spec)
            config.write_text(
                build_config_text(spec).replace("CONFIG_PACKAGE_luci-app-example=y\n", ""),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValidationError, "自定义插件"):
                validate_resolved_config(config, spec)
            config.write_text("CONFIG_TARGET_qualcommax=y\n", encoding="utf-8")
            with self.assertRaises(ValidationError):
                validate_resolved_config(config, spec)
