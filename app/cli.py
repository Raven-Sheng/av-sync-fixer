"""仅负责参数解析、结果展示及用户可读错误。"""

import argparse
import math
import sys
import os
from pathlib import Path
import shlex

from app.analyzer import analyze, diagnose, fps_to_float
from app.ffmpeg_utils import MediaError, progress_percent, DEFAULT_OUTPUT_PRESET, OUTPUT_PRESETS
from app.fixer import estimate_duration, execute_fix, prepare_fix, DEFAULT_REPAIR_MODE, REPAIR_MODES
from app.models import AnalysisResult, StreamInfo, SyncDiagnosis, FixResult
from app.media_profile import duration_text, rate_text, format_media_profile
from app.compatibility import diagnose_compatibility, format_compatibility_report
from app.color import color_plan_text
from app.repair_plan import format_repair_decisions
from app.deep_sync import format_deep_report, DEFAULT_DEEP_TIMEOUT, DEFAULT_MAX_RECORDS
from app.presets import PRESET_LABELS, format_validation_report


def diagnosis_lines(diagnosis: SyncDiagnosis) -> list[str]:
    video, audio = diagnosis.video, diagnosis.audio
    def track_label(stream: StreamInfo | None) -> str:
        if stream is None:
            return "无"
        return f"#{stream.index}" if stream.index is not None else "编号未知"

    def fps_text(value: float | None) -> str:
        return "未知" if value is None else f"{value:.6f} FPS"

    mode = "未知（帧率信息不足）"
    if diagnosis.suspected_vfr is True:
        mode = "疑似可变帧率 VFR"
    elif diagnosis.suspected_vfr is False:
        mode = "未发现明显帧率差异（不能据此确认 CFR）"
    start_status = "未知"
    if diagnosis.start_time_mismatch is not None:
        start_status = "存在明显起始偏移" if diagnosis.start_time_mismatch else "未发现明显起始偏移"
    risk = diagnosis.duration_risk or "未知（信息不足）"
    lines = [
        "", "音画同步分析",
        f"比较轨道：视频 {track_label(video)} / 音频 {track_label(audio)}",
        f"平均帧率：{fps_text(diagnosis.average_fps)}",
        f"标称帧率：{fps_text(diagnosis.nominal_fps)}",
        f"帧率差：{fps_text(diagnosis.fps_diff)}",
        f"帧率模式：{mode}",
        f"视频轨长度：{duration_text(video.duration if video else None)}",
        f"音频轨长度：{duration_text(audio.duration if audio else None)}",
        f"轨道长度差：{duration_text(diagnosis.duration_diff)}",
        f"起始时间差：{duration_text(diagnosis.start_time_diff)}",
        f"时间戳分析：{start_status}",
        f"同步风险：{risk}（等级按轨道长度差评估；帧率和起始偏移单独提示）",
        f"Sync pattern (候选): {', '.join(diagnosis.patterns)}",
        "渐进漂移与时钟偏差需要深度时间戳证据；时长差本身不足以判定。可使用 --deep-analysis。",
        "潜在原因：",
    ]
    lines.extend(f"- {cause}" for cause in diagnosis.potential_causes)
    if not diagnosis.potential_causes:
        lines.append("- 当前可用信息未提示明确原因；不代表实际播放一定同步。")
    if diagnosis.limitations:
        lines.append("信息限制：")
        lines.extend(f"- {limitation}" for limitation in diagnosis.limitations)
    return lines


def format_report(result: AnalysisResult, *, preset: str = DEFAULT_OUTPUT_PRESET) -> str:
    lines = [format_media_profile(result), "工具检查：ffmpeg、ffprobe 均可用"]
    lines.extend(diagnosis_lines(diagnose(result)))
    lines.extend([
        "",
        "说明：轨道时长缺失时显示未知，不使用容器时长替代。",
        "音视频 time_base 不同本身不代表异常，起始时间已按秒比较。",
        "诊断仅基于元数据线索，不能确认实际音画不同步；不修改源文件。",
    ])
    lines.extend(["", format_compatibility_report(diagnose_compatibility(result, preset=preset))])
    if result.deep_sync:
        lines.append(format_deep_report(result.deep_sync))
    return "\n".join(lines)


