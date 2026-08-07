"""PySide6 desktop interface for the local OpenWrt workflow."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import subprocess
import threading
import traceback
from typing import Any, Callable

from PySide6.QtCore import QDir, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QBrush, QCloseEvent, QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .configuration import (
    ConfigurationError,
    ImportedBuildConfig,
    load_build_config,
    load_complete_project_config,
)
from .desktop import open_directory
from .environment import EnvironmentProbe
from .logs import timestamp_log_text
from .models import (
    BuildSpec,
    EnvironmentReport,
    PluginSpec,
    PrebuiltPackageSpec,
    ProjectSpec,
    ScriptSpec,
)
from .ui import AdaptiveColumnSizer
from .validation import (
    FORCED_CONFIG_FILE,
    ValidationError,
    load_forced_config,
    platform_key,
    require_branch,
    require_component,
    require_repository,
    source_name,
    validate_build_spec,
)
from .workflow import OperationCancelled, Workflow, WorkflowError
from . import __version__


Job = Callable[[Callable[[str], None], Callable[[str], None], Callable[[], None], Callable[[], bool]], Any]


class OperationWorker(QThread):
    """Run blocking subprocess and archive work off the Qt event thread."""

    log = Signal(str)
    step = Signal(str)
    final_config_started = Signal()
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, job: Job, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.job = job
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        try:
            result = self.job(
                self.log.emit,
                self.step.emit,
                self.final_config_started.emit,
                self._cancelled.is_set,
            )
        except OperationCancelled as exc:
            self.failed.emit(str(exc))
        except WorkflowError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # Keep an actionable traceback in the live log.
            self.log.emit(traceback.format_exc())
            self.failed.emit(str(exc))
        else:
            self.completed.emit(result)


class MainWindow(QMainWindow):
    """The main application window and workflow state gate."""

    CONTROL_HEIGHT = 30
    ROW_SPACING = 8
    FIRMWARE_SUFFIXES = frozenset(
        {".bin", ".elf", ".gz", ".img", ".iso", ".itb", ".qcow2", ".tar", ".trx", ".ubi", ".vdi", ".vmdk", ".xz", ".zst"}
    )

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root.resolve()
        self.report: EnvironmentReport | None = None
        self.worker: OperationWorker | None = None
        self.current_project: ProjectSpec | None = None
        self.project_page: QWidget | None = None
        self.output_directory: Path | None = None
        self.last_backup_directory: Path | None = None
        self.custom_script: ScriptSpec | None = None
        self._extra_config_full_text = ""
        self._active_config_filter = ""
        self._config_filter_updating = False
        self._config_loading = False
        self._project_config_loaded = False
        self._config_override_notified = False
        self._project_config_path: Path | None = None
        self.final_started_at: datetime | None = None
        self.install_running = False
        self.install_status_path: Path | None = None
        self.install_process: subprocess.Popen[bytes] | None = None
        self.setWindowTitle(f"OpenWrt 本地编译工具 {__version__}")
        self.resize(1180, 800)
        self._build_ui()
        self._refresh_projects()
        self._refresh_toolchains()
        self.startup_timer = QTimer(self)
        self.startup_timer.setSingleShot(True)
        self.startup_timer.timeout.connect(self.check_environment)
        self.startup_timer.start(0)

    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        status = QFrame()
        status.setFrameShape(QFrame.Shape.StyledPanel)
        status_layout = QHBoxLayout(status)
        status_layout.setSpacing(self.ROW_SPACING)
        self.step_label = QLabel("当前步骤：等待环境检测")
        self.elapsed_label = QLabel("编译时间：00:00:00")
        self.elapsed_label.setVisible(False)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.about_button = self._action_button(
            "关于", QStyle.StandardPixmap.SP_MessageBoxInformation, self.show_about
        )
        self.about_button.setToolTip("关于本工具")
        self.cancel_button = self._action_button(
            "停止", QStyle.StandardPixmap.SP_BrowserStop, self.cancel_current_operation
        )
        self.cancel_button.setToolTip("停止当前操作")
        self.cancel_button.setEnabled(False)
        left_status = QHBoxLayout()
        left_status.setContentsMargins(0, 0, 0, 0)
        left_status.addWidget(self.step_label)
        left_status.addStretch(1)
        center_status = QHBoxLayout()
        center_status.setContentsMargins(0, 0, 0, 0)
        center_status.setSpacing(self.ROW_SPACING)
        center_status.addWidget(self.elapsed_label)
        center_status.addWidget(self.progress, 1)
        center_status.addWidget(self.cancel_button)
        right_status = QHBoxLayout()
        right_status.setContentsMargins(0, 0, 0, 0)
        right_status.addStretch(1)
        right_status.addWidget(self.about_button)
        status_layout.addLayout(left_status, 1)
        status_layout.addLayout(center_status, 2)
        status_layout.addLayout(right_status, 1)
        layout.addWidget(status)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._environment_tab(), "环境与设置")
        self.tabs.addTab(self._project_tab(), "项目与编译")
        self.tabs.addTab(self._toolchain_tab(), "工具链与日志")
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self._normalize_control_heights()

        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.setInterval(1000)
        self.elapsed_timer.timeout.connect(self._update_elapsed)
        self.install_timer = QTimer(self)
        self.install_timer.setInterval(500)
        self.install_timer.timeout.connect(self._poll_installation)

    def _forced_config(self) -> tuple[str, ...]:
        return load_forced_config(self.root / "support" / FORCED_CONFIG_FILE)

    def _action_button(
        self,
        text: str,
        icon: QStyle.StandardPixmap,
        slot: Callable[[], None],
    ) -> QPushButton:
        button = QPushButton(text)
        button.setIcon(self.style().standardIcon(icon))
        button.clicked.connect(slot)
        return button

    def _normalize_control_heights(self) -> None:
        for control_type in (QLineEdit, QComboBox, QProgressBar, QPushButton):
            for control in self.findChildren(control_type):
                control.setFixedHeight(self.CONTROL_HEIGHT)

    def _label_width(self, *labels: str) -> int:
        return max(self.fontMetrics().horizontalAdvance(label) for label in labels) + self.ROW_SPACING

    def _field_row(self, label: str, control: QWidget, label_width: int | None = None) -> QWidget:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(self.ROW_SPACING)
        label_widget = QLabel(label)
        label_widget.setFixedWidth(label_width or self._label_width(label))
        label_widget.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row_layout.addWidget(label_widget)
        row_layout.addWidget(control, 1)
        return row

    def _inline_segment(self, label: str, control: QWidget, label_width: int | None = None) -> QWidget:
        segment = QWidget()
        segment_layout = QHBoxLayout(segment)
        segment_layout.setContentsMargins(0, 0, 0, 0)
        segment_layout.setSpacing(self.ROW_SPACING)
        label_widget = QLabel(label)
        label_widget.setFixedWidth(label_width or self._label_width(label))
        label_widget.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        segment_layout.addWidget(label_widget)
        segment_layout.addWidget(control, 1)
        return segment

    def _button_segment(self, button: QPushButton) -> QWidget:
        segment = QWidget()
        segment_layout = QHBoxLayout(segment)
        segment_layout.setContentsMargins(0, 0, 0, 0)
        segment_layout.setSpacing(self.ROW_SPACING)
        segment_layout.addWidget(button, 1)
        return segment

    def _environment_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.environment_table = QTableWidget(0, 3)
        self.environment_table.setHorizontalHeaderLabels(("检查项", "状态", "详情"))
        self.environment_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.environment_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.environment_sizer = AdaptiveColumnSizer(self.environment_table, stretch_column=2)
        layout.addWidget(self.environment_table)
        buttons = QHBoxLayout()
        buttons.setSpacing(self.ROW_SPACING)
        self.check_environment_button = self._action_button(
            "检测环境", QStyle.StandardPixmap.SP_BrowserReload, self.check_environment
        )
        self.install_dependencies_button = self._action_button(
            "安装缺失依赖", QStyle.StandardPixmap.SP_DialogApplyButton, self.install_dependencies
        )
        buttons.addWidget(self.check_environment_button)
        buttons.addWidget(self.install_dependencies_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        settings = QWidget()
        settings_layout = QHBoxLayout(settings)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(8)

        directories = QGroupBox("编译设置")
        directories_layout = QVBoxLayout(directories)
        directories_layout.setSpacing(self.ROW_SPACING)
        directory_label_width = self._label_width("编译目录", "工具链目录", "日志目录")
        self.compile_directory_edit = QLineEdit(str(self.root / "projects"))
        self.compile_directory_edit.setPlaceholderText("源码项目默认保存目录")
        self.compile_directory_edit.editingFinished.connect(self._refresh_projects)
        compile_line = QHBoxLayout()
        compile_line.setContentsMargins(0, 0, 0, 0)
        compile_line.setSpacing(self.ROW_SPACING)
        compile_line.addWidget(self.compile_directory_edit, 1)
        self.choose_compile_button = self._action_button(
            "选择", QStyle.StandardPixmap.SP_DirIcon, self.choose_compile_directory
        )
        self.choose_compile_button.setToolTip("选择编译目录")
        self.open_compile_button = self._action_button(
            "打开", QStyle.StandardPixmap.SP_DirOpenIcon, self.open_compile_directory
        )
        self.open_compile_button.setToolTip("打开编译目录")
        compile_line.addWidget(self.choose_compile_button)
        compile_line.addWidget(self.open_compile_button)
        compile_controls = QWidget()
        compile_controls.setLayout(compile_line)
        directories_layout.addWidget(
            self._field_row("编译目录", compile_controls, directory_label_width)
        )

        self.toolchain_directory_edit = QLineEdit(str(self.root / "toolchains"))
        self.toolchain_directory_edit.setPlaceholderText("工具链归档保存目录")
        self.toolchain_directory_edit.editingFinished.connect(self._refresh_toolchains)
        toolchain_line = QHBoxLayout()
        toolchain_line.setContentsMargins(0, 0, 0, 0)
        toolchain_line.setSpacing(self.ROW_SPACING)
        toolchain_line.addWidget(self.toolchain_directory_edit, 1)
        self.choose_toolchain_directory_button = self._action_button(
            "选择", QStyle.StandardPixmap.SP_DirIcon, self.choose_toolchain_directory
        )
        self.choose_toolchain_directory_button.setToolTip("选择工具链保存目录")
        self.open_toolchain_directory_button = self._action_button(
            "打开", QStyle.StandardPixmap.SP_DirOpenIcon, self.open_toolchain_directory
        )
        self.open_toolchain_directory_button.setToolTip("打开工具链保存目录")
        toolchain_line.addWidget(self.choose_toolchain_directory_button)
        toolchain_line.addWidget(self.open_toolchain_directory_button)
        toolchain_controls = QWidget()
        toolchain_controls.setLayout(toolchain_line)
        directories_layout.addWidget(
            self._field_row("工具链目录", toolchain_controls, directory_label_width)
        )

        self.log_directory_edit = QLineEdit(str(self.root / "logs"))
        self.log_directory_edit.setPlaceholderText("实时构建日志保存目录")
        log_directory_line = QHBoxLayout()
        log_directory_line.setContentsMargins(0, 0, 0, 0)
        log_directory_line.setSpacing(self.ROW_SPACING)
        log_directory_line.addWidget(self.log_directory_edit, 1)
        self.choose_log_directory_button = self._action_button(
            "选择", QStyle.StandardPixmap.SP_DirIcon, self.choose_log_directory
        )
        self.choose_log_directory_button.setToolTip("选择日志目录")
        self.open_log_directory_button = self._action_button(
            "打开", QStyle.StandardPixmap.SP_DirOpenIcon, self.open_log_directory
        )
        self.open_log_directory_button.setToolTip("打开日志目录")
        log_directory_line.addWidget(self.choose_log_directory_button)
        log_directory_line.addWidget(self.open_log_directory_button)
        log_directory_controls = QWidget()
        log_directory_controls.setLayout(log_directory_line)
        directories_layout.addWidget(
            self._field_row("日志目录", log_directory_controls, directory_label_width)
        )
        compile_actions = QHBoxLayout()
        compile_actions.setContentsMargins(0, 0, 0, 0)
        compile_actions.setSpacing(self.ROW_SPACING)
        self.auto_open_checkbox = QCheckBox("编译完成后自动打开输出目录")
        self.auto_open_checkbox.setChecked(True)
        self.open_output_button = self._action_button(
            "打开当前输出目录",
            QStyle.StandardPixmap.SP_DirOpenIcon,
            self.open_output_directory,
        )
        self.open_output_button.setEnabled(False)
        compile_actions.addWidget(self.auto_open_checkbox)
        compile_actions.addStretch(1)
        compile_actions.addWidget(self.open_output_button)
        directories_layout.addLayout(compile_actions)
        self.compile_settings_group = directories
        settings_layout.addWidget(directories, 1)

        backup = QGroupBox("备份设置")
        backup_layout = QVBoxLayout(backup)
        backup_layout.setSpacing(self.ROW_SPACING)
        backup_label_width = self._label_width("备份留存数", "备份目录")
        self.backup_checkbox = QCheckBox("备份编译结果")
        self.backup_checkbox.setChecked(True)
        self.backup_checkbox.toggled.connect(self._update_backup_controls)
        self.backup_retention_edit = QLineEdit("3")
        self.backup_retention_edit.setPlaceholderText("1-100")
        # Keep the old attribute name for callers built against v1.0.
        self.backup_retention_spin = self.backup_retention_edit
        self.backup_directory_edit = QLineEdit(str(self.root / "backup_firmware"))
        backup_directory_line = QHBoxLayout()
        backup_directory_line.setContentsMargins(0, 0, 0, 0)
        backup_directory_line.setSpacing(self.ROW_SPACING)
        backup_directory_line.addWidget(self.backup_directory_edit, 1)
        self.choose_backup_button = self._action_button(
            "选择", QStyle.StandardPixmap.SP_DirIcon, self.choose_backup_directory
        )
        self.choose_backup_button.setToolTip("选择备份目录")
        self.open_backup_button = self._action_button(
            "打开", QStyle.StandardPixmap.SP_DirOpenIcon, self.open_backup_directory
        )
        self.open_backup_button.setToolTip("打开备份目录")
        backup_directory_line.addWidget(self.choose_backup_button)
        backup_directory_line.addWidget(self.open_backup_button)
        backup_controls = QWidget()
        backup_controls.setLayout(backup_directory_line)
        backup_layout.addWidget(self.backup_checkbox)
        backup_layout.addWidget(
            self._field_row("备份留存数", self.backup_retention_spin, backup_label_width)
        )
        backup_layout.addWidget(
            self._field_row("备份目录", backup_controls, backup_label_width)
        )
        self.backup_settings_group = backup
        settings_layout.addWidget(backup, 1)
        self.settings_controls = settings
        self._update_backup_controls(True)
        layout.addWidget(settings)
        return page

    def _project_tab(self) -> QWidget:
        page = QWidget()
        self.project_page = page
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)

        source = QGroupBox("项目来源")
        source_layout = QVBoxLayout(source)
        source_layout.setSpacing(self.ROW_SPACING)
        source_label_width = self._label_width("项目地址", "分支名", "项目名")
        self.repository_edit = QLineEdit()
        self.repository_edit.setPlaceholderText("https://github.com/openwrt/openwrt.git")
        self.branch_edit = QLineEdit("main")
        self.project_name_edit = QLineEdit()
        self.project_name_edit.setPlaceholderText("留空时按仓库名创建")
        self.clone_button = self._action_button(
            "添加并更新 feeds", QStyle.StandardPixmap.SP_DialogApplyButton, self.clone_project
        )
        source_layout.addWidget(
            self._field_row("项目地址", self.repository_edit, source_label_width)
        )
        source_line = QHBoxLayout()
        source_line.setContentsMargins(0, 0, 0, 0)
        source_line.setSpacing(self.ROW_SPACING)
        source_line.addWidget(
            self._inline_segment("分支名", self.branch_edit, source_label_width), 1
        )
        source_line.addWidget(
            self._inline_segment("项目名", self.project_name_edit, source_label_width), 1
        )
        source_line.addWidget(self._button_segment(self.clone_button), 1)
        source_layout.addLayout(source_line)
        layout.addWidget(source)

        build = QGroupBox("构建配置")
        build_layout = QVBoxLayout(build)
        build_layout.setSpacing(self.ROW_SPACING)
        build_label_width = self._label_width(
            "已有项目",
            "平台",
            "设备",
            "主机名",
            "IP 地址",
            "WiFi 账号",
            "WiFi 密码 *",
        )
        self.project_combo = QComboBox()
        self.project_combo.currentIndexChanged.connect(self._selected_project_changed)
        self.platform_edit = QLineEdit()
        self.platform_edit.setPlaceholderText("例如 qualcommax/ipq60xx 或 mediatek_filogic")
        self.devices_edit = QLineEdit()
        self.devices_edit.setPlaceholderText("多个设备以空格分开")
        self.hostname_edit = QLineEdit("OWRT")
        self.ip_edit = QLineEdit("192.168.10.1")
        self.wifi_ssid_edit = QLineEdit("OWRT")
        self.wifi_password_edit = QLineEdit("12345678")
        self.wifi_password_edit.setPlaceholderText("至少 8 位，必填")
        build_layout.addWidget(
            self._field_row("已有项目", self.project_combo, build_label_width)
        )
        build_layout.addWidget(self._field_row("平台", self.platform_edit, build_label_width))
        build_layout.addWidget(self._field_row("设备", self.devices_edit, build_label_width))
        network_line = QHBoxLayout()
        network_line.setContentsMargins(0, 0, 0, 0)
        network_line.setSpacing(self.ROW_SPACING)
        network_line.addWidget(
            self._inline_segment("主机名", self.hostname_edit, build_label_width), 1
        )
        network_line.addWidget(
            self._inline_segment("IP 地址", self.ip_edit, build_label_width), 1
        )
        network_line.addWidget(
            self._inline_segment("WiFi 账号", self.wifi_ssid_edit, build_label_width), 1
        )
        network_line.addWidget(
            self._inline_segment("WiFi 密码 *", self.wifi_password_edit, build_label_width), 1
        )
        build_layout.addLayout(network_line)

        plugin_input = QGroupBox("自定义插件")
        plugin_layout = QVBoxLayout(plugin_input)
        plugin_layout.setSpacing(self.ROW_SPACING)
        plugin_label_width = self._label_width("项目地址", "分支名", "插件名")
        self.plugin_repository_edit = QLineEdit()
        self.plugin_repository_edit.setPlaceholderText("https://github.com/...git")
        self.plugin_branch_edit = QLineEdit("main")
        self.plugin_names_edit = QLineEdit()
        self.plugin_names_edit.setPlaceholderText("插件名，多个以空格分开")
        self.add_plugin_button = self._action_button(
            "添加插件", QStyle.StandardPixmap.SP_DialogApplyButton, self.add_plugin
        )
        plugin_layout.addWidget(
            self._field_row("项目地址", self.plugin_repository_edit, plugin_label_width)
        )
        plugin_line = QHBoxLayout()
        plugin_line.setContentsMargins(0, 0, 0, 0)
        plugin_line.setSpacing(self.ROW_SPACING)
        plugin_line.addWidget(
            self._inline_segment("分支名", self.plugin_branch_edit, plugin_label_width), 1
        )
        plugin_line.addWidget(
            self._inline_segment("插件名", self.plugin_names_edit, plugin_label_width), 1
        )
        plugin_line.addWidget(self._button_segment(self.add_plugin_button), 1)
        plugin_layout.addLayout(plugin_line)
        self.plugin_table = QTableWidget(0, 4)
        self.plugin_table.setHorizontalHeaderLabels(("项目地址", "分支", "插件名", "操作"))
        self.plugin_table.verticalHeader().setVisible(False)
        self.plugin_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.plugin_table.verticalHeader().setDefaultSectionSize(self.CONTROL_HEIGHT)
        plugin_table_height = (
            self.plugin_table.horizontalHeader().sizeHint().height()
            + self.CONTROL_HEIGHT * 3
            + self.plugin_table.frameWidth() * 2
            + 4
        )
        self.plugin_table.setFixedHeight(plugin_table_height)
        self.plugin_sizer = AdaptiveColumnSizer(self.plugin_table, stretch_column=0, fixed_columns=(3,))
        plugin_layout.addWidget(self.plugin_table)
        build_layout.addWidget(plugin_input)

        prebuilt = QGroupBox("预编译软件包")
        prebuilt_layout = QVBoxLayout(prebuilt)
        prebuilt_layout.setSpacing(self.ROW_SPACING)
        prebuilt_header = QHBoxLayout()
        prebuilt_header.setContentsMargins(0, 0, 0, 0)
        prebuilt_header.setSpacing(self.ROW_SPACING)
        prebuilt_header.addWidget(QLabel("已导入软件包"))
        prebuilt_header.addStretch(1)
        self.import_prebuilt_button = self._action_button(
            "导入 IPK/APK",
            QStyle.StandardPixmap.SP_DialogOpenButton,
            self.import_prebuilt_packages,
        )
        self.import_prebuilt_button.setToolTip("导入已编译的 IPK 或 APK")
        prebuilt_header.addWidget(self.import_prebuilt_button)
        prebuilt_layout.addLayout(prebuilt_header)
        self.prebuilt_table = QTableWidget(0, 4)
        self.prebuilt_table.setHorizontalHeaderLabels(("文件名", "格式", "SHA256", "操作"))
        self.prebuilt_table.verticalHeader().setVisible(False)
        self.prebuilt_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.prebuilt_table.setFixedHeight(116)
        self.prebuilt_sizer = AdaptiveColumnSizer(
            self.prebuilt_table, stretch_column=0, fixed_columns=(1, 3)
        )
        prebuilt_layout.addWidget(self.prebuilt_table)
        build_layout.addWidget(prebuilt)

        config_header = QHBoxLayout()
        config_header.setContentsMargins(0, 0, 0, 0)
        config_header.setSpacing(self.ROW_SPACING)
        config_header.addWidget(QLabel("常规配置"))
        self.config_filter_edit = QLineEdit()
        self.config_filter_edit.setPlaceholderText("过滤配置")
        self.config_filter_edit.setClearButtonEnabled(True)
        self.config_filter_edit.textChanged.connect(self._apply_config_filter)
        self.config_filter_result_label = QLabel("0/0")
        config_header.addWidget(self.config_filter_edit, 1)
        config_header.addWidget(self.config_filter_result_label)
        config_header.addStretch(1)
        self.import_config_button = self._action_button(
            "导入配置", QStyle.StandardPixmap.SP_DialogOpenButton, self.import_configuration
        )
        self.import_config_button.setToolTip("导入 OpenWrt 配置")
        config_header.addWidget(self.import_config_button)
        build_layout.addLayout(config_header)
        self.extra_config_edit = QPlainTextEdit()
        self.extra_config_edit.setPlaceholderText("CONFIG_PACKAGE_xxx=y")
        self.extra_config_edit.setFixedHeight(108)
        for editor in (
            self.platform_edit,
            self.devices_edit,
            self.hostname_edit,
            self.ip_edit,
            self.wifi_ssid_edit,
            self.wifi_password_edit,
        ):
            editor.textEdited.connect(self._mark_project_config_modified)
        self.extra_config_edit.textChanged.connect(self._extra_config_changed)
        build_layout.addWidget(self.extra_config_edit)
        controls = QHBoxLayout()
        controls.setSpacing(self.ROW_SPACING)
        self.script_path_edit = QLineEdit()
        self.script_path_edit.setReadOnly(True)
        self.script_path_edit.setPlaceholderText("未选择自定义脚本")
        self.choose_script_button = self._action_button(
            "选择脚本", QStyle.StandardPixmap.SP_DialogOpenButton, self.choose_build_script
        )
        self.clear_script_button = self._action_button(
            "清除脚本", QStyle.StandardPixmap.SP_DialogCloseButton, self.clear_build_script
        )
        self.clear_script_button.setEnabled(False)
        self.build_button = self._action_button(
            "修改并开始编译", QStyle.StandardPixmap.SP_DialogApplyButton, self.start_build
        )
        controls.addWidget(self.choose_script_button)
        controls.addWidget(self.script_path_edit, 1)
        controls.addWidget(self.clear_script_button)
        controls.addStretch(1)
        controls.addWidget(self.build_button)
        build_layout.addLayout(controls)
        layout.addWidget(build, 1)
        self.build_group = build
        scroll.setWidget(content)
        page_layout.addWidget(scroll)
        return page

    def _toolchain_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        tools = QGroupBox("工具链")
        form = QVBoxLayout(tools)
        form.setSpacing(self.ROW_SPACING)
        self.toolchain_combo = QComboBox()
        self.save_toolchain_button = self._action_button(
            "保存当前工具链", QStyle.StandardPixmap.SP_DialogSaveButton, self.save_toolchain
        )
        self.apply_toolchain_button = self._action_button(
            "应用选定工具链", QStyle.StandardPixmap.SP_DialogApplyButton, self.apply_toolchain
        )
        form.addWidget(self._field_row("已保存工具链", self.toolchain_combo, self._label_width("已保存工具链")))
        action_layout = QHBoxLayout()
        action_layout.setSpacing(self.ROW_SPACING)
        action_layout.addWidget(self.save_toolchain_button)
        action_layout.addWidget(self.apply_toolchain_button)
        action_layout.addStretch(1)
        form.addLayout(action_layout)
        layout.addWidget(tools)
        layout.addWidget(QLabel("实时日志"))
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.document().setMaximumBlockCount(5000)
        self.log_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.log_edit, 1)
        return page

    def _set_workflow_enabled(self) -> None:
        ready = bool(self.report and self.report.ready)
        busy = self.worker is not None or self.install_running
        has_project = self.current_project is not None
        project_enabled = ready and not busy
        if self.project_page is not None:
            self.project_page.setEnabled(project_enabled)
        self.settings_controls.setEnabled(not busy)
        self.clone_button.setEnabled(project_enabled)
        self.build_group.setEnabled(project_enabled and has_project)
        self.save_toolchain_button.setEnabled(ready and not busy and has_project)
        self.apply_toolchain_button.setEnabled(ready and not busy and has_project and self.toolchain_combo.count() > 0)
        self.open_output_button.setEnabled(
            not busy and bool(self.output_directory and self.output_directory.is_dir())
        )
        self.check_environment_button.setEnabled(not busy)
        self.install_dependencies_button.setEnabled(not busy)
        self.cancel_button.setEnabled(self.worker is not None)

    def _start_job(self, job: Job, on_success: Callable[[Any], None]) -> None:
        if self.worker is not None:
            return
        worker = OperationWorker(job, self)
        self.worker = worker
        worker.log.connect(self.append_log)
        worker.step.connect(self.set_step)
        worker.final_config_started.connect(self.start_elapsed_timer)
        worker.completed.connect(
            lambda value, active_worker=worker: self._job_completed(
                active_worker, value, on_success
            )
        )
        worker.failed.connect(
            lambda message, active_worker=worker: self._job_failed(active_worker, message)
        )
        self.progress.setRange(0, 0)
        self._set_workflow_enabled()
        worker.start()

    def _release_worker(self, worker: OperationWorker) -> None:
        worker.wait()
        if self.worker is worker:
            self.worker = None
        worker.deleteLater()

    def _job_completed(
        self,
        worker: OperationWorker,
        value: Any,
        on_success: Callable[[Any], None],
    ) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self._release_worker(worker)
        on_success(value)
        self._set_workflow_enabled()

    def _job_failed(self, worker: OperationWorker, message: str) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self._release_worker(worker)
        if self.final_started_at is not None:
            self.elapsed_timer.stop()
            self._update_elapsed()
        self.set_step("操作失败" if message != "操作已取消。" else "操作已取消")
        self.append_log(f"[失败] {message}")
        self._set_workflow_enabled()
        QMessageBox.warning(self, "操作未完成", message)

    def append_log(self, message: str) -> None:
        self.log_edit.appendPlainText(timestamp_log_text(message))
        self.log_edit.verticalScrollBar().setValue(self.log_edit.verticalScrollBar().maximum())

    def set_step(self, step: str) -> None:
        self.step_label.setText(f"当前步骤：{step}")

    def check_environment(self) -> None:
        def job(log: Callable[[str], None], _step: Callable[[str], None], _final: Callable[[], None], _cancelled: Callable[[], bool]) -> EnvironmentReport:
            log("[检测] 正在检查依赖、权限和网络连通性。")
            return EnvironmentProbe(self.root, log, _cancelled).inspect()

        def complete(report: EnvironmentReport) -> None:
            self._display_environment_report(report)

        self._start_job(job, complete)

    def _display_environment_report(self, report: EnvironmentReport) -> None:
        self.report = report
        self.environment_table.setRowCount(0)
        for check in report.checks:
            row = self.environment_table.rowCount()
            self.environment_table.insertRow(row)
            self.environment_table.setItem(row, 0, QTableWidgetItem(check.name))
            status_item = QTableWidgetItem("通过" if check.ok else "失败")
            status_item.setForeground(
                QBrush(QColor("#15803d" if check.ok else "#c62828"))
            )
            status_font = status_item.font()
            status_font.setBold(True)
            status_item.setFont(status_font)
            self.environment_table.setItem(row, 1, status_item)
            self.environment_table.setItem(row, 2, QTableWidgetItem(check.detail))
        self.set_step("环境就绪" if report.ready else "环境未通过")
        self.append_log(
            "[检测] 环境全部通过。"
            if report.ready
            else "[检测] 存在未通过项，后续操作已禁用。"
        )

    def install_dependencies(self) -> None:
        script = self.root / "support" / "install.sh"
        if not script.is_file():
            QMessageBox.warning(self, "无法安装", f"未找到安装脚本：{script}")
            return
        status = self.root / f".install-{os.getpid()}.status"
        try:
            status.unlink(missing_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "无法安装", f"无法清理安装状态文件：{exc}")
            return
        terminals = (
            ("x-terminal-emulator", "-e", "sh", str(script), str(status)),
            ("gnome-terminal", "--wait", "--", "sh", str(script), str(status)),
            ("konsole", "-e", "sh", str(script), str(status)),
        )
        for command in terminals:
            try:
                process = subprocess.Popen(command, cwd=self.root)
            except OSError:
                continue
            self.install_running = True
            self.install_status_path = status
            self.install_process = process
            self.install_timer.start()
            self.set_step("安装依赖")
            self.append_log("[安装] 已在终端启动依赖安装，完成后将自动重新检测。")
            self._set_workflow_enabled()
            return
        QMessageBox.information(self, "手动安装", f"请在终端运行：\nsh {script}")

    def _poll_installation(self) -> None:
        path = self.install_status_path
        if path is None:
            return
        if not path.is_file():
            if self.install_process is None or self.install_process.poll() is None:
                return
            result = str(self.install_process.returncode)
            self._finish_installation(result, marker_exists=False)
            return
        try:
            result = path.read_text(encoding="ascii").strip()
        except OSError:
            return
        self._finish_installation(result, marker_exists=True)

    def _finish_installation(self, result: str, marker_exists: bool) -> None:
        if marker_exists and self.install_status_path is not None:
            try:
                self.install_status_path.unlink(missing_ok=True)
            except OSError:
                pass
        self.install_timer.stop()
        self.install_status_path = None
        self.install_process = None
        self.install_running = False
        self.append_log("[安装] 依赖安装完成，正在自动检测。" if result == "0" else f"[安装] 安装进程退出代码：{result}")
        self._set_workflow_enabled()
        self.check_environment()

    def _refresh_projects(self) -> None:
        selected = self.project_combo.currentData()
        try:
            projects = Workflow(self.root, source_root=self._compile_root()).list_projects()
        except (OSError, WorkflowError) as exc:
            self.append_log(f"[项目] 无法读取编译目录：{exc}")
            projects = []
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        for project in projects:
            self.project_combo.addItem(f"{project.name} ({project.branch})", project)
        index = self.project_combo.findData(selected) if selected else 0
        if index >= 0:
            self.project_combo.setCurrentIndex(index)
        self.project_combo.blockSignals(False)
        self._selected_project_changed()

    def _selected_project_changed(self) -> None:
        project = self.project_combo.currentData()
        self.current_project = project if isinstance(project, ProjectSpec) else None
        self.custom_script = None
        self.script_path_edit.clear()
        self.clear_script_button.setEnabled(False)
        self.output_directory = self._existing_output_directory(self.current_project)
        self._project_config_loaded = False
        self._config_override_notified = False
        self._project_config_path = None
        if self.current_project is not None:
            try:
                imported = load_complete_project_config(
                    self.current_project.directory,
                    self._forced_config(),
                )
            except ValidationError as exc:
                self.append_log(f"[配置] 无法读取强制配置清单：{exc}")
                imported = None
            if imported is not None:
                self._config_loading = True
                try:
                    self._apply_imported_config(imported)
                finally:
                    self._config_loading = False
                self._project_config_loaded = True
                self._project_config_path = self.current_project.directory / ".config"
                self.set_step("已加载项目完整配置")
                self.append_log(f"[配置] 已优先加载项目完整 .config：{self._project_config_path}")
        self._set_workflow_enabled()

    def _existing_output_directory(self, project: ProjectSpec | None) -> Path | None:
        if project is None:
            return None
        output = project.directory / "bin" / "targets"
        if not output.is_dir():
            return None
        try:
            has_image = any(
                path.is_file() and path.suffix.lower() in self.FIRMWARE_SUFFIXES
                for path in output.rglob("*")
            )
        except OSError as exc:
            self.append_log(f"[输出] 无法检查已有镜像产物：{exc}")
            return None
        return output if has_image else None

    def _mark_project_config_modified(self) -> None:
        if self._config_loading or not self._project_config_loaded:
            return
        self._project_config_loaded = False
        if self._config_override_notified:
            return
        self._config_override_notified = True
        path = self._project_config_path or Path(".config")
        message = (
            f"已自动加载项目完整配置：{path}\n"
            "当前手填、插件修改或导入的新配置将覆盖自动加载的配置，"
            "编译时会按当前界面内容重新生成配置。"
        )
        self.append_log("[配置] 已修改自动加载的完整 .config，后续编译将采用当前界面配置。")
        QMessageBox.information(self, "配置已修改", message)

    def _extra_config_changed(self) -> None:
        if self._config_filter_updating or self._active_config_filter:
            return
        self._extra_config_full_text = self.extra_config_edit.toPlainText()
        total = len(self._extra_config_full_text.splitlines())
        self.config_filter_result_label.setText(f"{total}/{total}")
        self._mark_project_config_modified()

    def _apply_config_filter(self, text: str) -> None:
        if self._config_filter_updating:
            return
        if not self._active_config_filter:
            self._extra_config_full_text = self.extra_config_edit.toPlainText()
        self._active_config_filter = text.strip().lower()
        self._render_config_filter()

    def _render_config_filter(self) -> None:
        lines = self._extra_config_full_text.splitlines()
        if self._active_config_filter:
            visible = [line for line in lines if self._active_config_filter in line.lower()]
            displayed = "\n".join(visible)
            read_only = True
        else:
            visible = lines
            displayed = self._extra_config_full_text
            read_only = False
        self._config_filter_updating = True
        try:
            self.extra_config_edit.setReadOnly(read_only)
            self.extra_config_edit.setPlainText(displayed)
        finally:
            self._config_filter_updating = False
        self.config_filter_result_label.setText(f"{len(visible)}/{len(lines)}")

    def _set_extra_config_text(self, text: str) -> None:
        self._extra_config_full_text = text
        self._render_config_filter()

    def _current_extra_config(self) -> str:
        if self._active_config_filter:
            return self._extra_config_full_text
        return self.extra_config_edit.toPlainText()

    def _compile_root(self) -> Path:
        text = self.compile_directory_edit.text().strip()
        return Path(text).expanduser().resolve() if text else (self.root / "projects").resolve()

    def choose_build_script(self) -> None:
        if self.current_project is None:
            QMessageBox.warning(self, "未选择项目", "请先选择已有项目，再选择自定义脚本。")
            return
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "选择自定义脚本",
            str(self.current_project.directory),
            "Shell 脚本 (*.sh);;所有文件 (*)",
        )
        if not selected:
            return
        source = Path(selected)
        project = self.current_project

        def job(
            log: Callable[[str], None],
            step: Callable[[str], None],
            final: Callable[[], None],
            cancelled: Callable[[], bool],
        ) -> ScriptSpec:
            del final
            step("准备自定义脚本")
            return Workflow(
                self.root,
                log,
                step,
                cancelled,
                log_root=self._log_root(),
            ).stage_build_script(project, source)

        def complete(script: ScriptSpec) -> None:
            self.custom_script = script
            self.script_path_edit.setText(str(source))
            self.script_path_edit.setToolTip(script.filename)
            self.clear_script_button.setEnabled(True)
            self._mark_project_config_modified()
            self.set_step("自定义脚本已就绪")
            message = (
                "脚本已转换为 Linux 换行格式，并通过 bash 语法校验和执行权限设置。\n"
                "点击“修改并开始编译”后，脚本将在配置校验前执行。"
            )
            self.append_log("[脚本] 已就绪，将在点击开始编译后执行。")
            QMessageBox.information(self, "脚本已就绪", message)

        self._start_job(job, complete)

    def clear_build_script(self) -> None:
        if self.custom_script is None:
            return
        self.custom_script = None
        self.script_path_edit.clear()
        self.script_path_edit.setToolTip("")
        self.clear_script_button.setEnabled(False)
        self._mark_project_config_modified()
        self.append_log("[脚本] 已取消本次自定义脚本。")

    def clone_project(self) -> None:
        repository = self.repository_edit.text()
        branch = self.branch_edit.text()
        name = self.project_name_edit.text()
        try:
            require_repository(repository)
            require_branch(branch)
            source_name(repository) if not name.strip() else require_component(name, "项目名")
        except ValidationError as exc:
            QMessageBox.warning(self, "输入无效", str(exc))
            return
        compile_root = self._compile_root()
        if compile_root.exists() and not compile_root.is_dir():
            QMessageBox.warning(self, "目录无效", f"编译目录不是文件夹：{compile_root}")
            return

        def job(log: Callable[[str], None], step: Callable[[str], None], final: Callable[[], None], cancelled: Callable[[], bool]) -> ProjectSpec:
            del final
            return Workflow(
                self.root,
                log,
                step,
                cancelled,
                source_root=compile_root,
                log_root=self._log_root(),
            ).clone_project(repository, branch, name)

        def complete(project: ProjectSpec) -> None:
            self._refresh_projects()
            index = self.project_combo.findData(project)
            if index >= 0:
                self.project_combo.setCurrentIndex(index)
            self.set_step("feeds 更新完成")
            self.tabs.setCurrentIndex(1)

        self._start_job(job, complete)

    def _plugin_specs(self) -> tuple[PluginSpec, ...]:
        plugins: list[PluginSpec] = []
        for row in range(self.plugin_table.rowCount()):
            repository = self.plugin_table.item(row, 0).text()
            branch = self.plugin_table.item(row, 1).text()
            names = tuple(self.plugin_table.item(row, 2).text().split())
            plugins.append(PluginSpec(repository, branch, names))
        return tuple(plugins)

    def _prebuilt_specs(self) -> tuple[PrebuiltPackageSpec, ...]:
        packages: list[PrebuiltPackageSpec] = []
        for row in range(self.prebuilt_table.rowCount()):
            filename = self.prebuilt_table.item(row, 0).text()
            digest = self.prebuilt_table.item(row, 2).text()
            packages.append(PrebuiltPackageSpec(filename, digest))
        return tuple(packages)

    def add_plugin(self) -> None:
        repository = self.plugin_repository_edit.text().strip()
        branch = self.plugin_branch_edit.text().strip()
        names = tuple(self.plugin_names_edit.text().split())
        if not repository or not branch or not names:
            QMessageBox.warning(self, "插件信息不完整", "请填写插件项目地址、分支和插件名。")
            return
        try:
            repository = require_repository(repository, "插件项目地址")
            branch = require_branch(branch, "插件分支")
            names = tuple(require_component(name, "插件名") for name in names)
            if len(set(names)) != len(names):
                raise ValidationError("同一插件项目中的插件名不能重复。")
        except ValidationError as exc:
            QMessageBox.warning(self, "插件信息无效", str(exc))
            return
        self._append_plugin(plugin=PluginSpec(repository, branch, names))
        self._mark_project_config_modified()
        self.plugin_repository_edit.clear()
        self.plugin_names_edit.clear()

    def _append_plugin(self, plugin: PluginSpec) -> None:
        row = self.plugin_table.rowCount()
        self.plugin_table.insertRow(row)
        self.plugin_table.setItem(row, 0, QTableWidgetItem(plugin.repository))
        self.plugin_table.setItem(row, 1, QTableWidgetItem(plugin.branch))
        self.plugin_table.setItem(row, 2, QTableWidgetItem(" ".join(plugin.package_names)))
        remove = QPushButton("移除")
        remove.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogCloseButton))
        remove.setFixedHeight(self.CONTROL_HEIGHT)
        remove.setToolTip("移除插件")
        remove.clicked.connect(self.remove_plugin)
        self.plugin_table.setCellWidget(row, 3, remove)
        self.plugin_sizer.schedule()

    def remove_plugin(self) -> None:
        button = self.sender()
        for row in range(self.plugin_table.rowCount()):
            if self.plugin_table.cellWidget(row, 3) is button:
                self.plugin_table.removeRow(row)
                self.plugin_sizer.schedule()
                self._mark_project_config_modified()
                return

    def import_prebuilt_packages(self) -> None:
        if self.current_project is None:
            QMessageBox.warning(self, "未选择项目", "请先选择已有项目，再导入预编译软件包。")
            return
        dialog = QFileDialog(self, "导入预编译软件包", str(self.current_project.directory))
        dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setNameFilters(("预编译软件包 (*.ipk *.apk)", "所有文件 (*)"))
        if dialog.exec() != QFileDialog.DialogCode.Accepted:
            return
        selected = [Path(path) for path in dialog.selectedFiles()]
        if not selected:
            return
        project = self.current_project

        def job(
            log: Callable[[str], None],
            step: Callable[[str], None],
            final: Callable[[], None],
            cancelled: Callable[[], bool],
        ) -> tuple[PrebuiltPackageSpec, ...]:
            del final
            workflow = Workflow(
                self.root,
                log,
                step,
                cancelled,
                toolchain_root=self._toolchain_root(),
                log_root=self._log_root(),
            )
            packages: list[PrebuiltPackageSpec] = []
            for index, source in enumerate(selected, 1):
                step(f"导入预编译软件包 {index}/{len(selected)}")
                packages.append(workflow.stage_prebuilt_package(project, source))
            return tuple(packages)

        def complete(packages: tuple[PrebuiltPackageSpec, ...]) -> None:
            existing = {package.sha256 for package in self._prebuilt_specs()}
            for package in packages:
                if package.sha256 not in existing:
                    self._append_prebuilt(package)
                    existing.add(package.sha256)
            self._mark_project_config_modified()
            self.set_step("预编译软件包导入完成")

        self._start_job(job, complete)

    def _append_prebuilt(self, package: PrebuiltPackageSpec) -> None:
        row = self.prebuilt_table.rowCount()
        self.prebuilt_table.insertRow(row)
        self.prebuilt_table.setItem(row, 0, QTableWidgetItem(package.filename))
        self.prebuilt_table.setItem(row, 1, QTableWidgetItem(Path(package.filename).suffix.upper()[1:]))
        self.prebuilt_table.setItem(row, 2, QTableWidgetItem(package.sha256))
        remove = QPushButton("移除")
        remove.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogCloseButton))
        remove.setFixedHeight(self.CONTROL_HEIGHT)
        remove.setToolTip("移除预编译软件包")
        remove.clicked.connect(self.remove_prebuilt)
        self.prebuilt_table.setCellWidget(row, 3, remove)
        self.prebuilt_sizer.schedule()

    def remove_prebuilt(self) -> None:
        button = self.sender()
        for row in range(self.prebuilt_table.rowCount()):
            if self.prebuilt_table.cellWidget(row, 3) is button:
                self.prebuilt_table.removeRow(row)
                self.prebuilt_sizer.schedule()
                self._mark_project_config_modified()
                return

    def import_configuration(self) -> None:
        initial = self.current_project.directory if self.current_project else self._compile_root()
        dialog = QFileDialog(self, "导入配置", str(initial))
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setFilter(QDir.Filter.AllEntries | QDir.Filter.Hidden | QDir.Filter.NoDotAndDotDot)
        dialog.setNameFilters(
            (
                "OpenWrt 配置 (.config *.config *.txt)",
                "所有文件 (*)",
            )
        )
        if dialog.exec() != QFileDialog.DialogCode.Accepted:
            return
        selected = dialog.selectedFiles()
        if not selected:
            return
        path = Path(selected[0])
        try:
            imported = load_build_config(path, self._forced_config())
        except (ConfigurationError, ValidationError) as exc:
            QMessageBox.warning(self, "配置导入失败", str(exc))
            return
        self._mark_project_config_modified()
        self._config_loading = True
        try:
            self._apply_imported_config(imported)
        finally:
            self._config_loading = False
        self.set_step("配置导入完成")
        detail = f"；工具元数据：{imported.metadata_path}" if imported.metadata_path else ""
        self.append_log(f"[导入] 已读取配置：{path}{detail}")

    def _apply_imported_config(self, imported: ImportedBuildConfig) -> None:
        previous_loading = self._config_loading
        self._config_loading = True
        try:
            self._apply_imported_config_fields(imported)
        finally:
            self._config_loading = previous_loading

    def _apply_imported_config_fields(self, imported: ImportedBuildConfig) -> None:
        self.platform_edit.setText(imported.platform)
        self.devices_edit.setText(" ".join(imported.devices))
        self._set_extra_config_text(imported.extra_config)
        for editor, value in (
            (self.hostname_edit, imported.hostname),
            (self.ip_edit, imported.ip_address),
            (self.wifi_ssid_edit, imported.wifi_ssid),
            (self.wifi_password_edit, imported.wifi_password),
        ):
            if value is not None:
                editor.setText(value)
        if imported.plugins is not None:
            self.plugin_table.setRowCount(0)
            for plugin in imported.plugins:
                self._append_plugin(plugin)
        if imported.prebuilt_packages is not None:
            self.prebuilt_table.setRowCount(0)
            for package in imported.prebuilt_packages:
                self._append_prebuilt(package)
        if imported.custom_script is not None:
            self.custom_script = imported.custom_script
            self.script_path_edit.setText(imported.custom_script.filename)
            self.script_path_edit.setToolTip(imported.custom_script.filename)
            self.clear_script_button.setEnabled(True)
        if imported.backup_enabled is not None:
            self.backup_checkbox.setChecked(imported.backup_enabled)
        if imported.backup_retention is not None:
            self.backup_retention_edit.setText(str(imported.backup_retention))
        if imported.backup_directory is not None:
            self.backup_directory_edit.setText(str(imported.backup_directory))

    def _choose_directory(self, title: str, editor: QLineEdit) -> None:
        initial = editor.text().strip() or str(self.root)
        selected = QFileDialog.getExistingDirectory(self, title, initial)
        if selected:
            editor.setText(selected)

    def _open_directory(self, path: Path) -> None:
        if not path.is_dir():
            QMessageBox.warning(self, "目录不存在", str(path))
            return
        try:
            open_directory(path)
        except OSError as exc:
            QMessageBox.warning(self, "无法打开目录", str(exc))

    def choose_compile_directory(self) -> None:
        self._choose_directory("选择编译目录", self.compile_directory_edit)
        self._refresh_projects()

    def _toolchain_root(self) -> Path:
        text = self.toolchain_directory_edit.text().strip()
        return Path(text).expanduser().resolve() if text else (self.root / "toolchains").resolve()

    def _log_root(self) -> Path:
        text = self.log_directory_edit.text().strip()
        return Path(text).expanduser().resolve() if text else (self.root / "logs").resolve()

    def choose_toolchain_directory(self) -> None:
        self._choose_directory("选择工具链保存目录", self.toolchain_directory_edit)
        self._refresh_toolchains()

    def open_toolchain_directory(self) -> None:
        path = self._toolchain_root()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "无法创建目录", str(exc))
            return
        self._open_directory(path)

    def choose_log_directory(self) -> None:
        self._choose_directory("选择日志目录", self.log_directory_edit)

    def open_log_directory(self) -> None:
        path = self._log_root()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "无法创建目录", str(exc))
            return
        self._open_directory(path)

    def open_compile_directory(self) -> None:
        path = self._compile_root()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "无法创建目录", str(exc))
            return
        self._open_directory(path)

    def choose_backup_directory(self) -> None:
        self._choose_directory("选择固件备份目录", self.backup_directory_edit)

    def open_backup_directory(self) -> None:
        path = Path(self.backup_directory_edit.text()).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "无法创建目录", str(exc))
            return
        self._open_directory(path)

    def _update_backup_controls(self, enabled: bool) -> None:
        for control in (
            self.backup_retention_edit,
            self.backup_directory_edit,
            self.choose_backup_button,
            self.open_backup_button,
        ):
            control.setEnabled(enabled)

    def _build_spec(self) -> BuildSpec:
        backup_text = self.backup_directory_edit.text().strip()
        retention_text = self.backup_retention_edit.text().strip()
        if self.backup_checkbox.isChecked():
            try:
                backup_retention = int(retention_text)
            except ValueError as exc:
                raise ValidationError("备份留存数必须是 1-100 的整数。") from exc
            if not 1 <= backup_retention <= 100:
                raise ValidationError("备份留存数必须是 1-100 的整数。")
        else:
            backup_retention = 3
        return BuildSpec(
            platform=self.platform_edit.text(),
            devices=tuple(self.devices_edit.text().split()),
            hostname=self.hostname_edit.text(),
            ip_address=self.ip_edit.text(),
            wifi_ssid=self.wifi_ssid_edit.text(),
            wifi_password=self.wifi_password_edit.text(),
            extra_config=self._current_extra_config(),
            plugins=self._plugin_specs(),
            prebuilt_packages=self._prebuilt_specs(),
            custom_script=self.custom_script,
            backup_enabled=self.backup_checkbox.isChecked(),
            backup_retention=backup_retention,
            backup_directory=Path(backup_text).expanduser() if backup_text else None,
        )

    def _active_project(self) -> ProjectSpec:
        if self.current_project is None:
            raise ValidationError("尚未选择项目。")
        return self.current_project

    def start_build(self) -> None:
        if self.current_project is None:
            return
        try:
            spec = self._build_spec()
            validate_build_spec(spec)
            if spec.backup_enabled and (not spec.backup_directory or not spec.backup_directory.parent.exists()):
                raise ValidationError("固件备份目录的上级目录不存在。")
            project = self._active_project()
            if not project.directory.is_dir():
                raise ValidationError(f"项目源码目录不存在：{project.directory}")
        except (ValidationError, AttributeError) as exc:
            QMessageBox.warning(self, "输入无效", str(exc))
            return
        self._reset_elapsed_timer()

        def job(log: Callable[[str], None], step: Callable[[str], None], final: Callable[[], None], cancelled: Callable[[], bool]) -> tuple[Path, Path | None]:
            workflow = Workflow(
                self.root,
                log,
                step,
                cancelled,
                final,
                toolchain_root=self._toolchain_root(),
                log_root=self._log_root(),
            )
            output = workflow.build(project, spec)
            return output, workflow.backup_output_directory

        def complete(result: tuple[Path, Path | None]) -> None:
            output, backup = result
            self.output_directory = output
            self.last_backup_directory = backup
            self.elapsed_timer.stop()
            self._update_elapsed()
            self.set_step("编译完成")
            self.open_output_button.setEnabled(True)
            if self.auto_open_checkbox.isChecked():
                self.open_output_directory()

        self._start_job(job, complete)

    def save_toolchain(self) -> None:
        if self.current_project is None:
            return
        try:
            platform = platform_key(self.platform_edit.text())
        except ValidationError as exc:
            QMessageBox.warning(self, "平台无效", str(exc))
            return
        try:
            project = self._active_project()
        except ValidationError as exc:
            QMessageBox.warning(self, "编译目录无效", str(exc))
            return

        def job(log: Callable[[str], None], step: Callable[[str], None], final: Callable[[], None], cancelled: Callable[[], bool]) -> Path:
            del final
            return Workflow(
                self.root,
                log,
                step,
                cancelled,
                toolchain_root=self._toolchain_root(),
                log_root=self._log_root(),
            ).save_toolchain(project, platform)

        def complete(_manifest: Path) -> None:
            self._refresh_toolchains()
            self.set_step("工具链已保存")

        self._start_job(job, complete)

    def _refresh_toolchains(self) -> None:
        try:
            manifests = Workflow(self.root, toolchain_root=self._toolchain_root()).list_toolchains()
        except OSError as exc:
            self.append_log(f"[工具链] 无法读取工具链目录：{exc}")
            manifests = []
        self.toolchain_combo.clear()
        for manifest in manifests:
            self.toolchain_combo.addItem(manifest.stem, manifest)
        self._set_workflow_enabled()

    def apply_toolchain(self) -> None:
        if self.current_project is None:
            return
        manifest = self.toolchain_combo.currentData()
        if not isinstance(manifest, Path):
            QMessageBox.warning(self, "未选择工具链", "请选择要应用的工具链。")
            return
        try:
            platform = platform_key(self.platform_edit.text())
        except ValidationError as exc:
            QMessageBox.warning(self, "平台无效", str(exc))
            return
        try:
            project = self._active_project()
        except ValidationError as exc:
            QMessageBox.warning(self, "编译目录无效", str(exc))
            return

        def job(log: Callable[[str], None], step: Callable[[str], None], final: Callable[[], None], cancelled: Callable[[], bool]) -> None:
            del final
            Workflow(
                self.root,
                log,
                step,
                cancelled,
                toolchain_root=self._toolchain_root(),
                log_root=self._log_root(),
            ).apply_toolchain(project, platform, manifest)
            return None

        def complete(_value: None) -> None:
            self.set_step("工具链已应用")

        self._start_job(job, complete)

    def start_elapsed_timer(self) -> None:
        self.final_started_at = datetime.now()
        self.elapsed_label.setVisible(True)
        self.elapsed_timer.start()
        self._update_elapsed()

    def _reset_elapsed_timer(self) -> None:
        self.elapsed_timer.stop()
        self.final_started_at = None
        self.elapsed_label.setText("编译时间：00:00:00")
        self.elapsed_label.setVisible(False)

    def _update_elapsed(self) -> None:
        if self.final_started_at is None:
            return
        total = int((datetime.now() - self.final_started_at).total_seconds())
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        self.elapsed_label.setText(f"编译时间：{hours:02d}:{minutes:02d}:{seconds:02d}")

    def cancel_current_operation(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.cancel_button.setEnabled(False)
            self.append_log("[取消] 已请求取消当前操作。")

    def open_output_directory(self) -> None:
        if self.output_directory and self.output_directory.is_dir():
            self._open_directory(self.output_directory)
        else:
            QMessageBox.warning(self, "没有可打开的输出", "尚未成功完成编译。")

    def show_about(self) -> None:
        QMessageBox.information(
            self,
            "关于",
            f"OpenWrt 本地编译工具 {__version__}\n\n支持 Linux 与 WSL2 环境。",
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        self.startup_timer.stop()
        worker = self.worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            if not worker.wait(5000):
                self.set_step("正在取消当前操作")
                event.ignore()
                return
        self.install_timer.stop()
        event.accept()


def run(root: Path) -> int:
    application = QApplication.instance() or QApplication([])
    application.setApplicationName("OpenWrt 本地编译工具")
    application.setApplicationVersion(__version__)
    window = MainWindow(root)
    window.show()
    return application.exec()
