"""解释输入兼容性风险；不探测媒体、不选择策略、不执行或构建 FFmpeg 命令。"""

from copy import deepcopy
from dataclasses import replace
import json
import re

from app.analyzer import (
    absolute_difference, compare_threshold, duration_risk_level, fps_to_float,
    FPS_TOLERANCE, DURATION_LOW_THRESHOLD, DURATION_MODERATE_THRESHOLD, START_TIME_TOLERANCE_SECONDS,
)
from app.ffmpeg_utils import BILIBILI_SAMPLE_RATE, DEFAULT_OUTPUT_PRESET, OUTPUT_PRESETS, VIDEO_PIXEL_FORMAT
from app.color import HDR_TRANSFERS, SDR_TRANSFERS, hdr_side_data_types
from app.models import (
    AnalysisResult, CompatibilityDiagnosis, CompatibilityFinding, CompatibilitySeverity as Severity,
    ProbeField, StreamInfo,
)


# 已知格式的分量位深，不是每像素总位数；未列出的格式保留未知。
EIGHT_BIT_FORMATS = frozenset({
    "yuv420p", "yuv422p", "yuv444p", "yuv440p", "yuv411p", "yuv410p",
    "yuvj420p", "yuvj422p", "yuvj444p", "yuvj440p", "yuvj411p",
    "nv12", "nv21", "rgb24", "bgr24", "rgba", "bgra", "gray", "gbrp",
})
HIGH_DEPTH_FORMAT = re.compile(r"(?:yuv(?:420|422|444|440)p|gbrp|gray)(9|10|12|14|16)(?:le|be)")
COMMON_CHANNEL_LAYOUTS = {"mono": 1, "stereo": 2}
CODEC_LABELS = {"hevc": "HEVC / H.265", "h265": "HEVC / H.265", "h.265": "HEVC / H.265",
                "h264": "H.264", "h.264": "H.264"}


def _label(value: str | None) -> str:
    return value.strip().lower() if value else ""


def pixel_format_depth(pixel_format: str | None) -> int | None:
    """只在诊断中解释已知像素格式；不回填 analyzer 的原始位深字段。"""
    name = _label(pixel_format)
    if name in EIGHT_BIT_FORMATS:
        return 8
    if name in {"p010le", "p010be"}:
        return 10
    match = HIGH_DEPTH_FORMAT.fullmatch(name)
    return int(match[1]) if match else None


def _evidence(stream: StreamInfo | AnalysisResult, *keys: str) -> dict:
    return {key: {"state": (item := stream.probe_fields.get(key, ProbeField())).state.value,
                  "raw": deepcopy(item.raw)} for key in keys}


def _finding(code, severity, summary, reason, **evidence) -> CompatibilityFinding:
    return CompatibilityFinding(code, severity, summary, reason, evidence)


def _bit_depth_finding(video: StreamInfo) -> CompatibilityFinding:
    inferred = pixel_format_depth(video.pixel_format)
    declared = video.bits_per_raw_sample
    conflict = declared is not None and inferred is not None and declared != inferred
    depth = None if conflict else declared if declared is not None else inferred
    target_depth = pixel_format_depth(VIDEO_PIXEL_FORMAT)
    severity = Severity.WARNING
    if conflict:
        reason = "位深字段与像素格式不一致；不能任选一个作为真实位深。"
    elif depth is None:
        reason = "没有足够证据，不能仅凭 HEVC Main/Main 10 Profile 猜测位深。"
    elif target_depth is None:
        reason = "目标像素格式的位深未知，无法评估精度变化。"
    elif depth > target_depth:
        severity = Severity.REPAIR_RECOMMENDED
        reason = f"当前 {target_depth}-bit 输出会降低精度，可能产生色带；高位深本身不等于 HDR，需要评估颜色处理。"
    elif depth < target_depth:
        severity = Severity.INFO
        reason = f"输入位深低于目标 {target_depth}-bit，位深扩展不会增加原始细节；此项不属于高位深降精度风险。"
    else:
        severity = Severity.INFO
        reason = "输入与目标分量位深相同；来自字段或已知像素格式解释，不是码流验证，也不等于已确认 SDR。"
    return _finding("video.bit_depth", severity,
        f"分量位深：{str(depth) + '-bit' if depth is not None else '未知或冲突'}", reason,
        **_evidence(video, "bits_per_raw_sample", "pix_fmt"), declared_depth=declared,
        pixel_format_depth=inferred, interpreted_depth=depth, target_pixel_format=VIDEO_PIXEL_FORMAT,
        target_depth=target_depth,
        source="conflict" if conflict else "bits_per_raw_sample" if declared is not None else "pix_fmt" if inferred else "unknown")


