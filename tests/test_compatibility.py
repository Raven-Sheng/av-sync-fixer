"""兼容性风险规则、证据边界，以及与修复决策的隔离。"""

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from app.analyzer import parse_analysis
from app.compatibility import diagnose_compatibility, format_compatibility_report, pixel_format_depth
from app.ffmpeg_utils import build_fix_command
from app.fixer import select_strategy
from app.models import CompatibilitySeverity as S, FieldState


VIDEO = {"codec_type": "video", "index": 3, "codec_name": "h264", "pix_fmt": "yuv420p",
         "color_range": "tv", "color_space": "bt709", "color_primaries": "bt709", "color_transfer": "bt709",
         "avg_frame_rate": "60/1", "r_frame_rate": "60/1", "start_time": "0", "start_pts": 0,
         "duration": "10", "width": 1920, "height": 1080}
AUDIO = {"codec_type": "audio", "index": 7, "sample_rate": "48000", "channels": 2,
         "channel_layout": "stereo", "start_time": "0", "duration": "10", "codec_name": "aac"}


def media(video=None, audio=None):
    return parse_analysis(Path("游戏 录屏.mp4"), {"format": {"start_time": "0"},
        "streams": [{**VIDEO, **(video or {})}, {**AUDIO, **(audio or {})}]})


def finding(report, code, scope="video:0"):
    return next(item for item in report.findings if item.code == code and item.scope == scope)


@pytest.mark.parametrize("codec,label", [("hevc", "HEVC"), ("h265", "HEVC"), ("h.265", "HEVC"), ("h264", "H.264"), ("h.264", "H.264")])
def test_legal_common_codecs_are_info(codec, label):
    item = finding(diagnose_compatibility(media({"codec_name": codec})), "video.codec")
    assert item.severity is S.INFO and label in item.summary
    assert "未做解码能力测试" in item.reason
    assert item.stream_index == 3 and item.evidence["codec_name"]["raw"] == codec


@pytest.mark.parametrize("codec", [None, "", "N/A", "av1", "future"])
def test_unknown_or_other_codec_is_not_declared_unsupported(codec):
    item = finding(diagnose_compatibility(media({"codec_name": codec})), "video.codec")
    assert item.severity is S.WARNING


@pytest.mark.parametrize("pixel,depth", [("yuv420p", 8), ("yuvj420p", 8), ("yuv422p", 8), ("yuv444p", 8),
    ("yuv420p10le", 10), ("yuv422p10be", 10), ("yuv444p12le", 12), ("p010le", 10),
    ("rgb24", 8), ("gray16be", 16), (None, None), ("future10", None), ("yuv420p10garbage", None)])
def test_bit_depth_uses_known_component_formats_not_arbitrary_digits(pixel, depth):
    assert pixel_format_depth(pixel) == depth
    source = media({"pix_fmt": pixel})
    item = finding(diagnose_compatibility(source), "video.bit_depth")
    assert item.evidence["interpreted_depth"] == depth
    assert item.severity is (S.WARNING if depth is None else S.INFO if depth == 8 else S.REPAIR_RECOMMENDED)
    assert source.videos[0].bits_per_raw_sample is None
    assert source.videos[0].probe_fields["bits_per_raw_sample"].state is FieldState.MISSING


@pytest.mark.parametrize("fields,depth,source", [
    ({"pix_fmt": "custom", "bits_per_raw_sample": "10"}, 10, "bits_per_raw_sample"),
    ({"pix_fmt": "yuv420p", "bits_per_raw_sample": "10"}, None, "conflict"),
    ({"pix_fmt": "yuv420p10le", "bits_per_raw_sample": "8"}, None, "conflict"),
    ({"pix_fmt": None, "profile": "Main 10"}, None, "unknown"),
    ({"pix_fmt": "yuv420p", "profile": "Main 10"}, 8, "pix_fmt"),
])
def test_depth_conflicts_and_profile_are_not_guesses(fields, depth, source):
    item = finding(diagnose_compatibility(media(fields)), "video.bit_depth")
    assert item.evidence["interpreted_depth"] == depth and item.evidence["source"] == source


@pytest.mark.parametrize("depth", [1, 2, 4, 6, 7])
def test_low_bit_depth_expansion_is_not_reported_as_precision_loss(depth):
    item = finding(diagnose_compatibility(media({"pix_fmt": "custom", "bits_per_raw_sample": str(depth)})), "video.bit_depth")
    assert item.severity is S.INFO
    assert "位深扩展不会增加原始细节" in item.reason
    assert "输出会降低精度" not in item.reason
    assert item.evidence["interpreted_depth"] == depth and item.evidence["target_depth"] == 8


