"""Resolve analyzer evidence, explicit modes and output requirements into a RepairPlan."""

from dataclasses import replace
from typing import Any
from app.analyzer import diagnose, fps_to_float, compare_threshold, DURATION_MODERATE_THRESHOLD
from app.color import select_color_plan
from app.ffmpeg_utils import MediaError, DEFAULT_OUTPUT_PRESET, OUTPUT_PRESETS, BILIBILI_SAMPLE_RATE
from app.models import (AnalysisResult, RepairStrategy, RepairPlan, VideoRepairPlan,
                        AudioRepairPlan, ContainerRepairPlan, RepairDecision, SyncDiagnosis,
                        ExpectedOutputSpec, ExpectedVideoSpec, ExpectedAudioSpec)
from app.deep_sync import soft_compensation_allowed, MAX_SOFT_DRIFT_PPM

COMMON_FPS = (24, 25, 30, 50, 60, 120)
DEFAULT_TARGET_FPS = 30
REPAIR_MODES = ("safe", "cfr", "timestamp", "audio-sync")
DEFAULT_REPAIR_MODE = "safe"
OUTPUT_DURATION_TOLERANCE = 0.1
OUTPUT_START_TOLERANCE = 0.05
MAX_SYNC_REGRESSION_TOLERANCE = 0.5
MAX_OUTPUT_START_TOLERANCE = 0.1
AAC_FRAME_SAMPLES = 1024
# Native FFmpeg AAC capabilities, in encoder preference order; see README sources.
AAC_SAMPLE_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
                    16000, 12000, 11025, 8000, 7350)
OUTPUT_FPS_TOLERANCE = 0.1


def expected_audio_sample_rate(audio: AudioRepairPlan, source_rate: int | None) -> int | None:
    if audio.sample_rate is not None or source_rate is None:
        return audio.sample_rate
    if audio.codec == "aac":
        # FFmpeg keeps supported rates; otherwise chooses the nearest candidate.
        # min preserves encoder order for ties. This describes the existing command.
        return min(AAC_SAMPLE_RATES, key=lambda rate: abs(rate - source_rate))
    return source_rate


def _audio_evidence(source: AnalysisResult, index: int | None):
    if source.deep_sync is None or index is None:
        return None
    return next((a for a in source.deep_sync.audios if a.stream_index == index), None)


def _soft_tracks(source: AnalysisResult) -> tuple[int, ...]:
    if source.deep_sync is None:
        return ()
    return tuple(i for i, audio in enumerate(source.audios)
                 if audio.sample_rate and soft_compensation_allowed(_audio_evidence(source, audio.index), source.deep_sync.video))


def _timeline_starts(source: AnalysisResult) -> list[float | None]:
    """完整且无缺失时间戳的帧证据可补充 metadata 起点，保留已观测的 offset。"""
    starts = [stream.start_time for stream in (*source.videos[:1], *source.audios)]
    if source.deep_sync:
        evidence = [source.deep_sync.video, *(_audio_evidence(source, a.index) for a in source.audios)]
        for i, item in enumerate(evidence):
            # A later valid PTS cannot establish the origin of earlier untimed frames.
            if (i < len(starts) and starts[i] is None and item and item.complete
                    and not item.missing_timestamps):
                starts[i] = item.first_pts
    return starts


def select_target_fps(avg_frame_rate: Any, r_frame_rate: Any = None) -> int:
    """优先平均 FPS，其次标称 FPS；等距离取较低值，未知时用 30。"""
    fps = fps_to_float(avg_frame_rate) or fps_to_float(r_frame_rate)
    if fps is None:
        return DEFAULT_TARGET_FPS
    if fps >= COMMON_FPS[-1]:
        return COMMON_FPS[-1]
    return min(COMMON_FPS, key=lambda common: abs(common - fps))


def _diagnose_tracks(source: AnalysisResult) -> tuple[SyncDiagnosis, ...]:
    """每轨只诊断一次，选择和原因说明共享同一组证据；无音轨时保留视频诊断。"""
    return tuple(diagnose(replace(source, audios=(audio,))) for audio in source.audios) or (diagnose(source),)


def select_strategy(source: AnalysisResult, mode: str = DEFAULT_REPAIR_MODE, *, preset: str = DEFAULT_OUTPUT_PRESET) -> RepairStrategy:
    return _select_strategy(source, mode, preset, _diagnose_tracks(source))


