"""面向用户的展示数据；复用诊断、RepairPlan 和验证模型，不执行媒体操作。"""

from dataclasses import dataclass
import re

from app.analyzer import diagnose
from app.compatibility import CODEC_LABELS, diagnose_compatibility
from app.ffmpeg_utils import MediaError
from app.models import AnalysisResult, CompatibilityDiagnosis, CompatibilitySeverity, OutputValidation, RepairPlan, SyncDiagnosis
from app.repair_plan import select_repair_plan, expected_audio_sample_rate


VALIDATION_LABELS = (("Timing", "同步"), ("Color", "颜色"), ("Video", "编码"),
                     ("Audio", "音频"), ("Compatibility", "平台兼容"))


@dataclass(frozen=True)
class MediaSummary:
    fields: dict[str, str]
    origin: str
    compatibility: tuple[str, ...]
    processing: tuple[str, ...]
    reasons: tuple[str, ...]
    plan: RepairPlan | None
    blocked: str | None
    diagnosis: CompatibilityDiagnosis
    sync: SyncDiagnosis


def codec_text(codec: str | None) -> str:
    return CODEC_LABELS.get(codec, codec.upper() if codec else "未知编码")


def seconds_text(value: float | None) -> str:
    return "未知" if value is None else f"{value:.3f} 秒"


def absent_track_text(source: AnalysisResult, kind: str) -> str:
    return f"无{kind}轨" if source.stream_list_complete else f"{kind}信息未知"


def audio_text(source: AnalysisResult) -> str:
    if not source.audios:
        return absent_track_text(source, "音频")
    return "；".join(f"{i + 1}: {codec_text(audio.codec)} · " +
                     (f"{audio.sample_rate / 1000:g} kHz" if audio.sample_rate else "采样率未知")
                     for i, audio in enumerate(source.audios))


def color_text(source: AnalysisResult) -> str:
    if not source.videos:
        return "未知"
    video = source.videos[0]
    range_text = f"{video.color_range_name} Range" if video.color_range_name else "范围未知"
    if video.color_space == video.color_primaries == video.color_transfer == "bt709":
        return f"{range_text} · BT.709"
    return f"{range_text} · 矩阵 {video.color_space or '未知'} / 原色 {video.color_primaries or '未知'} / 传递 {video.color_transfer or '未知'}"


def recording_origin(source: AnalysisResult) -> str:
    # Only expose explicit software/encoder metadata. Codec, range and filenames
    # are not recorder fingerprints; even metadata is not authenticated provenance.
    tags = [source.metadata, *(video.metadata for video in source.videos)]
    software = [value for tag in tags for key, value in (tag or {}).items()
                if key.casefold() in {"encoder", "software", "application", "writing_application"}
                and isinstance(value, str)]
    if any(re.search(r"\bsteam\b", value, re.IGNORECASE) for value in software):
        return "可能来自 Steam 录屏（软件元数据提及 Steam，来源未验证）"
    return "录制来源未确认；编码和颜色格式不能确定录屏软件。"


def frame_mode_text(source: AnalysisResult, diagnosis: SyncDiagnosis) -> str:
    sampled = source.deep_sync.video if source.deep_sync else None
    if sampled and sampled.vfr_suspected is True:
        return "疑似 VFR（深度抽样帧间隔存在变化）"
    if diagnosis.suspected_vfr is True:
        return "疑似 VFR（平均与标称 FPS 存在差异）"
    if sampled and sampled.vfr_suspected is False:
        return "抽样帧间隔未发现明显变化（不能确认整段 CFR）"
    return "未知（帧率信息不足）" if diagnosis.suspected_vfr is None else "未发现明显差异（不能据此确认 CFR）"


