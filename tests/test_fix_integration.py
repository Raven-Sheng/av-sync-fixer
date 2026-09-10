"""使用真实短视频检查输出帧间隔、时长、音轨与 CLI 流程。"""

import hashlib
import json
from array import array
from pathlib import Path
import subprocess

import pytest

from app.analyzer import analyze
from app.cli import main
from app.ffmpeg_utils import MediaError, find_tool
from app.fixer import REPAIR_MODES, fix_video
from tests.media_helpers import frame_times, generate_media


def test_native_aac_rate_capabilities_match_expected_output_policy():
    from app.repair_plan import AAC_SAMPLE_RATES
    try:
        ffmpeg = find_tool("ffmpeg")
    except MediaError as exc:
        pytest.skip(str(exc))
    result = subprocess.run([ffmpeg, "-hide_banner", "-h", "encoder=aac"],
                            capture_output=True, text=True, check=True, timeout=10)
    line = next(line for line in (result.stdout + result.stderr).splitlines() if "Supported sample rates:" in line)
    assert tuple(map(int, line.split(":", 1)[1].split())) == AAC_SAMPLE_RATES


@pytest.mark.parametrize("sample_rate,expected_rate", [(192000, 96000), (10000, 11025), (46050, 48000)])
def test_general_encoder_rate_policy_matches_output_spec(tmp_path, sample_rate, expected_rate):
    from app.fixer import prepare_fix
    try:
        ffmpeg = find_tool("ffmpeg")
        find_tool("ffprobe")
    except MediaError as exc:
        pytest.skip(str(exc))
    path = tmp_path / "中文 非标准采样率.nut"
    subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-n", "-f", "lavfi", "-i",
        "testsrc2=size=160x90:rate=30:duration=2", "-f", "lavfi", "-i",
        f"sine=sample_rate={sample_rate}:duration=2", "-c:v", "libx264", "-preset", "ultrafast",
        "-color_range", "tv", "-bsf:v", "h264_metadata=video_full_range_flag=0",
        "-c:a", "pcm_s16le", str(path)], capture_output=True, check=True, timeout=30)
    before = analyze(path)
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    plan = prepare_fix(before, tmp_path / "out")
    assert "-ar" not in plan.command
    result = fix_video(before, tmp_path / "out")
    assert result.after.audios[0].sample_rate == expected_rate
    assert plan.expected.audios[0].sample_rate == expected_rate
    assert not result.validation.errors
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    try:
        ffmpeg = find_tool("ffmpeg")
        find_tool("ffprobe")
    except MediaError as exc:
        pytest.skip(str(exc))
    directory = tmp_path_factory.mktemp("修复样本")
    video = ["-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=2"]
    audio = ["-f", "lavfi", "-i", "sine=sample_rate=48000:duration=2"]
    cases = {
        "normal": video + audio,
        "silent": video,
        "vfr": video + audio + ["-vf", "select=if(lt(t\\,1)\\,not(mod(n\\,2))\\,1)", "-fps_mode", "vfr"],
        "combined": video + ["-itsoffset", "0.3", "-f", "lavfi", "-i", "sine=sample_rate=48000:duration=2.8",
                              "-vf", "select=if(lt(t\\,1)\\,not(mod(n\\,2))\\,1)", "-fps_mode", "vfr"],
        "long_audio": video + ["-f", "lavfi", "-i", "sine=sample_rate=48000:duration=2.8"],
        "short_audio": video + ["-f", "lavfi", "-i", "sine=sample_rate=48000:duration=1.2"],
        "multiple_audio": video + audio + audio + ["-map", "0:v", "-map", "1:a", "-map", "2:a"],
        "offset_audio": video + ["-itsoffset", "0.4"] + audio,
        "offset_video": ["-itsoffset", "0.4"] + video + audio + ["-fps_mode", "passthrough"],
        "multiple_offsets": video + audio + ["-itsoffset", "0.3"] + audio + ["-map", "0:v", "-map", "1:a", "-map", "2:a"],
        "odd_size": ["-f", "lavfi", "-i", "testsrc=size=161x91:rate=30:duration=2", "-pix_fmt", "yuv444p"],
        "upload": video + ["-f", "lavfi", "-i", "sine=sample_rate=44100:duration=2",
                           "-vf", "select=if(lt(t\\,1)\\,not(mod(n\\,2))\\,1)", "-fps_mode", "vfr", "-pix_fmt", "yuv444p"],
    }
    paths = {}
    for name, options in cases.items():
        path = directory / f"中文 空格 🎬 {name}.mp4"
        generate_media(ffmpeg, path, [*options, "-c:v", "libx264", "-color_range", "tv", "-bsf:v", "h264_metadata=video_full_range_flag=0", "-c:a", "aac"])
        paths[name] = path
    return paths


