"""V2 输入画像：原始证据、类型解析、展示及不影响 V1 修复的边界。"""

from copy import deepcopy
from fractions import Fraction
from pathlib import Path

import pytest

from app.analyzer import parse_analysis, parse_rational, parse_stream
from app.ffmpeg_utils import build_fix_command
from app.fixer import select_strategy
from app.media_profile import format_media_profile, frame_rate_confidence
from app.models import FieldState, ProbeField


def detailed_video():
    return {
        "codec_type": "video", "index": 0, "codec_name": "hevc",
        "codec_long_name": "H.265 / HEVC", "profile": "Main", "level": 150,
        "pix_fmt": "yuvj420p", "width": 1920, "height": 1080,
        "sample_aspect_ratio": "1:1", "display_aspect_ratio": "16:9",
        "r_frame_rate": "60/1", "avg_frame_rate": "60000/1001",
        "time_base": "1/90000", "start_pts": -9000, "start_time": "-0.1",
        "duration_ts": 900000, "duration": "10", "nb_frames": "600",
        "color_range": "pc", "color_space": "bt709", "color_transfer": "bt709",
        "color_primaries": "bt709", "chroma_location": "left", "bits_per_raw_sample": "8",
        "field_order": "progressive", "tags": {"title": "录屏 🎬", "rotate": "90"},
        "disposition": {"default": 1, "attached_pic": 0},
        "side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90,
                            "displaymatrix": "original matrix"}],
    }


def test_all_requested_video_fields_and_rotation_precedence():
    raw = detailed_video()
    video = parse_stream(raw)
    assert (video.codec, video.codec_long_name, video.profile, video.level) == ("hevc", "H.265 / HEVC", "Main", 150)
    assert (video.width, video.height, video.pixel_format) == (1920, 1080, "yuvj420p")
    assert video.sample_aspect_ratio == Fraction(1) and video.display_aspect_ratio == Fraction(16, 9)
    assert video.r_frame_rate == 60 and video.avg_frame_rate == Fraction(60000, 1001)
    assert (video.time_base, video.start_pts, video.start_time) == (Fraction(1, 90000), -9000, -0.1)
    assert (video.duration_ts, video.duration, video.nb_frames) == (900000, 10, 600)
    assert (video.color_range, video.color_range_name) == ("pc", "Full")
    assert video.color_space == video.color_primaries == video.color_transfer == "bt709"
    assert (video.chroma_location, video.bits_per_raw_sample, video.field_order) == ("left", 8, "progressive")
    assert video.is_default is True
    assert video.rotation == -90 and video.rotation_source == "side_data_list.Display Matrix.rotation"
    assert video.metadata == raw["tags"] and video.side_data_list[0] == raw["side_data_list"][0]
    assert set(raw) <= set(video.probe_fields)
    assert all(video.probe_fields[key].state is FieldState.VALUE for key in raw)
    # 原始证据不能被调用者随后修改 ffprobe JSON 的动作污染。
    raw["side_data_list"][0]["rotation"] = 180
    raw["tags"]["title"] = "changed"
    assert video.side_data_list[0]["rotation"] == -90
    assert video.probe_fields["tags"].raw["title"] == "录屏 🎬"


def test_audio_and_container_features():
    raw = {
        "format": {"format_name": "mov,mp4", "duration": "10.3", "start_time": "-0.1",
                   "bit_rate": "5000000", "tags": {"major_brand": "isom", "title": "test"}},
        "streams": [{"codec_type": "audio", "codec_name": "aac", "profile": "LC",
                     "sample_fmt": "fltp", "sample_rate": "48000", "channels": 2,
                     "channel_layout": "stereo", "time_base": "1/48000", "start_time": "0.019",
                     "duration": "10.2", "bit_rate": "192000", "disposition": {"default": 0}}],
    }
    info = parse_analysis(Path("input.mp4"), raw)
    audio = info.audios[0]
    assert (audio.codec, audio.profile, audio.sample_fmt, audio.sample_rate) == ("aac", "LC", "fltp", 48000)
    assert (audio.channels, audio.channel_layout, audio.bit_rate) == (2, "stereo", 192000)
    assert (audio.time_base, audio.start_time, audio.duration) == (Fraction(1, 48000), 0.019, 10.2)
    assert audio.is_default is False
    assert (info.container, info.container_duration, info.container_start_time, info.container_bit_rate) == ("mov,mp4", 10.3, -0.1, 5000000)
    assert info.metadata == raw["format"]["tags"]
    report = format_media_profile(info)
    assert "48000 Hz" in report and "Stereo（stereo）" in report and "192000 bit/s" in report