def format_comparison(result: FixResult) -> str:
    lines = ["", "修复前后对比（第一条视频轨 / 第一条音频轨）"]
    for label, info in (("输入", result.before), ("输出", result.after)):
        diagnosis = diagnose(info)
        lines.extend([
            f"{label}：{info.path}",
            f"  视频时长：{duration_text(diagnosis.video.duration if diagnosis.video else None)}",
            f"  音频时长：{duration_text(diagnosis.audio.duration if diagnosis.audio else None)}",
            f"  时长差：{duration_text(diagnosis.duration_diff)}",
            f"  FPS（平均）：{rate_text(diagnosis.video.avg_frame_rate if diagnosis.video else None)}",
            f"  FPS（标称）：{rate_text(diagnosis.video.r_frame_rate if diagnosis.video else None)}",
        ])
    prefix = "CFR 转码" if result.target_fps is not None else "转码（未强制 CFR）"
    lines.append(f"{prefix}和输出探测已通过；这不代表已确认实际音画同步，也不会自动拉伸音频消除时长差。")
    return "\n".join(lines)


def format_command(command: list[str], *, windows: bool | None = None) -> str:
    """仅用于展示：Windows 使用 PowerShell 字面量，其他平台使用 POSIX 转义。"""
    if windows is None:
        windows = os.name == "nt"
    if windows:
        # list2cmdline 的 C 运行库规则不能保护 PowerShell 的括号、$、& 等。
        # PowerShell 也把弯单引号当作分隔符，三种单引号均须重复转义。
        # & 调用带引号的路径；字符串中的 $ 等字符不展开。
        quoted = (argument.replace("'", "''").replace("‘", "‘‘").replace("’", "’’") for argument in command)
        return "& " + " ".join("'" + argument + "'" for argument in quoted)
    return shlex.join(command)


def run_fix(source: AnalysisResult, *, mode: str = DEFAULT_REPAIR_MODE, dry_run: bool = False,
            preset: str = DEFAULT_OUTPUT_PRESET) -> None:
    plan = prepare_fix(source, mode=mode, preset=preset)
    strategy = plan.strategy
    selected = []
    if strategy.cfr:
        selected.append("cfr")
    if strategy.timestamp:
        selected.append("timestamp")
    if strategy.audio_sync_tracks:
        selected.append("audio-sync（音轨序号 " + ", ".join(str(i + 1) for i in strategy.audio_sync_tracks) + "）")
    print(f"\n修复模式：{mode}\n实际策略：{', '.join(selected) or '兼容性转码'}")
    print(f"输出预设：{PRESET_LABELS[preset]}")
    if plan.repair:
        print(format_repair_decisions(plan.repair))
    if plan.color:
        print(color_plan_text(plan.color))
        for warning in plan.color.warnings:
            print(f"颜色说明：{warning}")
    for reason in strategy.reasons:
        print(f"选择依据：{reason}")
    for warning in strategy.warnings:
        print(f"说明：{warning}")
    # 容器时长/轨道长度仅用于估计进度，不参与音频裁剪或拉伸。
    total = estimate_duration(source)
    last_percent = -1

    def show_start(output: Path, fps: int | None, command: list[str]) -> None:
        print(f"\n输入路径：{source.path}\n输出路径：{output}\n目标 FPS：{fps if fps is not None else '保持输入时序'}")
        print(f"修复范围：第一条非封面视频轨、全部 {len(source.audios)} 条音轨。")
        displayed = format_command(command)
        shell = "PowerShell" if os.name == "nt" else "POSIX shell"
        detail = "预览使用最终路径；实际执行只将输出参数替换为同目录临时文件" if dry_run else "先写同目录临时文件，复查后发布"
        print(f"FFmpeg 命令（{shell}；{detail}）：\n{displayed}", flush=True)
        if not dry_run:
            print("修复进度：开始", flush=True)

    if dry_run:
        show_start(plan.output_path, strategy.target_fps, list(plan.command))
        if os.path.lexists(plan.output_path):
            print("说明：目标已存在，真正执行时会拒绝覆盖。")
        print("dry-run：仅完成分析和命令预览，未执行转码，未创建输出目录或文件。")
        return

    def show_progress(seconds: float) -> None:
        nonlocal last_percent
        percent = progress_percent(seconds, total)
        if percent is not None:
            if percent > last_percent:
                print(f"修复进度：{percent}%（已处理 {seconds:.1f} 秒）", flush=True)
                last_percent = percent
        else:
            print(f"修复进度：已处理 {seconds:.1f} 秒（总时长未知）", flush=True)

    result = execute_fix(plan, on_start=show_start, on_progress=show_progress,
                         on_validation=lambda report: print("\n" + format_validation_report(report), flush=True))
    print("修复进度：100%（输出复查通过）")
    print(format_comparison(result))