@pytest.mark.parametrize("target,expected", [("yuv420p10le", S.INFO), ("yuv444p16le", S.INFO), ("custom", S.WARNING)])
def test_depth_risk_uses_output_format_and_keeps_unknown_target_unknown(monkeypatch, target, expected):
    # 只替换诊断所读的目标常量，验证不把所有输出一律假定为 8-bit；不构建或执行命令。
    monkeypatch.setattr("app.compatibility.VIDEO_PIXEL_FORMAT", target)
    item = finding(diagnose_compatibility(media({"pix_fmt": "yuv420p10le"})), "video.bit_depth")
    assert item.severity is expected
    assert item.evidence["target_pixel_format"] == target
    if target == "custom":
        assert item.evidence["target_depth"] is None
        assert "无法评估" in item.reason


@pytest.mark.parametrize("pixel,level", [("yuv420p", S.INFO), ("yuvj420p", S.WARNING),
    ("yuv422p", S.WARNING), ("yuv444p", S.WARNING), ("yuv420p10le", S.WARNING), ("rgb24", S.WARNING)])
def test_pixel_formats_are_legal_but_have_different_output_risks(pixel, level):
    item = finding(diagnose_compatibility(media({"pix_fmt": pixel})), "video.pixel_format")
    assert item.severity is level
    if pixel == "yuvj420p":
        assert "合法" in item.reason and "不代表文件损坏" in item.reason


@pytest.mark.parametrize("raw,level,name", [("pc", S.REPAIR_RECOMMENDED, "Full"), ("jpeg", S.REPAIR_RECOMMENDED, "Full"),
    ("tv", S.INFO, "Limited"), ("mpeg", S.INFO, "Limited"), (None, S.WARNING, None), ("future", S.WARNING, None)])
def test_range_normalization_retains_raw_and_does_not_assume_limited(raw, level, name):
    item = finding(diagnose_compatibility(media({"color_range": raw})), "video.color_range")
    assert item.severity is level
    assert item.evidence["color_range"]["raw"] == raw and item.evidence["normalized_range"] == name
    if name == "Full":
        assert "独立颜色策略" in item.reason and "不能只改标签" in item.reason


def test_yuvj_limited_conflict_and_yuvj_missing_range_are_not_silently_fixed():
    report = diagnose_compatibility(media({"pix_fmt": "yuvj420p", "color_range": "tv"}))
    assert finding(report, "video.range_conflict").severity is S.WARNING
    item = finding(diagnose_compatibility(media({"pix_fmt": "yuvj420p", "color_range": None})), "video.color_range")
    assert item.severity is S.WARNING and item.evidence["normalized_range"] is None


@pytest.mark.parametrize("matrix,primaries,label,level", [("bt709", "bt709", "BT.709", S.INFO),
    ("bt2020nc", "bt2020", "BT.2020", S.WARNING), ("bt2020c", None, "BT.2020", S.WARNING),
    (None, "bt709", "BT.709", S.WARNING), ("smpte170m", "smpte170m", "其他", S.WARNING)])
def test_color_matrix_and_primaries_are_separate_evidence(matrix, primaries, label, level):
    report = diagnose_compatibility(media({"color_space": matrix, "color_primaries": primaries}))
    item = finding(report, "video.colorimetry")
    assert label in item.summary and item.severity is level
    assert finding(report, "video.dynamic_range").severity is S.INFO  # bt709 transfer remains SDR evidence


@pytest.mark.parametrize("transfer,label", [("smpte2084", "PQ"), ("arib-std-b67", "HLG")])
def test_hdr_transfer_identifies_current_color_pipeline_limit(transfer, label):
    report = diagnose_compatibility(media({"color_transfer": transfer, "pix_fmt": "yuv420p10le", "color_primaries": "bt2020"}))
    item = finding(report, "video.dynamic_range")
    assert item.severity is S.UNSUPPORTED and label in item.summary
    assert "不代表文件损坏" in item.reason and "色调映射" in item.reason


@pytest.mark.parametrize("side_name", ["Mastering display metadata", "Content light level metadata", "DOVI configuration record", "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"])
def test_hdr_side_data_without_consistent_transfer_is_only_potential(side_name):
    item = finding(diagnose_compatibility(media({"side_data_list": [{"side_data_type": side_name}]})), "video.dynamic_range")
    assert item.severity is S.WARNING and item.evidence["hdr_side_data_types"] == [side_name]


