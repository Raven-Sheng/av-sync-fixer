"""单窗口桌面界面。所有耗时操作由 MediaWorker 调用核心完成。"""

from pathlib import Path
import sys

from PySide6.QtCore import Qt, QUrl, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from app.ffmpeg_utils import progress_percent, DEFAULT_OUTPUT_PRESET, OUTPUT_PRESETS
from app.fixer import DEFAULT_REPAIR_MODE, estimate_duration
from app.models import AnalysisResult, FixPlan, FixResult, OutputValidation
from app.media_profile import format_media_profile
from app.compatibility import format_compatibility_report
from app.color import color_plan_text
from app.presets import PRESET_LABELS, format_validation_report
from app.media_summary import (MediaSummary, build_media_summary, comparison_text,
                               processing_text, validation_summary, completion_text, VALIDATION_LABELS)
from app.repair_plan import format_repair_decisions
from gui.workers import MediaWorker


MODES = (("自动", "safe"), ("CFR", "cfr"), ("时间戳", "timestamp"), ("音频同步", "audio-sync"))
VIDEO_FILTER = "视频文件 (*.mp4 *.mkv *.mov *.webm);;所有文件 (*)"
LOG_MAX_LINES = 1000


def display_label(text: str = "—") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    label.setMinimumHeight(label.fontMetrics().height())
    return label


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.source: AnalysisResult | None = None
        self.summary_model: MediaSummary | None = None
        self.output_path: Path | None = None
        self._worker: MediaWorker | None = None
        self._total: float | None = None
        self.setWindowTitle("音画同步修复器 · av-sync-fixer")
        self.resize(820, 760)
        self.setMinimumSize(620, 640)
        body = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        self.setCentralWidget(scroll)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        title = QLabel("音画同步修复器")
        font = title.font()
        font.setPointSize(18)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        hint = QLabel("选择录屏视频，查看同步风险，再选择修复方式。")
        layout.addWidget(hint)

        file_row = QHBoxLayout()
        self.select_button = QPushButton("选择视频…")
        self.select_button.setMinimumHeight(34)
        self.select_button.clicked.connect(self.choose_video)
        self.file_path = QLineEdit()
        self.file_path.setReadOnly(True)
        self.file_path.setPlaceholderText("支持 MP4、MKV、MOV、WebM")
        self.file_path.setAccessibleName("输入视频路径")
        file_row.addWidget(self.select_button)
        file_row.addWidget(self.file_path, 1)
        layout.addLayout(file_row)

        summary = QGroupBox("媒体兼容性摘要 · 第一条视频 / 全部音轨")
        form = QFormLayout(summary)
        form.setVerticalSpacing(8)
        self.origin_label = display_label("选择视频后显示录制来源线索。")
        form.addRow(self.origin_label)
        self.fields = {}
        for key, label in (("video_codec", "视频编码"), ("resolution", "分辨率"), ("fps", "帧率"),
                           ("pixel_format", "像素格式"), ("color", "颜色"), ("audio", "音频")):
            value = display_label()
            form.addRow(label, value)
            self.fields[key] = value
        self.compatibility_label = display_label("等待分析")
        form.addRow("兼容性提示", self.compatibility_label)
        layout.addWidget(summary)

        mode_row = QHBoxLayout()
        mode_label = QLabel("修复模式")
        self.mode_combo = QComboBox()
        for label, mode in MODES:
            self.mode_combo.addItem(label, mode)
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(DEFAULT_REPAIR_MODE))
        mode_label.setBuddy(self.mode_combo)
        self.mode_combo.setMinimumWidth(140)
        preset_label = QLabel("输出预设")
        self.preset_combo = QComboBox()
        for preset in OUTPUT_PRESETS:
            self.preset_combo.addItem(PRESET_LABELS[preset], preset)
        self.preset_combo.setCurrentIndex(self.preset_combo.findData(DEFAULT_OUTPUT_PRESET))
        preset_label.setBuddy(self.preset_combo)
        self.preset_combo.setMinimumWidth(110)
        self.fix_button = QPushButton("开始修复")
        self.fix_button.setMinimumHeight(34)
        self.fix_button.clicked.connect(self.start_repair)
        mode_row.addWidget(mode_label)
        mode_row.addWidget(self.mode_combo)
        mode_row.addWidget(preset_label)
        mode_row.addWidget(self.preset_combo)
        mode_row.addStretch()
        mode_row.addWidget(self.fix_button)
        layout.addLayout(mode_row)

        processing = QGroupBox("建议处理 · 随当前模式与输出预设更新")
        processing_layout = QVBoxLayout(processing)
        self.processing_label = display_label("选择视频后显示处理计划。")
        processing_layout.addWidget(self.processing_label)
        self.why_button = QToolButton()
        self.why_button.setText("为什么需要修复？")
        self.why_button.setCheckable(True)
        self.why_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.why_button.setArrowType(Qt.ArrowType.RightArrow)
        self.why_label = display_label()
        self.why_label.hide()
        self.why_button.toggled.connect(self.why_label.setVisible)
        self.why_button.toggled.connect(lambda checked: self.why_button.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow))
        processing_layout.addWidget(self.why_button)
        processing_layout.addWidget(self.why_label)
        layout.addWidget(processing)

        self.status = QLabel("请选择视频，选择后会自动分析。")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setAccessibleName("修复进度")
        layout.addWidget(self.progress_bar)

        self.result_group = QGroupBox("修复结果")
        result_layout = QVBoxLayout(self.result_group)
        comparison = QHBoxLayout()
        for attribute, title in (("before_label", "修复前 / Before"), ("after_label", "修复后 / After")):
            column = QVBoxLayout()
            column.addWidget(QLabel(title))
            value = display_label()
            setattr(self, attribute, value)
            column.addWidget(value)
            comparison.addLayout(column, 1)
        result_layout.addLayout(comparison)
        validation_form = QFormLayout()
        self.validation_fields = {}
        for name, value in validation_summary(None).items():
            field = display_label(value)
            validation_form.addRow(name, field)
            self.validation_fields[name] = field
        result_layout.addLayout(validation_form)
        result_layout.addWidget(display_label("同步项核对时间信息与计划是否一致；实际音画同步仍需播放确认。"))
        layout.addWidget(self.result_group)
        self.result_group.hide()

        self.details_button = QToolButton()
        self.details_button.setText("技术详情 · 完整媒体画像、时间信息与处理日志")
        self.details_button.setCheckable(True)
        self.details_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_button.setArrowType(Qt.ArrowType.RightArrow)
        layout.addWidget(self.details_button)
        self.details_panel = QWidget()
        details_layout = QVBoxLayout(self.details_panel)
        details_layout.setContentsMargins(0, 0, 0, 0)
        timing = QFormLayout()
        for key, label in (("filename", "文件名"), ("vfr", "帧率模式"), ("video_duration", "视频时长"),
                           ("audio_duration", "首条音频时长"), ("difference", "首条音轨长度差"), ("risk", "长度差风险")):
            value = display_label()
            timing.addRow(label, value)
            self.fields[key] = value
        details_layout.addLayout(timing)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_MAX_LINES)
        self.log_view.setPlaceholderText("分析结果、策略选择和处理状态会显示在这里。")
        self.log_view.setAccessibleName("处理日志")
        self.log_view.setMinimumHeight(180)
        details_layout.addWidget(self.log_view)
        layout.addWidget(self.details_panel)
        self.details_panel.hide()
        self.details_button.toggled.connect(self.details_panel.setVisible)
        self.details_button.toggled.connect(lambda checked: self.details_button.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow))

        output_row = QHBoxLayout()
        self.output_field = QLineEdit()
        self.output_field.setReadOnly(True)
        self.output_field.setPlaceholderText("修复完成后显示输出位置")
        self.output_field.setAccessibleName("输出文件位置")
        self.open_button = QPushButton("打开文件所在目录")
        self.open_button.clicked.connect(self.open_output_directory)
        output_row.addWidget(self.output_field, 1)
        output_row.addWidget(self.open_button)
        layout.addLayout(output_row)
        note = QLabel("诊断依据媒体元数据，实际同步效果仍需播放确认。源文件不会被覆盖。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self._update_controls()
        self.preset_combo.currentIndexChanged.connect(self._show_compatibility)
        self.mode_combo.currentIndexChanged.connect(self._show_compatibility)

    @Slot()
    def choose_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择视频", "", VIDEO_FILTER)
        if path:
            self.load_video(path)

    def load_video(self, path: str | Path) -> None:
        if self._worker is not None:
            return
        self.file_path.setText(str(path))
        self.file_path.setToolTip(str(path))
        self.log_view.clear()
        self._clear_output()
        self._start_worker(MediaWorker(path, parent=self))

    @Slot()
    def start_repair(self) -> None:
        if self._worker is not None or self.source is None or not self.fix_button.isEnabled():
            return
        self._clear_output()
        self._start_worker(MediaWorker(self.source.path, self.mode_combo.currentData(), self,
                                       preset=self.preset_combo.currentData()))

    def _start_worker(self, worker: MediaWorker) -> None:
        self._worker = worker
        # 每次任务都会重新分析，不能在分析失败后继续展示并使用旧诊断。
        self.source = None
        self.summary_model = None
        self.origin_label.setText("正在读取录制来源信息…")
        self.compatibility_label.setText("正在分析…")
        self.processing_label.setText("分析完成后生成处理计划。")
        self.why_label.setText("—")
        self._total = None
        for field in self.fields.values():
            field.setText("—")
        self.progress_bar.setRange(0, 0)
        self.status.setText("正在分析视频…")
        worker.analysis_ready.connect(self._analysis_ready)
        worker.plan_ready.connect(self._plan_ready)
        worker.result_ready.connect(self._repair_finished)
        worker.validation_ready.connect(self._validation_ready)
        worker.progress.connect(self._show_progress)
        worker.log.connect(self._append_log)
        worker.failed.connect(self._show_error)
        worker.finished.connect(self._task_finished)
        self._update_controls()
        worker.start()

    @Slot(object)
    def _analysis_ready(self, source: AnalysisResult) -> None:
        self.source = source
        self._total = estimate_duration(source)
        self._append_log(format_media_profile(source))
        self._show_compatibility()
        info = self.summary_model.sync
        if info.suspected_vfr:
            self._append_log("检测到疑似 VFR")
        for message in (*info.potential_causes, *info.limitations):
            self._append_log(message)
        self._append_log("分析完成")
        self.status.setText("分析完成，请查看当前处理计划。" if self.summary_model.plan else "当前输入或所选模式无法安全生成修复计划，请查看原因。")

    @Slot()
    def _show_compatibility(self) -> None:
        if self.source is not None:
            self.summary_model = build_media_summary(self.source, self.mode_combo.currentData(), self.preset_combo.currentData())
            summary = self.summary_model
            for key, value in summary.fields.items():
                self.fields[key].setText(value)
            self.origin_label.setText(summary.origin)
            visible = summary.compatibility[:3]
            text = "\n".join(visible)
            if len(summary.compatibility) > len(visible):
                text += f"\n另有 {len(summary.compatibility) - len(visible)} 项提示，见技术详情。"
            self.compatibility_label.setText(text)
            self.processing_label.setText("当前无法修复：" + summary.blocked if summary.blocked else "\n".join(summary.processing))
            self.why_label.setText(summary.blocked or "\n\n".join(summary.reasons))
            self._append_log(format_compatibility_report(summary.diagnosis))
            if summary.plan:
                self._append_log(format_repair_decisions(summary.plan))
            if self._worker is None and self.output_path is None:
                self.status.setText("当前计划无法执行，请查看修复原因。" if summary.blocked else "当前处理计划已更新。")
            self._update_controls()

    @Slot(object)
    def _plan_ready(self, plan: FixPlan) -> None:
        if plan.repair:
            actions, reasons = processing_text(plan.repair, plan.source)
            self.processing_label.setText("\n".join(actions))
            self.why_label.setText("\n\n".join(reasons))
            self._append_log(format_repair_decisions(plan.repair))
        labels = dict((mode, label) for label, mode in MODES)
        self._append_log(f"修复模式：{labels[plan.strategy.mode]}")
        self._append_log(f"输出预设：{PRESET_LABELS[plan.preset]}")
        if plan.color:
            self._append_log(color_plan_text(plan.color))
            for warning in plan.color.warnings:
                self._append_log(warning)
        for message in (*plan.strategy.reasons, *plan.strategy.warnings):
            self._append_log(message)
        self._append_log(f"输出位置：{plan.output_path}")
        if plan.strategy.target_fps:
            self._append_log(f"目标帧率：{plan.strategy.target_fps} FPS")
        self.progress_bar.setRange(0, 100 if self._total and self._total > 0 else 0)
        self.progress_bar.setValue(0)
        self.status.setText("正在转码…")

    @Slot(float)
    def _show_progress(self, seconds: float) -> None:
        percent = progress_percent(seconds, self._total)
        if percent is not None:
            self.progress_bar.setValue(max(self.progress_bar.value(), percent))
        self.status.setText(f"正在转码 / 复查输出 · 已处理 {seconds:.1f} 秒")

    @Slot(object)
    def _repair_finished(self, result: FixResult) -> None:
        self.output_path = result.after.path
        self.output_field.setText(str(self.output_path))
        self.output_field.setToolTip(str(self.output_path))
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.before_label.setText(comparison_text(result.before))
        self.after_label.setText(comparison_text(result.after))
        self._display_validation(result.validation)
        preset = PRESET_LABELS.get(result.validation.preset, result.validation.preset) if result.validation else "未提供预设验证"
        self.result_group.setTitle(f"修复结果 · 已生成输出 · {preset}")
        self.result_group.show()
        self.status.setText(completion_text(result.validation))
        self._append_log(f"修复完成：{self.output_path}")

    @Slot(object)
    def _validation_ready(self, validation: OutputValidation) -> None:
        self.before_label.setText(comparison_text(self.source))
        self.after_label.setText(comparison_text(validation.media))
        self._display_validation(validation)
        self.result_group.setTitle("输出复查 · 尚未完成发布")
        self.result_group.show()
        self._append_log(format_validation_report(validation))

    def _display_validation(self, validation: OutputValidation | None) -> None:
        for name, text in validation_summary(validation).items():
            self.validation_fields[name].setText(text)
        for section, name in VALIDATION_LABELS:
            self.validation_fields[name].setToolTip("\n".join(c.message for c in validation.checks if c.section == section) if validation else "无验证证据")

    @Slot(str)
    def _append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    @Slot(str, str)
    def _show_error(self, title: str, detail: str) -> None:
        self.status.setText(f"{title}，请查看处理日志。")
        self.details_button.setChecked(True)
        if not self.result_group.isHidden():
            self.result_group.setTitle("修复未完成 · 输出未发布")
        if self.source is None:
            self.origin_label.setText("未获得媒体信息")
            self.compatibility_label.setText("分析未完成")
            self.processing_label.setText("无法生成计划，请重新选择可读取的视频。")
        self._append_log(f"{title}：{detail}")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

    @Slot()
    def _task_finished(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        if self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
        self._update_controls()

    def _clear_output(self) -> None:
        self.output_path = None
        self.output_field.clear()
        self.output_field.setToolTip("")
        self.result_group.hide()
        self.before_label.setText("—")
        self.after_label.setText("—")
        self._display_validation(None)

    @Slot()
    def _update_controls(self) -> None:
        idle = self._worker is None
        self.select_button.setEnabled(idle)
        self.mode_combo.setEnabled(idle)
        self.preset_combo.setEnabled(idle)
        repairable = bool(self.summary_model and self.summary_model.plan)
        self.fix_button.setEnabled(idle and repairable)
        self.fix_button.setToolTip(self.summary_model.blocked or "" if self.summary_model else "")
        self.open_button.setEnabled(idle and self.output_path is not None)

    @Slot()
    def open_output_directory(self) -> None:
        if self.output_path is not None:
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output_path.parent))):
                self._append_log(f"无法打开目录，请手动访问：{self.output_path.parent}")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker is not None:
            self.status.setText("任务仍在运行，请在完成后关闭窗口。")
            event.ignore()
        else:
            event.accept()


def launch() -> int:
    application = QApplication(sys.argv)
    application.setApplicationName("av-sync-fixer")
    window = MainWindow()
    window.show()
    return application.exec()
