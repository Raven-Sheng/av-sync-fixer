from pathlib import Path
import json
import shlex
import shutil
import subprocess
import sys

import pytest

from app.analyzer import parse_analysis
from app.cli import format_command, format_report, main
from app.ffmpeg_utils import MediaError


def test_report_has_requested_fields():
    result = parse_analysis(Path("中文 视频.mp4"), {"streams": [
        {"codec_type": "video"}, {"codec_type": "audio"}, {"codec_type": "audio", "index": 2},
    ]})
    report = format_report(result)
    for label in ("文件名", "文件大小", "总时长", "容器格式", "视频编码器", "音频编码器", "分辨率",
                  "avg_frame_rate", "r_frame_rate", "视频轨时长", "音频轨时长",
                  "视频 time_base", "音频 time_base", "音频采样率"):
        assert label in report
    assert "音频轨道 #2" in report
    assert "未知" in report
    assert "文件大小：未知" in report
    assert "音频采样率：未知" in report


def test_report_size_and_sample_rate():
    result = parse_analysis(Path("test.mp4"), {
        "format": {"size": "1048576"},
        "streams": [{"codec_type": "audio", "sample_rate": "44100"}],
    })
    report = format_report(result)
    assert "文件名：test.mp4" in report
    assert "文件大小：1048576 字节（1.00 MiB）" in report
    assert "音频采样率：44100 Hz" in report


def test_cli_success(monkeypatch, capsys):
    monkeypatch.setattr("app.cli.analyze", lambda path: parse_analysis(Path(path), {}))
    assert main(["中文 视频.mp4"]) == 0
    output = capsys.readouterr()
    assert "视频轨道：无" in output.out and "音频轨道：无" in output.out
    assert not output.err


def test_cli_readable_error(monkeypatch, capsys):
    def fail(path):
        raise MediaError("无法读取视频")
    monkeypatch.setattr("app.cli.analyze", fail)
    assert main(["test.mp4"]) == 1
    output = capsys.readouterr()
    assert "错误：无法读取视频" in output.err
    assert "Traceback" not in output.err


def test_posix_preview_preserves_arguments():
    command = ["/path with spaces/ffmpeg", "-vf", "setpts=N/(30*TB),pad=ceil(iw/2)*2:ceil(ih/2)*2",
               "录屏's $literal` (a)&b.mp4"]
    assert shlex.split(format_command(command, windows=False)) == command


@pytest.mark.skipif(sys.platform != "win32", reason="验证 Windows PowerShell 的真实参数传递")
def test_powershell_preview_preserves_filters_and_literal_paths(tmp_path):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell 不可用")
    # 用无副作用的参数回显程序验证预览文本。既检查语法，也检查是否误展开路径。
    script = tmp_path / "参数 echo.py"
    script.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    arguments = ["-vf", "setpts=N/(30*TB),pad=ceil(iw/2)*2:ceil(ih/2)*2",
                 "录屏's $literal` $(Write-Output changed) & (a); [1].mp4",
                 "中文 ‘片段’ “原版” 🎬.mp4"]
    preview = format_command([sys.executable, str(script), *arguments], windows=True)
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", preview],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == arguments
