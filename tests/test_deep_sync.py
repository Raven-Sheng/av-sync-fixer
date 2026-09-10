"""Synthetic timestamp clocks: duration metadata is deliberately not the signal."""

from dataclasses import replace
from fractions import Fraction
import sys
from time import monotonic
import tracemalloc

import pytest

from app.analyzer import analyze, diagnose
from app.cli import main
from app.deep_sync import (TrackAccumulator, OnlineFit, deep_analyze, soft_compensation_allowed,
                           format_deep_report, timestamp, MAX_CHECKPOINTS)
from app.ffmpeg_utils import MediaError, build_repair_command
from app.models import DeepSyncAnalysis, TimestampEvidence
from app.repair_plan import select_repair_plan
from app.timestamp_probe import scan_timestamp_lines
from tests.test_strategies import media


def audio_clock(*, ppm=0, offset=0, seconds=1200, step_at=None, missing_at=None, quantum=Fraction(1, 48000)):
    stream = replace(media().audios[0], sample_rate=48000, time_base=quantum)
    accumulator = TrackAccumulator(stream, "audio")
    for i in range(seconds * 10):
        pts = offset + i / 10 * (1 + ppm / 1e6)
        if step_at is not None and i >= step_at * 10:
            pts += 0.2
        fields = {"pts": str(round(pts / float(quantum))), "nb_samples": "4800"}
        if i == missing_at:
            del fields["pts"]
        accumulator.frame(fields)
    return accumulator


def good_video():
    return TimestampEvidence(0, "video", packets=36000, frames=600, first_pts=0, last_pts=1199.9, complete=True)


@pytest.mark.parametrize("ppm", [250, -250, 100, 1000, 50000])
def test_progressive_clock_drift_uses_pts_and_samples(ppm):
    evidence = audio_clock(ppm=ppm).finish(complete=True, video_start=0)
    assert evidence.patterns == ("PROGRESSIVE_DRIFT",)
    assert evidence.drift_ppm == pytest.approx(ppm, abs=0.01)
    assert evidence.drift_seconds == pytest.approx(1199.9 * ppm / 1e6, abs=0.0001)
    assert evidence.fit_r_squared > 0.999
    assert soft_compensation_allowed(evidence, good_video()) == (abs(ppm) <= 500)


@pytest.mark.parametrize("offset", [0.2, -0.2, 0.0])
def test_static_offset_is_separate_from_slope(offset):
    evidence = audio_clock(offset=offset).finish(complete=True, video_start=0)
    assert evidence.start_offset_seconds == pytest.approx(offset)
    assert evidence.drift_ppm == pytest.approx(0, abs=0.001)
    assert evidence.patterns == (("STATIC_OFFSET",) if offset else ("UNKNOWN",))
    assert not soft_compensation_allowed(evidence, good_video())


@pytest.mark.parametrize("kwargs", [{"step_at": 600}, {"missing_at": 200}, {"ppm": 250, "step_at": 600}])
def test_step_and_missing_pts_do_not_become_clock_drift(kwargs):
    evidence = audio_clock(**kwargs).finish(complete=True)
    assert "TIMESTAMP_ANOMALY" in evidence.patterns
    assert "PROGRESSIVE_DRIFT" not in evidence.patterns
    assert not soft_compensation_allowed(evidence, good_video())


@pytest.mark.parametrize("ppm", [0, 250])
def test_missing_pts_keeps_decoded_samples_without_manufacturing_drift(ppm):
    accumulator = TrackAccumulator(replace(media().audios[0], sample_rate=48000), "audio")
    for i in range(12000):
        fields = {"nb_samples": "4800", "pts_time": str(i / 10 * (1 + ppm / 1e6))}
        if i % 100 == 0:
            del fields["pts_time"]
        accumulator.frame(fields)
    result = accumulator.finish(complete=True)
    assert result.sample_duration == pytest.approx(1200)
    assert result.drift_ppm == pytest.approx(ppm, abs=0.001)
    assert result.discontinuities == 0
    assert result.missing_timestamps == 120
    assert result.patterns == ("TIMESTAMP_ANOMALY",)
    assert not soft_compensation_allowed(result, good_video())


