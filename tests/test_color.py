"""颜色策略、输出严格验证和失败时不发布文件。"""

from dataclasses import replace
from pathlib import Path

from tests.mp4_stub import MP4_STUB

import pytest

from app.analyzer import parse_analysis, parse_stream
from app.color import select_color_plan, color_validation_errors
from app.ffmpeg_utils import MediaError, build_fix_command
from app.fixer import prepare_fix, execute_fix, select_strategy


BASE = {"codec_type": "video", "index": 0, "codec_name": "h264", "pix_fmt": "yuv420p", "color_range": "tv",
        "color_space": "bt709", "color_transfer": "bt709", "color_primaries": "bt709",
        "width": 160, "height": 90, "avg_frame_rate": "30", "r_frame_rate": "30", "start_time": "0", "duration": "1"}


@pytest.mark.parametrize("name", ["Mastering display metadata", "Content light level metadata", "DOVI configuration record", "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"])
@pytest.mark.parametrize("where", ["stream", "frame"])
def test_hdr_side_evidence_blocks_even_with_sdr_or_missing_transfer(name, where):
    for transfer in (None, "bt709"):
        stream = {**BASE, "color_transfer": transfer}
        frames = []
        if where == "stream":
            stream["side_data_list"] = [{"side_data_type": name}]
        else:
            frames = [{"stream_index": 0, "side_data_list": [{"side_data_type": name}]}]
        source = parse_analysis(Path("input.mp4"), {"streams": [stream], "frames": frames})
        assert select_color_plan(source.videos[0]).action == "blocked"
        with pytest.raises(MediaError, match="HDR"):
            prepare_fix(source)


def test_frame_color_evidence_is_scoped_and_does_not_rewrite_stream_metadata():
    source = parse_analysis(Path("input.mp4"), {"streams": [BASE, {**BASE, "index": 2}], "frames": [
        {"stream_index": 2, "color_transfer": "smpte2084"},
        {"stream_index": 2, "color_transfer": "smpte2084"},
        {"stream_index": None, "color_transfer": "smpte2084"}]})
    assert source.videos[1].sampled_color_transfers == ("smpte2084",)
    assert source.videos[1].color_transfer == "bt709"
    assert select_color_plan(source.videos[0]).action == "preserve_limited"
    assert select_color_plan(source.videos[1]).action == "blocked"


@pytest.mark.parametrize("pixel,range_value,action,source", [
    ("yuv420p", "tv", "preserve_limited", "color_range"), ("yuv420p", "mpeg", "preserve_limited", "color_range"),
    ("yuvj420p", "pc", "full_to_limited", "color_range"), ("yuv420p", "pc", "full_to_limited", "color_range"),
    ("yuvj420p", "jpeg", "full_to_limited", "color_range"), ("yuvj420p", None, "full_to_limited", "pix_fmt"),
    ("yuv420p", None, "blocked", "unknown"), ("yuv420p", "N/A", "blocked", "unknown"),
    ("yuv420p", "", "blocked", "unknown"), ("yuvj420p", "tv", "blocked", "color_range"),
    ("yuvj420p", "bad", "blocked", "unknown"),
])
def test_color_plan_has_explicit_range_evidence(pixel, range_value, action, source):
    video = parse_stream({**BASE, "pix_fmt": pixel, "color_range": range_value})
    plan = select_color_plan(video)
    assert (plan.action, plan.range_source) == (action, source)
    assert plan.pixel_format == "yuv420p" and plan.output_range == "tv"


@pytest.mark.parametrize("field,value", [("color_transfer", "smpte2084"), ("color_transfer", "arib-std-b67"),
    ("color_space", "bt709,eq=brightness=1"), ("color_primaries", "future")])
def test_unsupported_or_unsafe_metadata_is_not_injected_into_filters(field, value):
    plan = select_color_plan(parse_stream({**BASE, field: value}))
    assert plan.action == "blocked"