def configure_output_encoding() -> None:
    """供命令行入口共用，确保 Windows 控制台和重定向均可显示 Unicode 路径。"""
    # Windows 重定向到文件/管道时也保持 UTF-8，支持中文和其他 Unicode 路径。
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    configure_output_encoding()
    parser = argparse.ArgumentParser(description="音画同步修复器：媒体诊断与多策略修复")
    parser.add_argument("video", help="本地媒体文件路径（含空格时请加引号）")
    parser.add_argument("--fix", action="store_true", help="分析后修复并输出到项目 output 目录")
    parser.add_argument("--mode", choices=REPAIR_MODES, default=None, help=f"修复模式，默认 {DEFAULT_REPAIR_MODE}；需要 --fix")
    parser.add_argument("--preset", choices=OUTPUT_PRESETS, default=None, help="输出预设，默认 general（通用）；需要 --fix")
    parser.add_argument("--dry-run", action="store_true", help="配合 --fix 仅分析并预览命令，不执行转码")
    parser.add_argument("--deep-analysis", action="store_true", help="流式扫描 packets / 音频 frames，并抽样视频帧时间戳")
    parser.add_argument("--deep-timeout", type=float, help=f"深度扫描总时限（秒），默认 {DEFAULT_DEEP_TIMEOUT:g}")
    parser.add_argument("--deep-max-records", type=int, help=f"深度扫描总记录上限，默认 {DEFAULT_MAX_RECORDS}")
    args = parser.parse_args(argv)
    if not args.fix and (args.mode is not None or args.preset is not None or args.dry_run):
        parser.error("--mode、--preset 和 --dry-run 需要与 --fix 一起使用")
    if not args.deep_analysis and (args.deep_timeout is not None or args.deep_max_records is not None):
        parser.error("--deep-timeout 和 --deep-max-records 需要 --deep-analysis")
    if args.deep_timeout is not None and (not math.isfinite(args.deep_timeout) or args.deep_timeout <= 0):
        parser.error("--deep-timeout 必须为有限正数")
    if args.deep_max_records is not None and args.deep_max_records <= 0:
        parser.error("--deep-max-records 必须为正整数")
    try:
        if args.deep_analysis:
            print("深度分析中：流式读取时间戳；达到预算后会返回部分结果。", flush=True)
            result = analyze(args.video, deep_analysis=True, deep_timeout=args.deep_timeout,
                             deep_max_records=args.deep_max_records)
        else:
            result = analyze(args.video)
        print(format_report(result, preset=args.preset or DEFAULT_OUTPUT_PRESET), flush=True)
        if args.fix:
            run_fix(result, mode=args.mode or DEFAULT_REPAIR_MODE, dry_run=args.dry_run,
                    preset=args.preset or DEFAULT_OUTPUT_PRESET)
    except MediaError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n操作已取消。", file=sys.stderr)
        return 130
    return 0
