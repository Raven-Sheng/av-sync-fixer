"""预设选择、输出复查和失败发布边界，不需要真实 FFmpeg。"""

from dataclasses import replace
from fractions import Fraction
from pathlib import Path

from tests.mp4_stub import MP4_STUB

import pytest

from app.analyzer import parse_analysis
from app.cli import main
from app.ffmpeg_utils import MediaError, build_fix_command
from app.fixer import REPAIR_MODES, execute_fix, prepare_fix, select_strategy
from app.presets import format_validation_report, validate_bilibili_output


@pytest.fixture
def source(tmp_path, monkeypatch):
    path = tmp_path / "中文 视频.mp4"
    path.write_bytes(b"original")
    monkeypatch.setattr("app.fixer.find_tool", lambda _: "ffmpeg")
    return parse_analysis(path, {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "tags": {"major_brand": "isom"}},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p", "color_range": "tv", "index": 0,
             "color_space": "bt709", "color_transfer": "bt709", "color_primaries": "bt709",
             "width": 160, "height": 90, "avg_frame_rate": "30/1", "r_frame_rate": "60/2",
             "start_time": "0", "duration": "2"},
            {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "index": 1,
             "start_time": "0", "duration": "2"},
        ],
    })


@pytest.mark.parametrize("tags", [None, {}, [], "invalid", {"major_brand": 123}])
def test_optional_preset_fields_are_safe(tmp_path, tags):
    info = parse_analysis(tmp_path / "input", {"format": {"tags": tags}, "streams": [{"codec_type": "video"}]})
    assert info.major_brand is None
    assert info.videos[0].pixel_format is None


@pytest.mark.parametrize("mode", REPAIR_MODES)
def test_bilibili_forces_cfr_and_48k_without_changing_resolution(source, tmp_path, mode):
    plan = prepare_fix(source, tmp_path / "out", mode=mode, preset="bilibili")
    command = plan.command
    assert plan.strategy.cfr and plan.strategy.target_fps == 30
    for flag, expected in (("-c:v", "libx264"), ("-pix_fmt", "yuv420p"), ("-preset", "medium"),
                           ("-crf", "18"), ("-c:a", "aac"), ("-b:a", "192k"), ("-ar", "48000"),
                           ("-f", "mp4"), ("-movflags", "+faststart")):
        assert command[command.index(flag) + 1] == expected
    assert command.index("-noautorotate") < command.index("-i")
    filters = command[command.index("-vf") + 1]
    assert "fps=fps=30" in filters
    assert not any(item in filters for item in ("pad=", "scale=", "crop="))
    assert not any(flag in command for flag in ("-shortest", "-t", "-to", "-copyts", "-start_at_zero"))
    assert ("+genpts" in command) == (mode == "timestamp")
    assert any("aresample=async" in item for item in command) == (mode == "audio-sync")
    assert not plan.output_path.parent.exists()


def test_general_default_keeps_existing_mode_and_sample_rate_policy(source, tmp_path):
    plan = prepare_fix(source, tmp_path / "out")
    assert plan.preset == "general"
    assert not plan.strategy.cfr
    assert "-ar" not in plan.command and "-noautorotate" not in plan.command
    assert "pad=" in plan.command[plan.command.index("-vf") + 1]


def test_bilibili_safe_selects_only_diagnosed_audio_and_timestamp_repairs(source, tmp_path):
    source = replace(source, audios=(source.audios[0], replace(source.audios[0], index=2, duration=2.8, start_time=0.3)))
    plan = prepare_fix(source, tmp_path / "out", preset="bilibili")
    assert plan.strategy.cfr and plan.strategy.timestamp
    assert plan.strategy.audio_sync_tracks == ()
    assert "aresample" not in plan.command[plan.command.index("-filter:a:0") + 1]
    assert "aresample" not in plan.command[plan.command.index("-filter:a:1") + 1]
    assert plan.command.count("-ar") == 1  # 全部输出音轨适用。