@pytest.mark.parametrize("raw,expected", [("60000/1001", Fraction(60000, 1001)), ("60/1", Fraction(60)),
                                         (60, Fraction(60)), (" 29.97 ", Fraction(2997, 100)),
                                         (Fraction(25), Fraction(25))])
def test_rational_parser_returns_exact_values(raw, expected):
    assert parse_rational(raw) == expected


@pytest.mark.parametrize("raw", ["0/0", "1/0", "0/1", "-60/1", "NaN", "inf", None, "", "N/A", [], {}, True, "a/b", "1/2/3"])
def test_invalid_rational_is_unknown_not_zero(raw):
    assert parse_rational(raw) is None


def test_aspect_ratios_allow_colon_without_accepting_it_as_fps():
    assert parse_rational("16:9", allow_colon=True) == Fraction(16, 9)
    assert parse_rational("16:9") is None
    assert parse_rational("0:1", allow_colon=True) is None


@pytest.mark.parametrize("key,value,state", [
    ("profile", None, FieldState.EMPTY), ("profile", "", FieldState.EMPTY),
    ("profile", "  ", FieldState.EMPTY), ("profile", "N/A", FieldState.UNKNOWN),
    ("profile", "unknown", FieldState.UNKNOWN), ("profile", 123, FieldState.INVALID),
    ("avg_frame_rate", "0/0", FieldState.INVALID), ("start_pts", 0, FieldState.VALUE),
    ("duration", "0", FieldState.VALUE), ("channels", 0, FieldState.INVALID),
    ("bits_per_raw_sample", "0", FieldState.UNKNOWN), ("bit_rate", False, FieldState.INVALID),
    ("tags", {}, FieldState.EMPTY), ("side_data_list", [], FieldState.EMPTY),
])
def test_field_states_preserve_empty_unknown_invalid_and_zero(key, value, state):
    video = parse_stream({key: value})
    assert video.probe_fields[key].state is state
    assert video.probe_fields[key].raw == value
    assert video.probe_fields[key].present
    assert parse_stream({}).probe_fields[key] == ProbeField()
    assert not parse_stream({}).probe_fields[key].present


@pytest.mark.parametrize("raw,normalized", [("pc", "Full"), ("jpeg", "Full"), ("tv", "Limited"),
                                           ("mpeg", "Limited"), (" JPEG ", "Full"), ("future", None)])
def test_color_aliases_preserve_original(raw, normalized):
    video = parse_stream({"color_range": raw})
    assert video.color_range_name == normalized
    assert video.color_range == video.probe_fields["color_range"].raw == raw


@pytest.mark.parametrize("side,tags,expected,source", [
    ([], {"rotate": "0"}, 0, "tags.rotate"), (None, {"rotate": "270"}, 270, "tags.rotate"),
    ([{"side_data_type": "Display Matrix", "rotation": "NaN"}], {"rotate": "90"}, 90, "tags.rotate"),
    ([{"side_data_type": "Other", "rotation": 180}], {}, None, None),
    ([{"side_data_type": "Display Matrix", "rotation": True}], {"rotate": False}, None, None),
    ("bad", [], None, None),
])
def test_rotation_is_optional_and_never_guessed(side, tags, expected, source):
    video = parse_stream({"side_data_list": side, "tags": tags})
    assert video.rotation == expected and video.rotation_source == source


def test_track_counts_defaults_subtitles_and_cover_art_do_not_select_tracks():
    info = parse_analysis(Path("multi.mp4"), {"streams": [
        {"codec_type": "video", "index": 0, "disposition": {"attached_pic": 1, "default": 1}},
        {"codec_type": "video", "index": 1, "disposition": {"default": 0}},
        {"codec_type": "audio", "index": 2},
        {"codec_type": "video", "index": 3, "disposition": {"default": 1}},
        {"codec_type": "audio", "index": 4, "disposition": {"default": 1}},
        {"codec_type": "subtitle", "index": 5, "codec_name": "subrip", "tags": {"language": "zho"}},
        {"codec_type": "attachment", "index": 6, "tags": {"filename": "font.ttf"}},
    ]})
    assert info.video_stream_count == 3 and info.audio_stream_count == 2
    assert [video.index for video in info.videos] == [1, 3]
    assert info.videos[0].is_default is False and info.videos[1].is_default is True
    assert info.audios[0].is_default is None and info.audios[1].is_default is True
    assert info.has_subtitles is True and info.subtitles[0].codec == "subrip"
    assert info.other_streams[0].metadata["filename"] == "font.ttf"
    assert info.cover_art[0].index == 0


@pytest.mark.parametrize("data,has_subtitles", [({}, None), ({"streams": []}, False),
    ({"streams": None}, None), ({"streams": [None, {}]}, None),
    ({"format": None, "streams": [{"codec_type": "video"}]}, False),
    ({"format": [], "streams": "bad"}, None)])