@pytest.mark.parametrize("mode", ["safe", "cfr", "timestamp", "audio-sync"])
@pytest.mark.parametrize("preset", ["general", "bilibili"])
@pytest.mark.parametrize("full", [False, True])
def test_range_command_is_independent_of_sync_mode(mode, preset, full):
    video = {**BASE, "pix_fmt": "yuvj420p" if full else "yuv420p", "color_range": "pc" if full else "tv"}
    info = parse_analysis(Path("in.mp4"), {"streams": [video, {"codec_type": "audio", "index": 1, "start_time": "0", "duration": "1"}]})
    command = build_fix_command("ffmpeg", info, Path("out.mp4"), strategy=select_strategy(info, mode, preset=preset), preset=preset)
    filters = command[command.index("-vf") + 1]
    assert ("scale=" in filters) is full
    if full:
        assert "in_range=full:out_range=limited" in filters
        assert "in_color_matrix=bt709:out_color_matrix=bt709" in filters
        if preset == "general":
            assert filters.index("scale=") < filters.index("pad=")
    assert "setparams=range=limited" in filters
    for flag in ("-colorspace", "-color_trc", "-color_primaries"):
        assert command[command.index(flag) + 1] == "bt709"
    assert command[command.index("-color_range") + 1] == "tv"
    assert command[command.index("-bsf:v") + 1] == "h264_metadata=video_full_range_flag=0"


def test_unknown_colorimetry_not_fabricated_from_resolution():
    info = parse_analysis(Path("in.mp4"), {"streams": [{**BASE, "color_space": None, "color_transfer": None, "color_primaries": None}]})
    plan = select_color_plan(info.videos[0])
    assert plan.action == "preserve_limited" and len(plan.warnings) == 3
    command = build_fix_command("ffmpeg", info, Path("out.mp4"), 30)
    assert not any(flag in command for flag in ("-colorspace", "-color_trc", "-color_primaries"))
    assert "bt709" not in command[command.index("-vf") + 1]


def test_unknown_range_stops_before_output_creation(tmp_path, monkeypatch):
    path = tmp_path / "unknown.mp4"
    path.write_bytes(b"source")
    info = parse_analysis(path, {"streams": [{**BASE, "color_range": None}]})
    monkeypatch.setattr("app.fixer.find_tool", lambda _: "ffmpeg")
    with pytest.raises(MediaError, match="范围未知"):
        prepare_fix(info, tmp_path / "output")
    assert not (tmp_path / "output").exists() and path.read_bytes() == b"source"


@pytest.mark.parametrize("pixel", [None, "", "rgb24", "gbrp", "gray"])
@pytest.mark.parametrize("color_range", ["tv", "pc"])
def test_range_tag_cannot_bypass_yuv_input_check(tmp_path, monkeypatch, pixel, color_range):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"original")
    info = parse_analysis(path, {"streams": [{**BASE, "pix_fmt": pixel, "color_range": color_range}]})
    assert select_color_plan(info.videos[0]).action == "blocked"
    monkeypatch.setattr("app.fixer.find_tool", lambda _: "ffmpeg")
    with pytest.raises(MediaError, match="YUV 像素格式"):
        prepare_fix(info, tmp_path / "output")
    assert not (tmp_path / "output").exists()
    assert path.read_bytes() == b"original"


@pytest.mark.parametrize("key,value", [("pix_fmt", "yuvj420p"), ("pix_fmt", None), ("color_range", "pc"),
    ("color_range", None), ("color_space", "smpte170m")])
@pytest.mark.parametrize("preset", ["general", "bilibili"])
def test_bad_output_color_never_published(tmp_path, monkeypatch, key, value, preset):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"original")
    info = parse_analysis(path, {"format": {"format_name": "mp4", "tags": {"major_brand": "isom"}}, "streams": [BASE]})
    monkeypatch.setattr("app.fixer.find_tool", lambda _: "ffmpeg")
    plan = prepare_fix(info, tmp_path / "out", preset=preset)
    monkeypatch.setattr("app.fixer.run_ffmpeg", lambda command, progress, **kwargs: Path(command[-1]).write_bytes(MP4_STUB))
    monkeypatch.setattr("app.fixer.analyze", lambda path: replace(info, path=path, videos=(parse_stream({**BASE, key: value}),)))
    reports = []
    with pytest.raises(MediaError, match="未发布"):
        execute_fix(plan, on_validation=reports.append)
    assert reports and reports[0].errors
    assert not plan.output_path.exists() and not list(plan.output_path.parent.glob(".avsync-*"))
    assert path.read_bytes() == b"original"


def test_validator_requires_actual_limited_not_merely_output_flag():
    plan = select_color_plan(parse_stream({**BASE, "pix_fmt": "yuvj420p", "color_range": "pc"}))
    assert not color_validation_errors(plan, parse_stream(BASE))
    assert color_validation_errors(plan, parse_stream({**BASE, "color_range": "pc"}))