@pytest.mark.parametrize("width,height,message", [(161, 90, "奇数"), (160, 91, "奇数"), (None, 90, "分辨率")])
def test_bilibili_rejects_incompatible_dimensions_before_encoding(source, tmp_path, width, height, message):
    info = replace(source, videos=(replace(source.videos[0], width=width, height=height),))
    with pytest.raises(MediaError, match=message):
        prepare_fix(info, tmp_path / "out", preset="bilibili")
    assert not (tmp_path / "out").exists()
    assert source.path.read_bytes() == b"original"


def test_bilibili_builder_rejects_non_cfr_strategy(source, tmp_path):
    with pytest.raises(MediaError, match="必须启用 CFR"):
        build_fix_command("ffmpeg", source, tmp_path / "out.mp4", strategy=select_strategy(source), preset="bilibili")


@pytest.mark.parametrize("args", [["--preset", "bilibili"], ["--fix", "--preset", "unknown"]])
def test_cli_rejects_invalid_preset_usage(args):
    with pytest.raises(SystemExit) as error:
        main(["input.mp4", *args])
    assert error.value.code == 2


def test_unknown_preset_rejected_by_core(source, tmp_path):
    with pytest.raises(MediaError, match="不支持的输出预设"):
        prepare_fix(source, tmp_path / "out", preset="unknown")


