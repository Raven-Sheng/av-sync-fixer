"""与命令行展示无关的媒体信息。"""

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


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


@dataclass(frozen=True)
class AnalysisResult:
    path: Path
    container: str | None
    container_duration: float | None
    videos: tuple[StreamInfo, ...]
    audios: tuple[StreamInfo, ...]
    file_size: int | None = None
    major_brand: str | None = None


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
class FixPlan:
    source: AnalysisResult
    output_path: Path
    strategy: RepairStrategy
    command: tuple[str, ...]
    preset: str = "general"


@dataclass(frozen=True)
class OutputValidation:
    preset: str
    media: AnalysisResult
    target_fps: int | None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class FixResult:
    before: AnalysisResult
    after: AnalysisResult
    target_fps: int | None
    strategy: RepairStrategy | None = None
    validation: OutputValidation | None = None
