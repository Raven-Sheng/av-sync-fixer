"""Timestamp evidence, not audiovisual content matching or physical-clock measurement."""

from dataclasses import replace
import math
from time import monotonic

from app.analyzer import finite_number, fps_to_float, integer
from app.ffmpeg_utils import MediaError
from app.models import AnalysisResult, DeepSyncAnalysis, StreamInfo, TimestampEvidence, MAX_SOFT_DRIFT_PPM
from app.timestamp_probe import scan_timestamp_lines


DEFAULT_DEEP_TIMEOUT = 120.0
DEFAULT_MAX_RECORDS = 1_000_000
MAX_ANALYZED_STREAMS = 32
VIDEO_WINDOW_SECONDS = 4.0
VIDEO_WINDOWS = 5
MIN_DRIFT_SPAN = 60.0
MIN_DRIFT_SECONDS = 0.05
MIN_FIT_R_SQUARED = 0.98
MAX_CHECKPOINTS = 128


class OnlineFit:
    """Centered Welford covariance, O(1) memory regardless of recording length."""
    def __init__(self):
        self.n = 0
        self.x = self.y = self.xx = self.xy = self.yy = 0.0

    def add(self, x, y):
        self.n += 1
        dx, dy = x - self.x, y - self.y
        self.x += dx / self.n
        self.y += dy / self.n
        self.xx += dx * (x - self.x)
        self.xy += dx * (y - self.y)
        self.yy += dy * (y - self.y)

    def result(self):
        if self.n < 3 or self.xx <= 0:
            return None, None, None
        slope = self.xy / self.xx
        error = max(0.0, self.yy - slope * self.xy)
        r2 = max(0.0, min(1.0, 1 - error / self.yy)) if self.yy > 1e-20 else 1.0
        return slope, r2, math.sqrt(error / self.n)


def parse_record(line: str) -> tuple[str, dict[str, str]]:
    parts = line.strip().split("|")
    return parts[0], dict(part.split("=", 1) for part in parts[1:] if "=" in part)


def timestamp(fields, name, stream):
    """Use integer ticks × time_base where available, avoiding absolute-time rounding."""
    raw = fields.get(name)
    if raw is not None and stream.time_base:
        try:
            value = finite_number(int(raw) * stream.time_base)
            if value is not None:
                return value
        except ValueError:
            pass
    return finite_number(fields.get(name + "_time"))