def _validate_request(source: AnalysisResult, mode: str, preset: str) -> None:
    if preset not in OUTPUT_PRESETS:
        raise MediaError(f"不支持的输出预设：{preset}")
    if mode not in REPAIR_MODES:
        raise MediaError(f"不支持的修复模式：{mode}")
    if not source.videos:
        raise MediaError("没有可修复的视频轨（封面图片不算视频）。")
    if mode == "audio-sync" and not source.audios:
        raise MediaError("audio-sync 模式需要音频轨。")


def _select_strategy(source: AnalysisResult, mode: str, preset: str,
                     diagnoses: tuple[SyncDiagnosis, ...]) -> RepairStrategy:
    _validate_request(source, mode, preset)
    diagnosis = diagnoses[0]
    video = source.videos[0]
    reasons, warnings = [], []
    if len(source.videos) > 1:
        warnings.append(f"仅导出第一条非封面视频轨，其余 {len(source.videos) - 1} 条视频轨不会写入输出；全部音轨仍保留。")
    force_cfr = preset == "bilibili"
    cfr = mode == "cfr" or force_cfr
    timestamp = mode == "timestamp"
    audio_tracks = tuple(range(len(source.audios))) if mode == "audio-sync" else ()
    if force_cfr:
        reasons.append("Bilibili 输出预设强制 CFR，音轨统一为 AAC / 48 kHz；其他修复项按所选模式处理。")
    if mode == "safe":
        deep_vfr = bool(source.deep_sync and source.deep_sync.video and source.deep_sync.video.vfr_suspected)
        cfr = diagnosis.suspected_vfr is True or deep_vfr or force_cfr
        if diagnosis.suspected_vfr is True:
            reasons.append("平均与标称 FPS 存在明显差异，启用 CFR。")
        comparisons = diagnoses if source.audios else ()
        audio_tracks = _soft_tracks(source)
        if audio_tracks:
            reasons.append("完整深度证据显示 PTS 与累计采样时钟存在稳定微小斜率，启用有上限的软补偿。")
        elif any(item.duration_diff is not None and compare_threshold(item.duration_diff, DURATION_MODERATE_THRESHOLD) >= 0
                 for item in comparisons):
            reasons.append("仅有音视频长度差不足以启用 async；可能只是尾部长度不同，需深度时间戳证据。")
        timestamp = (
            any(start is None or start < 0 for start in _timeline_starts(source))
            or any(item.start_time_mismatch is True for item in comparisons)
        )
        if timestamp:
            reasons.append("起始时间存在缺失、负值或明显偏移，启用基础时间戳处理；保留已知相对偏移。")
        if not (cfr or timestamp or audio_tracks):
            reasons.append("未发现足以启用专项修复的线索，仅进行 H.264/AAC MP4 兼容性转码。")
        if diagnosis.suspected_vfr is None and not force_cfr:
            warnings.append("帧率信息不足，safe 不据此强制 CFR；如需固定帧率可显式选择 cfr。")
        if any(item.duration_diff is None for item in comparisons):
            warnings.append("部分轨道时长未知，safe 不据此启用对应音轨的同步。")
    else:
        reasons.append(f"显式选择 {mode} 修复模式。")
        if mode == "audio-sync" and source.deep_sync:
            audio_tracks = _soft_tracks(source)
            reasons.append("已有深度证据时，仅对完整、稳定且微小的漂移启用软补偿；异常或证据不足时不猜测。")
    if audio_tracks:
        if source.deep_sync:
            warnings.append(f"软补偿只追随音频自身 PTS，上限 {MAX_SOFT_DRIFT_PPM:g} ppm；不追齐视频总长度，不做硬裁剪或补静音。")
        else:
            warnings.append("显式旧 audio-sync 使用补缺口/裁重叠，不能连续补偿微小时钟偏差；较大缺口修补可能可闻，建议先 --deep-analysis。")
    if timestamp:
        warnings.append("时间戳模式补缺失 PTS 并处理开头负时间戳，不能保证修复中途不单调或严重损坏的时间戳。")
    if any(start is None for start in _timeline_starts(source)) and (cfr or timestamp or audio_tracks):
        warnings.append("部分起始时间未知，各轨从零归一化，无法保留未知的起始偏移。")
    target_fps = select_target_fps(video.avg_frame_rate, video.r_frame_rate) if cfr else None
    if cfr and not (fps_to_float(video.avg_frame_rate) or fps_to_float(video.r_frame_rate)):
        warnings.append(f"输入 FPS 未知，使用默认 {target_fps} FPS。")
    return RepairStrategy(mode, cfr, timestamp, audio_tracks, target_fps, tuple(reasons), tuple(warnings))


