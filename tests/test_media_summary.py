"""User-facing summaries must reflect existing decisions, never invent repairs."""

from dataclasses import replace
from pathlib import Path

import pytest

from app.analyzer import parse_analysis
from app.media_summary import build_media_summary, comparison_text, validation_summary, recording_origin
from app.models import OutputValidation, ValidationCheck, ValidationLevel, DeepSyncAnalysis, TimestampEvidence


@pytest.fixture
def recording():
    return parse_analysis(Path("中文 Steam Recording.mp4"), {
        "format": {"duration": "10", "start_time": "0"},
        "streams": [
            {"codec_type": "video", "index": 0, "codec_name": "hevc", "width": 1920, "height": 1080,
             "pix_fmt": "yuvj420p", "color_range": "pc", "color_space": "bt709", "color_primaries": "bt709",
             "color_transfer": "bt709", "duration": "10", "start_time": "0", "avg_frame_rate": "60", "r_frame_rate": "60"},
            {"codec_type": "audio", "index": 1, "codec_name": "aac", "duration": "10", "start_time": "0", "sample_rate": "48000"},
        ]})


def test_full_range_summary_uses_core_plan_without_enabling_unneeded_sync(recording):
    result = build_media_summary(recording, "safe", "general")
    assert result.fields["video_codec"] == "HEVC / H.265"
    assert result.fields["pixel_format"] == "yuvj420p"
    assert result.fields["color"] == "Full Range · BT.709"
    assert "48 kHz" in result.fields["audio"]
    assert "Full → Limited 颜色范围转换" in result.processing
    assert result.plan.video.fps_mode == "preserve"
    assert result.plan.video.timestamp_strategy == "preserve"
    assert all(a.sync_strategy == "preserve" for a in result.plan.audios)
    assert "实际颜色数值" in " ".join(result.reasons)
    text = "\n".join((*result.fields.values(), *result.processing, *result.reasons, *result.compatibility))
    assert all(technical not in text for technical in ("time_base", "PTS", "DTS"))


def test_switch_preset_and_mode_reuses_resolved_cfr_policy(recording):
    general = build_media_summary(recording, "safe", "general")
    preset = build_media_summary(recording, "safe", "bilibili")
    explicit = build_media_summary(recording, "cfr", "general")
    vfr = replace(recording, videos=(replace(recording.videos[0], avg_frame_rate=recording.videos[0].avg_frame_rate - 1),))
    automatic = build_media_summary(vfr, "safe", "general")
    assert not general.plan.strategy.cfr
    assert preset.plan.strategy.cfr and explicit.plan.strategy.cfr and automatic.plan.strategy.cfr
    assert "Bilibili" in " ".join(preset.reasons)
    assert "显式选择" in " ".join(explicit.reasons)
    assert "Suspected VFR" in " ".join(automatic.reasons)


@pytest.mark.parametrize("metadata,expected", [({}, False), ({"title": "Steam"}, False),
    ({"encoder": "Steamroller"}, False), ({"encoder": "Steam Game Recording"}, True)])
def test_steam_hint_requires_software_metadata_not_name_or_format(recording, metadata, expected):
    text = recording_origin(replace(recording, metadata=metadata))
    assert ("可能来自 Steam" in text) == expected
    assert "来源未验证" in text if expected else "来源未确认" in text


@pytest.mark.parametrize("video", [{"color_range": None, "pixel_format": "yuv420p"}, {"color_transfer": "smpte2084"}])
def test_unknown_color_and_hdr_show_core_block_reason(recording, video):
    source = replace(recording, videos=(replace(recording.videos[0], **video),))
    result = build_media_summary(source, "safe", "general")
    assert result.plan is None and result.blocked
    assert not result.processing


