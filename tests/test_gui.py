"""在 Qt 事件循环中验证 GUI 状态、后台任务和真实修复流程。"""

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import subprocess
from threading import Event
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtCore import QThread, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.analyzer import parse_analysis
from app.ffmpeg_utils import MediaError, find_tool, progress_seconds
from app.fixer import prepare_fix
from app.models import FixResult
from gui.main_window import MainWindow
from gui.workers import MediaWorker


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication(["av-sync-fixer-tests"])


def wait_until(qt_app, condition, timeout=10):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        qt_app.processEvents()
        QTest.qWait(10)
    assert condition(), "Qt 任务未在时限内完成"
    qt_app.processEvents()


@pytest.fixture
def window(qt_app):
    instance = MainWindow()
    instance.show()
    qt_app.processEvents()
    yield instance
    if instance._worker is not None:
        assert instance._worker.wait(10000)
        qt_app.processEvents()
    instance.close()
    instance.deleteLater()
    qt_app.processEvents()


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "中文 录屏.mp4"
    path.write_bytes(b"input")
    return parse_analysis(path, {
        "format": {"duration": "10"},
        "streams": [
            {"codec_type": "video", "index": 0, "codec_name": "h264", "width": 1920, "height": 1080,
             "avg_frame_rate": "29", "r_frame_rate": "30", "duration": "10", "start_time": "0"},
            {"codec_type": "audio", "index": 1, "codec_name": "aac", "duration": "10.6", "start_time": "0"},
        ],
    })


def test_select_file_auto_analyzes_without_blocking_ui(window, qt_app, source, monkeypatch):
    entered, release = Event(), Event()
    def analyze(path):
        assert QThread.currentThread() != qt_app.thread()
        entered.set()
        assert release.wait(5)
        return source
    monkeypatch.setattr("gui.workers.analyze", analyze)
    monkeypatch.setattr("gui.main_window.QFileDialog.getOpenFileName", lambda *args: (str(source.path), ""))
    ticks = []
    timer = QTimer(window)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(10)
    try:
        window.select_button.click()
        wait_until(qt_app, entered.is_set)
        wait_until(qt_app, lambda: len(ticks) >= 2)
        assert not window.fix_button.isEnabled()
        assert not window.select_button.isEnabled()
        assert not window.preset_combo.isEnabled()
        assert not window.close()  # 运行中的 QThread 不能随窗口销毁。
        assert window.isVisible()
    finally:
        release.set()
        timer.stop()
    wait_until(qt_app, lambda: window._worker is None)
    assert window.fields["filename"].text() == source.path.name
    assert window.fields["resolution"].text() == "1920 × 1080"
    assert "疑似 VFR" in window.fields["vfr"].text()
    assert window.fields["difference"].text() == "0.600 秒"
    assert "较高" in window.fields["risk"].text()
    assert window.fix_button.isEnabled()
    assert "检测到疑似 VFR" in window.log_view.toPlainText()


@pytest.mark.parametrize("mode", ["safe", "cfr", "timestamp", "audio-sync"])
def test_gui_passes_mode_to_core_and_publishes_result(window, qt_app, source, tmp_path, monkeypatch, mode):
    monkeypatch.setattr("gui.workers.analyze", lambda path: source)
    monkeypatch.setattr("app.fixer.find_tool", lambda name: "ffmpeg")
    modes = []
    def prepare(info, *, mode, preset):
        modes.append(mode)
        assert preset == "general"
        return prepare_fix(info, tmp_path / "output", mode=mode, preset=preset)
    def execute(plan, *, on_start, on_progress, on_validation):
        assert QThread.currentThread() != qt_app.thread()
        on_start(plan.output_path, plan.strategy.target_fps, list(plan.command))
        on_progress(progress_seconds("frame=1 time=00:00:05.00"))
        on_progress(1000)  # 输出复查完成前，进度不能到 100%。
        plan.output_path.parent.mkdir()
        plan.output_path.write_bytes(b"encoded")
        return FixResult(plan.source, replace(source, path=plan.output_path), plan.strategy.target_fps, plan.strategy)
    monkeypatch.setattr("gui.workers.prepare_fix", prepare)
    monkeypatch.setattr("gui.workers.execute_fix", execute)
    seen = []
    window.progress_bar.valueChanged.connect(seen.append)
    window.load_video(source.path)
    wait_until(qt_app, lambda: window._worker is None)
    window.mode_combo.setCurrentIndex(window.mode_combo.findData(mode))
    window.fix_button.click()
    wait_until(qt_app, lambda: window._worker is None)
    assert modes == [mode]
    assert 50 in seen and 99 in seen
    assert seen[-1] == 100
    assert window.output_path.is_file()
    assert window.open_button.isEnabled()
    logs = window.log_view.toPlainText()
    for text in ("正在分析视频", "选择修复策略", "正在转码", "修复完成"):
        assert text in logs
    urls = []
    monkeypatch.setattr("gui.main_window.QDesktopServices.openUrl", lambda url: urls.append(url) or True)
    window.open_button.click()
    assert Path(urls[0].toLocalFile()) == window.output_path.parent


