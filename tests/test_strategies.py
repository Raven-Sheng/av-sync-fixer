"""策略选择、条件式参数组合和无副作用命令预览。"""

from dataclasses import replace
from pathlib import Path

from tests.mp4_stub import MP4_STUB

import pytest

from app.analyzer import parse_analysis
from app.cli import main
from app.ffmpeg_utils import MediaError, build_fix_command
from app.fixer import prepare_fix, select_strategy, execute_fix


def media(video=None, audio=None, *, audio_tracks=None):
    tracks = [{"codec_type": "video", "index": 0, "codec_name": "h264", "pix_fmt": "yuv420p", "color_range": "tv", "avg_frame_rate": "30", "r_frame_rate": "30", "duration": "10", "start_time": "0", **(video or {})}]
    tracks.extend(audio_tracks if audio_tracks is not None else [
        {"codec_type": "audio", "index": 1, "codec_name": "aac", "duration": "10", "start_time": "0", **(audio or {})},
    ])
    return parse_analysis(Path("录屏 视频.mp4"), {"streams": tracks})


def command_for(source, mode):
    strategy = select_strategy(source, mode)
    return build_fix_command("ffmpeg", source, Path("output/result.mp4"), strategy=strategy)


@pytest.mark.parametrize("mode, has_fps, has_timestamp, has_async", [
    ("safe", False, False, False), ("cfr", True, False, False),
    ("timestamp", False, True, False), ("audio-sync", False, False, True),
])
def test_explicit_modes_only_enable_requested_parameters(mode, has_fps, has_timestamp, has_async):
    command = command_for(media(), mode)
    assert ("fps=fps=" in " ".join(command)) is has_fps
    assert ("-fflags" in command) is has_timestamp
    assert ("-avoid_negative_ts" in command) is has_timestamp
    assert ("aresample=" in " ".join(command)) is has_async
    for flag in ("-copyts", "-start_at_zero", "-shortest", "-y", "-t", "-to"):
        assert flag not in command


@pytest.mark.parametrize("video, audio, expected", [
    ({}, {}, (False, False, ())),
    ({"avg_frame_rate": "59.27", "r_frame_rate": "60"}, {}, (True, False, ())),
    ({}, {"duration": "10.2"}, (False, False, ())),
    ({}, {"duration": "10.199"}, (False, False, ())),
    ({"start_time": "-0.1"}, {}, (False, True, ())),
    ({}, {"start_time": "0.3"}, (False, True, ())),
    ({"start_time": None}, {}, (False, True, ())),
    ({"avg_frame_rate": "59.27", "r_frame_rate": "60", "start_time": "-0.1"}, {"duration": "10.5"}, (True, True, ())),
    ({"avg_frame_rate": "60000/1001", "r_frame_rate": "60"}, {}, (False, False, ())),
    ({"avg_frame_rate": "0/0", "r_frame_rate": "N/A", "duration": None}, {"duration": None}, (False, False, ())),
])
def test_safe_dynamic_strategy(video, audio, expected):
    result = select_strategy(media(video, audio))
    assert (result.cfr, result.timestamp, result.audio_sync_tracks) == expected
    assert result.reasons


def test_safe_does_not_sync_audio_tracks_from_length_risk_alone():
    source = media(audio_tracks=[
        {"codec_type": "audio", "index": 3, "duration": "10", "start_time": "0"},
        {"codec_type": "audio", "index": 5, "duration": "11", "start_time": "0"},
        {"codec_type": "audio", "index": 6, "start_time": "0"},
    ])
    strategy = select_strategy(source)
    assert strategy.audio_sync_tracks == ()
    command = command_for(source, "safe")
    assert not any("aresample" in arg for arg in command)
    assert "0:5" in command


def test_audio_async_retains_timestamp_evidence_and_disables_pitch_changing_stretch():
    source = media({"avg_frame_rate": "29", "r_frame_rate": "30"}, {"duration": "11"})
    command = command_for(source, "audio-sync")
    audio_filter = command[command.index("-filter:a:0") + 1]
    assert "asetpts=PTS-STARTPTS,aresample=async=1:" in audio_filter
    assert "max_soft_comp=0" in audio_filter
    assert "N/SR/TB" not in audio_filter
    assert "asetrate" not in audio_filter
    assert "atempo" not in audio_filter
    assert "first_pts=0" not in audio_filter
    assert "fps=fps=" not in command[command.index("-vf") + 1]