@pytest.mark.parametrize("fields", [{"color_transfer": None}, {"color_transfer": None, "pix_fmt": "yuv420p10le"},
    {"color_transfer": None, "color_space": "bt2020nc", "color_primaries": "bt2020"},
    {"color_transfer": "unknown", "codec_name": "hevc"}])
def test_missing_transfer_does_not_prove_hdr_or_sdr(fields):
    item = finding(diagnose_compatibility(media(fields)), "video.dynamic_range")
    assert item.severity is S.WARNING and "证据不足" in item.summary


@pytest.mark.parametrize("avg,nominal,level", [("60000/1001", "60/1", S.INFO), ("120/2", "60/1", S.INFO),
    ("599/10", "60", S.INFO), ("59899/1000", "60", S.REPAIR_RECOMMENDED),
    ("59", "60", S.REPAIR_RECOMMENDED), ("0/0", "60", S.WARNING), (None, None, S.WARNING)])
def test_fps_comparison_uses_numeric_tolerance_and_never_proves_cfr(avg, nominal, level):
    item = finding(diagnose_compatibility(media({"avg_frame_rate": avg, "r_frame_rate": nominal})), "video.frame_rate")
    assert item.severity is level
    assert "不能" in item.reason


@pytest.mark.parametrize("duration,level", [("10", S.INFO), ("10.049", S.INFO), ("10.05", S.WARNING),
    ("10.199", S.WARNING), ("10.2", S.REPAIR_RECOMMENDED), ("10.5", S.REPAIR_RECOMMENDED),
    ("10.501", S.REPAIR_RECOMMENDED), ("9.7", S.REPAIR_RECOMMENDED), (None, S.WARNING)])
def test_duration_thresholds_and_missing_values(duration, level):
    item = finding(diagnose_compatibility(media(audio={"duration": duration})), "timing.duration_difference", "audio:0")
    assert item.severity is level and item.evidence["video_stream_index"] == 3


@pytest.mark.parametrize("start,level", [("0", S.INFO), ("0.05", S.INFO), ("0.051", S.REPAIR_RECOMMENDED),
    ("-0.051", S.REPAIR_RECOMMENDED), (None, S.WARNING)])
def test_start_offsets_use_seconds_and_existing_threshold(start, level):
    item = finding(diagnose_compatibility(media(audio={"start_time": start})), "timing.start_offset", "audio:0")
    assert item.severity is level


@pytest.mark.parametrize("fields,level", [({"start_time": "-0.001"}, S.REPAIR_RECOMMENDED),
    ({"start_time": "0", "start_pts": -1}, S.REPAIR_RECOMMENDED), ({"start_time": None, "start_pts": -1}, S.REPAIR_RECOMMENDED),
    ({"start_time": "0", "start_pts": 0}, S.INFO), ({"start_time": None, "start_pts": None}, S.WARNING)])
def test_negative_start_time_and_pts(fields, level):
    assert finding(diagnose_compatibility(media(fields)), "timing.negative_start").severity is level


def test_negative_container_start_is_independent_of_streams():
    report = diagnose_compatibility(parse_analysis(Path("x.mp4"), {"format": {"start_time": "-2"}, "streams": [VIDEO, AUDIO]}))
    assert finding(report, "timing.negative_start", "input").severity is S.REPAIR_RECOMMENDED
    assert finding(report, "timing.negative_start", "video:0").severity is S.INFO


@pytest.mark.parametrize("layout,channels,level", [("mono", 1, S.INFO), ("stereo", 2, S.INFO),
    ("stereo", 6, S.WARNING), ("5.1", 6, S.WARNING), ("7.1", 8, S.WARNING), (None, 2, S.WARNING), ("stereo", None, S.WARNING)])
def test_channel_layout_policy_does_not_call_surround_illegal(layout, channels, level):
    item = finding(diagnose_compatibility(media(audio={"channel_layout": layout, "channels": channels})), "audio.channel_layout", "audio:0")
    assert item.severity is level
    if level is S.WARNING:
        assert "仍是合法布局" in item.reason


@pytest.mark.parametrize("preset", ["general", "bilibili"])
@pytest.mark.parametrize("rate", ["48000", "44100", "96000", None])
def test_sample_rate_recommendation_depends_on_actual_preset(preset, rate):
    item = finding(diagnose_compatibility(media(audio={"sample_rate": rate}), preset=preset), "audio.sample_rate", "audio:0")
    expected = S.INFO if rate == "48000" else S.REPAIR_RECOMMENDED if rate and preset == "bilibili" else S.WARNING
    assert item.severity is expected


