"""Plan-driven output checks, timing regressions, and the publication boundary."""

from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from app.analyzer import parse_analysis
from app.ffmpeg_utils import MediaError
from app.fixer import prepare_fix, execute_fix
from app.models import ValidationLevel
from app.output_validation import validate_output, validate_output_file, _faststart
from app.presets import format_validation_report
from app.repair_plan import expected_output_spec
from tests.mp4_stub import MP4_STUB, box


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr("app.fixer.find_tool", lambda _: "ffmpeg")
    path = tmp_path / "-中文 空格 & 源.mp4"
    path.write_bytes(b"source")
    return parse_analysis(path, {"format": {"format_name": "mov,mp4", "duration": "10", "tags": {"major_brand": "isom"}},
        "streams": [
            {"codec_type": "video", "index": 0, "codec_name": "h264", "pix_fmt": "yuv420p", "color_range": "tv",
             "color_space": "bt709", "color_transfer": "bt709", "color_primaries": "bt709",
             "width": 160, "height": 90, "avg_frame_rate": "60", "r_frame_rate": "60", "duration": "10", "start_time": "0"},
            {"codec_type": "audio", "index": 1, "codec_name": "aac", "sample_rate": "48000", "duration": "10", "start_time": "0"}]})


def spec(source, preset="general", mode="safe"):
    return prepare_fix(source, source.path.parent / "out", preset=preset, mode=mode).expected


@pytest.mark.parametrize("evidence", [
    {"sampled_hdr_side_data_types": ("Mastering display metadata",)},
    {"sampled_color_transfers": ("arib-std-b67",)},
    {"side_data_list": ({"side_data_type": "DOVI configuration record"},)},
])
def test_unexpected_hdr_evidence_fails_output_even_when_stream_tags_match(source, evidence):
    after = replace(source, videos=(replace(source.videos[0], **evidence),))
    report = validate_output(spec(source), after)
    assert report.overall == ValidationLevel.FAIL
    assert any(c.code == "color.hdr" and c.level == ValidationLevel.FAIL for c in report.checks)


@pytest.mark.parametrize("preset", ["general", "bilibili"])
def test_hevc_full_range_input_does_not_fail_correct_output(source, preset):
    original = replace(source, videos=(replace(source.videos[0], codec="hevc", pixel_format="yuvj420p", color_range="pc"),))
    expected = spec(original, preset)
    assert expected.video.codec == "h264" and expected.video.color.output_range == "tv"
    result = validate_output(expected, source)
    assert result.overall == ValidationLevel.PASS
    assert all(level == ValidationLevel.PASS for level in result.sections.values())
    report = format_validation_report(result)
    for section in ("Video", "Audio", "Color", "Timing", "Compatibility", "Overall"):
        assert f"{section}: PASS" in report


def test_validator_reads_custom_spec_instead_of_preset_constants(source):
    expected = spec(source)
    color = replace(expected.video.color, pixel_format="yuv444p", output_range="pc")
    expected = replace(expected, video=replace(expected.video, codec="hevc", pixel_format="yuv444p", color=color),
                       audios=(replace(expected.audios[0], codec="opus", sample_rate=44100),))
    after = replace(source, videos=(replace(source.videos[0], codec="hevc", pixel_format="yuv444p", color_range="pc"),),
                    audios=(replace(source.audios[0], codec="opus", sample_rate=44100),))
    assert validate_output(expected, after).overall == ValidationLevel.PASS


def test_preserved_fps_tolerance_comes_from_spec(source):
    expected = spec(source)
    expected = replace(expected, video=replace(expected.video, fps_tolerance=0.25))
    after = replace(source, videos=(replace(source.videos[0], avg_frame_rate=Fraction(602, 10)),))
    assert not validate_output(expected, after).errors


@pytest.mark.parametrize("input_rate,expected_rate", [(44100, 44100), (48000, 48000), (192000, 96000),
                                                     (10000, 11025), (46050, 48000), (None, None)])
