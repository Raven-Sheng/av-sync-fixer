"""Compare actual output with an immutable spec; never re-diagnose the input."""

from dataclasses import replace
from pathlib import Path

from app.analyzer import absolute_difference, finite_number, fps_to_float, matches_target_fps, compare_threshold
from app.ffmpeg_utils import MediaError
from app.color import hdr_evidence
from app.models import (AnalysisResult, ColorConversionPlan, ExpectedOutputSpec, OutputValidation,
                        FieldState, StreamInfo, ValidationCheck, ValidationLevel)

PASS, WARNING, FAIL = ValidationLevel.PASS, ValidationLevel.WARNING, ValidationLevel.FAIL
MP4_BRANDS = frozenset({"mp41", "mp42", "avc1", "m4v"})
MAX_MP4_BOXES = 100_000


def _invalid_fields(fields, *names) -> bool:
    return any(fields.get(name) is not None and fields[name].state == FieldState.INVALID for name in names)


def color_checks(plan: ColorConversionPlan, video: StreamInfo | None) -> tuple[ValidationCheck, ...]:
    if video is None:
        return (ValidationCheck("Color", "color.unavailable", WARNING, "缺少输出视频轨，颜色未验证。"),)
    checks = []
    def add(code, level, message, expected, actual):
        checks.append(ValidationCheck("Color", code, level, message, expected, actual))
    if evidence := hdr_evidence(video):
        add("color.hdr", FAIL, "输出出现 HDR 或冲突的 HDR 附加证据，不满足当前 SDR 输出计划。", "SDR", evidence)
    pixel_ok = video.pixel_format == plan.pixel_format
    add("color.pixel_format", PASS if pixel_ok else FAIL,
        f"像素格式须为 {plan.pixel_format}，实际为 {video.pixel_format or '未知'}。", plan.pixel_format, video.pixel_format)
    aliases = {"mpeg": "tv", "jpeg": "pc"}
    actual_range = aliases.get(video.color_range, video.color_range)
    expected_range = aliases.get(plan.output_range, plan.output_range)
    range_ok = actual_range == expected_range
    add("color.range", PASS if range_ok else FAIL,
        f"颜色范围须为 {expected_range} / {'Limited' if expected_range == 'tv' else 'Full'}，实际为 {video.color_range or '未知'}。",
        expected_range, actual_range)
    for field in ("color_space", "color_transfer", "color_primaries"):
        expected, actual = getattr(plan, field), getattr(video, field)
        if actual is None:
            # Missing tags do not prove incorrect pixel values; nor does yuv420p alone
            # prove Limited. Range and pixel failures above remain fatal.
            level = WARNING if expected is None or (pixel_ok and range_ok) else FAIL
            message = f"输出 {field} 缺失，无法确认色彩体系标记；目标 {expected or '未指定'}。"
        elif expected is not None and actual != expected:
            level, message = FAIL, f"{field} 须保留 {expected}，实际为 {actual}。"
        else:
            level, message = PASS, f"{field}: {actual}（目标 {expected or '未指定'}）。"
        add("color." + field, level, message, expected, actual)
    return tuple(checks)


def _report(spec, after, checks):
    return OutputValidation(spec.preset, after, spec.video.target_fps,
        tuple(c.message for c in checks if c.level == FAIL),
        tuple(c.message for c in checks if c.level == WARNING), tuple(checks), spec)