def _video_findings(video: StreamInfo) -> list[CompatibilityFinding]:
    findings = []
    codec = _label(video.codec)
    known_codec = codec in CODEC_LABELS
    codec_name = CODEC_LABELS.get(codec, video.codec or "未知")
    findings.append(_finding("video.codec", Severity.INFO if known_codec else Severity.WARNING,
        f"编码：{codec_name}",
        "合法编码；编码名称不保证当前 FFmpeg 构建可以解码该文件，未做解码能力测试。" if known_codec else
        "编码未知或未列入常见输入清单；需验证当前 FFmpeg 解码能力，不能据此判为损坏或不支持。",
        **_evidence(video, "codec_name", "profile", "level")))
    pixel = _label(video.pixel_format)
    findings.append(_finding("video.pixel_format", Severity.INFO if pixel == "yuv420p" else Severity.WARNING,
        f"像素格式：{video.pixel_format or '未知'}",
        "常见 8-bit 4:2:0 格式；像素格式不能单独证明颜色范围或内容正常。" if pixel == "yuv420p" else
        "yuvj420p 是合法的全范围像素格式表示，不代表文件损坏；只设置输出 yuv420p 可能仍保留全范围标记。" if pixel == "yuvj420p" else
        "当前输出为 yuv420p；4:2:2、4:4:4、RGB 或其他格式可能涉及采样/精度转换，需检查画质。" if pixel else
        "像素格式未知，无法评估采样与编码兼容性。",
        **_evidence(video, "pix_fmt")))
    findings.append(_bit_depth_finding(video))
    color_range = video.color_range_name
    findings.append(_finding("video.color_range",
        Severity.REPAIR_RECOMMENDED if color_range == "Full" else Severity.INFO if color_range == "Limited" else Severity.WARNING,
        f"颜色范围：{color_range or '未知'}",
        "合法 Full Range 输入。面向 Limited Range 输出需要显式范围转换，不能只改标签；独立颜色策略负责受支持 SDR YUV 的数值转换和颜色复查。" if color_range == "Full" else
        "Limited Range 标记属于常见视频范围；标记不证明像素实际范围。" if color_range == "Limited" else
        "范围标签缺失或未识别；不能默认为 Limited。颜色策略仅可利用已知 yuvj 的格式证据，否则停止转码以避免错误范围转换。",
        **_evidence(video, "color_range", "pix_fmt"), normalized_range=color_range))
    if pixel.startswith("yuvj") and color_range == "Limited":
        findings.append(_finding("video.range_conflict", Severity.WARNING, "颜色范围证据冲突",
            "yuvj 格式指向全范围，但 color_range 标记为 Limited；需核实，不能盲目转换。",
            **_evidence(video, "pix_fmt", "color_range")))
    colors = {_label(video.color_space), _label(video.color_primaries)}
    bt2020 = bool(colors & {"bt2020", "bt2020nc", "bt2020c"})
    findings.append(_finding("video.colorimetry",
        Severity.WARNING if bt2020 or "" in colors else Severity.INFO if colors == {"bt709"} else Severity.WARNING,
        "色彩标记：BT.2020" if bt2020 else "色彩标记：BT.709" if "bt709" in colors else "色彩标记：其他或未知",
        "BT.2020 涉及广色域；当前未实现到 BT.709 的受控转换，不能仅改标签。BT.2020 单独不足以确认 HDR。" if bt2020 else
        "矩阵与原色均标记为 BT.709；传递特性另行检查，不能据此确认 SDR。" if colors == {"bt709"} else
        "矩阵/原色信息不完整或不是一致的 BT.709；需要核实，不能假定两字段应始终相同。",
        **_evidence(video, "color_space", "color_primaries")))
    transfer = _label(video.color_transfer)
    side_types = [item.get("side_data_type") for item in video.side_data_list or ()
                  if isinstance(item.get("side_data_type"), str)]
    hdr_side = list(dict.fromkeys((*hdr_side_data_types(video.side_data_list or ()), *video.sampled_hdr_side_data_types)))
    frame_hdr = [t for t in video.sampled_color_transfers if t in HDR_TRANSFERS]
    transfer = next(iter(frame_hdr), transfer)
    findings.append(_finding("video.dynamic_range",
        Severity.UNSUPPORTED if transfer in HDR_TRANSFERS else Severity.WARNING if hdr_side or transfer not in SDR_TRANSFERS else Severity.INFO,
        f"HDR 特征：{HDR_TRANSFERS[transfer]}" if transfer in HDR_TRANSFERS else
        "潜在 HDR 附加元数据" if hdr_side else "潜在 SDR 特征" if transfer in SDR_TRANSFERS else "HDR / SDR：证据不足",
        "当前处理流程未实现受控 HDR→SDR 色调映射或 HDR 保真输出，因此该颜色处理需求不受支持；不代表文件损坏或 FFmpeg 不能解码。" if transfer in HDR_TRANSFERS else
        "检测到 HDR 相关附加信息，但缺少一致的 PQ/HLG 传递标记；需核实冲突，本预设停止转换，不能只凭附加信息断言内容为 HDR。" if hdr_side else
        "传递特性具有 SDR 线索；未验证像素/码流，不能保证标签准确。" if transfer in SDR_TRANSFERS else
        "缺少可靠传递特性；不能根据位深、HEVC 或 BT.2020 单独确认 HDR/SDR。",
        **_evidence(video, "color_transfer", "side_data_list"), hdr_side_data_types=hdr_side,
        sampled_color_transfers=list(video.sampled_color_transfers),
        sampled_hdr_side_data_types=list(video.sampled_hdr_side_data_types)))
    avg, nominal = fps_to_float(video.avg_frame_rate), fps_to_float(video.r_frame_rate)
    difference = absolute_difference(avg, nominal)
    vfr = difference is not None and compare_threshold(difference, FPS_TOLERANCE) > 0
    findings.append(_finding("video.frame_rate",
        Severity.WARNING if difference is None else Severity.REPAIR_RECOMMENDED if vfr else Severity.INFO,
        "疑似 VFR" if vfr else "帧率证据不足" if difference is None else "未发现明显帧率差异",
        "平均/标称 FPS 差超过容差，建议评估 CFR 转换；未扫描逐帧时间戳，不能确认 VFR。" if vfr else
        "仅检查有效平均/标称 FPS；信息缺失或两者接近均不能证明 CFR，也不能发现所有丢帧。",
        **_evidence(video, "avg_frame_rate", "r_frame_rate"), average_fps=avg, nominal_fps=nominal,
        difference_fps=difference, tolerance_fps=FPS_TOLERANCE))
    rotation_present = video.rotation is not None or any(name == "Display Matrix" for name in side_types) or "rotate" in (video.metadata or {})
    findings.append(_finding("video.rotation", Severity.WARNING if rotation_present else Severity.INFO,
        "存在旋转/显示矩阵元数据" if rotation_present else "未获得旋转元数据",
        "需保留显示方向并核对存储尺寸；显示矩阵还可能包含翻转等信息，单个角度不足以表达全部变换。" if rotation_present else
        "没有可用标记不等于已经确认画面无需旋转。",
        **_evidence(video, "side_data_list", "tags"), rotation=video.rotation, rotation_source=video.rotation_source))
    return findings