def mp4_boxes(path):
    """只走顶层 box 边界，避免误将媒体载荷内的字节当作 moov。"""
    boxes = []
    with path.open("rb") as stream:
        end = path.stat().st_size
        while stream.tell() < end:
            offset = stream.tell()
            header = stream.read(8)
            assert len(header) == 8
            size = int.from_bytes(header[:4], "big")
            if size == 1:
                size = int.from_bytes(stream.read(8), "big")
            elif size == 0:
                size = end - offset
            assert size >= stream.tell() - offset and offset + size <= end
            boxes.append(header[4:8])
            stream.seek(offset + size)
    return boxes


@pytest.mark.parametrize("name", ["normal", "upload", "silent", "multiple_audio", "combined", "long_audio", "short_audio", "offset_video"])
def test_real_bilibili_output_meets_preset(media, tmp_path, name):
    before = analyze(media[name])
    original_hash = hashlib.sha256(before.path.read_bytes()).hexdigest()
    if name == "upload":
        assert before.audios[0].sample_rate == 44100
        assert before.videos[0].pixel_format == "yuv444p"
    reports = []
    result = fix_video(before, tmp_path / "output", preset="bilibili", on_validation=reports.append)
    after = result.after
    assert result.validation and not result.validation.errors and len(reports) == 1
    assert "mp4" in after.container.split(",") and after.major_brand == "isom"
    assert after.videos[0].codec == "h264" and after.videos[0].pixel_format == "yuv420p"
    assert (after.videos[0].width, after.videos[0].height) == (before.videos[0].width, before.videos[0].height)
    assert after.videos[0].avg_frame_rate == after.videos[0].r_frame_rate == result.target_fps
    assert len(after.audios) == len(before.audios)
    for old, audio in zip(before.audios, after.audios):
        assert audio.codec == "aac" and audio.sample_rate == 48000
        assert audio.duration == pytest.approx(old.duration, abs=0.05)
    times = frame_times(after.path)
    assert len(times) > 10
    assert all(b - a == pytest.approx(1 / result.target_fps, abs=2e-6) for a, b in zip(times, times[1:]))
    if name in {"combined", "long_audio", "short_audio"}:
        assert any("同步风险未消除" in warning for warning in result.validation.warnings)
    boxes = mp4_boxes(after.path)
    assert boxes.index(b"moov") < boxes.index(b"mdat")
    assert hashlib.sha256(before.path.read_bytes()).hexdigest() == original_hash


def test_real_bilibili_rejects_odd_dimensions_without_modifying_source(media, tmp_path):
    before = analyze(media["odd_size"])
    with pytest.raises(MediaError, match="奇数"):
        fix_video(before, tmp_path / "output", preset="bilibili")
    assert not (tmp_path / "output").exists()