def validate_output(spec: ExpectedOutputSpec, after: AnalysisResult) -> OutputValidation:
    """Pure metadata comparison. File existence/readability are checked by validate_output_file."""
    checks = []
    def add(section, code, level, message, expected=None, actual=None):
        checks.append(ValidationCheck(section, code, level, message, expected, actual))
    def requirement(section, code, okay, message, expected=None, actual=None):
        add(section, code, PASS if okay else FAIL, message, expected, actual)
    def duration(label, actual, expected, allowance=0.0, fields=None):
        value = finite_number(actual)
        if value is None:
            invalid = _invalid_fields(fields or {}, "duration", "duration_ts")
            add("Timing", label + ".duration", WARNING if actual is None and not invalid else FAIL,
                f"{label} 时长未知或无效，无法完成长度核对。", expected, actual)
        elif value <= 0:
            add("Timing", label + ".duration", FAIL, f"{label} 时长必须大于零。", expected, actual)
        elif expected is None:
            add("Timing", label + ".duration", WARNING, f"{label} 输入时长未知，输出 {value:.3f} 秒；无法确认内容长度保持。", expected, actual)
        else:
            delta = absolute_difference(value, expected)
            tolerance = spec.duration_tolerance + allowance
            requirement("Timing", label + ".duration", compare_threshold(delta, tolerance) <= 0,
                f"{label} 时长：计划保持 {expected:.3f} 秒，实际 {value:.3f} 秒，容差 {tolerance:.3f} 秒。", expected, value)

    def checked_start(stream, label):
        value = finite_number(stream.start_time)
        if ((value is None and (stream.start_time is not None or _invalid_fields(stream.probe_fields, "start_time", "start_pts")))
                or (value is not None and value < -spec.start_tolerance)):
            add("Timing", label + ".start.invalid", FAIL, f"{label} 输出起点无效或存在超出容差的负值。", actual=stream.start_time)
        return value

    formats = (after.container or "").split(",")
    brand = (after.major_brand or "").strip().lower()
    container_ok = spec.container.format in formats
    if spec.container.format == "mp4":
        container_ok = container_ok and (brand.startswith("iso") or brand in MP4_BRANDS)
    requirement("Compatibility", "container.format", container_ok,
        f"目标 {spec.container.format.upper()} 容器；实际 {after.container or '未知'}，major_brand {after.major_brand or '未知'}。",
        spec.container.format, after.container)
    video = after.videos[0] if after.videos else None
    expected = spec.video
    requirement("Video", "video.count", len(after.videos) == 1, "输出必须包含一条视频轨。", 1, len(after.videos))
    requirement("Audio", "audio.count", len(after.audios) == len(spec.audios), "输出音轨数量与计划不一致。", len(spec.audios), len(after.audios))
    if video:
        requirement("Video", "video.codec", video.codec == expected.codec,
            f"视频编码须为 {expected.codec}（{'H.264' if expected.codec == 'h264' else expected.codec}），实际 {video.codec or '未知'}。", expected.codec, video.codec)
        if expected.width is not None and expected.height is not None:
            requirement("Video", "video.dimensions", (video.width, video.height) == (expected.width, expected.height),
                f"分辨率须为 {expected.width} × {expected.height}，实际 {video.width} × {video.height}。",
                (expected.width, expected.height), (video.width, video.height))
        else:
            add("Video", "video.dimensions", WARNING, "计划分辨率无法确定，未确认输出尺寸。")
        if expected.fps_mode == "cfr":
            okay = matches_target_fps(video, expected.target_fps)
            requirement("Video", "video.fps", okay, f"平均/标称 FPS 须同时匹配 CFR 目标 {expected.target_fps}。",
                expected.target_fps, (fps_to_float(video.avg_frame_rate), fps_to_float(video.r_frame_rate)))
        else:
            actual_fps = fps_to_float(video.avg_frame_rate)
            if expected.average_fps is None or actual_fps is None:
                add("Video", "video.fps", WARNING, "保持帧率模式：平均 FPS 未知，无法核对。")
            else:
                tolerance = expected.fps_tolerance
                requirement("Video", "video.fps", abs(actual_fps - expected.average_fps) <= tolerance,
                    f"保持平均 FPS {expected.average_fps:g}，实际 {actual_fps:g}；容差 {tolerance:g}。", expected.average_fps, actual_fps)
        duration("Video", video.duration, expected.duration, fields=video.probe_fields)
        start = checked_start(video, "video")
        if start is None or expected.start_offset is None:
            add("Timing", "video.start", WARNING, "视频起点未知，不能确认计划时间轴。", expected.start_offset, video.start_time)
        else:
            okay = -spec.start_tolerance <= start - expected.start_offset <= spec.mux_start_allowance
            requirement("Timing", "video.start", okay,
                f"视频起点：计划 {expected.start_offset:.3f} 秒，实际 {start:.3f} 秒（允许编码封装平移 {spec.mux_start_allowance:.3f} 秒）。",
                expected.start_offset, start)
    else:
        add("Timing", "video.unavailable", WARNING, "缺少视频轨，视频时序未验证。")
    checks.extend(color_checks(replace(expected.color, pixel_format=expected.pixel_format), video))
    for position, (audio, target) in enumerate(zip(after.audios, spec.audios), 1):
        label = f"音轨 {position}"
        requirement("Audio", f"audio.{position}.codec", audio.codec == target.codec,
            f"{label} 编码须为 {target.codec} / {target.sample_rate or '输入'} Hz，实际 {audio.codec or '未知'}。", target.codec, audio.codec)
        if target.sample_rate is None:
            add("Audio", f"audio.{position}.rate", WARNING, f"{label} 输入采样率未知，不能核对保持策略。", None, audio.sample_rate)
        else:
            requirement("Audio", f"audio.{position}.rate", audio.sample_rate == target.sample_rate,
                f"{label} 采样率须为 {target.sample_rate} Hz，实际 {audio.sample_rate or '未知'}。", target.sample_rate, audio.sample_rate)
        duration(label, audio.duration, target.duration, target.soft_duration_allowance, audio.probe_fields)
        diff = absolute_difference(video.duration if video else None, audio.duration)
        if diff is None or target.duration_diff is None:
            add("Timing", f"audio.{position}.difference", WARNING, f"{label} 修复前或修复后的长度差未知，不能确认是否恶化。", target.duration_diff, diff)
        else:
            worse = compare_threshold(diff - target.duration_diff, spec.sync_regression_tolerance) > 0
            level = FAIL if worse else WARNING if compare_threshold(diff, spec.duration_risk_threshold) >= 0 else PASS
            add("Timing", f"audio.{position}.difference", level,
                f"{label} 音视频长度差：修复前 {target.duration_diff:.3f} 秒 → 修复后 {diff:.3f} 秒；"
                + (f"明显恶化（容差 {spec.sync_regression_tolerance:.3f} 秒）。" if worse else
                   "同步风险未消除；未强行截断或拉伸内容。" if level == WARNING else "未明显恶化。"), target.duration_diff, diff)
        audio_start = checked_start(audio, f"audio.{position}")
        actual_start = audio_start - start if video and audio_start is not None and start is not None else None
        if actual_start is None or target.start_relative_to_video is None:
            add("Timing", f"audio.{position}.start", WARNING, f"{label} 起点未知，无法核对相对偏移。", target.start_relative_to_video, actual_start)
        else:
            requirement("Timing", f"audio.{position}.start", abs(actual_start - target.start_relative_to_video) <= spec.start_tolerance,
                f"{label} 相对视频起点：计划 {target.start_relative_to_video:+.3f} 秒，实际 {actual_start:+.3f} 秒。",
                target.start_relative_to_video, actual_start)
    if after.container_duration is not None or _invalid_fields(after.probe_fields, "duration"):
        # Container duration includes offsets and may legitimately exceed video duration.
        # Use it only as a fallback when the input video duration is unavailable.
        if finite_number(after.container_duration) is None or after.container_duration <= 0:
            add("Timing", "container.duration", FAIL, "输出容器时长无效。")
        else:
            if expected.duration is None:
                duration("Container", after.container_duration, spec.container_duration, spec.mux_start_allowance)
            streams = (*after.videos, *after.audios)
            if streams and all(finite_number(s.duration) is not None and finite_number(s.start_time) is not None for s in streams):
                end = max(s.start_time + s.duration for s in streams)
                span = end - min(s.start_time for s in streams)
                # Demuxers may report a span or an endpoint including initial empty
                # time. Accept either convention, not arbitrarily inflated durations.
                lower, upper = sorted((end, span))
                okay = lower - spec.duration_tolerance <= after.container_duration <= upper + spec.duration_tolerance
                requirement("Timing", "container.duration", okay,
                    f"容器时长 {after.container_duration:.3f} 秒；输出轨道时间轴要求约 {lower:.3f}–{upper:.3f} 秒。",
                    (lower, upper), after.container_duration)
    return _report(spec, after, checks)