def _audio_findings(audio: StreamInfo, preset: str) -> list[CompatibilityFinding]:
    rate = audio.sample_rate
    nonstandard_rate = rate is not None and rate != BILIBILI_SAMPLE_RATE
    layout = _label(audio.channel_layout)
    expected_channels = COMMON_CHANNEL_LAYOUTS.get(layout)
    common = expected_channels is not None and audio.channels == expected_channels
    return [
        _finding("audio.sample_rate", Severity.REPAIR_RECOMMENDED if nonstandard_rate and preset == "bilibili" else
            Severity.WARNING if rate is None or nonstandard_rate else Severity.INFO,
            f"音频采样率：{str(rate) + ' Hz' if rate is not None else '未知'}",
            "Bilibili 预设要求 48 kHz；转换应使用重采样，不能修改播放速度。" if nonstandard_rate and preset == "bilibili" else
            "这是合法采样率；通用预设保留原采样率，平台兼容性需确认，不应无意义重采样。" if nonstandard_rate else
            "采样率未知，无法评估与目标的兼容性。" if rate is None else "符合常见 48 kHz 视频音频设置。",
            **_evidence(audio, "sample_rate"), parsed_sample_rate=rate, reference_sample_rate=BILIBILI_SAMPLE_RATE),
        _finding("audio.channel_layout", Severity.INFO if common else Severity.WARNING,
            f"声道布局：{audio.channel_layout or '未知'} / {audio.channels if audio.channels is not None else '未知'} 声道",
            "常见 mono/stereo 布局，声道数一致。" if common else
            "布局缺失、声道数不匹配或不在 mono/stereo 常见兼容基线内；5.1/7.1 等仍是合法布局，需验证 AAC 和播放端映射，不能自动当作立体声或混音。",
            **_evidence(audio, "channels", "channel_layout")),
    ]


