"""按诊断选择策略，生成可预览计划，再转码、复查与安全发布。"""

from collections.abc import Callable
from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from app.analyzer import (
    analyze, compare_threshold, diagnose, fps_to_float, matches_target_fps, DURATION_MODERATE_THRESHOLD,
)
from app.ffmpeg_utils import (
    MediaError, PROJECT_ROOT, build_fix_command, find_tool, run_ffmpeg,
    DEFAULT_OUTPUT_PRESET, OUTPUT_PRESETS,
)
from app.models import AnalysisResult, FixPlan, FixResult, OutputValidation, RepairStrategy
from app.presets import validate_bilibili_output


COMMON_FPS = (24, 25, 30, 50, 60, 120)
DEFAULT_TARGET_FPS = 30
OUTPUT_DIR = PROJECT_ROOT / "output"
REPAIR_MODES = ("safe", "cfr", "timestamp", "audio-sync")
DEFAULT_REPAIR_MODE = "safe"


def estimate_duration(source: AnalysisResult) -> float | None:
    """仅用于估计转码进度，不参与音频裁剪或速度调整。"""
    durations = [stream.duration for stream in (*source.videos[:1], *source.audios) if stream.duration is not None]
    return source.container_duration or (max(durations) if durations else None)


def select_target_fps(avg_frame_rate: Any, r_frame_rate: Any = None) -> int:
    """优先平均 FPS，其次标称 FPS；等距离取较低值，未知时用 30。"""
    fps = fps_to_float(avg_frame_rate) or fps_to_float(r_frame_rate)
    if fps is None:
        return DEFAULT_TARGET_FPS
    if fps >= COMMON_FPS[-1]:
        return COMMON_FPS[-1]
    return min(COMMON_FPS, key=lambda common: abs(common - fps))


