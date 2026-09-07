"""单窗口桌面界面。所有耗时操作由 MediaWorker 调用核心完成。"""

from pathlib import Path
import sys

from PySide6.QtCore import Qt, QUrl, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from app.analyzer import diagnose
from app.ffmpeg_utils import progress_percent, DEFAULT_OUTPUT_PRESET, OUTPUT_PRESETS
from app.fixer import DEFAULT_REPAIR_MODE, estimate_duration
from app.models import AnalysisResult, FixPlan, FixResult, OutputValidation
from app.presets import PRESET_LABELS, format_validation_report
from gui.workers import MediaWorker


MODES = (("自动", "safe"), ("CFR", "cfr"), ("时间戳", "timestamp"), ("音频同步", "audio-sync"))
VIDEO_FILTER = "视频文件 (*.mp4 *.mkv *.mov *.webm);;所有文件 (*)"
LOG_MAX_LINES = 1000


def seconds_text(value: float | None) -> str:
    return "未知" if value is None else f"{value:.3f} 秒"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.source: AnalysisResult | None = None
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

        summary = QGroupBox("诊断摘要 · 第一条视频 / 音频轨")
        form = QFormLayout(summary)
        form.setVerticalSpacing(8)
        self.fields = {}
        for key, label in (("filename", "文件名"), ("resolution", "分辨率"), ("fps", "帧率"),
                           ("vfr", "帧率模式"), ("video_duration", "视频时长"),
                           ("audio_duration", "音频时长"), ("difference", "长度差"), ("risk", "风险等级")):
            value = QLabel("—")
            value.setTextFormat(Qt.TextFormat.PlainText)
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            value.setMinimumHeight(value.fontMetrics().height())
            form.addRow(label, value)
            self.fields[key] = value
        layout.addWidget(summary)

        mode_row = QHBoxLayout()
        mode_label = QLabel("修复模式")
        self.mode_combo = QComboBox()
        for label, mode in MODES:
            self.mode_combo.addItem(label, mode)
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(DEFAULT_REPAIR_MODE))
        mode_label.setBuddy(self.mode_combo)
        self.mode_combo.setMinimumWidth(140)
        self.mode_combo.currentIndexChanged.connect(self._update_controls)
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

        self.status = QLabel("请选择视频，选择后会自动分析。")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setAccessibleName("修复进度")
        layout.addWidget(self.progress_bar)

        layout.addWidget(QLabel("处理日志"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_MAX_LINES)
        self.log_view.setPlaceholderText("分析结果、策略选择和处理状态会显示在这里。")
        self.log_view.setAccessibleName("处理日志")
        self.log_view.setMinimumHeight(100)
        self.log_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        layout.addWidget(self.log_view, 1)

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
        info = diagnose(source)
        video, audio = info.video, info.audio
        fps = "未知" if info.average_fps is None else f"{info.average_fps:.3f} FPS"
        if info.nominal_fps is not None:
            fps += f"（标称 {info.nominal_fps:.3f} FPS）"
        vfr = "未知（帧率信息不足）" if info.suspected_vfr is None else (
            "疑似 VFR" if info.suspected_vfr else "未发现明显差异（不能据此确认 CFR）")
        values = {
            "filename": source.path.name,
            "resolution": f"{video.width} × {video.height}" if video and video.width and video.height else "未知",
            "fps": fps, "vfr": vfr,
            "video_duration": seconds_text(video.duration) if video else "无视频轨",
            "audio_duration": seconds_text(audio.duration) if audio else "无音频轨",
            "difference": seconds_text(info.duration_diff),
            "risk": f"{info.duration_risk}风险（按轨道长度差）" if info.duration_risk else "未知（信息不足）",
        }
        for key, value in values.items():
            self.fields[key].setText(value)
        if info.suspected_vfr:
            self._append_log("检测到疑似 VFR")
        for message in (*info.potential_causes, *info.limitations):
            self._append_log(message)
        self._append_log("分析完成")
        self.status.setText("分析完成，可以选择模式并开始修复。" if video else "没有可修复的视频轨，请重新选择文件。")

    @Slot(object)
    def _plan_ready(self, plan: FixPlan) -> None:
        labels = dict((mode, label) for label, mode in MODES)
        self._append_log(f"修复模式：{labels[plan.strategy.mode]}")
        self._append_log(f"输出预设：{PRESET_LABELS[plan.preset]}")
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
        self.status.setText("修复完成，编码符合预设；仍有同步风险，请查看验证报告。"
                            if result.validation and result.validation.warnings else "修复完成，输出复查已通过。")
        self._append_log(f"修复完成：{self.output_path}")

    @Slot(object)
    def _validation_ready(self, validation: OutputValidation) -> None:
        self._append_log(format_validation_report(validation))

    @Slot(str)
    def _append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    @Slot(str, str)
    def _show_error(self, title: str, detail: str) -> None:
        self.status.setText(f"{title}，请查看处理日志。")
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

    @Slot()
    def _update_controls(self) -> None:
        idle = self._worker is None
        self.select_button.setEnabled(idle)
        self.mode_combo.setEnabled(idle)
        self.preset_combo.setEnabled(idle)
        repairable = bool(self.source and self.source.videos)
        if self.mode_combo.currentData() == "audio-sync" and not (self.source and self.source.audios):
            repairable = False
        self.fix_button.setEnabled(idle and repairable)
        self.fix_button.setToolTip("音频同步模式需要音频轨。" if self.source and not self.source.audios and self.mode_combo.currentData() == "audio-sync" else "")
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
