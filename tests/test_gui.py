from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QLabel, QLineEdit, QMessageBox

from core import __version__
from core.configuration import ImportedBuildConfig
from core.gui import MainWindow
from core.models import EnvironmentCheck, EnvironmentReport, PluginSpec, ProjectSpec
from core.workflow import WorkflowError


class GuiStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_version_backup_gate_and_plugin_rows(self) -> None:
        self.assertEqual(__version__, "4.0")
        window = MainWindow(Path.cwd())
        window.startup_timer.stop()
        window.resize(900, 720)
        window.show()
        self.assertIn(__version__, window.windowTitle())
        self.assertEqual(window.tabs.tabText(0), "环境与设置")
        self.assertEqual(window.about_button.text(), "关于")
        self.assertTrue(window.compile_directory_edit.text().endswith("projects"))
        self.assertTrue(window.toolchain_directory_edit.text().endswith("toolchains"))
        self.assertTrue(window.log_directory_edit.text().endswith("logs"))
        self.assertTrue(window.backup_directory_edit.text().endswith("backup_firmware"))
        self.assertIsInstance(window.backup_retention_edit, QLineEdit)
        self.assertTrue(window.elapsed_label.isHidden())
        self.assertEqual(window.wifi_password_edit.text(), "12345678")
        self.assertEqual(window.wifi_password_edit.echoMode(), QLineEdit.EchoMode.Normal)
        self.assertNotIn("编译标识", {label.text() for label in window.findChildren(QLabel)})
        with tempfile.TemporaryDirectory() as temporary:
            defaults_window = MainWindow(Path(temporary))
            defaults_window.startup_timer.stop()
            self.assertEqual(defaults_window.hostname_edit.text(), "OWRT")
            self.assertEqual(defaults_window.wifi_ssid_edit.text(), "OWRT")
            defaults_window.close()
        self.assertFalse(window.project_page.isEnabled())
        self.assertFalse(window.repository_edit.isEnabled())
        self.assertFalse(window.branch_edit.isEnabled())
        self.assertFalse(window.clone_button.isEnabled())
        window.report = EnvironmentReport((EnvironmentCheck("依赖", True, "ok"),))
        window._set_workflow_enabled()
        self.assertTrue(window.project_page.isEnabled())
        self.assertTrue(window.repository_edit.isEnabled())
        window.tabs.setCurrentIndex(1)
        self.application.processEvents()
        self.assertEqual(
            window.repository_edit.mapTo(window, QPoint(0, 0)).x(),
            window.branch_edit.mapTo(window, QPoint(0, 0)).x(),
        )
        self.assertEqual(
            window.devices_edit.mapTo(window, QPoint(0, 0)).x(),
            window.hostname_edit.mapTo(window, QPoint(0, 0)).x(),
        )
        self.assertLess(
            window.plugin_table.mapTo(window, QPoint(0, 0)).x(),
            window.plugin_repository_edit.mapTo(window, QPoint(0, 0)).x(),
        )
        self.assertGreater(window.plugin_table.width(), window.plugin_repository_edit.width())
        self.assertEqual(window.progress.height(), window.CONTROL_HEIGHT)
        self.assertGreaterEqual(
            window.plugin_table.height(),
            window.plugin_table.horizontalHeader().sizeHint().height()
            + window.CONTROL_HEIGHT * 3,
        )
        self.assertGreaterEqual(
            window.plugin_table.viewport().height(),
            window.plugin_table.verticalHeader().defaultSectionSize() * 3,
        )
        labels = {label.text() for label in window.findChildren(QLabel)}
        self.assertIn("已有项目", labels)
        self.assertNotIn("已添加项目", labels)
        for button_name in (
            "check_environment_button",
            "install_dependencies_button",
            "choose_compile_button",
            "open_compile_button",
            "choose_toolchain_directory_button",
            "open_toolchain_directory_button",
            "choose_log_directory_button",
            "open_log_directory_button",
            "open_output_button",
            "choose_backup_button",
            "open_backup_button",
            "clone_button",
            "add_plugin_button",
            "import_config_button",
            "choose_script_button",
            "clear_script_button",
            "build_button",
            "save_toolchain_button",
            "apply_toolchain_button",
            "about_button",
            "cancel_button",
        ):
            button = getattr(window, button_name)
            self.assertTrue(button.text(), button_name)
            self.assertFalse(button.icon().isNull(), button_name)
            self.assertEqual(button.height(), window.CONTROL_HEIGHT)
        self.assertEqual(window.compile_settings_group.title(), "编译设置")
        self.assertEqual(window.backup_settings_group.title(), "备份设置")
        window.backup_retention_edit.setText("5")
        self.assertEqual(window._build_spec().backup_retention, 5)
        window.backup_checkbox.setChecked(False)
        window.backup_retention_edit.setText("not-a-number")
        self.assertEqual(window._build_spec().backup_retention, 3)
        window.backup_checkbox.setChecked(True)
        with self.assertRaisesRegex(ValueError, "1-100"):
            window._build_spec()
        window.backup_retention_edit.setText("3")
        window.report = EnvironmentReport((EnvironmentCheck("依赖", False, "missing"),))
        window._set_workflow_enabled()
        self.assertFalse(window.project_page.isEnabled())
        self.assertFalse(window.repository_edit.isEnabled())
        self.assertEqual(window.plugin_table.horizontalHeaderItem(3).text(), "操作")
        self.assertEqual(window.prebuilt_table.horizontalHeaderItem(3).text(), "操作")
        field_labels = {
            "已有项目",
            "项目地址",
            "主机名",
            "WiFi 密码 *",
        }
        for label in window.findChildren(QLabel):
            if label.text() in field_labels:
                self.assertTrue(label.alignment() & Qt.AlignmentFlag.AlignRight)
        window.backup_checkbox.setChecked(False)
        self.assertFalse(window.backup_retention_spin.isEnabled())
        self.assertFalse(window.choose_backup_button.isEnabled())
        self.assertFalse(window.open_backup_button.isEnabled())
        window.backup_checkbox.setChecked(True)
        window.plugin_table.setRowCount(0)
        window._project_config_loaded = False
        window.plugin_repository_edit.setText("https://github.com/example/plugin.git")
        window.plugin_branch_edit.setText("main")
        window.plugin_names_edit.setText("axonhub luci-app-axonhub")
        with patch.object(
            QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
        ):
            window.add_plugin()
        self.assertEqual(window.plugin_table.rowCount(), 1)
        self.assertEqual(
            window.plugin_table.item(0, 2).text(),
            "axonhub luci-app-axonhub",
        )
        window.plugin_repository_edit.setText("https://github.com/example/packages.git")
        window.plugin_names_edit.setText("luci-app-two")
        window.add_plugin()
        self.assertEqual(window.plugin_table.rowCount(), 2)
        self.assertEqual(window.plugin_table.item(1, 2).text(), "luci-app-two")
        remove_button = window.plugin_table.cellWidget(0, 3)
        self.assertTrue(remove_button.text())
        self.assertFalse(remove_button.icon().isNull())
        self.assertEqual(remove_button.height(), window.CONTROL_HEIGHT)
        self.application.processEvents()
        header = window.plugin_table.horizontalHeaderItem(3).text()
        required_width = window.plugin_table.fontMetrics().horizontalAdvance(header) + 24
        self.assertGreaterEqual(window.plugin_table.columnWidth(3), required_width)
        self.assertEqual(
            window.plugin_table.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )
        self.assertFalse(window.plugin_table.horizontalScrollBar().isVisible())
        window.start_elapsed_timer()
        self.assertFalse(window.elapsed_label.isHidden())
        window._reset_elapsed_timer()
        self.assertTrue(window.elapsed_label.isHidden())
        window.close()

    def test_filter_status_script_and_existing_output_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_dir = root / "project"
            artifact = project_dir / "bin" / "targets" / "fixture" / "firmware.bin"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"firmware")
            project = ProjectSpec(
                "fixture", "https://github.com/example/fixture.git", "main", project_dir
            )
            window = MainWindow(root)
            window.startup_timer.stop()
            window.report = EnvironmentReport((EnvironmentCheck("依赖", True, "ok"),))
            window.project_combo.blockSignals(True)
            window.project_combo.clear()
            window.project_combo.addItem("fixture (main)", project)
            window.project_combo.setCurrentIndex(0)
            window.project_combo.blockSignals(False)
            window._selected_project_changed()
            self.assertEqual(window.output_directory, project_dir / "bin" / "targets")
            self.assertTrue(window.open_output_button.isEnabled())

            full_config = "CONFIG_PACKAGE_htop=y\nCONFIG_PACKAGE_nano=y\n"
            window.extra_config_edit.setPlainText(full_config)
            window.config_filter_edit.setText("htop")
            self.assertTrue(window.extra_config_edit.isReadOnly())
            self.assertEqual(window.extra_config_edit.toPlainText(), "CONFIG_PACKAGE_htop=y")
            self.assertEqual(window.config_filter_result_label.text(), "1/2")
            self.assertEqual(window._build_spec().extra_config, full_config)
            window.config_filter_edit.clear()
            self.assertFalse(window.extra_config_edit.isReadOnly())
            self.assertEqual(window.extra_config_edit.toPlainText(), full_config)

            report = EnvironmentReport(
                (
                    EnvironmentCheck("通过项", True, "ok"),
                    EnvironmentCheck("失败项", False, "missing"),
                )
            )
            window._display_environment_report(report)
            self.assertEqual(window.environment_table.item(0, 1).foreground().color().name(), "#15803d")
            self.assertEqual(window.environment_table.item(1, 1).foreground().color().name(), "#c62828")
            window.report = EnvironmentReport((EnvironmentCheck("依赖", True, "ok"),))
            window._set_workflow_enabled()

            script = root / "Settings.sh"
            script.write_bytes(b"#!/bin/bash\r\nprintf 'ok\\n'\r\n")
            with (
                patch.object(QFileDialog, "getOpenFileName", return_value=(str(script), "")),
                patch.object(
                    QMessageBox,
                    "information",
                    return_value=QMessageBox.StandardButton.Ok,
                ) as information,
            ):
                window.choose_build_script()
                deadline = time.monotonic() + 3
                while window.worker is not None and time.monotonic() < deadline:
                    self.application.processEvents()
                    time.sleep(0.001)
            self.assertIsNotNone(window.custom_script)
            self.assertEqual(window.script_path_edit.text(), str(script))
            self.assertTrue(window.clear_script_button.isEnabled())
            self.assertTrue(information.called)
            self.assertIn("将在点击开始编译后执行", window.log_edit.toPlainText())
            for line in window.log_edit.toPlainText().splitlines():
                self.assertRegex(line, r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} ")
            window.close()

    def test_expected_worker_failure_is_released_without_traceback(self) -> None:
        window = MainWindow(Path.cwd())
        window.startup_timer.stop()

        def fail_job(
            _log: object,
            _step: object,
            final: object,
            _cancelled: object,
        ) -> None:
            final()
            raise WorkflowError("simulated build failure")

        with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Ok):
            for _attempt in range(10):
                window._start_job(fail_job, lambda _value: self.fail("unexpected success"))
                deadline = time.monotonic() + 2
                while window.worker is not None and time.monotonic() < deadline:
                    self.application.processEvents()
                    time.sleep(0.001)
                self.assertIsNone(window.worker)
        self.assertFalse(window.elapsed_timer.isActive())
        self.assertFalse(window.elapsed_label.isHidden())
        self.assertIn("[失败] simulated build failure", window.log_edit.toPlainText())
        self.assertNotIn("Traceback", window.log_edit.toPlainText())
        window.close()

    def test_imported_configuration_populates_managed_fields(self) -> None:
        window = MainWindow(Path.cwd())
        window.startup_timer.stop()
        imported = ImportedBuildConfig(
            platform="mediatek/filogic",
            devices=("device_one", "device_two"),
            hostname="router",
            ip_address="192.168.88.1",
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
            backup_enabled=False,
            backup_retention=7,
            backup_directory=Path("/tmp/firmware"),
        )
        window._apply_imported_config(imported)
        self.assertEqual(window.platform_edit.text(), "mediatek/filogic")
        self.assertEqual(window.devices_edit.text(), "device_one device_two")
        self.assertEqual(window.hostname_edit.text(), "router")
        self.assertEqual(window.ip_edit.text(), "192.168.88.1")
        self.assertEqual(window.wifi_ssid_edit.text(), "Home WiFi")
        self.assertEqual(window.wifi_password_edit.text(), "password 123")
        self.assertEqual(window.extra_config_edit.toPlainText(), "CONFIG_PACKAGE_htop=y\n")
        self.assertEqual(window.plugin_table.rowCount(), 1)
        self.assertEqual(window.plugin_table.item(0, 2).text(), "luci-app-example")
        self.assertFalse(window.backup_checkbox.isChecked())
        self.assertEqual(window.backup_retention_edit.text(), "7")
        self.assertEqual(window.backup_directory_edit.text(), "/tmp/firmware")
        window.close()

    def test_existing_complete_config_is_loaded_before_manual_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project_dir = Path(temporary)
            (project_dir / ".config").write_text(
                "CONFIG_TARGET_x86=y\n"
                "CONFIG_TARGET_x86_64=y\n"
                "CONFIG_TARGET_DEVICE_x86_64_DEVICE_generic=y\n"
                "CONFIG_PACKAGE_htop=y\n",
                encoding="ascii",
            )
            window = MainWindow(Path.cwd())
            window.startup_timer.stop()
            project = ProjectSpec(
                "fixture", "https://github.com/example/fixture.git", "main", project_dir
            )
            window.project_combo.blockSignals(True)
            window.project_combo.clear()
            window.project_combo.addItem("fixture (main)", project)
            window.project_combo.setCurrentIndex(0)
            window.project_combo.blockSignals(False)
            with patch.object(
                QMessageBox, "information", return_value=QMessageBox.StandardButton.Ok
            ):
                window._selected_project_changed()
                self.assertTrue(window._project_config_loaded)
                self.assertEqual(window.platform_edit.text(), "x86_64")
                self.assertEqual(window.devices_edit.text(), "generic")
                window.hostname_edit.setText("changed")
                window._mark_project_config_modified()
                self.assertFalse(window._project_config_loaded)
            self.assertTrue(window._config_override_notified)
            window.close()
