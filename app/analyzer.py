"""读取媒体元数据并分析同步风险，不修改媒体文件。"""

from decimal import Decimal
from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
import math
from pathlib import Path
from typing import Any

from app.ffmpeg_utils import MediaError, check_tools, probe_media
from app.models import AnalysisResult, FieldState, ProbeField, StreamInfo, SyncDiagnosis
from app.color import hdr_side_data_types


FPS_TOLERANCE = 0.1
DURATION_LOW_THRESHOLD = 0.05
DURATION_MODERATE_THRESHOLD = 0.2
DURATION_HIGH_THRESHOLD = 0.5
START_TIME_TOLERANCE_SECONDS = 0.05
# 仅用于消除阈值附近的浮点舍入误差，不是业务风险阈值。
THRESHOLD_ROUNDING_TOLERANCE = 1e-9


def parse_rational(value: Any, *, allow_colon: bool = False) -> Fraction | None:
    """解析正有理数；FPS/time_base 用斜杠，宽高比还允许冒号。不以字符串比较。"""
    if not isinstance(value, (str, int, float, Fraction)) or isinstance(value, bool):
        return None
    try:
        text = str(value).strip()
        if allow_colon:
            text = text.replace(":", "/")
        number = Fraction(text)
        return number if number > 0 else None
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def positive_fraction(value: Any) -> Fraction | None:
    # 保留 V1 调用入口；修复逻辑仍使用原来的正数/无效值语义。
    return parse_rational(value)


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


def read_probe_field(data: dict, key: str, parser, fields: dict[str, ProbeField], *, name: str | None = None,
                     unknown_values: tuple = ()):
    """规范化值与原始证据分开保存，空字典/列表与缺失也不混为一谈。"""
    target = name or key
    if key not in data:
        fields[target] = ProbeField()
        return None
    raw = data[key]
    empty = raw is None or raw == "" or raw == [] or raw == {} or (isinstance(raw, str) and not raw.strip())
    unknown = (isinstance(raw, str) and raw.strip().lower() in {"n/a", "unknown"}) or raw in unknown_values
    value = None if empty or unknown or isinstance(raw, bool) else parser(raw)
    # 空集合是有效的“已明确提供空集合”，而不是未知集合。
    if empty and isinstance(raw, (dict, list)):
        value = parser(raw)
    state = (FieldState.EMPTY if empty else FieldState.UNKNOWN if unknown else
             FieldState.VALUE if value is not None else FieldState.INVALID)
    fields[target] = ProbeField(state, deepcopy(raw))
    return value


def mapping_field(value: Any) -> dict | None:
    return deepcopy(value) if isinstance(value, dict) else None


def side_data_field(value: Any) -> tuple[dict, ...] | None:
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return tuple(deepcopy(value))
    return None


def signed_integer(value: Any) -> int | None:
    try:
        return int(str(value))
    except (ValueError, TypeError):
        return None


def default_flag(value: Any) -> bool | None:
    number = integer(value)
    return bool(number) if number in (0, 1) else None


def normalize_color_range(value: str | None) -> str | None:
    return {"pc": "Full", "jpeg": "Full", "tv": "Limited", "mpeg": "Limited"}.get(
        value.strip().lower() if value else "")


def stream_rotation(side_data: tuple[dict, ...] | None, metadata: dict | None) -> tuple[float | None, str | None]:
    # 显示矩阵优先；不改角度符号、不旋转像素，保留原始矩阵和旧 rotate 标签。
    for item in side_data or ():
        if item.get("side_data_type") == "Display Matrix" and not isinstance(item.get("rotation"), bool):
            rotation = finite_number(item.get("rotation"))
            if rotation is not None:
                return rotation, "side_data_list.Display Matrix.rotation"
    raw = (metadata or {}).get("rotate")
    rotation = None if isinstance(raw, bool) else finite_number(raw)
    return (rotation, "tags.rotate") if rotation is not None else (None, None)


def parse_stream(stream: dict[str, Any]) -> StreamInfo:
    fields: dict[str, ProbeField] = {}
    def read(key, parser=text_field, **kwargs):
        return read_probe_field(stream, key, parser, fields, **kwargs)

    time_base = read("time_base", positive_fraction)
    duration = read("duration", nonnegative_number)
    source = "duration" if duration is not None else None
    ticks = read("duration_ts", integer)
    if duration is None and ticks is not None and time_base is not None:
        duration = nonnegative_number(ticks * time_base)
        if duration is not None:
            source = "duration_ts × time_base"
    metadata = read("tags", mapping_field)
    disposition = read("disposition", mapping_field)
    is_default = read_probe_field(disposition or {}, "default", default_flag, fields, name="disposition.default")
    side_data = read("side_data_list", side_data_field)
    rotation, rotation_source = stream_rotation(side_data, metadata)
    color_range = read("color_range")
    return StreamInfo(
        index=read("index", integer),
        codec=read("codec_name"),
        duration=duration,
        duration_source=source,
        time_base=time_base,
        width=read("width", lambda value: integer(value, minimum=1)),
        height=read("height", lambda value: integer(value, minimum=1)),
        avg_frame_rate=read("avg_frame_rate", positive_fraction),
        r_frame_rate=read("r_frame_rate", positive_fraction),
        sample_rate=read("sample_rate", lambda value: integer(value, minimum=1)),
        # 负起始时间在媒体中是有效值，不能按非负时长的规则过滤。
        start_time=read("start_time", finite_number),
        pixel_format=read("pix_fmt"),
        codec_type=read("codec_type"), codec_long_name=read("codec_long_name"),
        profile=read("profile"), level=read("level", integer),
        sample_aspect_ratio=read("sample_aspect_ratio", lambda value: parse_rational(value, allow_colon=True)),
        display_aspect_ratio=read("display_aspect_ratio", lambda value: parse_rational(value, allow_colon=True)),
        start_pts=read("start_pts", signed_integer), duration_ts=ticks,
        nb_frames=read("nb_frames", integer),
        color_range=color_range, color_range_name=normalize_color_range(color_range),
        color_space=read("color_space"), color_transfer=read("color_transfer"),
        color_primaries=read("color_primaries"), chroma_location=read("chroma_location"),
        bits_per_raw_sample=read("bits_per_raw_sample", lambda value: integer(value, minimum=1), unknown_values=(0, "0")),
        field_order=read("field_order"), side_data_list=side_data,
        sample_fmt=read("sample_fmt"), channels=read("channels", lambda value: integer(value, minimum=1)),
        channel_layout=read("channel_layout"), bit_rate=read("bit_rate", integer),
        metadata=metadata, disposition=disposition, is_default=is_default,
        rotation=rotation, rotation_source=rotation_source, probe_fields=fields,
    )


