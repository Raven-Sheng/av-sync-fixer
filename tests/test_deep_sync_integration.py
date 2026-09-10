"""Real ffprobe/FFmpeg clocks, pitch and duration checks on generated media only."""

from array import array
import hashlib
from pathlib import Path
import subprocess

import pytest

from app.analyzer import analyze
from app.deep_sync import deep_analyze
from app.ffmpeg_utils import MissingToolError, check_tools, run_command
from app.fixer import fix_video, prepare_fix


@pytest.fixture(scope="module")
def tools():
    try:
        return check_tools()
    except MissingToolError as exc:
        pytest.skip(str(exc))


def make_clock(tools, path, *, ppm=0, offset=0, duration=240, jump=False):
    expression = f"{1 + ppm / 1e6:.8f}*PTS+{offset}/TB"
    if jump:
        expression += "+if(gte(T\\,60)\\,0.2/TB\\,0)"
    run_command([tools["ffmpeg"], "-v", "error", "-nostdin", "-n",
        "-f", "lavfi", "-i", f"testsrc2=size=160x90:rate=2:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}",
        "-af", f"asetpts={expression}", "-c:v", "libx264", "-preset", "ultrafast",
        "-color_range", "tv", "-bsf:v", "h264_metadata=video_full_range_flag=0",
        "-c:a", "pcm_s16le", str(path)], timeout=60)


def pitch(tools, path):
    result = subprocess.run([tools["ffmpeg"], "-v", "error", "-nostdin", "-ss", "100",
        "-i", str(path), "-map", "0:a:0", "-t", "10", "-ac", "1", "-ar", "48000",
        "-f", "s16le", "pipe:1"], capture_output=True, check=True, timeout=20)
    samples = array("h")
    samples.frombytes(result.stdout)
    if __import__("sys").byteorder != "little":
        samples.byteswap()
    crossings, armed = [], False
    for index, sample in enumerate(samples):
        if sample < -500:
            armed = True
        elif armed and sample > 500:
            crossings.append(index)
            armed = False
    assert len(crossings) > 4000
    return 48000 * (len(crossings) - 1) / (crossings[-1] - crossings[0])


@pytest.mark.parametrize("ppm", [250, -250])
def test_real_progressive_compensation_preserves_pitch_speed_and_video_duration(tools, tmp_path, ppm):
    path = tmp_path / f"-中文 空格 & 漂移 {ppm}.nut"
    make_clock(tools, path, ppm=ppm)
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    before = analyze(path, deep_analysis=True)
    evidence = before.deep_sync.audios[0]
    assert evidence.complete and evidence.patterns == ("PROGRESSIVE_DRIFT",)
    assert evidence.drift_ppm == pytest.approx(ppm, abs=0.02)
    assert evidence.drift_seconds == pytest.approx(240 * ppm / 1e6, abs=0.001)
    plan = prepare_fix(before, tmp_path / "preview")
    assert plan.repair.audios[0].sync_strategy == "async_soft"
    result = fix_video(before, tmp_path / "修复 输出")
    after = analyze(result.after.path, deep_analysis=True)
    # NUT omits stream duration: compare observed frame endpoints and the known
    # generated content length, rather than substituting container duration.
    assert after.videos[0].duration == pytest.approx(240, abs=0.025)
    assert after.deep_sync.video.first_pts == pytest.approx(before.deep_sync.video.first_pts, abs=0.001)
    assert after.deep_sync.video.last_pts == pytest.approx(before.deep_sync.video.last_pts, abs=0.001)
    assert after.deep_sync.video.packets == before.deep_sync.video.packets
    assert after.videos[0].avg_frame_rate == before.videos[0].r_frame_rate
    corrected = after.deep_sync.audios[0]
    assert abs(corrected.drift_ppm) < 10
    change = corrected.sample_duration - evidence.sample_duration
    assert change == pytest.approx(evidence.drift_seconds, abs=0.035)
    assert abs(change) / evidence.sample_duration <= 0.0005
    assert pitch(tools, after.path) == pytest.approx(440, abs=0.5)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash


@pytest.mark.parametrize("offset,jump,expected", [(0.2, False, "STATIC_OFFSET"), (0, True, "TIMESTAMP_ANOMALY")])
def test_real_offset_and_discontinuity_do_not_enable_async(tools, tmp_path, offset, jump, expected):
    path = tmp_path / "offset or jump.nut"
    make_clock(tools, path, offset=offset, jump=jump, duration=120)
    source = analyze(path, deep_analysis=True)
    assert expected in source.deep_sync.audios[0].patterns
    assert "PROGRESSIVE_DRIFT" not in source.deep_sync.audios[0].patterns
    assert prepare_fix(source, tmp_path / "out").repair.audios[0].sync_strategy == "preserve"


def test_real_record_budget_reports_partial_and_never_authorizes_compensation(tools, tmp_path):
    path = tmp_path / "限额.nut"
    make_clock(tools, path, ppm=250, duration=2)
    source = analyze(path)
    deep = deep_analyze(source, tools["ffprobe"], max_records=10)
    assert sum(scan.records for scan in deep.scans) == 10
    assert any(not scan.complete for scan in deep.scans)
    assert all(not a.complete for a in deep.audios)
    assert any("不完整" in limitation for limitation in deep.limitations)


def test_real_vfr_detected_with_matching_metadata_fps(tools, tmp_path):
    from dataclasses import replace
    from fractions import Fraction
    path = tmp_path / "vfr.mp4"
    run_command([tools["ffmpeg"], "-v", "error", "-nostdin", "-n", "-f", "lavfi", "-i",
        "testsrc2=size=160x90:rate=30:duration=10", "-vf",
        "select=if(lt(t\\,5)\\,not(mod(n\\,2))\\,1)", "-fps_mode", "vfr", "-c:v", "libx264",
        "-color_range", "tv", "-bsf:v", "h264_metadata=video_full_range_flag=0", str(path)], 30)
    source = analyze(path)
    source = replace(source, videos=(replace(source.videos[0], avg_frame_rate=Fraction(30), r_frame_rate=Fraction(30)),))
    deep = deep_analyze(source, tools["ffprobe"])
    assert "VFR_SUSPECTED" in deep.patterns
    assert deep.video.dts_regressions == 0
    source = replace(source, deep_sync=deep)
    assert prepare_fix(source, tmp_path / "out").strategy.cfr