def test_partial_or_malformed_sections_do_not_crash(data, has_subtitles):
    info = parse_analysis(Path("partial.mp4"), data)
    assert info.has_subtitles is has_subtitles
    assert "媒体画像" in format_media_profile(info)


@pytest.mark.parametrize("kind", ["unknown", "N/A", "future", 123, ["audio"], {"type": "audio"}, "", None, "   "])
@pytest.mark.parametrize("known_subtitle", [False, True])
def test_unknown_track_type_cannot_prove_tracks_absent(kind, known_subtitle):
    streams = [{"codec_type": kind}]
    if known_subtitle:
        streams.append({"codec_type": "subtitle", "codec_name": "subrip"})
    info = parse_analysis(Path("partial.mp4"), {"streams": streams})
    assert info.has_subtitles is (True if known_subtitle else None)
    assert info.other_streams[0].probe_fields["codec_type"].raw == kind
    report = format_media_profile(info)
    assert ("字幕：有，1 条" if known_subtitle else "字幕：未知") in report
    for label in ("视频", "音频"):
        assert f"{label}轨道：无已解析轨道（轨道列表未知或不完整）" in report


@pytest.mark.parametrize("streams", [[], [{"codec_type": "data"}, {"codec_type": "attachment"}]])
def test_known_non_av_tracks_or_empty_list_confirm_absence(streams):
    info = parse_analysis(Path("data.mp4"), {"streams": streams})
    assert info.has_subtitles is False
    lines = format_media_profile(info).splitlines()
    assert "字幕：无" in lines
    assert "视频轨道：无" in lines and "音频轨道：无" in lines


def test_duration_fallback_keeps_empty_input_evidence():
    video = parse_stream({"duration": "", "duration_ts": "90000", "time_base": "1/90000"})
    assert video.duration == 1 and video.duration_source == "duration_ts × time_base"
    assert video.probe_fields["duration"].state is FieldState.EMPTY


@pytest.mark.parametrize("fields,label", [({}, "未知"), ({"r_frame_rate": "60"}, "低"),
    ({"r_frame_rate": "60/1", "avg_frame_rate": "120/2"}, "有限"),
    ({"r_frame_rate": "60", "avg_frame_rate": "59"}, "疑似 VFR")])
def test_frame_confidence_never_claims_verified_cfr(fields, label):
    description = frame_rate_confidence(parse_stream(fields))
    assert label in description
    assert "不能确认" in description or "不能据此确认" in description or "未检查" in description


def test_profile_report_distinguishes_missing_empty_unknown_and_invalid():
    raw = detailed_video()
    raw.update(profile="", level="N/A", bits_per_raw_sample="bad")
    del raw["chroma_location"]
    info = parse_analysis(Path("中文.mp4"), {"streams": [raw]})
    report = format_media_profile(info)
    for text in ("Full（原始值 pc）", "BT.709", "Profile：空（字段存在）", "未知（ffprobe 未指定）",
                 "位深字段（bits_per_raw_sample）：未知（字段无效）", "Chroma location：未知（字段缺失）",
                 "60000/1001", "Rotation：-90°", "帧率置信度：有限"):
        assert text in report


@pytest.mark.parametrize("mode", ["safe", "cfr", "timestamp", "audio-sync"])
@pytest.mark.parametrize("preset", ["general", "bilibili"])
def test_new_input_metadata_does_not_change_repair_strategy_or_command(mode, preset):
    video = detailed_video()
    legacy_keys = {"codec_type", "index", "codec_name", "width", "height", "pix_fmt", "avg_frame_rate",
                   "r_frame_rate", "time_base", "start_time", "duration", "duration_ts",
                   "color_range", "color_space", "color_transfer", "color_primaries"}
    minimal = {key: value for key, value in video.items() if key in legacy_keys}
    audio = {"codec_type": "audio", "index": 1, "codec_name": "aac", "duration": "10.3", "start_time": "0"}
    original = parse_analysis(Path("input.mp4"), {"streams": [minimal, audio]})
    expanded = parse_analysis(Path("input.mp4"), {"streams": [deepcopy(video), {**audio, "channels": 2,
                              "profile": "LC", "sample_fmt": "fltp", "tags": {"language": "eng"}}]})
    before = select_strategy(original, mode, preset=preset)
    after = select_strategy(expanded, mode, preset=preset)
    assert before == after
    assert build_fix_command("ffmpeg", original, Path("out.mp4"), strategy=before, preset=preset) == build_fix_command(
        "ffmpeg", expanded, Path("out.mp4"), strategy=after, preset=preset)