@pytest.mark.parametrize("streams", [[], [{"codec_type": "video"}], [{"codec_type": "video"}, {"codec_type": "audio"}]])
def test_gui_handles_missing_tracks_and_fields(window, qt_app, source, monkeypatch, streams):
    info = parse_analysis(source.path, {"streams": streams})
    monkeypatch.setattr("gui.workers.analyze", lambda path: info)
    window.load_video(source.path)
    wait_until(qt_app, lambda: window._worker is None)
    assert "未知" in window.fields["risk"].text()
    assert window.fix_button.isEnabled() == bool(info.videos)
    window.mode_combo.setCurrentIndex(window.mode_combo.findData("audio-sync"))
    assert window.fix_button.isEnabled() == bool(info.videos and info.audios)


@pytest.mark.parametrize("missing", ["ffmpeg", "ffprobe"])
def test_missing_tool_shows_recoverable_error(window, qt_app, source, monkeypatch, tmp_path, missing):
    monkeypatch.setattr("app.ffmpeg_utils.shutil.which", lambda name: None if name == missing else name)
    monkeypatch.setattr("app.ffmpeg_utils.LOCAL_BIN", tmp_path / "missing-tools")
    window.load_video(source.path)
    wait_until(qt_app, lambda: window._worker is None)
    assert ("未检测到 FFmpeg" if missing == "ffmpeg" else "未检测到 ffprobe") in window.status.text()
    assert f"未找到 {missing}" in window.log_view.toPlainText()
    assert window.select_button.isEnabled()
    assert not window.fix_button.isEnabled()


@pytest.mark.parametrize("failure", [MediaError("具体转码失败原因"), RuntimeError("后台异常")])
def test_worker_failure_restores_ui(window, qt_app, source, monkeypatch, failure):
    monkeypatch.setattr("gui.workers.analyze", lambda path: source)
    monkeypatch.setattr("app.fixer.find_tool", lambda name: "ffmpeg")
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr("gui.workers.execute_fix", fail)
    window.load_video(source.path)
    wait_until(qt_app, lambda: window._worker is None)
    window.start_repair()
    wait_until(qt_app, lambda: window._worker is None)
    assert "处理失败" in window.status.text()
    assert str(failure) in window.log_view.toPlainText()
    assert window.fix_button.isEnabled()
    assert window.progress_bar.value() == 0
    assert not window.open_button.isEnabled()


def test_failed_reanalysis_discards_stale_diagnosis(window, qt_app, source, monkeypatch):
    monkeypatch.setattr("gui.workers.analyze", lambda path: source)
    window.load_video(source.path)
    wait_until(qt_app, lambda: window._worker is None)
    assert window.fix_button.isEnabled()
    source.path.unlink()
    from app.analyzer import analyze
    monkeypatch.setattr("gui.workers.analyze", analyze)
    def unexpected(*args, **kwargs):
        pytest.fail("重新分析失败后不应启动转码")
    monkeypatch.setattr("gui.workers.execute_fix", unexpected)
    window.start_repair()
    wait_until(qt_app, lambda: window._worker is None)
    assert "文件不存在" in window.log_view.toPlainText()
    assert window.source is None
    assert all(field.text() == "—" for field in window.fields.values())
    assert not window.fix_button.isEnabled()
    assert window.select_button.isEnabled()


def test_extremely_small_duration_does_not_break_gui_progress(window):
    window._total = 1e-310
    window._show_progress(10)
    assert window.progress_bar.value() == 99


def test_repeated_gui_jobs_deliver_on_ui_thread_and_release_workers(window, qt_app, source, monkeypatch):
    worker_threads = []
    def analyze(path):
        worker_threads.append(QThread.currentThread() != qt_app.thread())
        return source
    monkeypatch.setattr("gui.workers.analyze", analyze)
    original_set_text = window.fields["filename"].setText
    ui_threads = []
    def set_text(value):
        if value == source.path.name:
            ui_threads.append(QThread.currentThread() == qt_app.thread())
        original_set_text(value)
    monkeypatch.setattr(window.fields["filename"], "setText", set_text)
    destroyed = []
    for _ in range(5):
        window.load_video(source.path)
        window._worker.destroyed.connect(lambda *_: destroyed.append(True))
        wait_until(qt_app, lambda: window._worker is None)
    wait_until(qt_app, lambda: len(destroyed) == 5)
    assert worker_threads == ui_threads == [True] * 5
    assert not window.findChildren(MediaWorker)


