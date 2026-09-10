"""共享的只读媒体画像展示；不选择修复策略，也不构建转码命令。"""

from fractions import Fraction
import json

from app.analyzer import absolute_difference, compare_threshold, fps_to_float, FPS_TOLERANCE
from app.models import AnalysisResult, FieldState, ProbeField, StreamInfo


STATE_LABELS = {
    FieldState.MISSING: "未知（字段缺失）",
    FieldState.EMPTY: "空（字段存在）",
    FieldState.UNKNOWN: "未知（ffprobe 未指定）",
    FieldState.INVALID: "未知（字段无效）",
    FieldState.VALUE: "未知",
}


def duration_text(duration: float | None) -> str:
    return "未知" if duration is None else f"{duration:.6f} 秒"


def rate_text(rate: Fraction | None) -> str:
    fps = fps_to_float(rate)
    return "未知" if fps is None else f"{fps:.6f} fps（{rate}）"


def field_text(owner, key: str, value, formatter=str) -> str:
    """规范化的有效值优先（含 duration_ts 推导值），其余按原字段状态显示。"""
    field = owner.probe_fields.get(key, ProbeField())
    if value is not None:
        if isinstance(value, (dict, tuple, list)) and not value:
            return STATE_LABELS[FieldState.EMPTY]
        return formatter(value)
    label = STATE_LABELS[field.state]
    if field.state in (FieldState.UNKNOWN, FieldState.INVALID):
        label += f"，原始值 {json.dumps(field.raw, ensure_ascii=False)}"
    return label


def frame_rate_confidence(stream: StreamInfo) -> str:
    average, nominal = fps_to_float(stream.avg_frame_rate), fps_to_float(stream.r_frame_rate)
    if average is None and nominal is None:
        return "未知：没有有效 FPS；未检查逐帧时间戳"
    if average is None or nominal is None:
        return "低：只有一个有效 FPS 字段；不能确认 CFR/VFR"
    difference = absolute_difference(average, nominal)
    if compare_threshold(difference, FPS_TOLERANCE) > 0:
        return "有限：平均/标称 FPS 不一致，疑似 VFR；未检查逐帧时间戳"
    return "有限：平均/标称 FPS 接近；不能据此确认 CFR，未检查逐帧时间戳"


def stream_lines(stream: StreamInfo, kind: str) -> list[str]:
    def value(key, attribute=None, formatter=str):
        return field_text(stream, key, getattr(stream, attribute or key), formatter)

    index = stream.index if stream.index is not None else "未知"
    lines = [f"{kind}轨道 #{index}", f"  {kind}编码器（codec_name）：{value('codec_name', 'codec')}",
             f"  Profile：{value('profile')}",
             f"  默认轨道：{value('disposition.default', 'is_default', lambda flag: '是' if flag else '否')}"]
    if kind in ("视频", "封面"):
        color_range = (f"{stream.color_range_name}（原始值 {stream.color_range}）" if stream.color_range_name
                       else value("color_range"))
        if stream.color_range and stream.color_range_name is None:
            color_range = f"未知（未识别的范围标记 {stream.color_range}）"
        lines.extend([
            f"  编码全称：{value('codec_long_name')}", f"  Level（原始等级编号）：{value('level')}",
            f"  分辨率：{value('width')} × {value('height')}",
            f"  像素格式（pix_fmt）：{value('pix_fmt', 'pixel_format')}",
            f"  Color range：{color_range}",
            f"  Color space：{value('color_space', formatter=lambda item: 'BT.709（bt709）' if item == 'bt709' else item)}",
            f"  Color primaries：{value('color_primaries')}",
            f"  Color transfer：{value('color_transfer')}",
            f"  Chroma location：{value('chroma_location')}",
            f"  位深字段（bits_per_raw_sample）：{value('bits_per_raw_sample')}",
            f"  场序（field_order）：{value('field_order')}",
            f"  sample_aspect_ratio：{value('sample_aspect_ratio')}",
            f"  display_aspect_ratio：{value('display_aspect_ratio')}",
            f"  视频帧率（avg_frame_rate）：{value('avg_frame_rate', formatter=rate_text)}",
            f"  视频帧率（r_frame_rate）：{value('r_frame_rate', formatter=rate_text)}",
            f"  帧率置信度：{frame_rate_confidence(stream)}",
            f"  start_pts：{value('start_pts')}", f"  duration_ts：{value('duration_ts')}",
            f"  帧数（nb_frames）：{value('nb_frames')}",
            f"  Rotation：{stream.rotation:g}°（{stream.rotation_source}，保留原始符号）" if stream.rotation is not None
            else "  Rotation：未知（未获得有效角度，不假定为 0°）",
            f"  Side data：{value('side_data_list', formatter=lambda item: json.dumps(item, ensure_ascii=False))}",
            f"  开头有限帧抽样 transfer：{', '.join(stream.sampled_color_transfers) or '未获得'}；"
            f"HDR 附加证据：{', '.join(stream.sampled_hdr_side_data_types) or '未观察到（不能排除未抽样位置）'}",
        ])
    if kind == "音频":
        lines.extend([
            f"  音频采样格式：{value('sample_fmt')}",
            f"  音频采样率：{value('sample_rate', formatter=lambda rate: f'{rate} Hz')}",
            f"  声道数：{value('channels')}",
            f"  声道布局：{value('channel_layout', formatter=lambda layout: 'Stereo（stereo）' if layout == 'stereo' else layout)}",
            f"  音频码率：{value('bit_rate', formatter=lambda rate: f'{rate} bit/s')}",
        ])
    source = f"（{stream.duration_source}）" if stream.duration_source else ""
    lines.extend([
        f"  {kind}轨时长：{value('duration', formatter=duration_text)}{source}",
        f"  {kind} time_base：{value('time_base')}",
        f"  {kind} start_time：{value('start_time', formatter=duration_text)}",
        f"  Metadata：{value('tags', 'metadata', lambda item: json.dumps(item, ensure_ascii=False))}",
    ])
    return lines