def select_repair_plan(source: AnalysisResult, mode: str = DEFAULT_REPAIR_MODE,
                       *, preset: str = DEFAULT_OUTPUT_PRESET) -> RepairPlan:
    diagnoses = _diagnose_tracks(source)
    strategy = _select_strategy(source, mode, preset, diagnoses)
    return _plan_from_strategy(source, strategy, preset, diagnoses)


def plan_from_strategy(source: AnalysisResult, strategy: RepairStrategy,
                       *, preset: str = DEFAULT_OUTPUT_PRESET) -> RepairPlan:
    """Migration adapter for callers with an already selected legacy strategy."""
    return _plan_from_strategy(source, strategy, preset, _diagnose_tracks(source), supplied_strategy=True)


def _plan_from_strategy(source: AnalysisResult, strategy: RepairStrategy, preset: str,
                        diagnoses: tuple[SyncDiagnosis, ...], *, supplied_strategy: bool = False) -> RepairPlan:
    _validate_request(source, strategy.mode, preset)
    if not strategy.cfr and strategy.target_fps is not None:
        raise MediaError("保持帧率的策略不能指定目标 FPS。")
    if strategy.cfr and (type(strategy.target_fps) is not int or strategy.target_fps <= 0):
        raise MediaError("目标 FPS 必须为正整数。")
    if any(type(i) is not int or not 0 <= i < len(source.audios) for i in strategy.audio_sync_tracks):
        raise MediaError("音频同步策略包含无效音轨编号。")
    if len(set(strategy.audio_sync_tracks)) != len(strategy.audio_sync_tracks):
        raise MediaError("音频同步策略包含重复音轨编号。")
    video = source.videos[0]
    if preset == "bilibili":
        if not strategy.cfr:
            raise MediaError("Bilibili 预设必须启用 CFR。")
        if video.width is None or video.height is None:
            raise MediaError("Bilibili 预设无法确认原始分辨率，不能验证保持原尺寸。")
        if video.width % 2 or video.height % 2:
            raise MediaError(f"原始分辨率 {video.width} × {video.height} 含奇数边长，"
                             "无法同时保持原尺寸并使用 libx264 / yuv420p；Bilibili 预设不会自动缩放、裁剪或补边。")
    color = select_color_plan(video)
    if color.action == "blocked":
        raise MediaError(color.reason)
    starts = _timeline_starts(source)
    offsets = [0.0] * len(starts)
    if all(start is not None for start in starts):
        origin = min(starts)
        offsets = [start - origin for start in starts]
    normalize = strategy.cfr or strategy.timestamp or bool(strategy.audio_sync_tracks)
    timestamp_strategy = ("regenerate_missing_pts" if strategy.timestamp else
                          "normalize" if normalize else "preserve")
    video_plan = VideoRepairPlan(
        video.index, "h264", strategy.target_fps if strategy.cfr else None,
        "cfr" if strategy.cfr else "preserve", color.pixel_format, color,
        timestamp_strategy, offsets[0], preset != "bilibili", preset != "bilibili",
    )
    audios = []
    for i, audio in enumerate(source.audios):
        evidence = _audio_evidence(source, audio.index)
        sync = "async_gaps" if i in strategy.audio_sync_tracks else "preserve"
        rate = BILIBILI_SAMPLE_RATE if preset == "bilibili" else None
        if sync != "preserve" and not supplied_strategy and source.deep_sync:
            sync = "async_soft"
            rate = rate or audio.sample_rate
        audios.append(AudioRepairPlan(
            audio.index, "aac", rate, sync, offsets[i + 1],
            max_soft_compensation=MAX_SOFT_DRIFT_PPM / 1e6 if sync == "async_soft" else 0.0,
            estimated_drift_seconds=evidence.drift_seconds if evidence else None,
            estimated_drift_ppm=evidence.drift_ppm if evidence else None,
        ))
    audios = tuple(audios)
    diagnosis = diagnoses[0]
    cfr_reason = ("Bilibili 输出要求 CFR。" if preset == "bilibili" else
                  "显式选择 cfr 模式。" if strategy.mode == "cfr" else
                  "Suspected VFR：抽样视频帧间隔存在明显变化。" if source.deep_sync and source.deep_sync.video and source.deep_sync.video.vfr_suspected else
                  "Suspected VFR：平均与标称 FPS 存在明显差异。" if strategy.cfr else
                  "当前模式不修复帧率。" if strategy.mode != "safe" else
                  "帧率信息不足，不据此启用 CFR。" if diagnosis.suspected_vfr is None else
                  "未发现明显 VFR 线索，保留输入帧间隔。")
    timestamp_reason = ("显式选择 timestamp 模式。" if strategy.mode == "timestamp" else
                        "起点缺失、负值或明显偏移；仅补缺失 PTS 并处理开头负时间戳。" if strategy.timestamp else
                        "当前模式不修复时间戳。" if strategy.mode != "safe" else
                        "未发现需补 PTS 的起点异常。")
    decisions = [
        RepairDecision("Video", "H.264 / " + color.pixel_format, "兼容性输出编码要求。", "output"),
        RepairDecision("CFR", f"Enabled ({strategy.target_fps} FPS)" if strategy.cfr else "Disabled",
                       cfr_reason, "preset" if preset == "bilibili" else "explicit" if strategy.mode == "cfr" else "analysis"),
        RepairDecision("Timestamp / genpts", "Enabled" if strategy.timestamp else "Disabled", timestamp_reason,
                       "explicit" if strategy.mode == "timestamp" else "analysis"),
        RepairDecision("Timeline normalization", "Enabled" if normalize else "Disabled",
                       ("供所选时序修复使用；保留各轨相对起点。" if all(s is not None for s in starts) else
                        "部分起点未知，从零归一化，无法保留未知偏移。") if normalize else "保留输入时序。"),
        RepairDecision("Color conversion", "Full → Limited" if color.action == "full_to_limited" else "Disabled (Limited preserve)",
                       f"Input {video.pixel_format}; range={color.input_range} ({color.range_source}). {color.reason}"),
        RepairDecision("Color metadata", "preserve", "; ".join(
            f"{name}={getattr(color, name) or 'unknown'}" for name in ("color_space", "color_transfer", "color_primaries"))),
    ]
    for i, audio in enumerate(audios):
        comparison = diagnoses[i]
        enabled = audio.sync_strategy != "preserve"
        reason = ("完整深度证据支持 PTS−采样时钟渐进漂移；仅追随音频 PTS，禁止硬补偿。" if audio.sync_strategy == "async_soft" else
                  "深度证据不足、不完整、存在突跳或漂移超过微小补偿上限，不自动修改采样时钟。" if source.deep_sync else
                  "显式选择 audio-sync 模式；按音频 PTS 补缺口/裁重叠，不保证修复渐进漂移。" if strategy.mode == "audio-sync" else
                  "当前模式不启用音频同步。" if strategy.mode != "safe" else
                  "时长未知，不据此启用 async；未确认漂移。" if comparison.duration_diff is None else
                  "长度差不能证明累计漂移；需 --deep-analysis 检查 PTS 与采样时钟。" if comparison.duration_diff >= DURATION_MODERATE_THRESHOLD else
                  "未发现显著音频长度差；未确认漂移，不启用 async。")
        value = "Enabled (bounded soft compensation)" if audio.sync_strategy == "async_soft" else "Enabled (gaps/overlaps only)" if enabled else "Disabled"
        decisions.extend([
            RepairDecision(f"Audio resync #{i + 1}", value, reason,
                           "deep-analysis" if source.deep_sync else "explicit" if strategy.mode == "audio-sync" else "analysis"),
            RepairDecision(f"Audio encoding #{i + 1}", f"AAC / {audio.sample_rate or 'input'} Hz",
                           "Bilibili 要求 AAC / 48 kHz。" if preset == "bilibili" else "AAC 兼容性转码，保持原采样率策略。", "output"),
        ])
        if audio.estimated_drift_ppm is not None:
            decisions.append(RepairDecision(f"Estimated drift #{i + 1}",
                f"{audio.estimated_drift_seconds * 1000:+.3f}ms / {audio.estimated_drift_ppm:+.3f}ppm",
                f"实际策略 {audio.sync_strategy}; 最大软补偿 {audio.max_soft_compensation * 1e6:g}ppm；估算值仅覆盖扫描区间。", "deep-analysis"))
    if not audios:
        decisions.append(RepairDecision("Audio resync", "Disabled", "输入无音轨，不生成音轨。"))
    decisions.extend([
        RepairDecision("Input frame validation", "Enabled",
                       "转码时流式检查输入帧颜色与尺寸；HDR、颜色变化或证据不完整时停止发布，不猜测转换。"),
        RepairDecision("Geometry", "pad to even" if video_plan.pad_to_even else "preserve dimensions",
                       "yuv420p 需要偶数边长。" if video_plan.pad_to_even else "Bilibili 保持原尺寸和旋转显示矩阵。", "output"),
        RepairDecision("Container", "MP4 / faststart", "兼容性封装，将索引移至文件头。", "output"),
    ])
    if supplied_strategy:
        # 旧接口接受调用方已选好的开关，不能根据 mode 名字反推分析依据。
        decisions = [replace(d, basis="strategy", reason="调用方提供的 RepairStrategy；按其已选参数执行。")
                     if d.item in {"CFR", "Timestamp / genpts"} or d.item.startswith("Audio resync #")
                     else d for d in decisions]
    return RepairPlan(source.path.resolve(), strategy.mode, preset, video_plan, audios,
                      ContainerRepairPlan(), tuple(decisions), strategy.reasons, strategy.warnings)


