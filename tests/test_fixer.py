from pathlib import Path
import os
from dataclasses import replace

from tests.mp4_stub import MP4_STUB

import pytest

from app.analyzer import parse_analysis
from app.ffmpeg_utils import MediaError, build_fix_command
from app.fixer import fix_video, select_target_fps


@pytest.mark.parametrize("average, nominal, expected", [
    ("30000/1001", None, 30), ("60000/1001", None, 60), ("119.88", None, 120),
    ("24000/1001", None, 24), (25, None, 25), (50, None, 50),
    (59.27, 60, 60), (48, None, 50), (27, None, 25), (40, None, 30),
    (90, None, 60), (240, None, 120), ("1e100", None, 120), (10, None, 24),
    ("0/0", "60/1", 60), (None, None, 30), ("bad", "N/A", 30),
    ("NaN", "inf", 30), (0, -1, 30),
])
def test_select_target_fps(average, nominal, expected):
    assert select_target_fps(average, nominal) == expected


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "中文 视频.mp4"
    path.write_bytes(b"original")
    return parse_analysis(path, {"streams": [
        {"codec_type": "video", "index": 0, "codec_name": "h264", "pix_fmt": "yuv420p", "color_range": "tv", "avg_frame_rate": "30", "r_frame_rate": "30", "duration": "2", "start_time": "0"},
        {"codec_type": "audio", "index": 1, "codec_name": "aac", "duration": "2", "start_time": "0"},
    ]})


def test_command_encodes_cfr_and_preserves_audio_samples(source, tmp_path):
    command = build_fix_command("ffmpeg", source, tmp_path / "fixed.mp4", 30)
    assert command[command.index("-i") + 1] == str(source.path.resolve())
    assert command[-1] == str((tmp_path / "fixed.mp4").resolve())
    for flag, expected in (("-c:v", "libx264"), ("-preset", "medium"), ("-crf", "18"),
                           ("-c:a", "aac"), ("-b:a", "192k"), ("-fps_mode:v", "passthrough"), ("-progress", "pipe:1")):
        assert command[command.index(flag) + 1] == expected
    video_filter = command[command.index("-vf") + 1]
    assert video_filter.index("fps=fps=30") < video_filter.index("setpts=N/")
    audio_filter = command[command.index("-filter:a:0") + 1]
    assert "asetpts=PTS-STARTPTS" in audio_filter
    assert "N/SR/TB" not in audio_filter
    assert "aresample" not in audio_filter
    for excluded in ("-shortest", "-copyts", "-y", "-t", "-to"):
        assert excluded not in command
    assert "-n" in command


def test_command_rejects_source_overwrite(source):
    with pytest.raises(MediaError, match="相同"):
        build_fix_command("ffmpeg", source, source.path, 30)


@pytest.mark.parametrize("fps", [0, -1, True, 29.97])
def test_command_rejects_invalid_fps(source, tmp_path, fps):
    with pytest.raises(MediaError, match="正整数"):
        build_fix_command("ffmpeg", source, tmp_path / "fixed.mp4", fps)


def mock_encoding(source, monkeypatch):
    monkeypatch.setattr("app.fixer.find_tool", lambda _: "ffmpeg")
    monkeypatch.setattr("app.fixer.run_ffmpeg", lambda command, callback, **kwargs: Path(command[-1]).write_bytes(MP4_STUB))
    monkeypatch.setattr("app.fixer.analyze", lambda path: replace(source, path=path, container="mp4", major_brand="isom"))


