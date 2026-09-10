"""Business decisions and argv snapshots, including the input safety monitor."""

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from app.cli import main, format_command
from app.ffmpeg_utils import MediaError, build_repair_command
from app.repair_plan import select_repair_plan, format_repair_decisions, plan_from_strategy
from app.models import RepairStrategy
from tests.test_strategies import media


CASES = {
    "normal": ({}, {}, None),
    "vfr": ({"avg_frame_rate": "59.27", "r_frame_rate": "60"}, {}, None),
    "full": ({"pix_fmt": "yuvj420p", "color_range": "pc", "color_space": "bt709"}, {}, None),
    "combined": ({"avg_frame_rate": "59.27", "r_frame_rate": "60", "start_time": "-0.1",
                  "pix_fmt": "yuvj420p", "color_range": "pc"}, {"duration": "11"}, None),
    "silent": ({}, {}, []),
    "unknown_start": ({"start_time": None}, {}, None),
    "offset": ({}, {"start_time": "0.3"}, None),
}
SNAPSHOTS = json.loads((Path(__file__).parent / "snapshots/repair_commands.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("key", SNAPSHOTS)
def test_commands_match_snapshots(key, tmp_path):
    case, preset, mode = key.split("/")
    video, audio, tracks = CASES[case]
    source = media({"width": 160, "height": 90, **video}, audio, audio_tracks=tracks)
    plan = select_repair_plan(source, mode, preset=preset)
    command = build_repair_command("ffmpeg", plan, tmp_path / "out.mp4")
    command[command.index("-i") + 1] = "<INPUT>"
    command[-1] = "<OUTPUT>"
    assert command == SNAPSHOTS[key]
    for flag in ("-vf", "-fps_mode:v", "-c:v", "-pix_fmt", "-fflags", "-ar", "-filter:a:0"):
        assert command.count(flag) <= 1


@pytest.mark.parametrize("video,audio,expected", [
    ({}, {}, ("preserve", "preserve", "preserve", "preserve_limited")),
    ({"avg_frame_rate": "59.27", "r_frame_rate": "60"}, {}, ("cfr", "normalize", "preserve", "preserve_limited")),
    ({"start_time": "-0.1"}, {}, ("preserve", "regenerate_missing_pts", "preserve", "preserve_limited")),
    ({}, {"duration": "10.2"}, ("preserve", "preserve", "preserve", "preserve_limited")),
    ({}, {"duration": "9.8"}, ("preserve", "preserve", "preserve", "preserve_limited")),
    ({"pix_fmt": "yuvj420p", "color_range": None}, {}, ("preserve", "preserve", "preserve", "full_to_limited")),
    ({"avg_frame_rate": None, "r_frame_rate": None}, {"duration": None}, ("preserve", "preserve", "preserve", "preserve_limited")),
])
def test_safe_enables_only_repairs_with_corresponding_evidence(video, audio, expected):
    plan = select_repair_plan(media(video, audio))
    assert (plan.video.fps_mode, plan.video.timestamp_strategy, plan.audios[0].sync_strategy,
            plan.video.color_conversion.action) == expected
    assert plan.video.codec == "h264" and plan.video.pixel_format == "yuv420p"
    assert plan.audios[0].codec == "aac" and plan.audios[0].sample_rate is None
    assert plan.container.format == "mp4" and plan.container.faststart
    assert all(d.reason for d in plan.decisions)


def test_multiple_tracks_preserve_offsets_without_guessing_drift_from_length(tmp_path):
    plan = select_repair_plan(media({"start_time": "0.5"}, audio_tracks=[
        {"codec_type": "audio", "index": 3, "start_time": "0", "duration": "10"},
        {"codec_type": "audio", "index": 7, "start_time": "0.2", "duration": "11"},
        {"codec_type": "audio", "start_time": "0.1"},
    ]))
    assert plan.video.start_offset == 0.5
    assert [a.start_offset for a in plan.audios] == [0, 0.2, 0.1]
    assert [a.sync_strategy for a in plan.audios] == ["preserve", "preserve", "preserve"]
    command = build_repair_command("ffmpeg", plan, tmp_path / "out.mp4")
    assert [command[i + 1] for i, arg in enumerate(command) if arg == "-map"] == ["0:0", "0:3", "0:7", "0:a:2"]
    for i in range(3):
        assert "aresample=" not in command[command.index(f"-filter:a:{i}") + 1]
    assert "时长未知" in format_repair_decisions(plan)
    assert "不能证明累计漂移" in format_repair_decisions(plan)


def test_bilibili_requirements_do_not_enable_unrelated_repairs():
    plan = select_repair_plan(media({"width": 160, "height": 90}), preset="bilibili")
    assert plan.video.fps_mode == "cfr" and plan.video.timestamp_strategy == "normalize"
    assert plan.audios[0].sample_rate == 48000 and plan.audios[0].sync_strategy == "preserve"
    assert plan.video.color_conversion.action == "preserve_limited"
    decision = next(d for d in plan.decisions if d.item == "CFR")
    assert decision.basis == "preset" and "Bilibili" in decision.reason


@pytest.mark.parametrize("mode,item", [("cfr", "CFR"), ("timestamp", "Timestamp / genpts"), ("audio-sync", "Audio resync #1")])
def test_explicit_mode_is_reported_as_user_choice_not_analyzer_evidence(mode, item):
    plan = select_repair_plan(media(), mode)
    decision = next(d for d in plan.decisions if d.item == item)
    assert decision.basis == "explicit" and "显式选择" in decision.reason


def test_builder_does_not_call_planner_or_use_mode_labels(monkeypatch, tmp_path):
    plan = select_repair_plan(media())
    command = build_repair_command("ffmpeg", plan, tmp_path / "out.mp4")
    def forbidden(*args, **kwargs):
        pytest.fail("builder must not analyze or select business policy")
    for name in ("diagnose", "select_strategy", "select_color_plan", "select_repair_plan"):
        monkeypatch.setattr(f"app.repair_plan.{name}", forbidden)
    assert build_repair_command("ffmpeg", replace(plan, mode="timestamp", preset="bilibili"), tmp_path / "out.mp4") == command
    with pytest.raises(FrozenInstanceError):
        plan.video.codec = "hevc"


def test_builder_uses_per_track_rates_and_faststart_from_plan(tmp_path):
    plan = select_repair_plan(media())
    plan = replace(plan, audios=(replace(plan.audios[0], sample_rate=48000),
                                replace(plan.audios[0], stream_index=3, sample_rate=44100)),
                   container=replace(plan.container, faststart=False))
    command = build_repair_command("ffmpeg", plan, tmp_path / "out.mp4")
    assert "-ar" not in command and "-movflags" not in command
    assert command[command.index("-ar:a:0") + 1] == "48000"
    assert command[command.index("-ar:a:1") + 1] == "44100"


@pytest.mark.parametrize("changes", [
    {"fps_mode": "bad"}, {"target_fps": 60}, {"fps_mode": "cfr", "target_fps": 0},
    {"timestamp_strategy": "bad"}, {"codec": "hevc"}, {"pixel_format": "yuv444p"},
    {"start_offset": float("nan")}, {"stream_index": -1},
    {"start_offset": None}, {"start_offset": "0"}, {"start_offset": True},
    {"start_offset": 10 ** 1000},
    {"fps_mode": "cfr", "target_fps": 60, "timestamp_strategy": "preserve"},
])
def test_builder_rejects_conflicts_instead_of_deciding_for_caller(changes, tmp_path):
    plan = select_repair_plan(media())
    with pytest.raises(MediaError):
        build_repair_command("ffmpeg", replace(plan, video=replace(plan.video, **changes)), tmp_path / "out.mp4")


@pytest.mark.parametrize("changes", [
    {"pixel_format": "yuv444p"}, {"output_range": "pc"}, {"action": "full_to_limited"},
    {"color_space": "bt709,eq=brightness=1"}, {"color_transfer": "smpte2084"},
])
def test_builder_rejects_inconsistent_color_plan(changes, tmp_path):
    plan = select_repair_plan(media())
    color = replace(plan.video.color_conversion, **changes)
    with pytest.raises(MediaError):
        build_repair_command("ffmpeg", replace(plan, video=replace(plan.video, color_conversion=color)), tmp_path / "out.mp4")


@pytest.mark.parametrize("audio_count", [0, 1, 3])
def test_strategy_and_report_share_one_diagnosis_per_track(audio_count, monkeypatch):
    from app import repair_plan
    calls = []
    original = repair_plan.diagnose
    def observe(source):
        calls.append(source)
        return original(source)
    monkeypatch.setattr(repair_plan, "diagnose", observe)
    source = media(audio_tracks=[{"codec_type": "audio", "index": i + 1, "duration": "11", "start_time": "0"}
                                 for i in range(audio_count)])
    plan = select_repair_plan(source)
    assert len(calls) == max(1, audio_count)
    assert len(plan.audios) == audio_count
    if audio_count:
        assert all(a.sync_strategy == "preserve" for a in plan.audios)
        assert format_repair_decisions(plan).count("长度差不能证明累计漂移") == audio_count


def test_legacy_supplied_strategy_never_fabricates_analyzer_findings():
    source = media()
    strategy = RepairStrategy("safe", True, True, (0,), 60)
    plan = plan_from_strategy(source, strategy)
    decisions = [d for d in plan.decisions if d.item in {"CFR", "Timestamp / genpts", "Audio resync #1"}]
    assert len(decisions) == 3
    assert all(d.basis == "strategy" and "调用方" in d.reason for d in decisions)
    assert plan.strategy.cfr and plan.strategy.timestamp and plan.strategy.audio_sync_tracks == (0,)
    assert "Suspected VFR" not in format_repair_decisions(plan)


@pytest.mark.parametrize("strategy", [
    RepairStrategy("safe", False, False, (), 60),
    RepairStrategy("safe", False, False, (0, 0), None),
    RepairStrategy("unknown", False, False, (), None),
])
def test_legacy_adapter_rejects_conflicts_instead_of_silently_dropping_them(strategy):
    with pytest.raises(MediaError):
        plan_from_strategy(media(), strategy)


def test_dry_run_explains_enabled_and_disabled_decisions(tmp_path, monkeypatch, capsys):
    source = replace(media({"pix_fmt": "yuvj420p", "color_range": "pc", "color_space": "bt709",
                            "avg_frame_rate": "59.27", "r_frame_rate": "60"}),
                     path=tmp_path / "-中文 空格 ‘片段’ & $.mp4")
    monkeypatch.setattr("app.cli.analyze", lambda _: source)
    monkeypatch.setattr("app.fixer.find_tool", lambda _: "ffmpeg")
    monkeypatch.setattr("app.fixer.OUTPUT_DIR", tmp_path / "输出 目录")
    assert main([str(source.path), "--fix", "--dry-run"]) == 0
    report = capsys.readouterr().out
    for text in ("Repair decisions:", "CFR: Enabled (60 FPS)", "Suspected VFR", "Full → Limited",
                 "Input yuvj420p", "color_space=bt709", "Audio resync #1: Disabled",
                 "Timestamp / genpts: Disabled", "Reason [analysis]"):
        assert text in report
    assert not (tmp_path / "输出 目录").exists()
    plan = select_repair_plan(source)
    command = build_repair_command("ffmpeg", plan, tmp_path / "输出 目录" / "结果.mp4")
    assert command[command.index("-i") + 1] == str(source.path.resolve())
    assert command[-1] == str((tmp_path / "输出 目录" / "结果.mp4").resolve())
    assert "‘‘片段’’" in format_command(command, windows=True)
    with pytest.raises(MediaError, match="相同"):
        build_repair_command("ffmpeg", plan, source.path)