def select_strategy(source: AnalysisResult, mode: str = DEFAULT_REPAIR_MODE, *, preset: str = DEFAULT_OUTPUT_PRESET) -> RepairStrategy:
    if preset not in OUTPUT_PRESETS:
        raise MediaError(f"不支持的输出预设：{preset}")
    if mode not in REPAIR_MODES:
        raise MediaError(f"不支持的修复模式：{mode}")
    if not source.videos:
        raise MediaError("没有可修复的视频轨（封面图片不算视频）。")
    if mode == "audio-sync" and not source.audios:
        raise MediaError("audio-sync 模式需要音频轨。")
    diagnosis = diagnose(source)
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
        cfr = diagnosis.suspected_vfr is True or force_cfr
        if diagnosis.suspected_vfr is True:
            reasons.append("平均与标称 FPS 存在明显差异，启用 CFR。")
        comparisons = [diagnose(replace(source, audios=(audio,))) for audio in source.audios]
        audio_tracks = tuple(
            position for position, item in enumerate(comparisons)
            if item.duration_diff is not None
            and compare_threshold(item.duration_diff, DURATION_MODERATE_THRESHOLD) >= 0
        )
        if audio_tracks:
            reasons.append("部分音轨与视频的长度差达到中等风险，启用按时间戳同步；长度差本身不证明累计漂移。")
        streams = (video, *source.audios)
        timestamp = (
            any(stream.start_time is None or stream.start_time < 0 for stream in streams)
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
    if audio_tracks:
        warnings.append("audio-sync 根据音频 PTS 与采样数不一致补缺口/裁重叠，不拉伸追齐视频长度；较大缺口的修补可能可闻。")
    if timestamp:
        warnings.append("时间戳模式补缺失 PTS 并处理开头负时间戳，不能保证修复中途不单调或严重损坏的时间戳。")
    if any(stream.start_time is None for stream in (video, *source.audios)) and (cfr or timestamp or audio_tracks):
        warnings.append("部分起始时间未知，各轨从零归一化，无法保留未知的起始偏移。")
    target_fps = select_target_fps(video.avg_frame_rate, video.r_frame_rate) if cfr else None
    if cfr and not (fps_to_float(video.avg_frame_rate) or fps_to_float(video.r_frame_rate)):
        warnings.append(f"输入 FPS 未知，使用默认 {target_fps} FPS。")
    return RepairStrategy(mode, cfr, timestamp, audio_tracks, target_fps, tuple(reasons), tuple(warnings))


def prepare_fix(source: AnalysisResult, output_dir: Path | None = None, *, mode: str = DEFAULT_REPAIR_MODE,
                preset: str = DEFAULT_OUTPUT_PRESET) -> FixPlan:
    """只生成计划，不创建目录、不运行 FFmpeg。预览命令使用最终输出路径。"""
    strategy = select_strategy(source, mode, preset=preset)
    try:
        directory = Path(output_dir if output_dir is not None else OUTPUT_DIR).resolve()
        output = directory / f"{source.path.stem}_fixed.mp4"
        command = build_fix_command(find_tool("ffmpeg"), source, output, strategy=strategy, preset=preset)
    except OSError as exc:
        raise MediaError(f"无法准备输出路径：{exc}") from exc
    return FixPlan(source, output, strategy, tuple(command), preset)


def fix_video(
    source: AnalysisResult,
    output_dir: Path | None = None,
    *,
    mode: str = DEFAULT_REPAIR_MODE,
    preset: str = DEFAULT_OUTPUT_PRESET,
    on_start: Callable[[Path, int | None, list[str]], None] | None = None,
    on_progress: Callable[[float], None] | None = None,
    on_validation: Callable[[OutputValidation], None] | None = None,
) -> FixResult:
    return execute_fix(prepare_fix(source, output_dir, mode=mode, preset=preset),
                       on_start=on_start, on_progress=on_progress, on_validation=on_validation)


def execute_fix(
    plan: FixPlan,
    *,
    on_start: Callable[[Path, int | None, list[str]], None] | None = None,
    on_progress: Callable[[float], None] | None = None,
    on_validation: Callable[[OutputValidation], None] | None = None,
) -> FixResult:
    """执行已选好的计划。预览与执行共享参数，只替换临时输出路径。"""
    source, output, strategy = plan.source, plan.output_path, plan.strategy
    target_fps = strategy.target_fps
    try:
        directory = output.parent
        directory.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(output):
            raise MediaError(f"输出文件已存在，不会覆盖：{output}。请先重命名或移走该文件。")
        with TemporaryDirectory(prefix=".avsync-", dir=directory) as temporary:
            staged = Path(temporary) / output.name
            command = [*plan.command[:-1], str(staged)]
            if on_start is not None:
                on_start(output, target_fps, command)
            run_ffmpeg(command, on_progress)
            try:
                after = analyze(staged)
            except MediaError as exc:
                raise MediaError(f"转码结束，但输出复查失败，未发布最终文件：{exc}") from exc
            validation = None
            if plan.preset == "bilibili":
                validation = validate_bilibili_output(plan, after)
                if on_validation is not None:
                    on_validation(validation)
                if validation.errors:
                    raise MediaError("输出不符合 Bilibili 预设，未发布最终文件：\n" + "\n".join(validation.errors))
            else:
                if not after.videos or after.videos[0].codec != "h264":
                    raise MediaError("输出复查失败：未找到 H.264 视频轨，未发布最终文件。")
                if strategy.cfr and not matches_target_fps(after.videos[0], target_fps):
                    raise MediaError("输出复查失败：输出 FPS 与目标不符，未发布最终文件。")
                if len(after.audios) != len(source.audios) or any(audio.codec != "aac" for audio in after.audios):
                    raise MediaError("输出复查失败：音轨数量或编码不符，未发布最终文件。")
            # 临时文件与目标位于同一文件系统。硬链接原子创建，目标已存在时
            # 必然失败，避免检查和发布之间有其他进程创建同名文件而被覆盖。
            try:
                os.link(staged, output)
            except FileExistsError as exc:
                raise MediaError(f"输出文件已被其他任务创建，不会覆盖：{output}") from exc
            after = replace(after, path=output)
            if validation is not None:
                validation = replace(validation, media=after)
            return FixResult(source, after, target_fps, strategy, validation)
    except OSError as exc:
        raise MediaError(f"修复文件操作失败（请检查空间、权限及文件系统硬链接支持）：{exc}") from exc