def test_success_publishes_verified_file_and_preserves_source(source, tmp_path, monkeypatch):
    mock_encoding(source, monkeypatch)
    starts = []
    result = fix_video(source, tmp_path / "out", mode="cfr", on_start=lambda *args: starts.append(args))
    assert result.after.path.name == "中文 视频_fixed.mp4"
    assert result.after.path.read_bytes() == MP4_STUB
    assert source.path.read_bytes() == b"original"
    assert result.target_fps == 30
    assert len(starts) == 1
    assert not list((tmp_path / "out").glob(".avsync-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows exclusive rename semantics")
def test_windows_publication_does_not_require_hardlinks(source, tmp_path, monkeypatch):
    mock_encoding(source, monkeypatch)
    def unavailable(*args):
        raise OSError("filesystem does not support hard links")
    monkeypatch.setattr("app.fixer.os.link", unavailable)
    result = fix_video(source, tmp_path / "输出 空格")
    assert result.after.path.read_bytes() == MP4_STUB
    assert source.path.read_bytes() == b"original"


@pytest.mark.skipif(os.name != "nt", reason="Windows exclusive rename semantics")
def test_windows_publication_failure_cleans_staging_without_final_file(source, tmp_path, monkeypatch):
    mock_encoding(source, monkeypatch)
    def denied(*args):
        raise PermissionError("publication denied")
    monkeypatch.setattr("app.fixer.os.rename", denied)
    with pytest.raises(MediaError, match="文件操作失败"):
        fix_video(source, tmp_path / "out")
    assert list((tmp_path / "out").iterdir()) == []


def test_existing_output_not_overwritten(source, tmp_path, monkeypatch):
    mock_encoding(source, monkeypatch)
    output = tmp_path / "中文 视频_fixed.mp4"
    output.write_bytes(b"existing")
    with pytest.raises(MediaError, match="已存在"):
        fix_video(source, tmp_path)
    assert output.read_bytes() == b"existing"


def test_concurrent_output_creation_not_overwritten(source, tmp_path, monkeypatch):
    mock_encoding(source, monkeypatch)
    def competing_writer(output, fps, command):
        output.write_bytes(b"other task")
    with pytest.raises(MediaError, match="其他任务"):
        fix_video(source, tmp_path, on_start=competing_writer)
    assert (tmp_path / "中文 视频_fixed.mp4").read_bytes() == b"other task"


@pytest.mark.parametrize("failure", [MediaError("encoder failed"), KeyboardInterrupt()])
def test_failure_and_cancellation_remove_partial_output(source, tmp_path, monkeypatch, failure):
    mock_encoding(source, monkeypatch)
    def fail(command, callback, **kwargs):
        Path(command[-1]).write_bytes(b"partial")
        raise failure
    monkeypatch.setattr("app.fixer.run_ffmpeg", fail)
    with pytest.raises(type(failure)):
        fix_video(source, tmp_path / "out")
    assert list((tmp_path / "out").iterdir()) == []
    assert source.path.read_bytes() == b"original"


def test_failed_output_probe_not_published(source, tmp_path, monkeypatch):
    mock_encoding(source, monkeypatch)
    def fail(path):
        raise MediaError("bad output")
    monkeypatch.setattr("app.fixer.analyze", fail)
    with pytest.raises(MediaError, match="复查失败"):
        fix_video(source, tmp_path / "out")
    assert list((tmp_path / "out").iterdir()) == []


def test_audio_only_rejected_before_encoding(tmp_path):
    source = parse_analysis(tmp_path / "audio.wav", {"streams": [{"codec_type": "audio"}]})
    with pytest.raises(MediaError, match="没有可修复"):
        fix_video(source, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("streams", [
    [],
    [{"codec_type": "video", "codec_name": "h264", "avg_frame_rate": "25", "r_frame_rate": "25"}, {"codec_type": "audio", "codec_name": "aac"}],
    [{"codec_type": "video", "codec_name": "h264", "avg_frame_rate": "30", "r_frame_rate": "30"}],
])
def test_invalid_output_is_not_published(source, tmp_path, monkeypatch, streams):
    mock_encoding(source, monkeypatch)
    monkeypatch.setattr("app.fixer.analyze", lambda path: parse_analysis(path, {"streams": streams}))
    with pytest.raises(MediaError, match="输出复查失败"):
        fix_video(source, tmp_path / "out", mode="cfr")
    assert list((tmp_path / "out").iterdir()) == []


def test_file_operation_error_is_readable(source, tmp_path, monkeypatch):
    mock_encoding(source, monkeypatch)
    not_a_directory = tmp_path / "file"
    not_a_directory.write_bytes(b"keep")
    with pytest.raises(MediaError, match="文件操作失败"):
        fix_video(source, not_a_directory)
    assert not_a_directory.read_bytes() == b"keep"