def processing_text(plan: RepairPlan, source: AnalysisResult) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Translate resolved actions, without reselecting or inferring any switch."""
    video, color = plan.video, plan.video.color_conversion
    actions = [f"{codec_text(video.codec)} 输出 · {video.pixel_format}"]
    reasons = [f"当前输出计划采用 {codec_text(video.codec)}，用于提高播放和上传兼容性；输入编码本身不代表文件损坏。"]
    if video.fps_mode == "cfr":
        actions.append(f"恒定帧率（CFR）转换 · {video.target_fps} FPS")
        decision = next((d for d in plan.decisions if d.item == "CFR"), None)
        if decision:
            reasons.append(decision.reason)
    else:
        actions.append("保留输入帧间隔 · 未启用 CFR 转换")
    if color.action == "full_to_limited":
        actions.append("Full → Limited 颜色范围转换")
        reasons.append("检测到 Full Range 输入。目标输出采用 Limited Range，将转换实际颜色数值并保留已知色彩体系，以降低平台转码出现颜色异常的风险。")
    else:
        actions.append("保留 Limited Range · 不重复压缩颜色范围")
    if video.timestamp_strategy == "regenerate_missing_pts":
        actions.append("处理缺失时间信息与开头异常")
        reasons.append("当前计划处理时间起点问题，并尽可能保留音视频相对关系；不能保证修复录制过程中的所有时间异常。")
    for i, audio in enumerate(plan.audios):
        if audio.sync_strategy == "async_soft":
            actions.append(f"音轨 {i + 1}：微量时钟补偿")
            reasons.append(f"音轨 {i + 1} 的深度分析支持微量漂移补偿，具体估算与补偿上限见技术详情。")
        elif audio.sync_strategy == "async_gaps":
            actions.append(f"音轨 {i + 1}：修补间隙或重叠（显式模式）")
            reasons.append("当前显式音频同步模式会修补间隙或重叠，不保证解决渐进漂移，明显缺口可能可闻。")
    if plan.audios and all(audio.sync_strategy == "preserve" for audio in plan.audios):
        actions.append("保留音频时间关系 · 未启用自动重同步")
    if not plan.audios:
        actions.append(f"{absent_track_text(source, '音频')} · 不生成音频")
    for i, (audio_plan, original) in enumerate(zip(plan.audios, source.audios)):
        rate = expected_audio_sample_rate(audio_plan, original.sample_rate)
        actions.append(f"音轨 {i + 1}：{codec_text(audio_plan.codec)} · " + (f"{rate / 1000:g} kHz" if rate else "采样率待确认"))
    return tuple(actions), tuple(reasons)


def build_media_summary(source: AnalysisResult, mode: str, preset: str) -> MediaSummary:
    diagnosis = diagnose(source)
    video, audio = diagnosis.video, diagnosis.audio
    fields = {
        "filename": source.path.name,
        "video_codec": codec_text(video.codec) if video else absent_track_text(source, "视频"),
        "resolution": f"{video.width} × {video.height}" if video and video.width and video.height else "未知",
        "fps": f"{diagnosis.average_fps:g} FPS" if diagnosis.average_fps is not None else "未知",
        "pixel_format": video.pixel_format or "未知" if video else "未知",
        "color": color_text(source), "audio": audio_text(source),
        "vfr": frame_mode_text(source, diagnosis),
        "video_duration": seconds_text(video.duration) if video else absent_track_text(source, "视频"),
        "audio_duration": seconds_text(audio.duration) if audio else absent_track_text(source, "音频"),
        "difference": seconds_text(diagnosis.duration_diff),
        "risk": f"{diagnosis.duration_risk}风险（按轨道长度差）" if diagnosis.duration_risk else "未知（信息不足）",
    }
    report = diagnose_compatibility(source, preset=preset)
    # Only display finding summaries here; detailed evidence remains in the full
    # report, including unsupported secondary tracks, without inventing a pass.
    severity_order = {CompatibilitySeverity.UNSUPPORTED: 0, CompatibilitySeverity.REPAIR_RECOMMENDED: 1,
                      CompatibilitySeverity.WARNING: 2, CompatibilitySeverity.INFO: 3}
    def scope_text(scope):
        if scope == "input":
            return "输入"
        kind, position = scope.split(":")
        return f"{'视频' if kind == 'video' else '音轨'} {int(position) + 1}"
    important = tuple(f"{scope_text(f.scope)} · {f.summary}" for f in sorted(report.findings, key=lambda f: severity_order[f.severity])
                      if f.severity != CompatibilitySeverity.INFO)
    if not important:
        important = ("未发现需要特别提示的输入兼容性项；不代表已验证实际播放同步。",)
    try:
        plan = select_repair_plan(source, mode, preset=preset)
    except MediaError as exc:
        return MediaSummary(fields, recording_origin(source), important, (), (), None, str(exc), report, diagnosis)
    actions, reasons = processing_text(plan, source)
    return MediaSummary(fields, recording_origin(source), important, actions, reasons, plan, None, report, diagnosis)


def comparison_text(source: AnalysisResult | None) -> str:
    if source is None:
        return "无可读取的媒体结果"
    video = source.videos[0] if source.videos else None
    return "\n".join((codec_text(video.codec) if video else absent_track_text(source, "视频"),
                      video.pixel_format or "像素格式未知" if video else "像素格式未知",
                      color_text(source), audio_text(source)))


def validation_summary(report: OutputValidation | None) -> dict[str, str]:
    sections = report.sections if report else {}
    observed = {check.section for check in report.checks} if report else set()
    return {label: sections[section].value if section in observed else "NOT TESTED"
            for section, label in VALIDATION_LABELS}


def completion_text(report: OutputValidation | None) -> str:
    levels = set(validation_summary(report).values())
    if report and (report.errors or "FAIL" in levels):
        return "输出验证未通过，请查看分项结果。"
    if "NOT TESTED" in levels:
        return "修复完成，尚无完整的输出验证结果。"
    if report.warnings or "WARNING" in levels:
        return "修复完成，验证有需关注项，请查看分项结果。"
    return "修复完成，输出复查已通过。"
