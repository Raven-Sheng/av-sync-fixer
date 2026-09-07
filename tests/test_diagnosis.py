"""第二阶段：数值容差、阈值边界与缺失信息的诊断。"""

from fractions import Fraction
from pathlib import Path

import pytest

from app.analyzer import diagnose, duration_risk_level, fps_to_float, parse_analysis
from app.cli import format_report


def media(video=None, audio=None):
    return parse_analysis(Path("录屏.mp4"), {"streams": [
        {"codec_type": "video", "index": 0, **(video or {})},
        {"codec_type": "audio", "index": 1, **(audio or {})},
    ]})


@pytest.mark.parametrize("value, expected", [
    ("60000/1001", 59.94005994005994), ("30000/1001", 29.97002997002997),
    ("60/1", 60.0), ("59.27", 59.27), (Fraction(25, 1), 25.0),
])
def test_fraction_fps_to_float(value, expected):
    assert fps_to_float(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", [None, "", "N/A", "bad", "0/0", "1/0", "0", "-25/1", "NaN", "inf", "1e400"])
def test_invalid_fps(value):
    assert fps_to_float(value) is None


@pytest.mark.parametrize("average, nominal, suspected", [
    ("60/1", "120/2", False),
    ("60000/1001", "60/1", False),
    ("59.9", "60", False),
    ("59.900001", "60", False),
    ("59.899999", "60", True),
    ("59.27", "60", True),
    ("60", "59.27", True),
    ("0/0", "60/1", None),
    ("60/1", None, None),
])
def test_fps_tolerance(average, nominal, suspected):
    result = diagnose(media({"avg_frame_rate": average, "r_frame_rate": nominal}))
    assert result.suspected_vfr is suspected


@pytest.mark.parametrize("difference, expected", [
    (0, "低"), (0.049999, "低"), (0.05, "轻微"), (0.050001, "轻微"),
    (0.199999, "轻微"), (0.2, "中等"), (0.200001, "中等"),
    (0.499999, "中等"), (0.5, "中等"), (0.500001, "较高"), (0.66, "较高"),
    (None, None), (-1, None), (float("nan"), None), (float("inf"), None),
])
def test_duration_risk_boundaries(difference, expected):
    assert duration_risk_level(difference) == expected


@pytest.mark.parametrize("video, audio, difference, expected", [
    ("10", "10.05", 0.05, "轻微"),
    ("10.05", "10", 0.05, "轻微"),
    ("10", "10.2", 0.2, "中等"),
    ("302.15", "302.65", 0.5, "中等"),
    ("302.15", "302.81", 0.66, "较高"),
    ("0", "0", 0, "低"),
])
def test_actual_duration_subtraction_boundaries(video, audio, difference, expected):
    result = diagnose(media({"duration": video}, {"duration": audio}))
    assert result.duration_diff == difference
    assert result.duration_risk == expected


@pytest.mark.parametrize("video, audio, difference, mismatch", [
    ("0", "0", 0, False), ("10", "10.05", 0.05, False),
    ("10", "10.050001", 0.050001, True),
    ("-0.25", "0", 0.25, True), ("0.25", "0", 0.25, True),
    ("N/A", "0", None, None), (None, "0", None, None),
    ("NaN", "0", None, None), ("inf", "0", None, None),
])
def test_start_time_comparison(video, audio, difference, mismatch):
    result = diagnose(media({"start_time": video}, {"start_time": audio}))
    assert result.start_time_diff == difference
    assert result.start_time_mismatch is mismatch


def test_missing_audio_does_not_report_low_risk():
    result = parse_analysis(Path("silent.mp4"), {"streams": [{
        "codec_type": "video", "duration": "10", "avg_frame_rate": "60", "r_frame_rate": "60",
    }]})
    diagnosis = diagnose(result)
    assert diagnosis.audio is None
    assert diagnosis.duration_diff is None
    assert diagnosis.duration_risk is None
    assert diagnosis.suspected_vfr is False
    report = format_report(result)
    assert "缺少音频轨" in report
    assert "同步风险：未知" in report


def test_audio_only_and_empty_media():
    for streams in ([], [{"codec_type": "audio", "duration": "10"}]):
        diagnosis = diagnose(parse_analysis(Path("test"), {"streams": streams}))
        assert diagnosis.duration_risk is None
        assert diagnosis.suspected_vfr is None
        assert any("缺少视频轨" in item for item in diagnosis.limitations)


def test_missing_fields_do_not_use_container_as_duration():
    result = parse_analysis(Path("test.mkv"), {
        "format": {"duration": "1000"},
        "streams": [{"codec_type": "video"}, {"codec_type": "audio", "duration": "1000"}],
    })
    diagnosis = diagnose(result)
    assert diagnosis.duration_diff is None
    assert diagnosis.start_time_diff is None
    assert diagnosis.duration_risk is None
    assert len(diagnosis.limitations) == 3
    assert "未知" in format_report(result)


def test_different_time_bases_alone_are_not_a_risk():
    result = diagnose(media(
        {"duration": "10", "start_time": "0", "time_base": "1/12800", "avg_frame_rate": "25", "r_frame_rate": "25"},
        {"duration": "10", "start_time": "0", "time_base": "1/48000"},
    ))
    assert result.duration_risk == "低"
    assert not result.potential_causes
    assert not result.limitations


def test_start_offset_is_reported_even_when_durations_match():
    result = media({"duration": "10", "start_time": "-0.25"}, {"duration": "10", "start_time": "0"})
    diagnosis = diagnose(result)
    assert diagnosis.video.start_time == -0.25
    assert diagnosis.duration_risk == "低"
    assert diagnosis.start_time_mismatch
    report = format_report(result)
    assert "存在明显起始偏移" in report
    assert "固定偏移" in report
    assert "等级按轨道长度差评估" in report


def test_report_example_and_multiple_tracks():
    result = parse_analysis(Path("录屏.mp4"), {"streams": [
        {"codec_type": "video", "index": 2, "avg_frame_rate": "59.27", "r_frame_rate": "60", "duration": "302.15"},
        {"codec_type": "audio", "index": 3, "duration": "302.81"},
        {"codec_type": "audio", "index": 4, "duration": "302.15"},
    ]})
    report = format_report(result)
    assert "比较轨道：视频 #2 / 音频 #3" in report
    assert "疑似可变帧率 VFR" in report
    assert "轨道长度差：0.660000 秒" in report
    assert "同步风险：较高" in report
    assert "音频与视频轨长度不一致" in report
    assert "多轨文件仅比较第一条视频轨与第一条音频轨" in report
    assert "音频轨道 #4" in report


def test_duration_ticks_still_used_by_diagnosis():
    result = diagnose(media(
        {"duration_ts": 250, "time_base": "1/25"},
        {"duration_ts": 504000, "time_base": "1/48000"},
    ))
    assert result.duration_diff == 0.5
    assert result.duration_risk == "中等"


@pytest.mark.parametrize("ticks, extra_ticks, difference, risk", [
    (2000, 3, 0.05, "轻微"), (2020, 3, 0.05, "轻微"),
    (2000, 12, 0.2, "中等"), (2020, 12, 0.2, "中等"),
    (2000, 30, 0.5, "中等"), (2020, 30, 0.5, "中等"),
])
def test_fractional_tick_duration_boundaries(ticks, extra_ticks, difference, risk):
    result = diagnose(media(
        {"duration_ts": ticks, "time_base": "1/60"},
        {"duration_ts": ticks + extra_ticks, "time_base": "1/60"},
    ))
    assert result.duration_diff == pytest.approx(difference)
    assert result.duration_risk == risk
    assert any("轨长度不一致" in cause for cause in result.potential_causes)


def test_repeating_fraction_fps_at_tolerance():
    # 13/30 - 1/3 恰好是 0.1，不应因浮点舍入标记疑似 VFR。
    result = diagnose(media({"avg_frame_rate": "1/3", "r_frame_rate": "13/30"}))
    assert result.fps_diff == pytest.approx(0.1)
    assert result.suspected_vfr is False


@pytest.mark.parametrize("difference, risk", [
    (0.04999999, "低"), (0.05000001, "轻微"),
    (0.19999999, "轻微"), (0.20000001, "中等"),
    (0.49999999, "中等"), (0.50000001, "较高"),
])
def test_rounding_tolerance_does_not_hide_real_duration_differences(difference, risk):
    assert duration_risk_level(difference) == risk


@pytest.mark.parametrize("nominal, suspected", [("0.09999999", False), ("0.10000001", True)])
def test_rounding_tolerance_does_not_hide_real_fps_differences(nominal, suspected):
    average = Fraction(1, 3)
    result = diagnose(media({"avg_frame_rate": str(average), "r_frame_rate": str(average + Fraction(nominal))}))
    assert result.suspected_vfr is suspected


@pytest.mark.parametrize("start, mismatch", [("0.04999999", False), ("0.05000001", True)])
def test_rounding_tolerance_does_not_hide_real_start_offsets(start, mismatch):
    result = diagnose(media({"start_time": "0"}, {"start_time": start}))
    assert result.start_time_mismatch is mismatch