def test_expected_rate_follows_native_encoder_policy(source, input_rate, expected_rate):
    before = replace(source, audios=(replace(source.audios[0], sample_rate=input_rate),))
    assert spec(before).audios[0].sample_rate == expected_rate
    assert spec(before, preset="bilibili").audios[0].sample_rate == 48000


@pytest.mark.parametrize("field,value,code", [
    ("codec", "hevc", "video.codec"), ("pixel_format", "yuvj420p", "color.pixel_format"),
    ("pixel_format", None, "color.pixel_format"), ("color_range", "pc", "color.range"),
    ("color_range", None, "color.range"), ("color_space", "smpte170m", "color.color_space"),
    ("avg_frame_rate", Fraction(30), "video.fps"), ("r_frame_rate", None, "video.fps"),
    ("duration", 0, "Video.duration"), ("duration", 2, "Video.duration"),
    ("duration", 13, "Video.duration"), ("start_time", 1, "video.start"), ("start_time", -1, "video.start"),
])
def test_actual_video_mismatch_fails(source, field, value, code):
    after = replace(source, videos=(replace(source.videos[0], **{field: value}),))
    result = validate_output(spec(source, mode="cfr"), after)
    assert any(c.code == code and c.level == ValidationLevel.FAIL for c in result.checks)
    assert result.overall == ValidationLevel.FAIL


@pytest.mark.parametrize("field", ["color_space", "color_transfer", "color_primaries"])
@pytest.mark.parametrize("preset", ["general", "bilibili"])
def test_missing_color_tag_warns_and_publishes_only_with_verified_range(source, monkeypatch, field, preset):
    plan = prepare_fix(source, source.path.parent / "out", preset=preset)
    after = replace(source, videos=(replace(source.videos[0], **{field: None}),))
    monkeypatch.setattr("app.fixer.run_ffmpeg", lambda argv, callback, **kwargs: Path(argv[-1]).write_bytes(MP4_STUB))
    monkeypatch.setattr("app.fixer.analyze", lambda path: replace(after, path=path))
    result = execute_fix(plan)
    assert result.validation.overall == ValidationLevel.WARNING
    assert result.validation.sections["Color"] == ValidationLevel.WARNING
    assert result.after.path.exists() and source.path.read_bytes() == b"source"
    unproven = replace(after, videos=(replace(after.videos[0], color_range=None),))
    assert validate_output(plan.expected, unproven).overall == ValidationLevel.FAIL


@pytest.mark.parametrize("preset", ["general", "bilibili"])
@pytest.mark.parametrize("duration", [7, 13])
def test_point_two_to_three_seconds_diff_fails_even_with_correct_encoding(source, preset, duration):
    before = replace(source, audios=(replace(source.audios[0], duration=9.8),))
    after = replace(source, audios=(replace(source.audios[0], duration=duration),))
    result = validate_output(spec(before, preset), after)
    regression = next(c for c in result.checks if c.code == "audio.1.difference")
    assert regression.expected == pytest.approx(0.2) and regression.actual == 3
    assert regression.level == ValidationLevel.FAIL
    assert result.sections["Timing"] == ValidationLevel.FAIL
    assert result.sections["Video"] == result.sections["Audio"] == result.sections["Color"] == ValidationLevel.PASS


@pytest.mark.parametrize("duration", [9.8, 10.2, 7, 13])
def test_existing_duration_difference_is_warning_not_output_failure(source, duration):
    source = replace(source, container_duration=max(10, duration), audios=(replace(source.audios[0], duration=duration),))
    result = validate_output(spec(source), source)
    assert result.overall == ValidationLevel.WARNING and not result.errors


@pytest.mark.parametrize("delta,fail", [(0.04, False), (0.1, False), (0.10001, True)])
def test_duration_regression_tolerance_boundary(source, delta, fail):
    after = replace(source, audios=(replace(source.audios[0], duration=10 + delta),))
    assert bool(validate_output(spec(source), after).errors) == fail