def format_media_profile(result: AnalysisResult) -> str:
    size = "未知" if result.file_size is None else f"{result.file_size} 字节（{result.file_size / 1024**2:.2f} MiB）"
    subtitle_status = "未知（轨道列表信息不足）" if result.has_subtitles is None else (
        f"有，{len(result.subtitles)} 条" if result.has_subtitles else "无")
    lines = [
        "媒体画像（输入元数据）", f"文件名：{result.path.name}", f"文件路径：{result.path}",
        f"文件大小：{size}",
        f"容器格式：{field_text(result, 'format_name', result.container)}",
        f"总时长（容器）：{field_text(result, 'duration', result.container_duration, duration_text)}",
        f"容器起点：{field_text(result, 'start_time', result.container_start_time, duration_text)}",
        f"容器码率：{field_text(result, 'bit_rate', result.container_bit_rate, lambda rate: f'{rate} bit/s')}",
        f"容器 Metadata：{field_text(result, 'tags', result.metadata, lambda item: json.dumps(item, ensure_ascii=False))}",
        f"已解析视频轨数量：{result.video_stream_count}（可处理视频 {len(result.videos)}，封面 {len(result.cover_art)}）",
        f"已解析音频轨数量：{result.audio_stream_count}", f"字幕：{subtitle_status}",
        "默认标记仅用于展示；V1 修复仍选择第一条非封面视频及全部音轨。",
    ]
    for kind, streams in (("视频", result.videos), ("音频", result.audios), ("字幕", result.subtitles),
                          ("封面", result.cover_art), ("其他", result.other_streams)):
        if not streams and kind in ("视频", "音频"):
            lines.append(f"{kind}轨道：无" if result.stream_list_complete else
                         f"{kind}轨道：无已解析轨道（轨道列表未知或不完整）")
        for stream in streams:
            lines.extend(stream_lines(stream, kind))
    if result.videos:
        video = result.videos[0]
        for position, audio in enumerate(result.audios, 1):
            offset = audio.start_time - video.start_time if audio.start_time is not None and video.start_time is not None else None
            lines.append(f"Timing：音轨 {position} 起始偏移（音频 - 首条视频）：{duration_text(offset)}")
            lines.append(f"Timing：音轨 {position} 与首条视频时长差：{duration_text(absolute_difference(video.duration, audio.duration))}")
    lines.append("画像基于元数据；不验证实际颜色、逐帧节奏或内容同步，不推断未知位深和旋转。")
    return "\n".join(lines)