@pytest.mark.parametrize("fields", [{"tags": {"rotate": "90"}}, {"tags": {"rotate": "bad"}},
    {"side_data_list": [{"side_data_type": "Display Matrix", "rotation": 0}]},
    {"side_data_list": [{"side_data_type": "Display Matrix", "displaymatrix": "unparsed"}]}])
def test_rotation_or_matrix_metadata_requires_review(fields):
    assert finding(diagnose_compatibility(media(fields)), "video.rotation").severity is S.WARNING


def test_all_tracks_are_diagnosed_with_unambiguous_positions_and_original_indices():
    info = parse_analysis(Path("multi.mp4"), {"streams": [
        {"codec_type": "video", "index": 0, "disposition": {"attached_pic": 1}}, VIDEO,
        {**VIDEO, "index": 4, "codec_name": "hevc", "color_transfer": "smpte2084"}, AUDIO,
        {**AUDIO, "index": 9, "duration": "11", "sample_rate": "44100"}]})
    report = diagnose_compatibility(info)
    assert finding(report, "input.multiple_video", "input").severity is S.WARNING
    assert finding(report, "input.multiple_audio", "input").severity is S.WARNING
    assert finding(report, "video.dynamic_range", "video:1").severity is S.UNSUPPORTED
    assert finding(report, "timing.duration_difference", "audio:0").severity is S.INFO
    item = finding(report, "timing.duration_difference", "audio:1")
    assert item.stream_index == 9 and item.severity is S.REPAIR_RECOMMENDED
    assert len({(item.code, item.scope) for item in report.findings}) == len(report.findings)


@pytest.mark.parametrize("data,unsupported", [({}, False), ({"streams": []}, True),
    ({"streams": [{"codec_type": "unknown"}]}, False), ({"streams": [AUDIO]}, True),
    ({"streams": [{"codec_type": "video", "disposition": {"attached_pic": 1}}]}, True)])
def test_unknown_inventory_is_not_the_same_as_confirmed_no_video(data, unsupported):
    item = finding(diagnose_compatibility(parse_analysis(Path("x"), data)), "input.no_video", "input")
    assert item.severity is (S.UNSUPPORTED if unsupported else S.WARNING)


@pytest.mark.parametrize("value", [None, "", "N/A", "unknown", [], {}, True, "bad"])
def test_malformed_or_empty_fields_never_crash_and_keep_evidence(value):
    keys = {key: value for key in ("pix_fmt", "bits_per_raw_sample", "color_range", "color_space", "color_primaries",
        "color_transfer", "avg_frame_rate", "r_frame_rate", "start_time", "duration", "side_data_list", "tags")}
    info = media(keys, {"sample_rate": value, "channel_layout": value, "channels": value})
    report = diagnose_compatibility(info)
    assert "Input Compatibility Report" in format_compatibility_report(report)
    assert finding(report, "video.pixel_format").evidence["pix_fmt"]["raw"] == value
    json.dumps(asdict(report), ensure_ascii=False)


@pytest.mark.parametrize("mode", ["safe", "cfr", "timestamp", "audio-sync"])
@pytest.mark.parametrize("preset", ["general", "bilibili"])
def test_diagnosis_has_no_media_or_strategy_or_command_side_effects(mode, preset):
    info = media({"codec_name": "hevc", "pix_fmt": "yuv420p10le", "color_transfer": "bt709", "color_range": "pc"},
                 {"sample_rate": "44100", "duration": "10.7"})
    original = deepcopy(info)
    strategy = select_strategy(info, mode, preset=preset)
    command = build_fix_command("ffmpeg", info, Path("out.mp4"), strategy=strategy, preset=preset)
    report = diagnose_compatibility(info, preset=preset)
    assert any(item.severity is S.REPAIR_RECOMMENDED for item in report.findings)
    format_compatibility_report(report)
    assert info == original
    assert select_strategy(info, mode, preset=preset) == strategy
    assert build_fix_command("ffmpeg", info, Path("out.mp4"), strategy=strategy, preset=preset) == command
    finding(report, "video.color_range").evidence["color_range"]["raw"] = "edited"
    assert info == original


def test_invalid_preset_is_not_silently_treated_as_general():
    with pytest.raises(ValueError, match="未知输出预设"):
        diagnose_compatibility(media(), preset="typo")