def _faststart(path: Path) -> bool:
    """Walk only top-level box headers (including 64-bit sizes), never load mdat."""
    with path.open("rb") as stream:
        end = path.stat().st_size
        moov = mdat = None
        for _ in range(MAX_MP4_BOXES):
            offset = stream.tell()
            if offset == end:
                break
            header = stream.read(8)
            if len(header) != 8:
                raise ValueError("MP4 box header 截断")
            size, kind = int.from_bytes(header[:4], "big"), header[4:]
            if size == 1:
                extended = stream.read(8)
                if len(extended) != 8:
                    raise ValueError("MP4 extended size 截断")
                size = int.from_bytes(extended, "big")
            elif size == 0:
                size = end - offset
            if size < stream.tell() - offset or offset + size > end:
                raise ValueError("MP4 box size 无效")
            if kind == b"moov" and moov is None:
                moov = offset
            if kind == b"mdat" and mdat is None:
                mdat = offset
            stream.seek(offset + size)
        if stream.tell() != end:
            raise ValueError("MP4 box 数量超过验证上限")
        return moov is not None and mdat is not None and moov < mdat


def validate_output_file(spec: ExpectedOutputSpec, path: Path, *, probe) -> OutputValidation:
    checks = []
    after = None
    try:
        if not path.is_file():
            raise MediaError("输出文件不存在或不是普通文件。")
        size = path.stat().st_size
        if size <= 0:
            raise MediaError("输出文件大小必须大于零。")
        checks.append(ValidationCheck("Compatibility", "file.size", PASS, f"输出文件存在，大小 {size} bytes。", "> 0", size))
        after = replace(probe(path), path=path)
        checks.append(ValidationCheck("Compatibility", "file.probe", PASS, "ffprobe 正常读取输出。"))
    except (MediaError, OSError) as exc:
        checks.append(ValidationCheck("Compatibility", "file.readable", FAIL, f"输出复查失败：{exc}"))
        checks.extend(ValidationCheck(section, "unverified", WARNING, f"{section} 未验证：输出不可读取。")
                      for section in ("Video", "Audio", "Color", "Timing"))
        return _report(spec, after, checks)
    result = validate_output(spec, after)
    checks.extend(result.checks)
    if spec.container.format == "mp4" and spec.container.faststart:
        try:
            okay = _faststart(path)
            checks.append(ValidationCheck("Compatibility", "container.faststart", PASS if okay else FAIL,
                "faststart 要求 moov 位于 mdat 之前。", True, okay))
        except (OSError, ValueError) as exc:
            checks.append(ValidationCheck("Compatibility", "container.faststart", FAIL, f"无法验证 faststart：{exc}"))
    return _report(spec, after, checks)