def test_low_fps_cannot_hide_multi_second_sync_regression(source):
    before = replace(source, videos=(replace(source.videos[0], avg_frame_rate=Fraction(1, 10), r_frame_rate=Fraction(1, 10)),),
                     audios=(replace(source.audios[0], duration=9.8),))
    after = replace(before, audios=(replace(before.audios[0], duration=13),))
    result = validate_output(spec(before), after)
    assert any(c.code == "audio.1.difference" and c.level == ValidationLevel.FAIL for c in result.checks)


def test_low_fps_cannot_hide_unplanned_audio_start_offset(source):
    before = replace(source, videos=(replace(source.videos[0], avg_frame_rate=Fraction(2), r_frame_rate=Fraction(2)),))
    after = replace(before, audios=(replace(before.audios[0], start_time=0.3),))
    assert any(c.code == "audio.1.start" and c.level == ValidationLevel.FAIL
               for c in validate_output(spec(before), after).checks)


def test_unreasonable_container_duration_fails_even_if_stream_durations_match(source):
    result = validate_output(spec(source), replace(source, container_duration=1000))
    assert any(c.code == "container.duration" and c.level == ValidationLevel.FAIL for c in result.checks)


@pytest.mark.parametrize("raw", ["NaN", "-2", "broken"])
def test_invalid_duration_metadata_fails_instead_of_becoming_unknown_warning(source, raw):
    from app.models import ProbeField, FieldState
    video = replace(source.videos[0], duration=None, probe_fields={"duration": ProbeField(FieldState.INVALID, raw)})
    result = validate_output(spec(source), replace(source, videos=(video,)))
    assert any(c.code == "Video.duration" and c.level == ValidationLevel.FAIL for c in result.checks)


def test_both_tracks_truncated_equally_still_fail(source):
    after = replace(source, videos=(replace(source.videos[0], duration=1),), audios=(replace(source.audios[0], duration=1),))
    result = validate_output(spec(source), after)
    assert result.sections["Timing"] == ValidationLevel.FAIL
    assert any(c.code == "audio.1.difference" and c.level == ValidationLevel.PASS for c in result.checks)


@pytest.mark.parametrize("offset", [-0.2, 0.2])
def test_planned_relative_offset_and_common_mux_shift_are_preserved(source, offset):
    before = replace(source, audios=(replace(source.audios[0], start_time=offset),))
    expected = spec(before)
    vstart = expected.video.start_offset + 0.067
    after = replace(source, container_duration=10 + max(vstart, vstart + offset), videos=(replace(source.videos[0], start_time=vstart),),
                    audios=(replace(source.audios[0], start_time=vstart + offset),))
    assert not validate_output(expected, after).errors
    broken = replace(after, audios=(replace(after.audios[0], start_time=vstart),))
    assert validate_output(expected, broken).sections["Timing"] == ValidationLevel.FAIL


def test_every_audio_track_is_compared_to_its_own_plan(source):
    source = replace(source, audios=(source.audios[0], replace(source.audios[0], index=7, sample_rate=44100)))
    expected = spec(source)
    assert [a.sample_rate for a in expected.audios] == [48000, 44100]
    after = replace(source, audios=(source.audios[0], replace(source.audios[1], sample_rate=48000, duration=13)))
    result = validate_output(expected, after)
    assert any(c.code == "audio.2.rate" and c.level == ValidationLevel.FAIL for c in result.checks)
    assert any(c.code == "audio.2.difference" and c.level == ValidationLevel.FAIL for c in result.checks)


def test_unknown_timing_does_not_claim_pass(source):
    before = replace(source, videos=(replace(source.videos[0], duration=None),), audios=(replace(source.audios[0], duration=None),))
    result = validate_output(spec(before), before)
    assert result.sections["Timing"] == ValidationLevel.WARNING


