"""Shared synthetic/private regression steps with independent pixel checks."""

from dataclasses import asdict
from fractions import Fraction
import hashlib
import json
from math import isfinite
from pathlib import Path
from statistics import mean
import subprocess
from tempfile import TemporaryDirectory

from app.analyzer import analyze, absolute_difference
from app.compatibility import diagnose_compatibility, format_compatibility_report
from app.fixer import prepare_fix, execute_fix
from app.media_profile import format_media_profile
from app.models import CompatibilitySeverity
from app.output_validation import validate_output_file
from app.presets import format_validation_report
from tests.media_helpers import frame_times
from .catalog import observed_categories
from .pixels import WIDTH, HEIGHT, FULL_LEVELS, LIMITED_LEVELS, patch_means, PIXEL_TOLERANCE
from .report import aggregate


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def step(row, name, action):
    row.stages[name] = "FAIL"
    result = action()
    row.stages[name] = "PASS"
    return result


def cadence_of(times):
    assert len(times) >= 3, "Insufficient decoded frames"
    deltas = [b - a for a, b in zip(times, times[1:])]
    assert min(deltas) > 0, "Non-monotonic frame timestamps"
    return "CFR" if max(deltas) - min(deltas) < 0.00001 else "VFR"


def assert_case(case, info, cadence):
    video = info.videos[0]
    assert video.codec == case.codec
    assert video.color_range == ("pc" if case.full else "tv")
    assert video.pixel_format in ({"yuv420p", "yuvj420p"} if case.full else {"yuv420p"})
    assert video.color_space == video.color_transfer == video.color_primaries == "bt709"
    assert (video.width, video.height) == (WIDTH, 2 * HEIGHT)
    assert 2 <= info.container_duration <= 10
    assert cadence == ("VFR" if case.vfr else "CFR")
    if not case.vfr:
        assert video.avg_frame_rate == Fraction(case.fps)
    assert len(info.audios) == (0 if case.audio_seconds is None else 1)
    if info.audios:
        audio = info.audios[0]
        assert audio.codec == "aac" and audio.sample_rate == case.sample_rate
        assert abs(audio.duration - case.audio_seconds) < 0.05


def checked_compatibility(info, preset):
    report = diagnose_compatibility(info, preset=preset)
    findings = {(f.scope, f.code): f for f in report.findings}
    inventory = findings[("input", "input.track_inventory")].evidence
    assert inventory["video_count"] == len(info.videos) and inventory["audio_count"] == len(info.audios)
    for i, video in enumerate(info.videos):
        scope = f"video:{i}"
        codec = findings[(scope, "video.codec")]
        assert codec.stream_index == video.index
        assert codec.evidence["codec_name"]["raw"] == video.probe_fields["codec_name"].raw
        if video.codec in {"h264", "hevc"}:
            assert codec.severity == CompatibilitySeverity.INFO
        color = findings[(scope, "video.color_range")]
        assert color.evidence["normalized_range"] == video.color_range_name
        if video.color_range_name in {"Full", "Limited"}:
            assert color.severity == (CompatibilitySeverity.REPAIR_RECOMMENDED if video.color_range_name == "Full" else CompatibilitySeverity.INFO)
        if video.color_transfer in {"smpte2084", "arib-std-b67"}:
            assert findings[(scope, "video.dynamic_range")].severity == CompatibilitySeverity.UNSUPPORTED
        frame_rate = findings[(scope, "video.frame_rate")]
        assert frame_rate.evidence["difference_fps"] == absolute_difference(
            float(video.avg_frame_rate) if video.avg_frame_rate else None,
            float(video.r_frame_rate) if video.r_frame_rate else None)
    for i, audio in enumerate(info.audios):
        scope = f"audio:{i}"
        assert findings[(scope, "audio.sample_rate")].evidence["parsed_sample_rate"] == audio.sample_rate
        if info.videos:
            assert findings[(scope, "timing.duration_difference")].evidence["difference_seconds"] == absolute_difference(info.videos[0].duration, audio.duration)
    if not info.audios and info.stream_list_complete:
        assert findings[("input", "input.no_audio")].severity == CompatibilitySeverity.INFO
    return report


def decode_pixels(ffmpeg, path, filters, pixel_format, *, autorotate=True, expected_bytes):
    argv = [ffmpeg, "-v", "error", "-nostdin"]
    if not autorotate:
        argv.append("-noautorotate")
    argv += ["-i", str(path), "-map", "0:V:0", "-frames:v", "1", "-vf", filters,
             "-pix_fmt", pixel_format, "-f", "rawvideo", "pipe:1"]
    result = subprocess.run(argv, capture_output=True, check=True, timeout=60)
    assert len(result.stdout) == expected_bytes
    return result.stdout


