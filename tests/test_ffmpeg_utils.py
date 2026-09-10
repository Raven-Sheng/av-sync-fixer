import json
from pathlib import Path
import subprocess
import sys

import pytest

from app import ffmpeg_utils as utils


@pytest.mark.parametrize("name", ["ffmpeg", "ffprobe"])
def test_missing_tool(name, tmp_path, monkeypatch):
    monkeypatch.setattr(utils.shutil, "which", lambda _: None)
    monkeypatch.setattr(utils, "LOCAL_BIN", tmp_path)
    with pytest.raises(utils.MediaError, match=f"未找到 {name}"):
        utils.find_tool(name)


def test_existing_path_preferred(tmp_path, monkeypatch):
    monkeypatch.setattr(utils.shutil, "which", lambda _: "/existing/ffprobe")
    monkeypatch.setattr(utils, "LOCAL_BIN", tmp_path)
    assert utils.find_tool("ffprobe") == "/existing/ffprobe"


def test_local_tool_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(utils.shutil, "which", lambda _: None)
    monkeypatch.setattr(utils, "LOCAL_BIN", tmp_path)
    executable = tmp_path / ("ffprobe.exe" if utils.os.name == "nt" else "ffprobe")
    executable.write_bytes(b"test")
    executable.chmod(0o755)
    assert utils.find_tool("ffprobe") == str(executable)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 批处理启动规则")
@pytest.mark.parametrize("runner", ["run_command", "run_ffmpeg"])
@pytest.mark.parametrize("extension", [".cmd", ".BAT"])
def test_batch_wrapper_is_rejected_before_process_start(tmp_path, monkeypatch, runner, extension):
    wrapper = tmp_path / f"ffprobe{extension}"
    wrapper.write_text("@echo off\necho wrapper\n", encoding="ascii")
    def unexpected(*args, **kwargs):
        pytest.fail("批处理包装器不能进入进程启动阶段")
    monkeypatch.setattr(utils.subprocess, "Popen", unexpected)
    command = [str(wrapper), str(tmp_path / "input&ver&rem.mp4")]
    with pytest.raises(utils.MediaError, match="不执行批处理包装器"):
        if runner == "run_command":
            utils.run_command(command, 10)
        else:
            utils.run_ffmpeg(command)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 工具发现规则")
@pytest.mark.parametrize("fallback", ["path", "local", "none"])
def test_tool_discovery_uses_native_executable_instead_of_batch(tmp_path, monkeypatch, fallback):
    wrapper = tmp_path / "ffmpeg.cmd"
    native = tmp_path / "ffmpeg.exe"
    monkeypatch.setattr(utils, "LOCAL_BIN", tmp_path)
    monkeypatch.setattr(utils.shutil, "which", lambda name: str(wrapper) if name == "ffmpeg" else (
        str(native) if fallback == "path" else None))
    if fallback == "local":
        native.write_bytes(b"native placeholder")
    if fallback == "none":
        with pytest.raises(utils.MediaError, match="原生 .exe"):
            utils.find_tool("ffmpeg")
    else:
        assert utils.find_tool("ffmpeg") == str(native)


def test_both_tools_are_checked(monkeypatch):
    calls = []
    monkeypatch.setattr(utils, "find_tool", lambda name: name)
    monkeypatch.setattr(utils, "run_command", lambda command, timeout: calls.append(command))
    assert utils.check_tools() == {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"}
    assert calls == [["ffmpeg", "-version"], ["ffprobe", "-version"]]


def test_unicode_space_and_leading_dash_path(tmp_path, monkeypatch):
    media = tmp_path / "-中文 视频.mp4"
    media.write_bytes(b"source unchanged")
    calls = []
    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps({"streams": []}), "")
    monkeypatch.setattr(utils.subprocess, "run", fake_run)
    assert utils.probe_media(media, "ffprobe") == {"streams": []}
    command, kwargs = calls[0]
    assert command[-1] == str(media.resolve())
    assert command == ["ffprobe", "-v", "quiet", "-print_format", "json",
                       "-show_format", "-show_streams", "-show_frames", "-read_intervals", "%+#32",
                       "-show_entries", "frame=stream_index,color_transfer:frame_side_data=side_data_type",
                       str(media.resolve())]
    assert not kwargs.get("shell", False)
    assert kwargs["timeout"] == 60
    assert media.read_bytes() == b"source unchanged"


def test_process_error_keeps_stderr(monkeypatch):
    monkeypatch.setattr(utils.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], 1, "", "Invalid data found"))
    with pytest.raises(utils.MediaError, match="Invalid data found"):
        utils.probe_media(Path("broken.mp4"), "ffprobe")


@pytest.mark.parametrize("error, message", [
    (subprocess.TimeoutExpired("ffprobe", 60), "超时"),
    (OSError("access denied"), "无法运行"),
])
def test_process_exceptions(error, message, monkeypatch):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(utils.subprocess, "run", fail)
    with pytest.raises(utils.MediaError, match=message):
        utils.probe_media(Path("test.mp4"), "ffprobe")