class TrackAccumulator:
    def __init__(self, stream: StreamInfo, kind: str):
        self.stream, self.kind = stream, kind
        self.packets = self.frames = self.missing = self.regressions = self.jumps = 0
        self.first = self.last = self.previous_dts = self.previous_pts = None
        self.samples = 0
        self.first_sample = None
        self.previous_sample_clock = None
        self.clock_valid = True
        self.fit = OnlineFit()
        self.points = []
        self.point_stride = 1
        self.last_point = None
        self.cadence_reference = None
        self.cadence_count = self.cadence_changes = 0
        self.quantum = float(stream.time_base or 0)

    def packet(self, fields):
        self.packets += 1
        pts = timestamp(fields, "pts", self.stream)
        dts = timestamp(fields, "dts", self.stream)
        if pts is None:
            self.missing += 1
        # Video PTS may legitimately move backwards in packet/decode order (B frames).
        if dts is not None and self.previous_dts is not None:
            step = dts - self.previous_dts
            if step < -max(self.quantum / 2, 1e-9):
                self.regressions += 1
            reference = fps_to_float(self.stream.r_frame_rate) if self.kind == "video" else None
            gap_limit = max(0.5, 10 / reference) if reference else 0.5
            if step > gap_limit:
                self.jumps += 1
        if dts is not None:
            self.previous_dts = dts

    def new_video_window(self):
        # A seek boundary is not a discontinuity or a dropped frame.
        self.previous_pts = None

    def frame(self, fields):
        self.frames += 1
        sample_before = self.samples
        if self.kind == "audio":
            count = integer(fields.get("nb_samples"), minimum=1)
            rate = self.stream.sample_rate
            reported_rate = integer(fields.get("sample_rate"), minimum=1)
            if count is None or not rate or (reported_rate and reported_rate != rate):
                self.missing += 1
                self.clock_valid = False
                return
            # Decoded samples exist even when their timestamp is missing. Omitting
            # these samples would manufacture a growing PTS/sample-clock gap.
            self.samples += count
        pts = timestamp(fields, "pts", self.stream)
        if pts is None:
            # Reconstructed best-effort timestamps can describe cadence, but cannot
            # prove that the original PTS was healthy or authorize compensation.
            self.missing += 1
            pts = timestamp(fields, "best_effort_timestamp", self.stream)
        if pts is None:
            return
        if self.first is None:
            self.first = pts
            self.first_sample = sample_before
        self.last = pts
        if self.kind == "audio":
            if not self.clock_valid:
                return
            x = (sample_before - self.first_sample) / rate
            y = (pts - self.first) - x
            self.fit.add(x, y)
            self.last_point = (x, y)
            if (self.fit.n - 1) % self.point_stride == 0:
                self.points.append((x, y))
                if len(self.points) > MAX_CHECKPOINTS:
                    self.points = self.points[::2]
                    self.point_stride *= 2
            if self.previous_pts is not None and self.previous_sample_clock is not None:
                gap = pts - self.previous_pts - (x - self.previous_sample_clock)
                if abs(gap) > max(0.02, 8 * self.quantum):
                    self.jumps += 1
            self.previous_sample_clock = x
        elif self.previous_pts is not None:
            step = pts - self.previous_pts
            tolerance = max(0.0002, 2 * self.quantum)
            if step <= 0:
                self.jumps += 1
            else:
                if self.cadence_reference is None:
                    self.cadence_reference = step
                self.cadence_count += 1
                if abs(step - self.cadence_reference) > max(tolerance, self.cadence_reference * 0.02):
                    self.cadence_changes += 1
        self.previous_pts = pts

    def finish(self, *, complete: bool, video_start=None) -> TimestampEvidence:
        slope, r2, residual = self.fit.result()
        if not self.clock_valid:
            slope = r2 = residual = None
        span = self.last_point[0] if self.last_point else 0.0
        drift = slope * span if slope is not None else None
        vfr = None
        if self.kind == "video" and self.cadence_count >= 8:
            vfr = self.cadence_changes >= max(3, self.cadence_count * 0.05)
        patterns = []
        if self.jumps or self.regressions or self.missing:
            patterns.append("TIMESTAMP_ANOMALY")
        if (self.kind == "audio" and not patterns and span >= MIN_DRIFT_SPAN and drift is not None
                and abs(drift) >= MIN_DRIFT_SECONDS and r2 >= MIN_FIT_R_SQUARED
                and residual <= max(0.003, 4 * self.quantum)):
            patterns.append("PROGRESSIVE_DRIFT")
        offset = None if self.first is None or video_start is None else self.first - video_start
        if (self.kind == "audio" and not patterns and complete and span >= MIN_DRIFT_SPAN
                and offset is not None and abs(offset) > 0.05 and drift is not None
                and abs(drift) < 0.02 and residual <= max(0.003, 4 * self.quantum)):
            patterns.append("STATIC_OFFSET")
        if vfr:
            patterns.append("VFR_SUSPECTED")
        points = self.points[:]
        if self.last_point and (not points or self.last_point != points[-1]):
            points.append(self.last_point)
        return TimestampEvidence(
            stream_index=self.stream.index, kind=self.kind, packets=self.packets, frames=self.frames,
            first_pts=self.first, last_pts=self.last, sample_duration=self.samples / self.stream.sample_rate
            if self.stream.sample_rate else 0.0, missing_timestamps=self.missing,
            dts_regressions=self.regressions, discontinuities=self.jumps, vfr_suspected=vfr,
            drift_seconds=drift, drift_ppm=slope * 1e6 if slope is not None else None,
            fit_r_squared=r2, fit_residual_seconds=residual, start_offset_seconds=offset,
            complete=complete, patterns=tuple(patterns or ["UNKNOWN"]), checkpoints=tuple(points),
        )


