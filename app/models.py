"""与命令行展示无关的媒体信息。"""

from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal


MAX_SOFT_DRIFT_PPM = 500.0  # RepairPlan safety limit: 0.05%, approximately 0.87 cents.


class FieldState(str, Enum):
    MISSING = "missing"
    EMPTY = "empty"
    UNKNOWN = "unknown"
    INVALID = "invalid"
    VALUE = "value"


@dataclass(frozen=True)
class ProbeField:
    """保留 ffprobe 原值及状态；规范化值为 None 时仍可区分原因。"""

    state: FieldState = FieldState.MISSING
    raw: Any = None

    @property
    def present(self) -> bool:
        return self.state is not FieldState.MISSING


@dataclass(frozen=True)
class StreamInfo:
    index: int | None
    codec: str | None
    duration: float | None
    duration_source: str | None
    time_base: Fraction | None
    width: int | None = None
    height: int | None = None
    avg_frame_rate: Fraction | None = None
    r_frame_rate: Fraction | None = None
    sample_rate: int | None = None
    start_time: float | None = None
    pixel_format: str | None = None
    codec_type: str | None = None
    codec_long_name: str | None = None
    profile: str | None = None
    level: int | None = None
    sample_aspect_ratio: Fraction | None = None
    display_aspect_ratio: Fraction | None = None
    start_pts: int | None = None
    duration_ts: int | None = None
    nb_frames: int | None = None
    color_range: str | None = None
    color_range_name: str | None = None
    color_space: str | None = None
    color_transfer: str | None = None
    color_primaries: str | None = None
    chroma_location: str | None = None
    bits_per_raw_sample: int | None = None
    field_order: str | None = None
    side_data_list: tuple[dict[str, Any], ...] | None = None
    sample_fmt: str | None = None
    channels: int | None = None
    channel_layout: str | None = None
    bit_rate: int | None = None
    metadata: dict[str, Any] | None = None
    disposition: dict[str, Any] | None = None
    is_default: bool | None = None
    rotation: float | None = None
    rotation_source: str | None = None
    probe_fields: dict[str, ProbeField] = field(default_factory=dict)

    # Bounded opening-frame evidence, kept separate from raw stream metadata.
    # An empty tuple does not prove that the rest of the file is SDR.
    sampled_color_transfers: tuple[str, ...] = ()
    sampled_hdr_side_data_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnalysisResult:
    path: Path
    container: str | None
    container_duration: float | None
    videos: tuple[StreamInfo, ...]
    audios: tuple[StreamInfo, ...]
    file_size: int | None = None
    major_brand: str | None = None
    container_start_time: float | None = None
    container_bit_rate: int | None = None
    metadata: dict[str, Any] | None = None
    subtitles: tuple[StreamInfo, ...] = ()
    cover_art: tuple[StreamInfo, ...] = ()
    other_streams: tuple[StreamInfo, ...] = ()
    probe_fields: dict[str, ProbeField] = field(default_factory=dict)
    deep_sync: "DeepSyncAnalysis | None" = None

    @property
    def video_stream_count(self) -> int:
        """包括封面；videos 继续只包含可修复视频，保持 V1 选轨规则。"""
        return len(self.videos) + len(self.cover_art)

    @property
    def audio_stream_count(self) -> int:
        return len(self.audios)

    @property
    def stream_list_complete(self) -> bool:
        """只有列表及每条流的类型都已知，才能据此确认某类轨道不存在。"""
        stream_field = self.probe_fields.get("streams", ProbeField())
        if stream_field.state not in (FieldState.VALUE, FieldState.EMPTY) or not isinstance(stream_field.raw, list):
            return False
        return all(isinstance(item, dict) and item.get("codec_type") in
                   ("video", "audio", "subtitle", "data", "attachment") for item in stream_field.raw)

    @property
    def has_subtitles(self) -> bool | None:
        if self.subtitles:
            return True
        return False if self.stream_list_complete else None


@dataclass(frozen=True)
class SyncDiagnosis:
    video: StreamInfo | None
    audio: StreamInfo | None
    average_fps: float | None
    nominal_fps: float | None
    fps_diff: float | None
    suspected_vfr: bool | None
    duration_diff: float | None
    duration_risk: str | None
    start_time_diff: float | None
    start_time_mismatch: bool | None
    potential_causes: tuple[str, ...]
    limitations: tuple[str, ...]
    patterns: tuple[str, ...] = ("UNKNOWN",)


class SyncPattern(str, Enum):
    STATIC_OFFSET = "STATIC_OFFSET"
    PROGRESSIVE_DRIFT = "PROGRESSIVE_DRIFT"
    VFR_SUSPECTED = "VFR_SUSPECTED"
    TIMESTAMP_ANOMALY = "TIMESTAMP_ANOMALY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class TimestampScan:
    purpose: str
    records: int
    complete: bool
    elapsed_seconds: float
    reason: str = ""


@dataclass(frozen=True)
class TimestampEvidence:
    stream_index: int
    kind: str
    packets: int = 0
    frames: int = 0
    first_pts: float | None = None
    last_pts: float | None = None
    sample_duration: float = 0.0
    missing_timestamps: int = 0
    dts_regressions: int = 0
    discontinuities: int = 0
    vfr_suspected: bool | None = None
    drift_seconds: float | None = None
    drift_ppm: float | None = None
    fit_r_squared: float | None = None
    fit_residual_seconds: float | None = None
    start_offset_seconds: float | None = None
    complete: bool = False
    patterns: tuple[str, ...] = ("UNKNOWN",)
    # (elapsed sample-clock seconds, PTS minus sample-clock seconds), bounded checkpoints.
    checkpoints: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class DeepSyncAnalysis:
    video: TimestampEvidence | None
    audios: tuple[TimestampEvidence, ...]
    scans: tuple[TimestampScan, ...]
    patterns: tuple[str, ...]
    limitations: tuple[str, ...]


class CompatibilitySeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    REPAIR_RECOMMENDED = "REPAIR_RECOMMENDED"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class CompatibilityFinding:
    code: str
    severity: CompatibilitySeverity
    summary: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    scope: str = "input"
    stream_index: int | None = None


@dataclass(frozen=True)
class CompatibilityDiagnosis:
    preset: str
    findings: tuple[CompatibilityFinding, ...]


@dataclass(frozen=True)
class RepairStrategy:
    mode: str
    cfr: bool
    timestamp: bool
    audio_sync_tracks: tuple[int, ...]
    target_fps: int | None
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ColorConversionPlan:
    action: str
    input_range: str | None
    range_source: str
    reason: str
    pixel_format: str = "yuv420p"
    output_range: str = "tv"
    color_space: str | None = None
    color_transfer: str | None = None
    color_primaries: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepairDecision:
    item: str
    value: str
    reason: str
    basis: str = "analysis"


@dataclass(frozen=True)
class VideoRepairPlan:
    stream_index: int | None
    codec: str
    target_fps: int | None
    fps_mode: Literal["cfr", "preserve"]  # builder uses passthrough after fps filter
    pixel_format: str
    color_conversion: ColorConversionPlan
    timestamp_strategy: Literal["preserve", "normalize", "regenerate_missing_pts"]
    start_offset: float
    pad_to_even: bool
    autorotate: bool
    encoder_preset: str = "medium"
    crf: int = 18


@dataclass(frozen=True)
class AudioRepairPlan:
    stream_index: int | None
    codec: str
    sample_rate: int | None  # None preserves encoder's input-rate policy
    sync_strategy: Literal["preserve", "async_gaps", "async_soft"]
    start_offset: float
    bitrate: str = "192k"
    max_soft_compensation: float = 0.0
    estimated_drift_seconds: float | None = None
    estimated_drift_ppm: float | None = None


@dataclass(frozen=True)
class ContainerRepairPlan:
    format: str = "mp4"
    faststart: bool = True


@dataclass(frozen=True)
class RepairPlan:
    """Resolved business decisions; contains no FFmpeg arguments or raw probe data."""

    input_path: Path
    mode: str
    preset: str
    video: VideoRepairPlan
    audios: tuple[AudioRepairPlan, ...]
    container: ContainerRepairPlan
    decisions: tuple[RepairDecision, ...]
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def strategy(self) -> RepairStrategy:
        """Compatibility view for existing CLI, GUI and output validation."""
        return RepairStrategy(
            self.mode, self.video.fps_mode == "cfr",
            self.video.timestamp_strategy == "regenerate_missing_pts",
            tuple(i for i, audio in enumerate(self.audios) if audio.sync_strategy != "preserve"),
            self.video.target_fps, self.reasons, self.warnings,
        )


@dataclass(frozen=True)
class FixPlan:
    source: AnalysisResult
    output_path: Path
    strategy: RepairStrategy
    command: tuple[str, ...]
    preset: str = "general"
    color: ColorConversionPlan | None = None
    repair: RepairPlan | None = None
    expected: "ExpectedOutputSpec | None" = None


class ValidationLevel(str, Enum):
    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"


@dataclass(frozen=True)
class ValidationCheck:
    section: str
    code: str
    level: ValidationLevel
    message: str
    expected: Any = None
    actual: Any = None


@dataclass(frozen=True)
class ExpectedVideoSpec:
    codec: str
    pixel_format: str
    color: ColorConversionPlan
    fps_mode: str
    target_fps: int | None
    width: int | None
    height: int | None
    duration: float | None
    start_offset: float | None
    frame_seconds: float
    average_fps: float | None = None
    fps_tolerance: float = 0.1


@dataclass(frozen=True)
class ExpectedAudioSpec:
    codec: str
    sample_rate: int | None
    duration: float | None
    duration_diff: float | None
    start_relative_to_video: float | None
    soft_duration_allowance: float = 0.0


@dataclass(frozen=True)
class ExpectedOutputSpec:
    preset: str
    video: ExpectedVideoSpec
    audios: tuple[ExpectedAudioSpec, ...]
    container: ContainerRepairPlan
    container_duration: float | None
    duration_tolerance: float
    sync_regression_tolerance: float
    start_tolerance: float
    mux_start_allowance: float
    duration_risk_threshold: float


def validation_level(checks) -> ValidationLevel:
    levels = {check.level for check in checks}
    return (ValidationLevel.FAIL if ValidationLevel.FAIL in levels else
            ValidationLevel.WARNING if ValidationLevel.WARNING in levels else ValidationLevel.PASS)


@dataclass(frozen=True)
class OutputValidation:
    preset: str
    media: AnalysisResult | None
    target_fps: int | None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    checks: tuple[ValidationCheck, ...] = ()
    expected: ExpectedOutputSpec | None = None

    @property
    def overall(self) -> ValidationLevel:
        return ValidationLevel.FAIL if self.errors else ValidationLevel.WARNING if self.warnings else ValidationLevel.PASS

    @property
    def sections(self) -> dict[str, ValidationLevel]:
        return {name: validation_level(c for c in self.checks if c.section == name)
                for name in ("Video", "Audio", "Color", "Timing", "Compatibility")}


@dataclass(frozen=True)
class FixResult:
    before: AnalysisResult
    after: AnalysisResult
    target_fps: int | None
    strategy: RepairStrategy | None = None
    validation: OutputValidation | None = None
