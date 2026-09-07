"""读取媒体元数据并分析同步风险，不修改媒体文件。"""

from decimal import Decimal
from fractions import Fraction
import math
from pathlib import Path
from typing import Any

from app.ffmpeg_utils import MediaError, check_tools, probe_media
from app.models import AnalysisResult, StreamInfo, SyncDiagnosis


FPS_TOLERANCE = 0.1
DURATION_LOW_THRESHOLD = 0.05
DURATION_MODERATE_THRESHOLD = 0.2
DURATION_HIGH_THRESHOLD = 0.5
START_TIME_TOLERANCE_SECONDS = 0.05
# 仅用于消除阈值附近的浮点舍入误差，不是业务风险阈值。
THRESHOLD_ROUNDING_TOLERANCE = 1e-9


def positive_fraction(value: Any) -> Fraction | None:
    try:
        number = Fraction(str(value))
        return number if number > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def nonnegative_number(value: Any) -> float | None:
    number = finite_number(value)
    return number if number is not None and number >= 0 else None


def fps_to_float(value: Any) -> float | None:
    """将分数字符串或 Fraction 转换为有效 FPS；无效值不当作零。"""
    rate = positive_fraction(value)
    fps = finite_number(rate) if rate is not None else None
    return fps if fps is not None and fps > 0 else None


def matches_target_fps(video: StreamInfo | None, target_fps: int | None) -> bool:
    """输出复查需同时匹配目标 FPS，不使用输入 VFR 诊断的容差。"""
    if video is None or target_fps is None:
        return False
    return (fps_to_float(video.avg_frame_rate) == target_fps
            and fps_to_float(video.r_frame_rate) == target_fps)


def absolute_difference(first: float | None, second: float | None) -> float | None:
    if first is None or second is None:
        return None
    # 按十进制值相减，避免 10.2 - 10 的二进制舍入影响 0.2 秒边界。
    return nonnegative_number(abs(Decimal(str(first)) - Decimal(str(second))))


def compare_threshold(value: float, threshold: float) -> int:
    """返回 -1/0/1；将距离阈值不超过 1e-9 的值视为相等。"""
    if math.isclose(value, threshold, rel_tol=0.0, abs_tol=THRESHOLD_ROUNDING_TOLERANCE):
        return 0
    return -1 if value < threshold else 1


def duration_risk_level(difference: float | None) -> str | None:
    difference = nonnegative_number(difference)
    if difference is None:
        return None
    if compare_threshold(difference, DURATION_LOW_THRESHOLD) < 0:
        return "低"
    if compare_threshold(difference, DURATION_MODERATE_THRESHOLD) < 0:
        return "轻微"
    if compare_threshold(difference, DURATION_HIGH_THRESHOLD) <= 0:
        return "中等"
    return "较高"


def integer(value: Any, minimum: int = 0) -> int | None:
    try:
        number = int(str(value))
        return number if number >= minimum else None
    except (ValueError, TypeError):
        return None


def text_field(value: Any) -> str | None:
    return value if isinstance(value, str) and value and value != "N/A" else None


def parse_stream(stream: dict[str, Any]) -> StreamInfo:
    time_base = positive_fraction(stream.get("time_base"))
    duration = nonnegative_number(stream.get("duration"))
    source = "duration" if duration is not None else None
    ticks = integer(stream.get("duration_ts"))
    if duration is None and ticks is not None and time_base is not None:
        duration = nonnegative_number(ticks * time_base)
        if duration is not None:
            source = "duration_ts × time_base"
    return StreamInfo(
        index=integer(stream.get("index")),
        codec=text_field(stream.get("codec_name")),
        duration=duration,
        duration_source=source,
        time_base=time_base,
        width=integer(stream.get("width"), minimum=1),
        height=integer(stream.get("height"), minimum=1),
        avg_frame_rate=positive_fraction(stream.get("avg_frame_rate")),
        r_frame_rate=positive_fraction(stream.get("r_frame_rate")),
        sample_rate=integer(stream.get("sample_rate"), minimum=1),
        # 负起始时间在媒体中是有效值，不能按非负时长的规则过滤。
        start_time=finite_number(stream.get("start_time")),
        pixel_format=text_field(stream.get("pix_fmt")),
    )