def test_bilibili_preserves_encoded_dimensions_and_display_rotation(media, tmp_path):
    path = tmp_path / "rotated.mov"
    subprocess.run([
        find_tool("ffmpeg"), "-v", "error", "-nostdin", "-n",
        "-display_rotation:v:0", "90", "-i", str(media["normal"]), "-c", "copy", str(path),
    ], capture_output=True, check=True, timeout=30)
    from app.ffmpeg_utils import probe_media
    def rotation(path):
        video = next(stream for stream in probe_media(path, find_tool("ffprobe"))["streams"] if stream["codec_type"] == "video")
        return next(item["rotation"] for item in video["side_data_list"] if "rotation" in item)
    assert rotation(path) == 90
    before = analyze(path)
    result = fix_video(before, tmp_path / "out", preset="bilibili")
    assert (result.after.videos[0].width, result.after.videos[0].height) == (160, 90)
    assert rotation(result.after.path) == rotation(path)
    assert not result.validation.errors


def test_real_cli_bilibili_prints_validation_report(media, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("app.fixer.OUTPUT_DIR", tmp_path / "out")
    assert main([str(media["upload"]), "--fix", "--preset", "bilibili"]) == 0
    report = capsys.readouterr().out
    for text in ("输出预设：Bilibili", "输出验证报告", "像素格式：yuv420p", "采样率 48000 Hz", "轨道差异", "预设编码要求通过", "100%"):
        assert text in report


@pytest.fixture(scope="module")
def truncated_media(media, tmp_path_factory):
    directory = tmp_path_factory.mktemp("损坏媒体")
    healthy = directory / "healthy.mp4"
    generate_media(find_tool("ffmpeg"), healthy, ["-i", str(media["normal"]), "-c", "copy", "-movflags", "+faststart"])
    data = healthy.read_bytes()
    damaged = directory / "截断的 视频 🎬.mp4"
    damaged.write_bytes(data[:len(data) // 2])
    # 头部仍声称原始时长，能被 probe 读取；这不代表后续内容完整。
    source = analyze(damaged)
    assert source.videos[0].duration == pytest.approx(2)
    assert source.audios[0].duration == pytest.approx(2)
    return damaged


@pytest.mark.parametrize("mode", REPAIR_MODES)
@pytest.mark.parametrize("preset", ["general", "bilibili"])
def test_corrupt_input_cannot_publish_truncated_success(truncated_media, tmp_path, mode, preset):
    original = hashlib.sha256(truncated_media.read_bytes()).hexdigest()
    with pytest.raises(MediaError, match="FFmpeg 修复失败") as failure:
        fix_video(analyze(truncated_media), tmp_path / "output", mode=mode, preset=preset)
    assert any(detail in str(failure.value).lower() for detail in ("corrupt", "invalid", "error"))
    assert list((tmp_path / "output").iterdir()) == []
    assert hashlib.sha256(truncated_media.read_bytes()).hexdigest() == original


def test_cli_corruption_error_is_clear_without_success(truncated_media, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("app.fixer.OUTPUT_DIR", tmp_path / "output")
    assert main([str(truncated_media), "--fix"]) == 1
    report = capsys.readouterr()
    assert "FFmpeg 修复失败" in report.err and "Traceback" not in report.err
    assert "100%" not in report.out and "修复前后对比" not in report.out


def test_multiple_video_tracks_keep_first_video_and_all_audio(media, tmp_path):
    source_path = tmp_path / "-中文 '多轨' $literal & [1]; 🎬.mp4"
    generate_media(find_tool("ffmpeg"), source_path, [
        "-i", str(media["normal"]),
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=2",
        "-f", "lavfi", "-i", "sine=sample_rate=44100:duration=2",
        "-map", "0:v", "-map", "0:a", "-map", "1:v", "-map", "2:a",
        "-c:v", "libx264", "-color_range", "tv", "-bsf:v", "h264_metadata=video_full_range_flag=0", "-c:a", "aac",
    ])
    original_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    before = analyze(source_path)
    assert len(before.videos) == len(before.audios) == 2
    assert [stream.index for stream in before.videos] == [0, 2]
    assert [stream.index for stream in before.audios] == [1, 3]
    result = fix_video(before, tmp_path / "out", mode="cfr")
    after = analyze(result.after.path)
    assert len(after.videos) == 1 and len(after.audios) == 2
    assert (after.videos[0].width, after.videos[0].height) == (160, 90)
    assert all(audio.codec == "aac" for audio in after.audios)
    assert [audio.sample_rate for audio in after.audios] == [48000, 44100]
    assert any("其余 1 条视频轨不会写入输出" in item for item in result.strategy.warnings)
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == original_hash


@pytest.mark.parametrize("name", ["normal", "silent", "vfr", "long_audio", "short_audio", "multiple_audio", "offset_audio", "offset_video", "multiple_offsets", "odd_size"])
def test_real_fix_is_cfr_and_preserves_media(media, tmp_path, name):
    path = media[name]
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    before = analyze(path)
    updates = []
    result = fix_video(before, tmp_path / "output", mode="cfr", on_progress=updates.append)
    after = result.after
    assert after.videos[0].codec == "h264"
    assert after.videos[0].avg_frame_rate == result.target_fps
    assert after.videos[0].r_frame_rate == result.target_fps
    expected_size = (162, 92) if name == "odd_size" else (160, 90)
    assert (after.videos[0].width, after.videos[0].height) == expected_size
    assert len(after.audios) == len(before.audios)
    for original, encoded in zip(before.audios, after.audios):
        assert encoded.codec == "aac"
        assert abs(encoded.duration - original.duration) < 0.05
        old_offset = original.start_time - before.videos[0].start_time
        new_offset = encoded.start_time - after.videos[0].start_time
        assert new_offset == pytest.approx(old_offset, abs=0.06)
    times = frame_times(after.path)
    assert len(times) > 10
    assert all(b - a == pytest.approx(1 / result.target_fps, abs=2e-6) for a, b in zip(times, times[1:]))
    if name == "vfr":
        original_times = frame_times(path)
        assert len({round(b - a, 3) for a, b in zip(original_times, original_times[1:])}) > 1
    assert after.videos[0].duration == pytest.approx(before.videos[0].duration, abs=0.08)
    assert updates
    assert not list(after.path.parent.glob(".avsync-*"))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == source_hash


def test_cli_fix_and_existing_output(media, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("app.fixer.OUTPUT_DIR", tmp_path / "output")
    assert main([str(media["normal"]), "--fix"]) == 0
    report = capsys.readouterr().out
    for text in ("音画同步分析", "输入路径", "输出路径", "目标 FPS", "FFmpeg 命令", "修复进度", "100%", "修复前后对比"):
        assert text in report
    assert main([str(media["normal"]), "--fix"]) == 1
    error = capsys.readouterr().err
    assert "输出文件已存在" in error
    assert "Traceback" not in error


@pytest.mark.parametrize("mode", ["timestamp", "audio-sync"])
def test_non_cfr_modes_preserve_variable_frame_intervals(media, tmp_path, mode):
    before = analyze(media["vfr"])
    result = fix_video(before, tmp_path / "output", mode=mode)
    original = frame_times(before.path)
    encoded = frame_times(result.after.path)
    old_intervals = [b - a for a, b in zip(original, original[1:])]
    new_intervals = [b - a for a, b in zip(encoded, encoded[1:])]
    assert len({round(value, 3) for value in old_intervals}) > 1
    assert new_intervals == pytest.approx(old_intervals, abs=2e-6)
    assert result.target_fps is None


def packets(path):
    output = subprocess.run([
        find_tool("ffprobe"), "-v", "error", "-show_packets",
        "-show_entries", "packet=codec_type,pts_time,dts_time,duration_time", "-of", "json", str(path),
    ], capture_output=True, text=True, encoding="utf-8", check=True, timeout=30)
    return json.loads(output.stdout)["packets"]


def test_timestamp_removes_leading_negative_dts_and_keeps_relative_offset(media, tmp_path):
    before = analyze(media["offset_audio"])
    assert min(float(packet["dts_time"]) for packet in packets(before.path)) < 0
    result = fix_video(before, tmp_path / "output", mode="timestamp")
    assert min(float(packet["dts_time"]) for packet in packets(result.after.path)) >= 0
    old_offset = before.audios[0].start_time - before.videos[0].start_time
    new_offset = result.after.audios[0].start_time - result.after.videos[0].start_time
    assert new_offset == pytest.approx(old_offset, abs=0.06)


@pytest.mark.parametrize("name", ["long_audio", "short_audio"])
@pytest.mark.parametrize("mode", ["audio-sync", "safe"])
def test_audio_sync_does_not_force_matching_track_lengths(media, tmp_path, name, mode):
    before = analyze(media[name])
    result = fix_video(before, tmp_path / "output", mode=mode)
    assert result.strategy.audio_sync_tracks == ((0,) if mode == "audio-sync" else ())
    assert result.after.audios[0].duration == pytest.approx(before.audios[0].duration, abs=0.05)
    assert abs(result.after.audios[0].duration - result.after.videos[0].duration) > 0.5


def test_safe_combines_strategies_for_real_risks(media, tmp_path):
    before = analyze(media["combined"])
    result = fix_video(before, tmp_path / "output")
    assert result.strategy.cfr and result.strategy.timestamp
    assert result.strategy.audio_sync_tracks == ()  # Length difference alone is not clock-drift evidence.
    times = frame_times(result.after.path)
    assert all(b - a == pytest.approx(1 / result.target_fps, abs=2e-6) for a, b in zip(times, times[1:]))
    assert result.after.audios[0].duration == pytest.approx(before.audios[0].duration, abs=0.05)


def decoded_samples(path):
    output = subprocess.run([
        find_tool("ffmpeg"), "-v", "error", "-nostdin", "-i", str(path),
        "-map", "0:a:0", "-ac", "1", "-ar", "48000", "-f", "s16le", "pipe:1",
    ], capture_output=True, check=True, timeout=30)
    samples = array("h")
    samples.frombytes(output.stdout)
    return samples


def test_audio_sync_corrects_accumulating_pts_gap_without_pitch_shift(media, tmp_path):
    # PCM 的采样长度为 6 秒，但其 PTS 持续变慢，时间轴约 6.3 秒。
    # 这比仅测试两条长度不同、内部 PTS 正常的音视频更能检验 async 的作用。
    source = tmp_path / "drifting-audio.nut"
    subprocess.run([
        find_tool("ffmpeg"), "-v", "error", "-nostdin", "-n",
        "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=6.3",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
        "-af", "asetpts=1.05*PTS", "-c:v", "libx264", "-color_range", "tv", "-bsf:v", "h264_metadata=video_full_range_flag=0", "-c:a", "pcm_s16le", str(source),
    ], capture_output=True, check=True, timeout=30)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    original = decoded_samples(source)
    audio_packets = [packet for packet in packets(source) if packet["codec_type"] == "audio"]
    timeline = max(float(p["pts_time"]) + float(p["duration_time"]) for p in audio_packets) - float(audio_packets[0]["pts_time"])
    result = fix_video(analyze(source), tmp_path / "output", mode="audio-sync")
    encoded = decoded_samples(result.after.path)
    assert len(original) / 48000 == pytest.approx(6, abs=0.001)
    assert timeline > 6.25
    assert len(encoded) > len(original) + 0.15 * 48000
    assert abs(len(encoded) / 48000 - timeline) < 0.12
    # 带迟滞的过零检测忽略静音中的 AAC 噪声；排除补静音造成的长间隔。
    crossings, armed = [], False
    for index, value in enumerate(encoded):
        if value < -500:
            armed = True
        elif armed and value > 500:
            crossings.append(index)
            armed = False
    periods = [b - a for a, b in zip(crossings, crossings[1:]) if b - a < 48000 / 200]
    assert len(periods) > 2000
    assert 48000 * len(periods) / sum(periods) == pytest.approx(440, abs=2)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
