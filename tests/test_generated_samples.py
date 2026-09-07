"""五类 6 秒样本的诊断、修复、重新探测与时长差回归验证。"""

import hashlib
import os
import subprocess
import sys
from tempfile import TemporaryDirectory

import pytest

from app.analyzer import analyze, diagnose
from app.ffmpeg_utils import MediaError, MissingToolError, PROJECT_ROOT, check_tools, run_command
from app.fixer import fix_video
from tests.generate_samples import (
    SAMPLE_DIRECTORY, SAMPLE_FPS, SAMPLE_RESOLUTION, SAMPLES, VIDEO_DURATION_SECONDS, generate_samples,
)
from tests.media_helpers import MEDIA_COMMAND_TIMEOUT_SECONDS, frame_times


# 容许视频帧取整和 AAC 编码边界的少量误差，不允许用原时长百分比放大容差。
MAX_DURATION_DIFF_INCREASE_SECONDS = 0.1
DURATION_TOLERANCE_SECONDS = 0.05
FRAME_TIMESTAMP_TOLERANCE_SECONDS = 2e-6
PROFILES = ("cfr", "safe", "timestamp", "audio-sync", "bilibili")


@pytest.fixture(scope="module")
def generated():
    try:
        executables = check_tools()
    except MissingToolError as exc:
        pytest.skip(str(exc))
    # 每次运行生成新样本；人工查看的默认目录文件不参与测试、不被改写。
    SAMPLE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="test-run-", dir=SAMPLE_DIRECTORY) as directory:
        yield generate_samples(directory), executables


@pytest.mark.parametrize("spec", SAMPLES, ids=lambda spec: spec.name)
def test_generated_sample_diagnosis(generated, spec):
    paths, _ = generated
    source = analyze(paths[spec.name])
    diagnosis = diagnose(source)
    assert len(source.videos) == 1
    video = source.videos[0]
    assert (video.width, video.height) == SAMPLE_RESOLUTION
    assert video.duration == pytest.approx(VIDEO_DURATION_SECONDS, abs=DURATION_TOLERANCE_SECONDS)
    assert diagnosis.suspected_vfr is spec.variable_frame_rate
    if not spec.variable_frame_rate:
        assert video.avg_frame_rate == video.r_frame_rate == SAMPLE_FPS
    times = frame_times(source.path)
    intervals = [b - a for a, b in zip(times, times[1:])]
    assert len(intervals) > 100
    if spec.variable_frame_rate:
        assert min(intervals) == pytest.approx(1 / SAMPLE_FPS, abs=FRAME_TIMESTAMP_TOLERANCE_SECONDS)
        assert max(intervals) == pytest.approx(2 / SAMPLE_FPS, abs=FRAME_TIMESTAMP_TOLERANCE_SECONDS)
    else:
        assert all(value == pytest.approx(1 / SAMPLE_FPS, abs=FRAME_TIMESTAMP_TOLERANCE_SECONDS) for value in intervals)
    if spec.audio_duration is None:
        assert source.audios == ()
        assert diagnosis.duration_diff is None and diagnosis.duration_risk is None
    else:
        assert len(source.audios) == 1
        audio = source.audios[0]
        assert audio.duration == pytest.approx(spec.audio_duration, abs=DURATION_TOLERANCE_SECONDS)
        expected_signed_diff = spec.audio_duration - VIDEO_DURATION_SECONDS
        assert audio.duration - video.duration == pytest.approx(expected_signed_diff, abs=DURATION_TOLERANCE_SECONDS)
        assert diagnosis.duration_risk == ("中等" if expected_signed_diff else "低")