def parse_analysis(
    path: Path, data: dict[str, Any], *, file_size: int | None = None,
) -> AnalysisResult:
    container = data.get("format", {})
    tags = container.get("tags") or {}
    videos, audios = [], []
    for stream in data.get("streams", []):
        if not isinstance(stream, dict):
            continue
        kind = stream.get("codec_type")
        disposition = stream.get("disposition") or {}
        if kind == "video":
            # 音乐文件里的封面不视作视频轨道。
            if isinstance(disposition, dict) and disposition.get("attached_pic") == 1:
                continue
            videos.append(parse_stream(stream))
        elif kind == "audio":
            audios.append(parse_stream(stream))
    return AnalysisResult(
        path=path,
        container=text_field(container.get("format_name")),
        container_duration=nonnegative_number(container.get("duration")),
        videos=tuple(videos),
        audios=tuple(audios),
        file_size=file_size if file_size is not None else integer(container.get("size")),
        major_brand=text_field(tags.get("major_brand")) if isinstance(tags, dict) else None,
    )


def diagnose(result: AnalysisResult) -> SyncDiagnosis:
    """比较第一条非封面视频轨与第一条音频轨，并明确报告缺失证据。"""
    video = result.videos[0] if result.videos else None
    audio = result.audios[0] if result.audios else None
    average_fps = fps_to_float(video.avg_frame_rate) if video else None
    nominal_fps = fps_to_float(video.r_frame_rate) if video else None
    fps_diff = absolute_difference(average_fps, nominal_fps)
    suspected_vfr = compare_threshold(fps_diff, FPS_TOLERANCE) > 0 if fps_diff is not None else None
    duration_diff = absolute_difference(
        video.duration if video else None, audio.duration if audio else None,
    )
    start_diff = absolute_difference(
        video.start_time if video else None, audio.start_time if audio else None,
    )
    start_mismatch = compare_threshold(start_diff, START_TIME_TOLERANCE_SECONDS) > 0 if start_diff is not None else None
    duration_risk = duration_risk_level(duration_diff)
    causes, limitations = [], []
    if suspected_vfr:
        causes.append("视频可能采用可变帧率；仅凭元数据不能确认 VFR。")
    if duration_risk is not None and duration_risk != "低":
        causes.append("音频与视频轨长度不一致，可能存在累计漂移，也可能只是尾部长度不同。")
    if start_mismatch:
        causes.append("音视频起始时间不一致，可能存在固定偏移；这不等同于累计漂移。")
    if video is None:
        limitations.append("缺少视频轨，无法评估音画同步风险。")
    if audio is None:
        limitations.append("缺少音频轨，无法评估音画同步风险。")
    if video and fps_diff is None:
        limitations.append("帧率字段缺失或无效，无法比较平均帧率与标称帧率。")
    if video and audio:
        if duration_diff is None:
            limitations.append("轨道时长缺失或无效，无法评估长度差风险；不使用容器时长替代。")
        if start_diff is None:
            limitations.append("起始时间缺失或无效，无法判断是否存在起始偏移。")
    if len(result.videos) > 1 or len(result.audios) > 1:
        limitations.append("多轨文件仅比较第一条视频轨与第一条音频轨，其他轨道信息保留在上方。")
    return SyncDiagnosis(
        video=video, audio=audio,
        average_fps=average_fps, nominal_fps=nominal_fps,
        fps_diff=fps_diff, suspected_vfr=suspected_vfr,
        duration_diff=duration_diff, duration_risk=duration_risk,
        start_time_diff=start_diff, start_time_mismatch=start_mismatch,
        potential_causes=tuple(causes), limitations=tuple(limitations),
    )


def analyze(path: str | Path) -> AnalysisResult:
    try:
        media_path = Path(path).expanduser().resolve()
        if not media_path.is_file():
            raise MediaError(f"文件不存在或不是普通文件：{media_path}")
        file_size = media_path.stat().st_size
    except (OSError, ValueError, RuntimeError) as exc:
        raise MediaError(f"无法访问输入文件：{exc}") from exc
    tools = check_tools()
    return parse_analysis(media_path, probe_media(media_path, tools["ffprobe"]), file_size=file_size)