def test_unknown_sample_count_invalidates_clock_fit():
    accumulator = audio_clock(ppm=250, seconds=100)
    accumulator.frame({"pts_time": "100.025"})
    result = accumulator.finish(complete=True)
    assert result.drift_ppm is None and result.drift_seconds is None
    assert result.patterns == ("TIMESTAMP_ANOMALY",)


def test_coarse_millisecond_timebase_not_mistaken_for_clock_jumps():
    evidence = audio_clock(ppm=250, quantum=Fraction(1, 1000)).finish(complete=True)
    assert evidence.patterns == ("PROGRESSIVE_DRIFT",)
    assert evidence.drift_ppm == pytest.approx(250, abs=0.2)


def test_incomplete_short_or_anomalous_video_evidence_cannot_authorize_soft_compensation():
    evidence = audio_clock(ppm=250).finish(complete=False)
    assert not soft_compensation_allowed(evidence, good_video())
    evidence = audio_clock(ppm=250, seconds=10).finish(complete=True)
    assert "PROGRESSIVE_DRIFT" not in evidence.patterns
    full = audio_clock(ppm=250).finish(complete=True)
    assert not soft_compensation_allowed(full, replace(good_video(), patterns=("TIMESTAMP_ANOMALY",)))


def test_clock_reset_not_linear_drift():
    accumulator = audio_clock(seconds=120)
    accumulator.frame({"pts_time": "0", "nb_samples": "4800"})
    assert "TIMESTAMP_ANOMALY" in accumulator.finish(complete=True).patterns


def test_video_frame_cadence_and_packet_b_frame_reorder():
    stream = replace(media().videos[0], time_base=Fraction(1, 90000))
    accumulator = TrackAccumulator(stream, "video")
    for i in range(100):
        accumulator.packet({"dts_time": str(i / 30), "pts_time": str((i + (2 if i % 3 == 0 else -1)) / 30)})
        accumulator.frame({"pts_time": str(i / 30)})
    result = accumulator.finish(complete=True)
    assert result.dts_regressions == 0 and result.vfr_suspected is False
    accumulator.new_video_window()
    for i in range(100):
        accumulator.frame({"pts_time": str(600 + i / 15)})
    result = accumulator.finish(complete=True)
    assert result.vfr_suspected and result.discontinuities == 0
    accumulator.packet({"dts_time": "-10", "pts_time": "1"})
    assert accumulator.finish(complete=True).dts_regressions == 1


def test_timestamp_uses_timebase_ticks_and_validates_fallback():
    stream = replace(media().videos[0], time_base=Fraction(1, 90000))
    assert timestamp({"pts": "90000", "pts_time": "bad"}, "pts", stream) == 1
    assert timestamp({"pts": "N/A", "pts_time": "-0.1"}, "pts", stream) == -0.1
    assert timestamp({"pts_time": "nan"}, "pts", stream) is None


def test_long_stream_memory_is_bounded():
    tracemalloc.start()
    accumulator = audio_clock(seconds=6000, ppm=250)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(accumulator.points) <= MAX_CHECKPOINTS
    assert len(accumulator.finish(complete=True).checkpoints) <= MAX_CHECKPOINTS + 1
    assert peak < 2_000_000


def test_centered_fit_with_long_time_origin():
    fit = OnlineFit()
    for i in range(1000):
        fit.add(1e9 + i, i * 0.00025)
    slope, r2, residual = fit.result()
    assert slope == pytest.approx(0.00025, rel=1e-7) and r2 > 0.999 and residual < 1e-5


@pytest.mark.parametrize("audio_duration", ["10", "10.3", "9.7", None])
def test_metadata_duration_alone_never_enables_async(audio_duration):
    source = media(audio={"duration": audio_duration})
    assert "PROGRESSIVE_DRIFT" not in diagnose(source).patterns
    assert select_repair_plan(source).audios[0].sync_strategy == "preserve"


def deep_source(ppm=250, *, complete=True):
    source = media(audio={"sample_rate": "48000"})
    evidence = audio_clock(ppm=ppm).finish(complete=complete, video_start=0)
    return replace(source, deep_sync=DeepSyncAnalysis(good_video(), (evidence,), (), evidence.patterns, ()))