@pytest.mark.parametrize("profile,spec", [
    pytest.param(profile, spec, id=f"{profile}-{spec.name}")
    for profile in PROFILES for spec in SAMPLES
    if not (profile == "audio-sync" and spec.audio_duration is None)
])
def test_generated_sample_repair(generated, tmp_path, profile, spec, record_property):
    paths, executables = generated
    path = paths[spec.name]
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    before = analyze(path)
    mode, preset = ("safe", "bilibili") if profile == "bilibili" else (profile, "general")
    result = fix_video(before, tmp_path / "output", mode=mode, preset=preset)
    assert result.after.path.is_file() and result.after.path.stat().st_size > 0
    # 再从已发布的最终路径调用真实 ffprobe，不只检查内存中的 FixResult。
    after = analyze(result.after.path)
    assert len(after.videos) == 1 and after.videos[0].codec == "h264"
    assert (after.videos[0].width, after.videos[0].height) == SAMPLE_RESOLUTION
    assert len(after.audios) == len(before.audios)
    expected_fps = spec.expected_cfr_fps if profile in {"cfr", "bilibili"} or (profile == "safe" and spec.variable_frame_rate) else None
    assert result.target_fps == expected_fps
    if expected_fps is not None:
        assert after.videos[0].avg_frame_rate == after.videos[0].r_frame_rate == expected_fps
        times = frame_times(after.path)
        assert len(times) > 100
        assert all(b - a == pytest.approx(1 / expected_fps, abs=FRAME_TIMESTAMP_TOLERANCE_SECONDS) for a, b in zip(times, times[1:]))
    assert after.videos[0].duration == pytest.approx(before.videos[0].duration, abs=MAX_DURATION_DIFF_INCREASE_SECONDS)
    before_diff, after_diff = diagnose(before).duration_diff, diagnose(after).duration_diff
    if spec.audio_duration is None:
        assert before_diff is None and after_diff is None
    else:
        assert before_diff is not None and after_diff is not None
        assert after.audios[0].codec == "aac"
        assert after.audios[0].duration == pytest.approx(before.audios[0].duration, abs=MAX_DURATION_DIFF_INCREASE_SECONDS)
        assert after_diff <= before_diff + MAX_DURATION_DIFF_INCREASE_SECONDS, f"轨道差恶化：{before_diff:.6f} → {after_diff:.6f} 秒"
    if preset == "bilibili":
        assert result.validation and not result.validation.errors
        assert after.videos[0].pixel_format == "yuv420p"
        assert all(audio.sample_rate == 48000 for audio in after.audios)
    # ffprobe 能读到头部并不代表内容可解码，额外完整解码这段短片。
    run_command([executables["ffmpeg"], "-v", "error", "-xerror", "-nostdin", "-i", str(after.path),
                 "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"], timeout=MEDIA_COMMAND_TIMEOUT_SECONDS)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash
    assert not list(after.path.parent.glob(".avsync-*"))
    for key, value in (("sample", spec.name), ("profile", profile), ("fps", after.videos[0].avg_frame_rate),
                       ("duration_diff_before", before_diff), ("duration_diff_after", after_diff)):
        record_property(key, str(value))


def test_generated_silent_sample_rejects_audio_sync_cleanly(generated, tmp_path):
    paths, _ = generated
    with pytest.raises(MediaError, match="需要音频轨"):
        fix_video(analyze(paths["no_audio"]), tmp_path / "output", mode="audio-sync")
    assert not (tmp_path / "output").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="验证 Windows 非 UTF-8 输出编码")
def test_sample_cli_handles_unicode_paths_and_repeat_run_with_legacy_encoding(generated, tmp_path):
    directory = tmp_path / "中文 样本 🎬"
    command = [sys.executable, "-m", "tests.generate_samples", "--output-dir", str(directory)]
    environment = dict(os.environ, PYTHONIOENCODING="gbk")
    def run():
        return subprocess.run(command, cwd=PROJECT_ROOT, env=environment, capture_output=True,
                              timeout=MEDIA_COMMAND_TIMEOUT_SECONDS)
    result = run()
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert str(directory) in result.stdout.decode("utf-8")
    files = list(directory.glob("*.mp4"))
    assert len(files) == len(SAMPLES)
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    repeated = run()
    assert repeated.returncode == 1
    error = repeated.stderr.decode("utf-8")
    assert "样本已存在" in error and str(directory) in error
    assert "Traceback" not in error
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in files} == hashes
    assert not list(directory.glob(".generating-*"))