def soft_compensation_allowed(evidence: TimestampEvidence | None, video: TimestampEvidence | None) -> bool:
    return bool(evidence and video and evidence.complete and video.complete
                and "PROGRESSIVE_DRIFT" in evidence.patterns
                and "TIMESTAMP_ANOMALY" not in evidence.patterns
                and "TIMESTAMP_ANOMALY" not in video.patterns
                and video.frames >= 8 and evidence.drift_ppm is not None
                and 0 < abs(evidence.drift_ppm) <= MAX_SOFT_DRIFT_PPM)


def video_windows(source: AnalysisResult) -> tuple[float, ...]:
    duration = source.videos[0].duration if source.videos else None
    duration = duration or source.container_duration
    start = source.videos[0].start_time if source.videos else None
    start = max(0.0, start or 0.0)
    if duration is None or duration <= VIDEO_WINDOW_SECONDS:
        return (start,)
    end = max(0.0, duration - VIDEO_WINDOW_SECONDS)
    return tuple(start + end * i / (VIDEO_WINDOWS - 1) for i in range(VIDEO_WINDOWS))


def deep_analyze(source: AnalysisResult, ffprobe: str, *, timeout: float = DEFAULT_DEEP_TIMEOUT,
                 max_records: int = DEFAULT_MAX_RECORDS) -> DeepSyncAnalysis:
    if not math.isfinite(timeout) or timeout <= 0 or type(max_records) is not int or max_records <= 0:
        raise MediaError("深度分析时限和记录上限必须为正数。")
    selected = (*source.videos[:1], *source.audios)
    if len(selected) > MAX_ANALYZED_STREAMS:
        raise MediaError(f"深度分析最多支持 {MAX_ANALYZED_STREAMS} 条选中轨道。")
    valid = [s for s in selected if s.index is not None]
    accumulators = {s.index: TrackAccumulator(s, "video" if s in source.videos else "audio") for s in valid}
    if len(accumulators) != len(valid):
        raise MediaError("流编号重复，不能可靠关联深度时间戳。")
    scans = []
    started = monotonic()
    base = [ffprobe, "-v", "error", "-threads", "1"]

    def run(options, purpose, consumer, per_pass):
        remaining_time = min(per_pass, timeout - (monotonic() - started))
        remaining_records = max_records - sum(s.records for s in scans)
        command = [*base, *options, "-of", "compact=p=1:nk=0", str(source.path.resolve())]
        result = scan_timestamp_lines(command, consumer, purpose=purpose, timeout=remaining_time,
                                      max_records=remaining_records)
        scans.append(result)
        return result.complete

    def consume(line):
        kind, fields = parse_record(line)
        index = integer(fields.get("stream_index"))
        accumulator = accumulators.get(index)
        if accumulator and kind == "packet":
            accumulator.packet(fields)
        elif accumulator and kind == "frame":
            accumulator.frame(fields)

    packet_ok = run(["-show_packets", "-show_entries", "packet=stream_index,pts,pts_time,dts,dts_time,duration_time"],
                    "packets (全文件流式扫描)", consume, timeout * 0.35)
    audio_ok = True
    if source.audios:
        audio_ok = run(["-select_streams", "a", "-show_frames", "-show_entries",
                        "frame=stream_index,pts,pts_time,best_effort_timestamp,best_effort_timestamp_time,nb_samples,sample_rate"],
                       "audio frames (全文件流式扫描)", consume, timeout * 0.8 - (monotonic() - started))
    video_ok = True
    video_stream = source.videos[0] if source.videos else None
    video_acc = accumulators.get(video_stream.index) if video_stream else None
    if video_acc:
        windows = video_windows(source)
        for window_index, start in enumerate(windows):
            video_acc.new_video_window()
            def consume_window(line, lower=start):
                kind, fields = parse_record(line)
                pts = timestamp(fields, "pts", video_stream)
                if pts is None:
                    pts = timestamp(fields, "best_effort_timestamp", video_stream)
                # Seeking may land on an earlier keyframe; exclude preroll, never
                # compare it with a previous window or count it as a discontinuity.
                if kind == "frame" and (pts is None or lower <= pts < lower + VIDEO_WINDOW_SECONDS):
                    video_acc.frame(fields)
            before = video_acc.frames
            ok = run(["-select_streams", str(video_stream.index), "-read_intervals", f"{start:.6f}%{start + VIDEO_WINDOW_SECONDS:.6f}",
                      "-show_frames", "-show_entries", "frame=stream_index,pts,pts_time,best_effort_timestamp,best_effort_timestamp_time"],
                     f"video frames ({start:.1f}s 抽样)", consume_window,
                     (timeout - (monotonic() - started)) / (len(windows) - window_index))
            video_ok = video_ok and ok and video_acc.frames > before
    video = video_acc.finish(complete=packet_ok and video_ok and video_acc.frames > 0) if video_acc else None
    audios = tuple(accumulators[a.index].finish(complete=packet_ok and audio_ok,
                    video_start=video.first_pts if video else None)
                   for a in source.audios if a.index in accumulators)
    patterns = tuple(dict.fromkeys(p for evidence in ((video,) if video else ()) + audios
                                  for p in evidence.patterns if p != "UNKNOWN")) or ("UNKNOWN",)
    limitations = [
        "以上为时间戳模式候选，不是声音与画面内容对齐测量；无法确认物理音频时钟故障。",
        "视频帧仅抽查多个窗口；未抽查位置仍可能存在 VFR 或异常。packet PTS 重排不等于异常。",
        "PTS 与采样时钟斜率无法发现已被容器重新标记、但内容本身漂移的录屏。",
        "软补偿假设音频 PTS 是可信参考；它只改善 PTS 与采样数的一致性，不能证明实际口型或事件同步。",
    ]
    if len(valid) != len(selected):
        limitations.append("部分流编号缺失，未扫描对应轨道，不据此自动补偿。")
    if any(not s.complete for s in scans) or not video_ok:
        limitations.append("分析不完整或抽样窗口无帧；仅报告已观察部分，禁止自动软补偿。")
        # A failed unrelated pass also means incomplete evidence for auto repair.
        audios = tuple(replace(a, complete=False) for a in audios)
    return DeepSyncAnalysis(video, audios, tuple(scans), patterns, tuple(limitations))


