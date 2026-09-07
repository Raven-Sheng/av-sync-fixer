"""真实工具验证；未安装工具时跳过，不下载外部媒体。"""

import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

from app.analyzer import analyze, diagnose
from app.ffmpeg_utils import MediaError, PROJECT_ROOT, find_tool
from tests.media_helpers import generate_media


@pytest.fixture(scope="module")
def ffmpeg():
    try:
        executable = find_tool("ffmpeg")
        find_tool("ffprobe")
    except MediaError as exc:
        pytest.skip(str(exc))
    return executable


@pytest.fixture(scope="module")
def media_files(ffmpeg, tmp_path_factory):
    directory = tmp_path_factory.mktemp("真实媒体")
    video_input = ["-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=1"]
    audio_input = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1"]
    cases = {
        "av.mp4": video_input + audio_input + ["-c:v", "libx264", "-c:a", "aac"],
        "silent.mp4": video_input + ["-c:v", "libx264", "-an"],
        "audio.wav": audio_input + ["-c:a", "pcm_s16le"],
        "av.mkv": video_input + audio_input + ["-c:v", "libx264", "-c:a", "aac"],
        "different_lengths.mp4": video_input + [
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.8",
            "-c:v", "libx264", "-c:a", "aac",
        ],
    }
    paths = {}
    for name, options in cases.items():
        path = directory / f"中文 空格 🎬 {name}"
        generate_media(ffmpeg, path, options)
        paths[name] = path
    return paths


@pytest.mark.parametrize("name, videos, audios", [
    ("av.mp4", 1, 1), ("silent.mp4", 1, 0), ("audio.wav", 0, 1), ("av.mkv", 1, 1),
])
def test_real_metadata(media_files, name, videos, audios):
    path = media_files[name]
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = analyze(path)
    assert result.container
    assert result.file_size == path.stat().st_size
    assert len(result.videos) == videos
    assert len(result.audios) == audios
    if audios:
        assert result.audios[0].sample_rate == 48000
    if videos:
        assert result.videos[0].codec == "h264"
        assert (result.videos[0].width, result.videos[0].height) == (160, 90)
        assert result.videos[0].avg_frame_rate == 25
        assert result.videos[0].time_base is not None
    if name == "av.mp4":
        assert result.videos[0].duration == pytest.approx(1, abs=0.05)
        assert result.audios[0].duration == pytest.approx(1, abs=0.05)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_cli_from_other_directory_with_unicode(media_files, tmp_path):
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "main.py"), str(media_files["av.mp4"])],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "中文 空格 🎬" in result.stdout
    assert "视频编码器（codec_name）：h264" in result.stdout
    assert "音频编码器（codec_name）：aac" in result.stdout
    assert "音频采样率：48000 Hz" in result.stdout
    assert f"文件大小：{media_files['av.mp4'].stat().st_size} 字节" in result.stdout
    assert "音画同步分析" in result.stdout
    assert "同步风险：低" in result.stdout


def test_real_duration_risk(media_files):
    path = media_files["different_lengths.mp4"]
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = diagnose(analyze(path))
    assert result.duration_diff == pytest.approx(0.8, abs=0.05)
    assert result.duration_risk == "较高"
    assert result.suspected_vfr is False
    assert result.start_time_diff is not None
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_corrupt_file_readable_failure(ffmpeg, tmp_path):
    path = tmp_path / "损坏.mp4"
    path.write_bytes(b"not a media file")
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "main.py"), str(path)],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 1
    assert "执行失败" in result.stderr
    assert "Traceback" not in result.stderr