@pytest.mark.parametrize("failure", ["missing", "empty", "directory", "probe", "timing"])
def test_failed_file_or_timing_reports_before_rejection_and_never_publishes(source, monkeypatch, failure):
    plan = prepare_fix(source, source.path.parent / "out")
    def encode(argv, callback, **kwargs):
        path = Path(argv[-1])
        if failure == "missing":
            return
        if failure == "directory":
            path.mkdir()
        else:
            path.write_bytes(b"" if failure == "empty" else MP4_STUB)
    def probe(path):
        if failure == "probe":
            raise MediaError("ffprobe rejected output")
        if failure in {"missing", "empty", "directory"}:
            pytest.fail("Unreadable files must be rejected before probing")
        return replace(source, path=path, audios=(replace(source.audios[0], duration=13),))
    monkeypatch.setattr("app.fixer.run_ffmpeg", encode)
    monkeypatch.setattr("app.fixer.analyze", probe)
    reports = []
    def report(result):
        assert not plan.output_path.exists()
        reports.append(result)
    with pytest.raises(MediaError, match="未发布"):
        execute_fix(plan, on_validation=report)
    assert len(reports) == 1 and reports[0].overall == ValidationLevel.FAIL
    assert "Overall: FAIL" in format_validation_report(reports[0])
    assert list(plan.output_path.parent.iterdir()) == []
    assert source.path.read_bytes() == b"source"


@pytest.mark.parametrize("data,okay", [(MP4_STUB, True), (box(b"mdat") + box(b"moov"), False),
    (box(b"moov"), False), (b"broken", False), (b"\0\0\0\1moov" + (16).to_bytes(8, "big") + box(b"mdat"), True),
    (box(b"moov") + b"\0\0\0\0mdat", True), (b"\0\0\0\4moov", False)])
def test_faststart_box_headers_are_verified(source, data, okay):
    path = source.path.parent / "输出 中文.mp4"
    path.write_bytes(data)
    result = validate_output_file(spec(source), path, probe=lambda p: replace(source, path=p))
    assert (not result.errors) == okay


def test_faststart_scan_has_record_budget(source, monkeypatch):
    source.path.write_bytes(box(b"free") * 3 + MP4_STUB)
    monkeypatch.setattr("app.output_validation.MAX_MP4_BOXES", 2)
    with pytest.raises(ValueError, match="上限"):
        _faststart(source.path)


def test_faststart_exact_box_budget_can_complete(source, monkeypatch):
    source.path.write_bytes(box(b"moov") + box(b"mdat"))
    monkeypatch.setattr("app.output_validation.MAX_MP4_BOXES", 2)
    assert _faststart(source.path)


def test_unknown_video_duration_does_not_retain_dropped_stream_container_length(source):
    before = replace(source, container_duration=1000,
                     videos=(replace(source.videos[0], duration=None), replace(source.videos[0], index=7, duration=1000)))
    result = validate_output(spec(before), source)
    assert not result.errors
    assert result.sections["Timing"] == ValidationLevel.WARNING


def test_container_consistency_is_checked_even_when_input_video_duration_unknown(source):
    before = replace(source, container_duration=1000, videos=(replace(source.videos[0], duration=None),))
    after = replace(source, container_duration=1000)
    result = validate_output(spec(before), after)
    assert any(c.code == "container.duration" and c.level == ValidationLevel.FAIL for c in result.checks)


@pytest.mark.parametrize("target", ["video", "audio", "container"])
def test_invalid_timestamp_or_container_duration_is_not_a_missing_field_warning(source, target):
    from app.models import ProbeField, FieldState
    if target == "container":
        after = replace(source, container_duration=None, probe_fields={"duration": ProbeField(FieldState.INVALID, "NaN")})
    else:
        name = "videos" if target == "video" else "audios"
        stream = getattr(source, name)[0]
        after = replace(source, **{name: (replace(stream, start_time=None, probe_fields={"start_time": ProbeField(FieldState.INVALID, "NaN")}),)})
    assert validate_output(spec(source), after).sections["Timing"] == ValidationLevel.FAIL


def test_expected_geometry_follows_rotation_and_padding(source):
    source = replace(source, videos=(replace(source.videos[0], width=161, height=91, rotation=90),))
    expected = spec(source)
    assert (expected.video.width, expected.video.height) == (92, 162)
    repair = prepare_fix(source).repair
    assert expected == expected_output_spec(repair, source)