def test_silent_and_multiple_audio_are_not_hidden(recording):
    silent = replace(recording, audios=())
    result = build_media_summary(silent, "safe", "general")
    assert result.fields["audio"] == "无音频轨"
    assert "无音频轨 · 不生成音频" in result.processing
    assert build_media_summary(silent, "audio-sync", "general").blocked
    multiple = replace(recording, audios=(*recording.audios, replace(recording.audios[0], index=2, sample_rate=44100)))
    result = build_media_summary(multiple, "safe", "bilibili")
    assert "44.1 kHz" in result.fields["audio"]
    assert "音轨 2：AAC · 48 kHz" in result.processing


def test_missing_validation_and_missing_sections_are_not_pass(recording):
    assert set(validation_summary(None).values()) == {"NOT TESTED"}
    report = OutputValidation("general", recording, None, checks=(
        ValidationCheck("Color", "color.range", ValidationLevel.FAIL, "wrong range"),
        ValidationCheck("Timing", "audio.difference", ValidationLevel.WARNING, "tail difference"),))
    summary = validation_summary(report)
    assert summary["颜色"] == "FAIL"
    assert summary["同步"] == "WARNING"
    assert summary["编码"] == summary["音频"] == summary["平台兼容"] == "NOT TESTED"


def test_comparison_reads_actual_output_and_does_not_fill_unknown_tags(recording):
    after = replace(recording, videos=(replace(recording.videos[0], codec="h264", pixel_format="yuv420p",
                    color_range="tv", color_range_name="Limited", color_primaries=None),))
    assert "HEVC" in comparison_text(recording) and "Full Range" in comparison_text(recording)
    assert "H.264" in comparison_text(after) and "Limited Range" in comparison_text(after)
    assert "原色 未知" in comparison_text(after)


@pytest.mark.parametrize("data", [{}, {"streams": None}, {"streams": [None]}])
def test_unreadable_track_inventory_is_unknown_in_every_summary_field(data):
    source = parse_analysis(Path("unreadable.mp4"), data)
    result = build_media_summary(source, "safe", "general")
    assert "未知" in result.fields["video_codec"]
    assert "未知" in result.fields["video_duration"]
    assert "未知" in result.fields["audio_duration"]
    assert "无视频轨" not in comparison_text(source)
    assert "无音频轨" not in comparison_text(source)


def test_complete_empty_inventory_still_reports_absent_tracks():
    source = parse_analysis(Path("empty.mp4"), {"streams": []})
    result = build_media_summary(source, "safe", "general")
    assert result.fields["video_codec"] == "无视频轨"
    assert result.fields["audio"] == result.fields["audio_duration"] == "无音频轨"


def test_incomplete_inventory_does_not_claim_silence_in_processing(recording):
    source = parse_analysis(recording.path, {"streams": [recording.probe_fields["streams"].raw[0], {"codec_type": "unknown"}]})
    result = build_media_summary(source, "safe", "general")
    assert result.plan is not None and not result.plan.audios
    assert result.fields["audio"] == result.fields["audio_duration"] == "音频信息未知"
    assert "音频信息未知 · 不生成音频" in result.processing
    assert "无音频轨" not in " ".join(result.processing)


def test_deep_frame_evidence_not_hidden_by_equal_fps_metadata(recording):
    evidence = TimestampEvidence(0, "video", frames=120, vfr_suspected=True, patterns=("VFR_SUSPECTED",))
    source = replace(recording, deep_sync=DeepSyncAnalysis(evidence, (), (), ("VFR_SUSPECTED",), ()))
    summary = build_media_summary(source, "safe", "general")
    assert summary.plan.video.fps_mode == "cfr"
    assert "疑似 VFR" in summary.fields["vfr"] and "抽样" in summary.fields["vfr"]


def test_negative_frame_sample_does_not_claim_whole_file_cfr(recording):
    evidence = TimestampEvidence(0, "video", frames=120, vfr_suspected=False)
    source = replace(recording, deep_sync=DeepSyncAnalysis(evidence, (), (), ("UNKNOWN",), ()))
    summary = build_media_summary(source, "safe", "general")
    assert "抽样" in summary.fields["vfr"] and "不能" in summary.fields["vfr"]