def format_deep_report(result: DeepSyncAnalysis) -> str:
    lines = ["", "Deep sync analysis", "Sync pattern (候选): " + ", ".join(result.patterns)]
    for scan in result.scans:
        lines.append(f"扫描 {scan.purpose}: {scan.records} records / {scan.elapsed_seconds:.2f}s / "
                     + ("complete" if scan.complete else "partial: " + scan.reason))
    for evidence in ((result.video,) if result.video else ()) + result.audios:
        lines.append(f"轨道 #{evidence.stream_index} {evidence.kind}: {', '.join(evidence.patterns)}; "
                     f"packets={evidence.packets}, frames={evidence.frames}; PTS={evidence.first_pts} → {evidence.last_pts}; "
                     f"缺失={evidence.missing_timestamps}, DTS 回退={evidence.dts_regressions}, 突跳={evidence.discontinuities}")
        if evidence.kind == "audio":
            lines.append(f"  起始 offset（音频−视频）: {evidence.start_offset_seconds}; "
                         f"累计解码采样时长: {evidence.sample_duration:.3f}s")
        if evidence.drift_ppm is not None:
            lines.append(f"  PTS−采样时钟估算漂移: {evidence.drift_seconds * 1000:+.3f}ms / "
                         f"{evidence.drift_ppm:+.3f}ppm; R²={evidence.fit_r_squared:.5f}; "
                         f"拟合 RMS={evidence.fit_residual_seconds * 1000:.3f}ms")
            points = evidence.checkpoints
            indexes = sorted({round(i * (len(points) - 1) / 4) for i in range(5)}) if points else []
            lines.extend(f"    {points[i][0] / 60:.2f} min: {points[i][1] * 1000:+.3f}ms (相对采样时钟)" for i in indexes)
            if "PROGRESSIVE_DRIFT" in evidence.patterns:
                lines.append("  Audio clock drift: SUSPECTED（PTS 与累计采样时钟不一致，不能确证物理时钟漂移）")
    lines.extend("限制：" + message for message in result.limitations)
    return "\n".join(lines)
