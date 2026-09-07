from fractions import Fraction
from pathlib import Path

import pytest

from app.analyzer import analyze, parse_analysis, parse_stream, positive_fraction
from app.ffmpeg_utils import MediaError


def test_normal_media():
    result = parse_analysis(Path("视频.mp4"), {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "11", "size": "123456"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264",
             "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001",
             "r_frame_rate": "30000/1001", "duration": "10", "time_base": "1/30000"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac",
             "duration": "11", "time_base": "1/48000", "sample_rate": "48000"},
        ],
    })
    video = result.videos[0]
    assert video.codec == "h264"
    assert (video.width, video.height) == (1920, 1080)
    assert video.avg_frame_rate == Fraction(30000, 1001)
    assert video.time_base == Fraction(1, 30000)
    assert video.duration == 10
    assert result.audios[0].duration == 11
    assert result.audios[0].codec == "aac"
    assert result.audios[0].time_base == Fraction(1, 48000)
    assert result.container_duration == 11
    assert result.file_size == 123456
    assert result.audios[0].sample_rate == 48000


@pytest.mark.parametrize("value", [None, "N/A", "0/0", "1/0", "0/1", "-1/25", "bad", "NaN", "inf"])
def test_invalid_rates(value):
    assert positive_fraction(value) is None


def test_missing_fields_do_not_use_container_duration():
    result = parse_analysis(Path("test.mkv"), {
        "format": {"duration": "600"},
        "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
    })
    assert result.container_duration == 600
    assert result.videos[0].duration is None
    assert result.audios[0].duration is None
    assert result.videos[0].time_base is None
    assert result.videos[0].avg_frame_rate is None


def test_duration_from_ticks():
    stream = parse_stream({"duration": "N/A", "duration_ts": 144000, "time_base": "1/48000"})
    assert stream.duration == 3
    assert stream.duration_source == "duration_ts × time_base"


@pytest.mark.parametrize("value", ["N/A", "NaN", "inf", "-1", None])
def test_invalid_duration(value):
    assert parse_stream({"duration": value}).duration is None


def test_zero_duration_is_valid():
    assert parse_stream({"duration": "0", "duration_ts": 30, "time_base": "1/30"}).duration == 0


def test_audio_only_and_cover_art():
    result = parse_analysis(Path("music.mp3"), {"streams": [
        {"codec_type": "video", "disposition": {"attached_pic": 1}},
        {"codec_type": "audio", "codec_name": "mp3"},
    ]})
    assert not result.videos
    assert len(result.audios) == 1


def test_video_only_and_multiple_tracks():
    result = parse_analysis(Path("test.mp4"), {"streams": [
        {"codec_type": "video", "index": 0}, {"codec_type": "video", "index": 1},
    ]})
    assert len(result.videos) == 2
    assert not result.audios
    assert parse_analysis(Path("empty"), {}).videos == ()


@pytest.mark.parametrize("audio_duration", [9, 11])
def test_track_durations_remain_independent(audio_duration):
    result = parse_analysis(Path("test.mp4"), {"streams": [
        {"codec_type": "video", "duration": "10", "avg_frame_rate": "25/1", "r_frame_rate": "30/1"},
        {"codec_type": "audio", "duration": str(audio_duration)},
    ]})
    assert result.videos[0].duration == 10
    assert result.audios[0].duration == audio_duration
    assert result.videos[0].avg_frame_rate != result.videos[0].r_frame_rate


def test_invalid_file_before_tool_check(tmp_path, monkeypatch):
    def unexpected():
        pytest.fail("不应在无效输入时启动工具")
    monkeypatch.setattr("app.analyzer.check_tools", unexpected)
    for path in (tmp_path, tmp_path / "missing.mp4"):
        with pytest.raises(MediaError, match="文件不存在或不是普通文件"):
            analyze(path)


@pytest.mark.parametrize("value", [None, "N/A", "0", "-48000", "bad", "NaN"])
def test_invalid_sample_rate(value):
    assert parse_stream({"sample_rate": value}).sample_rate is None


@pytest.mark.parametrize("value", [None, "N/A", "-1", "bad"])
def test_invalid_file_size(value):
    result = parse_analysis(Path("test.mp4"), {"format": {"size": value}})
    assert result.file_size is None


def test_file_size_from_disk_when_probe_size_missing(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"12345")
    monkeypatch.setattr("app.analyzer.check_tools", lambda: {"ffprobe": "ffprobe"})
    monkeypatch.setattr("app.analyzer.probe_media", lambda *args: {})
    assert analyze(path).file_size == 5