def format_repair_decisions(plan: RepairPlan) -> str:
    lines = ["Repair decisions:"]
    for decision in plan.decisions:
        lines.extend([f"{decision.item}: {decision.value}", f"  Reason [{decision.basis}]: {decision.reason}"])
    return "\n".join(lines)


def expected_output_spec(plan: RepairPlan, source: AnalysisResult) -> ExpectedOutputSpec:
    """Resolve output requirements and timing budgets once, independently of validation."""
    from app.analyzer import absolute_difference
    video = source.videos[0]
    average = fps_to_float(video.avg_frame_rate)
    rate = plan.video.target_fps or average or fps_to_float(video.r_frame_rate)
    frame_seconds = 1 / rate if rate else 0.0
    width, height = video.width, video.height
    if plan.video.autorotate and video.rotation:
        angle = video.rotation % 360
        if angle in (90, 270):
            width, height = height, width
        elif angle != 180:
            width = height = None  # Arbitrary-angle geometry needs decoded-frame evidence.
    if plan.video.pad_to_even:
        width = width + width % 2 if width is not None else None
        height = height + height % 2 if height is not None else None
    starts = _timeline_starts(source)
    normalize = plan.video.timestamp_strategy != "preserve"
    start = plan.video.start_offset if normalize else (
        starts[0] - min(starts) if all(s is not None for s in starts) else None)
    audios = []
    for i, audio in enumerate(plan.audios):
        original = source.audios[i]
        relative = (audio.start_offset - plan.video.start_offset if normalize else
                    starts[i + 1] - starts[0] if starts[i + 1] is not None and starts[0] is not None else None)
        audios.append(ExpectedAudioSpec(
            audio.codec, expected_audio_sample_rate(audio, original.sample_rate), original.duration,
            absolute_difference(video.duration, original.duration), relative,
            (original.duration or 0) * audio.max_soft_compensation if audio.sync_strategy == "async_soft" else 0.0,
        ))
    audio_padding = max((2 * AAC_FRAME_SAMPLES / a.sample_rate for a in audios if a.sample_rate), default=0.0)
    duration_tolerance = max(OUTPUT_DURATION_TOLERANCE, 2 * frame_seconds, audio_padding)
    # A container's tail can belong to video/subtitle/data tracks omitted by the plan.
    # It is not a valid duration target for the retained streams in that case.
    retains_timeline = (len(source.videos) == 1 and len(plan.audios) == len(source.audios)
                        and not source.subtitles and not source.other_streams and not source.cover_art)
    return ExpectedOutputSpec(
        plan.preset, ExpectedVideoSpec(plan.video.codec, plan.video.pixel_format, plan.video.color_conversion,
            plan.video.fps_mode, plan.video.target_fps, width, height, video.duration, start, frame_seconds, average,
            max(OUTPUT_FPS_TOLERANCE, 2 / video.duration) if video.duration else OUTPUT_FPS_TOLERANCE),
        tuple(audios), plan.container, source.container_duration if retains_timeline else None,
        duration_tolerance, min(duration_tolerance, MAX_SYNC_REGRESSION_TOLERANCE),
        min(max(OUTPUT_START_TOLERANCE, frame_seconds, audio_padding), MAX_OUTPUT_START_TOLERANCE),
        max(OUTPUT_DURATION_TOLERANCE, 3 * frame_seconds) if plan.video.timestamp_strategy == "regenerate_missing_pts" else OUTPUT_START_TOLERANCE,
        DURATION_MODERATE_THRESHOLD,
    )