def test_preset_dry_run_has_no_output_or_transcode(source, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("app.cli.analyze", lambda _: source)
    monkeypatch.setattr("app.fixer.OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr("app.fixer.run_ffmpeg", lambda *args: pytest.fail("dry-run 不应执行"))
    assert main([str(source.path), "--fix", "--preset", "bilibili", "--dry-run"]) == 0
    report = capsys.readouterr().out
    assert "Bilibili" in report and "48000" in report and "fps=fps=30" in report
    assert "输出验证报告" not in report
    assert not (tmp_path / "out").exists()


def test_valid_output_report_has_all_required_fields(source, tmp_path):
    plan = prepare_fix(source, tmp_path / "out", preset="bilibili")
    result = validate_bilibili_output(plan, source)
    assert not result.errors and not result.warnings
    text = format_validation_report(result)
    for label in ("容器", "视频编码", "像素格式", "分辨率", "帧率", "采样率", "视频时长", "音频时长", "轨道差异", "预设编码要求通过"):
        assert label in text


@pytest.mark.parametrize("field,value,message", [
    ("codec", "hevc", "H.264"), ("pixel_format", "yuv444p", "yuv420p"),
    ("pixel_format", None, "yuv420p"), ("width", 162, "分辨率"), ("width", None, "分辨率"),
    ("avg_frame_rate", Fraction(29), "FPS"), ("r_frame_rate", None, "FPS"),
])
def test_video_requirement_failures(source, tmp_path, field, value, message):
    plan = prepare_fix(source, tmp_path / "out", preset="bilibili")
    after = replace(source, videos=(replace(source.videos[0], **{field: value}),))
    result = validate_bilibili_output(plan, after)
    assert any(message in error for error in result.errors)
    assert "不符合预设" in format_validation_report(result)


@pytest.mark.parametrize("field,value", [("codec", "opus"), ("sample_rate", 44100), ("sample_rate", None)])
def test_every_audio_track_must_satisfy_preset(source, tmp_path, field, value):
    info = replace(source, audios=(source.audios[0], replace(source.audios[0], index=2)))
    plan = prepare_fix(info, tmp_path / "out", preset="bilibili")
    after = replace(info, audios=(info.audios[0], replace(info.audios[1], **{field: value})))
    result = validate_bilibili_output(plan, after)
    assert any("音轨 2" in error and "48000" in error for error in result.errors)


@pytest.mark.parametrize("container,brand", [(None, None), ("matroska,webm", "isom"), ("mov,mp4,m4a,3gp,3g2,mj2", "qt  "), ("mov,mp4", None)])
def test_mp4_alias_alone_does_not_validate_container(source, tmp_path, container, brand):
    plan = prepare_fix(source, tmp_path / "out", preset="bilibili")
    result = validate_bilibili_output(plan, replace(source, container=container, major_brand=brand))
    assert any("MP4 容器" in error for error in result.errors)


def test_missing_tracks_are_reported_without_crash(source, tmp_path):
    plan = prepare_fix(source, tmp_path / "out", preset="bilibili")
    result = validate_bilibili_output(plan, replace(source, videos=(), audios=()))
    assert len(result.errors) == 2
    assert "未知" in format_validation_report(result)


def test_silent_input_stays_silent_and_passes(source, tmp_path):
    source = replace(source, audios=())
    plan = prepare_fix(source, tmp_path / "out", preset="bilibili")
    assert "-an" in plan.command and "-ar" not in plan.command
    result = validate_bilibili_output(plan, source)
    assert not result.errors and not result.warnings
    assert "无音频轨" in format_validation_report(result)


@pytest.mark.parametrize("duration,warning", [(2.04, False), (2.199, False), (2.2, True), (2.8, True), (1.2, True), (None, True)])
def test_existing_length_difference_warns_without_rejecting_encoding(source, tmp_path, duration, warning):
    source = replace(source, audios=(replace(source.audios[0], duration=duration),))
    plan = prepare_fix(source, tmp_path / "out", preset="bilibili")
    after = replace(source, audios=(replace(source.audios[0], duration=duration),))
    result = validate_bilibili_output(plan, after)
    assert not result.errors
    assert bool(result.warnings) == warning
    if duration is None:
        assert "轨道差异：未知" in format_validation_report(result)


@pytest.mark.parametrize("start", [None, -0.1, 0.3])
def test_timestamp_risks_remain_explicit(source, tmp_path, start):
    plan = prepare_fix(source, tmp_path / "out", preset="bilibili")
    result = validate_bilibili_output(plan, replace(source, audios=(replace(source.audios[0], start_time=start),)))
    assert (bool(result.errors), bool(result.warnings)) == ((False, True) if start is None else (True, False))


@pytest.mark.parametrize("valid", [True, False])
def test_verification_report_precedes_publish_or_rejection(source, tmp_path, monkeypatch, valid):
    plan = prepare_fix(source, tmp_path / "out", preset="bilibili")
    def encode(command, callback, **kwargs):
        Path(command[-1]).write_bytes(MP4_STUB)
    monkeypatch.setattr("app.fixer.run_ffmpeg", encode)
    monkeypatch.setattr("app.fixer.analyze", lambda path: replace(
        source, path=path, audios=(replace(source.audios[0], sample_rate=48000 if valid else 44100),)))
    reports = []
    def report(result):
        assert not plan.output_path.exists()
        reports.append(result)
    if valid:
        result = execute_fix(plan, on_validation=report)
        assert result.after.path.read_bytes() == MP4_STUB
        assert result.validation.media.path == result.after.path
    else:
        with pytest.raises(MediaError, match="不符合 Bilibili"):
            execute_fix(plan, on_validation=report)
        assert not plan.output_path.exists()
        assert reports[0].errors
    assert len(reports) == 1
    assert not list(plan.output_path.parent.glob(".avsync-*"))
    assert source.path.read_bytes() == b"original"


def test_cli_shows_full_validation_failure_without_traceback(source, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("app.cli.analyze", lambda _: source)
    monkeypatch.setattr("app.fixer.OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr("app.fixer.run_ffmpeg", lambda command, callback, **kwargs: Path(command[-1]).write_bytes(b"bad"))
    monkeypatch.setattr("app.fixer.analyze", lambda path: replace(source, major_brand="qt"))
    assert main([str(source.path), "--fix", "--preset", "bilibili"]) == 1
    captured = capsys.readouterr()
    assert "输出验证报告" in captured.out and "轨道差异" in captured.out
    assert "不符合 Bilibili" in captured.err and "Traceback" not in captured.err
    assert "100%" not in captured.out
    assert list((tmp_path / "out").iterdir()) == []