def _timestamp_finding(stream: StreamInfo | AnalysisResult) -> CompatibilityFinding:
    is_container = isinstance(stream, AnalysisResult)
    start = stream.container_start_time if is_container else stream.start_time
    pts = None if is_container else stream.start_pts
    negative = (start is not None and start < 0) or (pts is not None and pts < 0)
    return _finding("timing.negative_start", Severity.REPAIR_RECOMMENDED if negative else Severity.WARNING if start is None else Severity.INFO,
        "存在负起始时间戳" if negative else "起始时间戳未知" if start is None else "已知起点非负",
        "负起点可能来自预卷或编码延迟，不代表损坏；建议检查时间轴兼容性，并保留合法的轨道相对偏移。" if negative else
        "只检查容器/轨道 start_time 和可用 start_pts；未扫描中途 PTS/DTS，不能排除负 DTS 或非单调时间戳。",
        **_evidence(stream, "start_time", *(() if is_container else ("start_pts",))), parsed_start_time=start, parsed_start_pts=pts)


def _timing_findings(video: StreamInfo, audio: StreamInfo) -> list[CompatibilityFinding]:
    offset = absolute_difference(video.start_time, audio.start_time)
    difference = absolute_difference(video.duration, audio.duration)
    start_risk = offset is not None and compare_threshold(offset, START_TIME_TOLERANCE_SECONDS) > 0
    severity = Severity.WARNING if difference is None else Severity.REPAIR_RECOMMENDED if compare_threshold(difference, DURATION_MODERATE_THRESHOLD) >= 0 else Severity.WARNING if compare_threshold(difference, DURATION_LOW_THRESHOLD) >= 0 else Severity.INFO
    return [
        _finding("timing.start_offset", Severity.WARNING if offset is None else Severity.REPAIR_RECOMMENDED if start_risk else Severity.INFO,
            "音视频起点差未知" if offset is None else f"音视频起点差：{offset:.6f} 秒",
            "偏移超过容差；可能是固定剪辑偏移，应确认后再处理，不能据此认定累计漂移。" if start_risk else
            "按秒比较每条音轨与第一条非封面视频；信息不足时不假定起点相同。",
            video=_evidence(video, "start_time"), audio=_evidence(audio, "start_time"),
            video_stream_index=video.index, difference_seconds=offset, tolerance_seconds=START_TIME_TOLERANCE_SECONDS),
        _finding("timing.duration_difference", severity,
            "音视频时长差未知" if difference is None else f"音视频时长差：{difference:.6f} 秒（{duration_risk_level(difference)}风险）",
            "可能是尾部长度不同或累计漂移；时长差不能决定拉伸比例，不建议为凑齐时长盲目裁剪/拉伸。" if difference is not None else
            "轨道时长证据不足；不使用容器时长替代，不将未知视为低风险。",
            video=_evidence(video, "duration", "duration_ts", "time_base"), audio=_evidence(audio, "duration", "duration_ts", "time_base"),
            video_stream_index=video.index, video_duration=video.duration, audio_duration=audio.duration,
            difference_seconds=difference, recommendation_threshold_seconds=DURATION_MODERATE_THRESHOLD),
    ]