def calibration(ffmpeg, path, pixel_format):
    return patch_means(decode_pixels(ffmpeg, path, f"crop={WIDTH}:{HEIGHT}:0:0", pixel_format,
                                    expected_bytes=WIDTH * HEIGHT * 3 // 2))


def assert_calibration(original, actual, full):
    assert len(original) == len(actual) == 3, "Expected all Y/U/V planes"
    for plane, (before, after) in enumerate(zip(original, actual)):
        assert len(before) == len(after) == len(FULL_LEVELS), "Incomplete calibration patches"
        assert all(isfinite(value) for value in (*before, *after)), "Non-finite calibration value"
        span = 219 if plane == 0 else 224
        expected = [16 + value * span / 255 for value in before] if full else before
        assert max(abs(a - b) for a, b in zip(after, expected)) <= PIXEL_TOLERANCE, f"Decoded color conversion differs in plane {plane}"


def private_pixel_reference(ffmpeg, before, after, *, autorotate):
    """One small frame, independent range mapping; no multi-million-frame JSON or full-resolution buffers."""
    v = before.videos[0]
    assert v.color_range in {"pc", "tv"} or v.pixel_format == "yuvj420p", "Unknown source range cannot establish a pixel oracle"
    full = v.color_range == "pc" or (v.color_range is None and v.pixel_format == "yuvj420p")
    size = 128 * 72
    reference = decode_pixels(ffmpeg, before.path,
        f"scale=128:72:in_range={'full' if full else 'limited'}:out_range=limited,format=yuv444p", "yuv444p",
        autorotate=autorotate, expected_bytes=3 * size)
    actual = decode_pixels(ffmpeg, after.path,
        "scale=128:72:in_range=limited:out_range=limited,format=yuv444p", "yuv444p",
        autorotate=autorotate, expected_bytes=3 * size)
    errors = [mean(abs(a - b) for a, b in zip(reference[i*size:(i+1)*size], actual[i*size:(i+1)*size])) for i in range(3)]
    assert max(errors) <= PIXEL_TOLERANCE, f"First-frame reference Y/U/V mean absolute errors: {errors}"
    return errors


def run_sample(path, row, recorder, tools, temporary_root, *, case=None):
    artifact = recorder.directory / "artifacts" / hashlib.sha256(row.id.encode()).hexdigest()[:16]
    artifact.mkdir(parents=True, exist_ok=True)
    row.artifacts = artifact.relative_to(recorder.directory).as_posix()
    (artifact / "post-repair.txt").write_text("NOT TESTED\n", encoding="utf-8")
    original_hash = None
    try:
        original_hash = file_hash(path)
        def inspect():
            info = analyze(path)
            assert info.videos, "No video stream"
            cadence = cadence_of(frame_times(path)) if case else None
            row.observed = observed_categories(info, cadence=cadence)
            v = info.videos[0]
            row.input = {"path": str(path), "sha256": original_hash, "bytes": path.stat().st_size,
                         "description": f"{v.codec} / {v.pixel_format} / {v.color_range} / avg={v.avg_frame_rate} / {cadence or 'cadence not scanned'}",
                         "video_duration": v.duration, "audio_durations": [a.duration for a in info.audios]}
            if case:
                assert_case(case, info, cadence)
                # Confirm the generator really contains the intended numeric levels.
                bars = calibration(tools["ffmpeg"], path, v.pixel_format)
                expected = FULL_LEVELS if case.full else LIMITED_LEVELS
                assert_calibration([list(expected), list(expected), list(reversed(expected))], bars, False)
                if case.full and v.pixel_format == "yuvj420p":
                    row.notes.append("Full input decoded as yuvj420p; not counted as literal yuv420p+full coverage.")
            (artifact / "input-profile.txt").write_text(format_media_profile(info), encoding="utf-8")
            return info
        before = step(row, "Analyze", inspect)
        diagnosis = step(row, "Compatibility", lambda: checked_compatibility(before, row.preset))
        (artifact / "input-compatibility.txt").write_text(format_compatibility_report(diagnosis), encoding="utf-8")
        # Real sources are never copied into Git or overwritten. All encoded outputs
        # are inside this temporary directory and removed even after failed checks.
        with TemporaryDirectory(prefix="matrix-output-", dir=temporary_root) as output_dir:
            row.stages["Repair"] = "FAIL"
            plan = prepare_fix(before, Path(output_dir), preset=row.preset)
            (artifact / "expected-output.json").write_text(json.dumps(asdict(plan.expected), default=str, ensure_ascii=False, indent=2), encoding="utf-8")
            def record_validation(report):
                row.stages["Validate"] = report.overall.value
                row.stages["Duration"] = aggregate(c.level.value for c in report.checks if c.code.endswith((".duration", ".difference")))
                row.notes.extend(warning for warning in report.warnings if warning not in row.notes)
                (artifact / "post-repair.txt").write_text(format_validation_report(report), encoding="utf-8")
            result = step(row, "Repair", lambda: execute_fix(plan, on_validation=record_validation))
            def validate():
                report = validate_output_file(plan.expected, result.after.path, probe=analyze)
                record_validation(report)
                assert not report.errors, "\n".join(report.errors)
                return report
            report = step(row, "Validate", validate)
            row.stages["Validate"] = report.overall.value
            assert row.stages["Duration"] != "FAIL"
            def color():
                if case:
                    assert plan.color.action == ("full_to_limited" if case.full else "preserve_limited")
                    assert_calibration(calibration(tools["ffmpeg"], path, before.videos[0].pixel_format),
                                       calibration(tools["ffmpeg"], result.after.path, "yuv420p"), case.full)
                    row.notes.append("Color checked on decoded Y/U/V calibration patch interiors (tolerance 3 code values).")
                else:
                    errors = private_pixel_reference(tools["ffmpeg"], before, result.after,
                                                     autorotate=plan.repair.video.autorotate)
                    row.notes.append(f"Color sampled on first decoded frame; Y/U/V MAE={errors}; not exhaustive content validation.")
            try:
                step(row, "Color", color)
            except Exception:
                row.stages["Validate"] = "FAIL"
                raise
    finally:
        if original_hash is not None:
            def unchanged():
                assert file_hash(path) == original_hash, "Source file changed during regression"
            try:
                step(row, "Source unchanged", unchanged)
            except Exception:
                row.stages["Validate"] = "FAIL"
                raise