def parse_analysis(
    path: Path, data: dict[str, Any], *, file_size: int | None = None,
) -> AnalysisResult:
    fields: dict[str, ProbeField] = {}
    container = read_probe_field(data, "format", mapping_field, fields) or {}
    def read(key, parser=text_field):
        return read_probe_field(container, key, parser, fields)
    tags = read("tags", mapping_field)
    videos, audios, subtitles, covers, others = [], [], [], [], []
    streams = read_probe_field(data, "streams", lambda value: deepcopy(value) if isinstance(value, list) else None, fields)
    for stream in streams or []:
        if not isinstance(stream, dict):
            continue
        kind = stream.get("codec_type")
        disposition = stream.get("disposition") or {}
        if kind == "video":
            # 音乐文件里的封面不视作视频轨道。
            if isinstance(disposition, dict) and disposition.get("attached_pic") == 1:
                covers.append(parse_stream(stream))
                continue
            videos.append(parse_stream(stream))
        elif kind == "audio":
            audios.append(parse_stream(stream))
        elif kind == "subtitle":
            subtitles.append(parse_stream(stream))
        else:
            others.append(parse_stream(stream))
    probe_size = read("size", integer)
    # ffprobe samples a fixed opening packet budget. Preserve this evidence
    # separately: it must not replace or fabricate stream-level color tags.
    frames_by_index = {}
    for frame in data.get("frames", ()) if isinstance(data.get("frames", ()), (list, tuple)) else ():
        if isinstance(frame, dict) and (index := integer(frame.get("stream_index"))) is not None:
            frames_by_index.setdefault(index, []).append(frame)
    for i, video in enumerate(videos):
        sampled = frames_by_index.get(video.index, ())
        transfers = tuple(dict.fromkeys(t for frame in sampled
            if (t := text_field(frame.get("color_transfer"))) is not None))
        side_types = tuple(dict.fromkeys(name for frame in sampled
            for name in hdr_side_data_types(side_data_field(frame.get("side_data_list")) or ())))
        videos[i] = replace(video, sampled_color_transfers=transfers, sampled_hdr_side_data_types=side_types)
    return AnalysisResult(
        path=path,
        container=read("format_name"),
        container_duration=read("duration", nonnegative_number),
        videos=tuple(videos),
        audios=tuple(audios),
        file_size=file_size if file_size is not None else probe_size,
        major_brand=text_field(tags.get("major_brand")) if isinstance(tags, dict) else None,
        container_start_time=read("start_time", finite_number),
        container_bit_rate=read("bit_rate", integer), metadata=tags,
        subtitles=tuple(subtitles), cover_art=tuple(covers), other_streams=tuple(others),
        probe_fields=fields,
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
    patterns = []
    if result.deep_sync:
        patterns.extend(result.deep_sync.patterns)
    else:
        if start_mismatch:
            patterns.append("STATIC_OFFSET")
        if suspected_vfr:
            patterns.append("VFR_SUSPECTED")
    return SyncDiagnosis(
        video=video, audio=audio,
        average_fps=average_fps, nominal_fps=nominal_fps,
        fps_diff=fps_diff, suspected_vfr=suspected_vfr,
        duration_diff=duration_diff, duration_risk=duration_risk,
        start_time_diff=start_diff, start_time_mismatch=start_mismatch,
        potential_causes=tuple(causes), limitations=tuple(limitations),
        patterns=tuple(patterns or ["UNKNOWN"]),
    )


def analyze(path: str | Path, *, deep_analysis: bool = False, deep_timeout: float | None = None,
            deep_max_records: int | None = None) -> AnalysisResult:
    try:
        media_path = Path(path).expanduser().resolve()
        if not media_path.is_file():
            raise MediaError(f"文件不存在或不是普通文件：{media_path}")
        file_size = media_path.stat().st_size
    except (OSError, ValueError, RuntimeError) as exc:
        raise MediaError(f"无法访问输入文件：{exc}") from exc
    tools = check_tools()
    result = parse_analysis(media_path, probe_media(media_path, tools["ffprobe"]), file_size=file_size)
    if deep_analysis:
        from app.deep_sync import deep_analyze
        options = {}
        if deep_timeout is not None:
            options["timeout"] = deep_timeout
        if deep_max_records is not None:
            options["max_records"] = deep_max_records
        result = replace(result, deep_sync=deep_analyze(result, tools["ffprobe"], **options))
    return result