def test_deep_evidence_selects_bounded_soft_compensation_and_reports_estimate(tmp_path):
    source = deep_source()
    plan = select_repair_plan(source)
    audio = plan.audios[0]
    assert audio.sync_strategy == "async_soft" and audio.sample_rate == 48000
    assert audio.estimated_drift_ppm == pytest.approx(250, abs=0.01)
    command = build_repair_command("ffmpeg", plan, tmp_path / "out.mp4")
    af = command[command.index("-filter:a:0") + 1]
    assert "max_soft_comp=0.0005" in af and "min_hard_comp=2147483647" in af
    assert "async=24" in af and "N/SR/TB" not in af
    assert "-fflags" not in command and "fps=fps=" not in command[command.index("-vf") + 1]
    for forbidden in ("atempo", "asetrate", "-shortest", "-t"):
        assert forbidden not in command and forbidden not in af
    assert any(d.item.startswith("Estimated drift") for d in plan.decisions)


@pytest.mark.parametrize("ppm,complete", [(0, True), (1000, True), (250, False)])
@pytest.mark.parametrize("mode", ["safe", "audio-sync"])
def test_large_or_incomplete_drift_is_not_automatically_compensated(ppm, complete, mode):
    plan = select_repair_plan(deep_source(ppm, complete=complete), mode)
    assert plan.audios[0].sync_strategy == "preserve"


def test_multiple_tracks_use_stream_identity_not_first_audio_result():
    source = deep_source()
    source = replace(source, audios=(replace(source.audios[0], index=7), source.audios[0]))
    plan = select_repair_plan(source)
    assert [a.sync_strategy for a in plan.audios] == ["preserve", "async_soft"]


def test_missing_metadata_start_uses_complete_frame_evidence_to_preserve_offset(tmp_path):
    source = deep_source()
    evidence = audio_clock(ppm=250, offset=10.2).finish(complete=True, video_start=10)
    source = replace(source, videos=(replace(source.videos[0], start_time=None),),
                     audios=(replace(source.audios[0], start_time=None),),
                     deep_sync=replace(source.deep_sync, video=replace(good_video(), first_pts=10), audios=(evidence,)))
    plan = select_repair_plan(source)
    assert plan.video.start_offset == 0
    assert plan.audios[0].start_offset == pytest.approx(0.2)
    command = build_repair_command("ffmpeg", plan, tmp_path / "out.mp4")
    assert "-fflags" not in command
    assert "asetpts=PTS+0.200000000/TB" in command[command.index("-filter:a:0") + 1]


def test_missing_leading_pts_is_not_used_as_an_observed_stream_start():
    source = deep_source()
    evidence = audio_clock(missing_at=0).finish(complete=True, video_start=0)
    assert evidence.first_pts == pytest.approx(0.1)
    source = replace(source, audios=(replace(source.audios[0], start_time=None),),
                     deep_sync=replace(source.deep_sync, audios=(evidence,)))
    plan = select_repair_plan(source, "cfr")
    assert plan.audios[0].start_offset == 0
    assert any(d.item == "Timeline normalization" and "起点未知" in d.reason for d in plan.decisions)


@pytest.mark.parametrize("ratio", [None, True, float("nan"), float("inf"), 0.001, 0])
def test_builder_rejects_unsafe_soft_compensation_limits(tmp_path, ratio):
    plan = select_repair_plan(deep_source())
    plan = replace(plan, audios=(replace(plan.audios[0], max_soft_compensation=ratio),))
    with pytest.raises(MediaError, match="补偿"):
        build_repair_command("ffmpeg", plan, tmp_path / "out.mp4")


def test_report_labels_observed_drift_not_content_alignment():
    report = format_deep_report(deep_source().deep_sync)
    assert "PROGRESSIVE_DRIFT" in report and "+250.000ppm" in report
    assert "相对采样时钟" in report and "Audio clock drift: SUSPECTED" in report


def test_cli_deep_flag_reaches_analyzer_without_requiring_fix(monkeypatch, capsys):
    calls = []
    def fake(path, **kwargs):
        calls.append(kwargs)
        return deep_source()
    monkeypatch.setattr("app.cli.analyze", fake)
    assert main(["中文 录屏.mp4", "--deep-analysis", "--deep-timeout", "10", "--deep-max-records", "100"]) == 0
    assert calls == [{"deep_analysis": True, "deep_timeout": 10, "deep_max_records": 100}]
    assert "Deep sync analysis" in capsys.readouterr().out