@pytest.mark.parametrize("output", ["not json", "[]", '{"streams": null}', '{"format": []}'])
def test_bad_probe_output(output, monkeypatch):
    monkeypatch.setattr(utils, "run_command", lambda *a, **kw: output)
    with pytest.raises(utils.MediaError, match="无效"):
        utils.probe_media(Path("test.mp4"), "ffprobe")


@pytest.mark.parametrize("line, seconds", [
    ("out_time_us=1500000\n", 1.5), ("out_time_us=-100", 0),
    ("out_time_us=N/A", None), ("out_time_ms=1500000", None), ("progress=end", None),
    ("out_time=01:02:03.500000", 3723.5),
    ("frame= 25 fps=30 time=00:01:02.34 bitrate=120kbits/s", 62.34),
    ("time= 00:00:00.50\r", 0.5), ("time=-00:00:00.05", 0),
    ("time=N/A", None), ("time=00:65:00.00", None),
    ("time=00:00:99", None), ("time=00:00:01junk", None),
    ("time=00:00:nan", None), ("not_time=00:00:01", None),
])
def test_parse_progress(line, seconds):
    assert utils.progress_seconds(line) == seconds


@pytest.mark.parametrize("seconds, total, expected", [
    (0, 10, 0), (5, 10, 50), (10, 10, 99), (11, 10, 99),
    (-1, 10, 0), (10, 1e-310, 99), (-10, 1e-310, 0),
    (1e308, 1e308, 99), (5e307, 1e308, 50),
    (5, None, None), (5, 0, None), (5, -1, None),
    (5, float("inf"), None), (5, float("nan"), None),
    (float("inf"), 10, None), (float("nan"), 10, None),
])
def test_progress_percent_handles_limits_and_invalid_duration(seconds, total, expected):
    assert utils.progress_percent(seconds, total) == expected


def test_runner_drains_stderr_without_blocking():
    progress = []
    code = "import sys; sys.stderr.write('warning\\n' * 100000); print('out_time_us=1500000', flush=True)"
    utils.run_ffmpeg([sys.executable, "-u", "-c", code], progress.append)
    assert progress == [1.5]


def test_runner_reports_nonzero_stderr():
    code = "import sys; sys.stderr.write('specific encoding failure'); sys.exit(7)"
    with pytest.raises(utils.MediaError, match="specific encoding failure"):
        utils.run_ffmpeg([sys.executable, "-u", "-c", code])


@pytest.mark.parametrize("runner", ["run_command", "run_ffmpeg"])
def test_subprocess_flags_and_argument_safety(monkeypatch, tmp_path, runner):
    original_popen = subprocess.Popen
    launches = []
    def launch(*args, **kwargs):
        launches.append((args, kwargs))
        return original_popen(*args, **kwargs)
    monkeypatch.setattr(utils.subprocess, "Popen", launch)
    path_argument = str(tmp_path / "-中文 ‘片段’ & $(literal); [1] 🎬.mp4")
    code = "import sys; assert sys.argv[1] == sys.argv[2]; print('out_time_us=1')"
    command = [sys.executable, "-c", code, path_argument, path_argument]
    if runner == "run_command":
        assert utils.run_command(command, 10).strip() == "out_time_us=1"
    else:
        progress = []
        utils.run_ffmpeg(command, progress.append)
        assert progress == [0.000001]
    args, kwargs = launches[0]
    assert args[0] == command
    assert not kwargs.get("shell", False)
    expected_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    assert kwargs["creationflags"] == expected_flags


def test_runner_cancellation_stops_process(monkeypatch):
    original_popen = subprocess.Popen
    processes = []
    def launch(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(utils.subprocess, "Popen", launch)
    def cancel(seconds):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        utils.run_ffmpeg([sys.executable, "-u", "-c", "import time; print('out_time_us=1', flush=True); time.sleep(60)"], cancel)
    assert processes[0].poll() is not None


@pytest.mark.parametrize("failure, expected", [
    (RuntimeError("can't start new thread"), utils.MediaError),
    (KeyboardInterrupt(), KeyboardInterrupt),
])
def test_runner_cleans_up_when_reader_start_fails(monkeypatch, failure, expected):
    original_popen = subprocess.Popen
    processes = []
    def launch(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process
    def fail_start(self):
        raise failure
    monkeypatch.setattr(utils.subprocess, "Popen", launch)
    monkeypatch.setattr(utils.Thread, "start", fail_start)
    try:
        with pytest.raises(expected):
            utils.run_ffmpeg([sys.executable, "-u", "-c", "import time; time.sleep(60)"])
        assert processes[0].poll() is not None
        assert processes[0].stdout.closed
        assert processes[0].stderr.closed
    finally:
        # 即使回归测试失败，也不让测试自身留下后台进程。
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