def test_gui_explains_multi_video_export_scope(window, source, tmp_path, monkeypatch):
    monkeypatch.setattr("app.fixer.find_tool", lambda _: "ffmpeg")
    multiple = replace(source, videos=(source.videos[0], replace(source.videos[0], index=2)))
    window._plan_ready(prepare_fix(multiple, tmp_path / "output"))
    assert "其余 1 条视频轨不会写入输出" in window.log_view.toPlainText()


def test_unknown_duration_uses_busy_progress(window, source):
    window._analysis_ready(replace(source, container_duration=None,
                                   videos=(replace(source.videos[0], duration=None),),
                                   audios=(replace(source.audios[0], duration=None),)))
    window.progress_bar.setRange(0, 0)
    window._show_progress(1.2)
    assert window.progress_bar.maximum() == 0
    assert "1.2 秒" in window.status.text()


def test_compact_window_keeps_diagnosis_text_readable(window, qt_app, source):
    window._analysis_ready(source)
    window.resize(620, 640)
    qt_app.processEvents()
    assert all(value.height() >= value.fontMetrics().height() for value in window.fields.values())


@pytest.mark.parametrize("preset", ["general", "bilibili"])
def test_real_gui_analyze_repair_and_existing_output(window, qt_app, tmp_path, monkeypatch, preset):
    try:
        ffmpeg = find_tool("ffmpeg")
        find_tool("ffprobe")
    except MediaError as exc:
        pytest.skip(str(exc))
    path = tmp_path / "GUI 实际录屏 🎬.mp4"
    subprocess.run([
        ffmpeg, "-v", "error", "-nostdin", "-n",
        "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=1",
        "-f", "lavfi", "-i", "sine=sample_rate=44100:duration=1",
        "-c:v", "libx264", "-c:a", "aac", str(path),
    ], capture_output=True, check=True, timeout=30)
    before_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr("app.fixer.OUTPUT_DIR", tmp_path / "output")
    window.load_video(path)
    wait_until(qt_app, lambda: window._worker is None, timeout=30)
    assert window.fields["resolution"].text() == "160 × 90"
    window.preset_combo.setCurrentIndex(window.preset_combo.findData(preset))
    assert window.preset_combo.currentText() == ("Bilibili" if preset == "bilibili" else "通用")
    window.start_repair()
    wait_until(qt_app, lambda: window._worker is None, timeout=30)
    assert "修复完成" in window.status.text()
    assert window.progress_bar.value() == 100
    assert window.output_path.is_file()
    if preset == "bilibili":
        assert "输出验证报告 · Bilibili" in window.log_view.toPlainText()
        assert "采样率 48000 Hz" in window.log_view.toPlainText()
        assert "预设编码要求通过" in window.log_view.toPlainText()
    assert window.preset_combo.isEnabled()
    output = window.output_path
    output_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    window.start_repair()
    wait_until(qt_app, lambda: window._worker is None, timeout=30)
    assert "输出文件已存在" in window.log_view.toPlainText()
    assert hashlib.sha256(output.read_bytes()).hexdigest() == output_hash
    assert not window.open_button.isEnabled()


def test_gui_bilibili_validation_failure_reports_details_and_recovers(window, qt_app, source, tmp_path, monkeypatch):
    monkeypatch.setattr("gui.workers.analyze", lambda path: source)
    monkeypatch.setattr("app.fixer.find_tool", lambda _: "ffmpeg")
    monkeypatch.setattr("app.fixer.OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr("app.fixer.run_ffmpeg", lambda command, callback: Path(command[-1]).write_bytes(b"encoded"))
    monkeypatch.setattr("app.fixer.analyze", lambda path: replace(source, path=path))
    window.load_video(source.path)
    wait_until(qt_app, lambda: window._worker is None)
    window.preset_combo.setCurrentIndex(window.preset_combo.findData("bilibili"))
    window.start_repair()
    wait_until(qt_app, lambda: window._worker is None)
    text = window.log_view.toPlainText()
    for expected in ("输出验证报告", "不符合项", "yuv420p", "48000", "轨道差异"):
        assert expected in text
    assert "处理失败" in window.status.text()
    assert window.preset_combo.isEnabled() and window.fix_button.isEnabled()
    assert window.progress_bar.value() == 0
    assert not window.open_button.isEnabled()
    assert list((tmp_path / "out").iterdir()) == []