def diagnose_compatibility(source: AnalysisResult, *, preset: str = DEFAULT_OUTPUT_PRESET) -> CompatibilityDiagnosis:
    """返回逐项诊断，不产生通过/失败结论，不改变 source 或 fixer 的决策。"""
    if preset not in OUTPUT_PRESETS:
        raise ValueError(f"未知输出预设：{preset}")
    findings = [_finding("input.track_inventory", Severity.INFO if source.stream_list_complete else Severity.WARNING,
        "轨道列表完整" if source.stream_list_complete else "轨道列表信息不足",
        "以下诊断覆盖全部已解析音视频轨；音视频配对比较以第一条非封面视频为基准。封面单独计数。",
        video_count=len(source.videos), audio_count=len(source.audios), cover_count=len(source.cover_art),
        has_subtitles=source.has_subtitles, stream_list_complete=source.stream_list_complete)]
    if not source.videos:
        findings.append(_finding("input.no_video", Severity.UNSUPPORTED if source.stream_list_complete else Severity.WARNING,
            "没有可处理视频轨" if source.stream_list_complete else "未解析出视频轨",
            "当前视频修复流程需要至少一条非封面视频；信息不足时不能断言原文件没有视频。"))
    if not source.audios:
        findings.append(_finding("input.no_audio", Severity.INFO if source.stream_list_complete else Severity.WARNING,
            "没有音频轨" if source.stream_list_complete else "未解析出音频轨",
            "无音轨视频是合法输入；无法评估音画同步，不能使用需要音轨的 audio-sync 模式。"))
    if len(source.videos) > 1:
        findings.append(_finding("input.multiple_video", Severity.WARNING, "存在多视频轨",
            "当前修复仅导出第一条非封面视频，其余视频不会写入输出；默认轨标记不改变选轨。",
            stream_indices=[stream.index for stream in source.videos]))
    if len(source.audios) > 1:
        findings.append(_finding("input.multiple_audio", Severity.WARNING, "存在多音频轨",
            "当前修复保留全部音轨；上传/播放端可能只使用其中一轨，需确认默认轨和语言，不自动混音。",
            stream_indices=[stream.index for stream in source.audios]))
    findings.append(_timestamp_finding(source))
    for kind, streams in (("video", source.videos), ("audio", source.audios)):
        for position, stream in enumerate(streams):
            items = _video_findings(stream) if kind == "video" else _audio_findings(stream, preset)
            items.append(_timestamp_finding(stream))
            if kind == "audio" and source.videos:
                items.extend(_timing_findings(source.videos[0], stream))
            findings.extend(replace(item, scope=f"{kind}:{position}", stream_index=stream.index) for item in items)
    return CompatibilityDiagnosis(preset, tuple(findings))


def format_compatibility_report(report: CompatibilityDiagnosis) -> str:
    lines = ["输入兼容性诊断 · Input Compatibility Report", f"评估预设：{report.preset}",
             "INFO：事实或常见特征；WARNING：需核实；REPAIR_RECOMMENDED：建议评估处理；UNSUPPORTED：当前处理能力边界。"]
    for item in report.findings:
        index = f" / 流 #{item.stream_index}" if item.stream_index is not None else ""
        lines.extend([f"[{item.severity.value}] {item.scope}{index} · {item.summary} ({item.code})",
                      f"  原因：{item.reason}", f"  依据：{json.dumps(item.evidence, ensure_ascii=False)}"])
    lines.append("诊断仅解释输入风险，不直接控制命令或转码；独立颜色策略会拒绝未知范围、冲突或不支持的颜色处理。UNSUPPORTED 不表示文件损坏，实际效果仍需验证。")
    return "\n".join(lines)