@pytest.mark.parametrize("args", [["--deep-timeout", "2"], ["--deep-analysis", "--deep-timeout", "nan"],
                                  ["--deep-analysis", "--deep-max-records", "0"]])
def test_cli_rejects_invalid_deep_options(args):
    with pytest.raises(SystemExit) as exc:
        main(["input.mp4", *args])
    assert exc.value.code == 2


def child(code):
    return [sys.executable, "-u", "-c", code]


def test_streaming_reader_caps_records_and_drains_stderr():
    lines = []
    result = scan_timestamp_lines(child("import sys; sys.stderr.write('x'*200000); print('packet|pts=0\\n'*10000)"),
                                  lines.append, purpose="test", timeout=10, max_records=50)
    assert result.records == len(lines) == 50 and not result.complete
    assert "记录上限" in result.reason


def test_streaming_exact_record_budget_can_complete():
    lines = []
    result = scan_timestamp_lines(child("for i in range(50): print('packet|pts=' + str(i))"),
                                  lines.append, purpose="test", timeout=10, max_records=50)
    assert result.records == len(lines) == 50
    assert result.complete and not result.reason


@pytest.mark.parametrize("pipe_name", ["stdout", "stderr"])
def test_stream_read_failure_cannot_count_as_complete(monkeypatch, pipe_name):
    from app import timestamp_probe
    original = timestamp_probe.subprocess.Popen
    processes = []

    class FailedReader:
        def __init__(self, stream):
            self.stream = stream

        def readline(self, limit):
            raise OSError("simulated pipe read failure")

        def close(self):
            self.stream.close()

    def observe(*args, **kwargs):
        process = original(*args, **kwargs)
        setattr(process, pipe_name, FailedReader(getattr(process, pipe_name)))
        processes.append(process)
        return process

    monkeypatch.setattr(timestamp_probe.subprocess, "Popen", observe)
    result = scan_timestamp_lines(child("print('packet|pts=1')"), lambda _: None,
                                  purpose="test", timeout=10, max_records=100)
    assert not result.complete and pipe_name in result.reason
    assert "simulated pipe read failure" in result.reason
    assert processes[0].poll() is not None
    assert getattr(processes[0], pipe_name).stream.closed


def test_streaming_timeout_reaps_silent_process(monkeypatch):
    from app import timestamp_probe
    processes = []
    original = timestamp_probe.subprocess.Popen
    def observe(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(timestamp_probe.subprocess, "Popen", observe)
    started = monotonic()
    result = scan_timestamp_lines(child("import time; time.sleep(60)"), lambda _: None,
                                  purpose="test", timeout=0.2, max_records=50)
    assert not result.complete and "时限" in result.reason and monotonic() - started < 5
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed


@pytest.mark.parametrize("code", ["import sys; print('packet|pts=1'); sys.exit(2)",
                                  "import sys; sys.stderr.write('decode error'); print('packet|pts=1')"])
def test_probe_error_cannot_count_as_complete(code):
    result = scan_timestamp_lines(child(code), lambda _: None, purpose="test", timeout=10, max_records=100)
    assert not result.complete and "错误" in result.reason


def test_streaming_callback_cancellation_cleans_up(monkeypatch):
    from app import timestamp_probe
    processes = []
    original = timestamp_probe.subprocess.Popen
    def observe(*args, **kwargs):
        p = original(*args, **kwargs)
        processes.append(p)
        return p
    monkeypatch.setattr(timestamp_probe.subprocess, "Popen", observe)
    def cancel(_):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        scan_timestamp_lines(child("import time; print('packet|pts=1'); time.sleep(60)"), cancel,
                             purpose="test", timeout=10, max_records=100)
    assert processes[0].poll() is not None


@pytest.mark.parametrize("timeout,records", [(0, 100), (float("nan"), 100), (1, 0), (1, True)])
def test_invalid_budget_rejected(timeout, records):
    with pytest.raises(MediaError):
        deep_analyze(media(), "ffprobe", timeout=timeout, max_records=records)
