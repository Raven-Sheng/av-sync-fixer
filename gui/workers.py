"""在 QThread 中调用现有核心，只通过信号返回结果。"""

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.analyzer import analyze
from app.ffmpeg_utils import MediaError, MissingToolError, DEFAULT_OUTPUT_PRESET
from app.fixer import execute_fix, prepare_fix


class MediaWorker(QThread):
    analysis_ready = Signal(object)
    plan_ready = Signal(object)
    result_ready = Signal(object)
    validation_ready = Signal(object)
    progress = Signal(float)
    log = Signal(str)
    failed = Signal(str, str)

    def __init__(self, path: str | Path, mode: str | None = None, parent=None, *, preset: str = DEFAULT_OUTPUT_PRESET):
        super().__init__(parent)
        self.path = path
        self.mode = mode
        self.preset = preset

    def run(self) -> None:
        try:
            self.log.emit("正在分析视频")
            # 开始修复时重新探测，避免使用文件选择后已被修改的旧元数据。
            source = analyze(self.path)
            self.analysis_ready.emit(source)
            if self.mode is not None:
                self.log.emit("选择修复策略")
                plan = prepare_fix(source, mode=self.mode, preset=self.preset)
                self.plan_ready.emit(plan)
                result = execute_fix(
                    plan,
                    on_start=lambda *_: self.log.emit("正在转码"),
                    on_progress=self.progress.emit,
                    on_validation=self.validation_ready.emit,
                )
                self.result_ready.emit(result)
        except MissingToolError as exc:
            title = "未检测到 FFmpeg" if exc.name == "ffmpeg" else f"未检测到 {exc.name}"
            self.failed.emit(title, str(exc))
        except MediaError as exc:
            self.failed.emit("处理失败", str(exc))
        except Exception as exc:
            # QThread 中未捕获异常不会回到窗口；必须通知 UI 恢复操作状态。
            self.failed.emit("处理失败", f"{type(exc).__name__}：{exc}")