def test_timestamp_option_positions():
    command = command_for(media(), "timestamp")
    assert command.index("-fflags") < command.index("-i")
    assert command[command.index("-fflags") + 1] == "+genpts"
    assert command.index("-avoid_negative_ts") > command.index("-i")
    assert command[command.index("-avoid_negative_ts") + 1] == "make_non_negative"
    assert "fps=fps=" not in command[command.index("-vf") + 1]
    assert "-enc_time_base:v" in command


def test_timestamp_alone_does_not_change_video_cadence():
    source = media({"avg_frame_rate": "59.27", "r_frame_rate": "60"}, {"duration": "11"})
    strategy = select_strategy(source, "timestamp")
    assert not strategy.cfr
    assert not strategy.audio_sync_tracks
    assert strategy.target_fps is None


def test_modes_without_audio():
    source = media(audio_tracks=[])
    for mode in ("safe", "cfr", "timestamp"):
        command = command_for(source, mode)
        assert "-an" in command
        assert "aresample" not in " ".join(command)
    with pytest.raises(MediaError, match="需要音频轨"):
        select_strategy(source, "audio-sync")


def test_invalid_mode_and_no_video():
    with pytest.raises(MediaError, match="不支持"):
        select_strategy(media(), "bad-mode")
    with pytest.raises(MediaError, match="没有可修复"):
        select_strategy(parse_analysis(Path("audio.wav"), {}))


def test_prepare_is_pure_and_execution_reuses_preview_arguments(tmp_path, monkeypatch):
    source = replace(media(), path=tmp_path / "source.mp4")
    source.path.write_bytes(b"original")
    monkeypatch.setattr("app.fixer.find_tool", lambda name: "ffmpeg")
    plan = prepare_fix(source, tmp_path / "output", mode="cfr")
    assert not plan.output_path.parent.exists()
    observed = []
    def encode(command, callback, **kwargs):
        observed.append(command)
        Path(command[-1]).write_bytes(MP4_STUB)
    monkeypatch.setattr("app.fixer.run_ffmpeg", encode)
    monkeypatch.setattr("app.fixer.analyze", lambda path: replace(source, path=path, container="mp4", major_brand="isom"))
    result = execute_fix(plan)
    assert tuple(observed[0][:-1]) == plan.command[:-1]
    assert observed[0][-1] != str(plan.output_path)
    assert result.after.path.read_bytes() == MP4_STUB


@pytest.mark.parametrize("mode", ["safe", "cfr", "timestamp", "audio-sync"])
def test_cli_dry_run_has_no_output_side_effects(mode, tmp_path, monkeypatch, capsys):
    source = replace(media(), path=tmp_path / "input.mp4")
    source.path.write_bytes(b"original")
    directory = tmp_path / "does-not-exist"
    monkeypatch.setattr("app.cli.analyze", lambda path: source)
    monkeypatch.setattr("app.fixer.find_tool", lambda name: "ffmpeg")
    monkeypatch.setattr("app.fixer.OUTPUT_DIR", directory)
    def unexpected(*args, **kwargs):
        pytest.fail("dry-run 不应执行修复或输出复查")
    monkeypatch.setattr("app.cli.execute_fix", unexpected)
    monkeypatch.setattr("app.fixer.run_ffmpeg", unexpected)
    monkeypatch.setattr("app.fixer.analyze", unexpected)
    assert main([str(source.path), "--fix", "--mode", mode, "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert f"修复模式：{mode}" in output
    assert "FFmpeg 命令" in output
    assert "未执行转码" in output
    assert "100%" not in output
    assert not directory.exists()
    assert source.path.read_bytes() == b"original"


def test_dry_run_can_preview_existing_output(tmp_path, monkeypatch, capsys):
    source = media()
    output = tmp_path / "录屏 视频_fixed.mp4"
    output.write_bytes(b"keep")
    monkeypatch.setattr("app.cli.analyze", lambda path: source)
    monkeypatch.setattr("app.fixer.find_tool", lambda name: "ffmpeg")
    monkeypatch.setattr("app.fixer.OUTPUT_DIR", tmp_path)
    assert main([str(source.path), "--fix", "--dry-run"]) == 0
    assert "目标已存在" in capsys.readouterr().out
    assert output.read_bytes() == b"keep"
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize("arguments", [
    ["--dry-run"], ["--mode", "safe"], ["--fix", "--mode", "bad"],
])
def test_invalid_cli_combinations(arguments):
    with pytest.raises(SystemExit) as exc:
        main(["video.mp4", *arguments])
    assert exc.value.code == 2
