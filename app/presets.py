"""输出预设的复查和共享报告；CLI 与 GUI 不自行判断是否达标。"""

from app.analyzer import (
    absolute_difference, compare_threshold, fps_to_float, matches_target_fps,
    DURATION_MODERATE_THRESHOLD, START_TIME_TOLERANCE_SECONDS,
)
from app.ffmpeg_utils import BILIBILI_SAMPLE_RATE, VIDEO_PIXEL_FORMAT
from app.models import AnalysisResult, FixPlan, OutputValidation


PRESET_LABELS = {"general": "通用", "bilibili": "Bilibili"}


def validate_bilibili_output(plan: FixPlan, after: AnalysisResult) -> OutputValidation:
    errors, warnings = [], []
    formats = (after.container or "").split(",")
    brand = (after.major_brand or "").strip().lower()
    if "mp4" not in formats or not (brand.startswith("iso") or brand in {"mp41", "mp42", "avc1", "m4v"}):
        errors.append(f"无法确认 MP4 容器（格式 {after.container or '未知'}，major_brand {after.major_brand or '未知'}）。")
    video = after.videos[0] if after.videos else None
    original = plan.source.videos[0]
    target = plan.strategy.target_fps
    if len(after.videos) != 1 or video is None or video.codec != "h264":
        errors.append("输出必须包含一条 H.264 视频轨。")
    if video:
        if video.pixel_format != VIDEO_PIXEL_FORMAT:
            errors.append(f"像素格式须为 {VIDEO_PIXEL_FORMAT}，实际为 {video.pixel_format or '未知'}。")
        if (video.width, video.height) != (original.width, original.height):
            errors.append(f"分辨率未保持原尺寸 {original.width} × {original.height}。")
        if not matches_target_fps(video, target):
            errors.append(f"平均/标称 FPS 未同时匹配 CFR 目标 {target}。")
        if video.duration is None:
            warnings.append("视频时长未知，不能完成轨道长度核对。")
    if len(after.audios) != len(plan.source.audios):
        errors.append("输出音轨数量与输入不一致。")
    for position, audio in enumerate(after.audios, 1):
        if audio.codec != "aac" or audio.sample_rate != BILIBILI_SAMPLE_RATE:
            errors.append(f"音轨 {position} 须为 AAC / {BILIBILI_SAMPLE_RATE} Hz，实际为 {audio.codec or '未知'} / {audio.sample_rate or '未知'}。")
        difference = absolute_difference(video.duration if video else None, audio.duration)
        if difference is None:
            warnings.append(f"音轨 {position} 的长度差未知，无法确认时长是否接近。")
        elif compare_threshold(difference, DURATION_MODERATE_THRESHOLD) >= 0:
            warnings.append(f"音轨 {position} 与视频长度差仍为 {difference:.3f} 秒，同步风险未消除；未强行截断或拉伸内容。")
        start_difference = absolute_difference(video.start_time if video else None, audio.start_time)
        if start_difference is not None and compare_threshold(start_difference, START_TIME_TOLERANCE_SECONDS) > 0:
            warnings.append(f"音轨 {position} 与视频的起始偏移仍为 {start_difference:.3f} 秒，请确认是否为预期偏移。")
    for stream in (*after.videos, *after.audios):
        if stream.start_time is None or stream.start_time < 0:
            warnings.append("输出存在未知或负起点，需检查时间戳。")
            break
    return OutputValidation(plan.preset, after, target, tuple(errors), tuple(warnings))


def format_validation_report(result: OutputValidation) -> str:
    info = result.media
    video = info.videos[0] if info.videos else None
    def seconds(value):
        return "未知" if value is None else f"{value:.3f} 秒"
    def fps(value):
        number = fps_to_float(value)
        return "未知" if number is None else f"{number:g} FPS"
    lines = [
        f"输出验证报告 · {PRESET_LABELS[result.preset]}",
        f"容器：{info.container or '未知'}（major_brand：{info.major_brand or '未知'}）",
        f"视频编码：{video.codec if video and video.codec else '未知'}",
        f"像素格式：{video.pixel_format if video and video.pixel_format else '未知'}",
        f"分辨率：{video.width} × {video.height}" if video and video.width and video.height else "分辨率：未知",
        f"帧率：平均 {fps(video.avg_frame_rate if video else None)} / 标称 {fps(video.r_frame_rate if video else None)}；CFR 目标 {result.target_fps}",
        f"视频时长：{seconds(video.duration if video else None)}",
    ]
    if not info.audios:
        lines.append("音频编码 / 采样率 / 时长 / 轨道差异：无音频轨（输入无音轨时不自动生成）")
    for position, audio in enumerate(info.audios, 1):
        lines.extend([
            f"音轨 {position}：编码 {audio.codec or '未知'}；采样率 {audio.sample_rate or '未知'} Hz",
            f"  音频时长：{seconds(audio.duration)}；轨道差异：{seconds(absolute_difference(video.duration if video else None, audio.duration))}",
        ])
    if result.errors:
        lines.append("结论：不符合预设编码要求，未发布最终文件。")
    else:
        lines.append("结论：预设编码要求通过。" if not result.warnings else "结论：预设编码要求通过，但仍有需要检查的同步风险。")
    lines.extend(f"不符合项：{message}" for message in result.errors)
    lines.extend(f"提示：{message}" for message in result.warnings)
    lines.append("CFR 由 fps 滤镜生成，复查平均/标称 FPS；元数据验证不能证明声音与画面内容同步。")
    return "\n".join(lines)
