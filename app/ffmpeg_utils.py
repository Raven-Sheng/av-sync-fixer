"""集中管理工具发现、可用性检查和 ffprobe 子进程。"""

import json
from collections import deque
from collections.abc import Callable
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
from threading import Thread
from typing import Any

from app.models import AnalysisResult, RepairStrategy, ColorConversionPlan, RepairPlan, MAX_SOFT_DRIFT_PPM
from app.color import OUTPUT_PIXEL_FORMAT, SDR_MATRICES, SDR_TRANSFERS, COLOR_PRIMARIES
from app.frame_validation import FRAME_MONITOR_FILTER, FRAME_MONITOR_NAME


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_BIN = PROJECT_ROOT / "tools" / "ffmpeg" / "bin"
VIDEO_PIXEL_FORMAT = OUTPUT_PIXEL_FORMAT
AUDIO_ASYNC = 1
AUDIO_MIN_HARD_COMP_SECONDS = 0.1
MAX_RUNNING_PROGRESS = 99
COLOR_PROBE_PACKETS = 32
OUTPUT_PRESETS = ("general", "bilibili")
DEFAULT_OUTPUT_PRESET = "general"
BILIBILI_SAMPLE_RATE = 48000
# GUI 经 pythonw 启动时也不为每次探测/转码弹出控制台；其他平台保持默认。
SUBPROCESS_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class MediaError(Exception):
    """可向用户直接展示的环境或媒体读取错误。"""


class MissingToolError(MediaError):
    """保留工具名称，让界面识别缺失依赖，不依赖解析错误文案。"""

    def __init__(self, name: str):
        self.name = name
        super().__init__(
            f"未找到 {name}。请安装包含 ffmpeg 和 ffprobe 的 FFmpeg，"
            f"将 bin 目录加入 PATH，或将工具放入 {LOCAL_BIN}。"
        )


def _is_windows_batch(executable: str) -> bool:
    return os.name == "nt" and Path(executable).suffix.lower() in {".bat", ".cmd"}


def _check_executable(executable: str) -> None:
    if _is_windows_batch(executable):
        raise MediaError(
            f"为避免文件名被 Windows shell 解释为命令，不执行批处理包装器：{executable}。"
            f"请将 FFmpeg / ffprobe 原生 .exe 加入 PATH，或放入 {LOCAL_BIN}。"
        )


def find_tool(name: str) -> str:
    executable = shutil.which(name)
    batch_wrapper = None
    if executable and _is_windows_batch(executable):
        batch_wrapper = executable
        executable = shutil.which(f"{name}.exe")
    if executable:
        _check_executable(executable)
        return executable
    local = LOCAL_BIN / (f"{name}.exe" if os.name == "nt" else name)
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    if batch_wrapper:
        _check_executable(batch_wrapper)
    raise MissingToolError(name)


def run_command(command: list[str], timeout: float) -> str:
    _check_executable(command[0])
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=SUBPROCESS_CREATION_FLAGS,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{Path(command[0]).name} 执行超时（{timeout:g} 秒）。") from exc
    except OSError as exc:
        raise MediaError(f"无法运行 {Path(command[0]).name}：{exc}") from exc
    if result.returncode:
        detail = result.stderr.strip()[-2000:] or "工具未返回错误详情，请检查文件是否可读、是否损坏及工具是否可用"
        raise MediaError(
            f"{Path(command[0]).name} 执行失败（退出码 {result.returncode}）：{detail}"
        )
    return result.stdout


def check_tools() -> dict[str, str]:
    """检查两个工具都存在且可以启动；不执行任何转码。"""
    paths = {name: find_tool(name) for name in ("ffmpeg", "ffprobe")}
    for executable in paths.values():
        run_command([executable, "-version"], timeout=10)
    return paths


def probe_media(path: Path, ffprobe: str, timeout: float = 60) -> dict[str, Any]:
    # 使用绝对路径，避免以 '-' 开头的文件名被当成选项。
    # Limit decoded opening evidence by packet count. SEI may exist only on frames,
    # and an empty sample must never be treated as proof of full-file SDR.
    output = run_command(
        [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format",
         "-show_streams", "-show_frames", "-read_intervals", f"%+#{COLOR_PROBE_PACKETS}",
         "-show_entries", "frame=stream_index,color_transfer:frame_side_data=side_data_type",
         str(path.resolve())],
        timeout=timeout,
    )
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise MediaError("ffprobe 返回了无效的 JSON，无法读取媒体信息。") from exc
    if not isinstance(data, dict):
        raise MediaError("ffprobe 返回的媒体信息格式无效。")
    if not isinstance(data.get("streams", []), list) or not isinstance(data.get("format", {}), dict):
        raise MediaError("ffprobe 返回的轨道或容器信息格式无效。")
    return data


def build_fix_command(
    ffmpeg: str, source: AnalysisResult, output_path: Path, target_fps: int | None = None,
    *, strategy: RepairStrategy | None = None, preset: str = DEFAULT_OUTPUT_PRESET,
) -> list[str]:
    """Legacy adapter; new code calls build_repair_command with a resolved plan."""
    from app.repair_plan import plan_from_strategy
    if strategy is None:
        strategy = RepairStrategy("cfr", True, False, (), target_fps)
    plan = plan_from_strategy(source, strategy, preset=preset)
    return build_repair_command(ffmpeg, plan, output_path)


def build_repair_command(ffmpeg: str, plan: RepairPlan, output_path: Path) -> list[str]:
    """Translate the resolved plan to argv; never analyze media or select a mode/preset."""
    if plan.input_path.resolve() == output_path.resolve():
        raise MediaError("输出路径不能与输入路径相同。")
    video = plan.video
    target_fps = video.target_fps
    cfr = video.fps_mode == "cfr"
    timestamp = video.timestamp_strategy == "regenerate_missing_pts"
    normalize = video.timestamp_strategy != "preserve"
    validate_repair_plan(plan)
    color_filters, color_options = build_color_parameters(video.color_conversion)
    # 默认 FFmpeg 可能容忍损坏包并返回成功，不能把丢失内容的输出当作修复成功。
    command = [ffmpeg, "-hide_banner", "-loglevel", "info", "-xerror", "-nostdin", "-n"]
    if timestamp:
        # genpts 只补缺失 PTS，不会修复所有不单调时间戳。
        command.extend(["-fflags", "+genpts"])
    if not video.autorotate:
        # 保持编码宽高，旋转仍由 MP4 显示矩阵表达，不把旋转烘焙进像素。
        command.append("-noautorotate")
    command.extend([
        "-i", str(plan.input_path.resolve()),
        "-map", f"0:{video.stream_index}" if video.stream_index is not None else "0:V:0",
    ])
    for position, audio in enumerate(plan.audios):
        command.extend(["-map", f"0:{audio.stream_index}" if audio.stream_index is not None else f"0:a:{position}"])
    video_filters = [FRAME_MONITOR_FILTER]
    if cfr:
        video_filters.extend([
            "setpts=PTS-STARTPTS", f"fps=fps={target_fps}",
            f"setpts=N/({target_fps}*TB)+{video.start_offset:.9f}/TB",
        ])
    elif normalize:
        video_filters.append(f"setpts=PTS-STARTPTS+{video.start_offset:.9f}/TB")
    # 先转换范围再补边，避免把 Limited 黑边再按 Full 压缩成灰边。
    video_filters.extend(color_filters)
    if video.pad_to_even:
        video_filters.append("pad=ceil(iw/2)*2:ceil(ih/2)*2")
    command.extend([
        "-vf", ",".join(video_filters),
        # fps 滤镜已强制 CFR。后级保留滤镜生成的时间戳，避免 cfr 输出同步
        # 将晚开始的视频补帧至零，从而抹掉原本合法的音视频起始偏移。
        "-fps_mode:v", "passthrough",
        "-c:v", {"h264": "libx264"}[video.codec], "-preset", video.encoder_preset, "-crf", str(video.crf),
        "-pix_fmt", video.pixel_format,
    ])
    command.extend(color_options)
    if not cfr:
        # 不强制 CFR 的模式保留原帧间隔，避免编码器按平均 FPS 量化时间戳。
        command.extend(["-enc_time_base:v", "filter"])
    if plan.audios:
        # Compact identical settings without emitting conflicting global/track options.
        for flag, values in (("-c:a", [a.codec for a in plan.audios]),
                             ("-b:a", [a.bitrate for a in plan.audios]),
                             ("-ar", [a.sample_rate for a in plan.audios])):
            if len(set(values)) == 1:
                if values[0] is not None:
                    command.extend([flag, str(values[0])])
            else:
                for position, value in enumerate(values):
                    if value is not None:
                        track_flag = "-ar:a" if flag == "-ar" else flag
                        command.extend([f"{track_flag}:{position}", str(value)])
        for position, audio in enumerate(plan.audios):
            audio_filters = []
            if normalize:
                audio_filters.append("asetpts=PTS-STARTPTS")
            if audio.sync_strategy == "async_gaps":
                # 必须保留原 PTS 间隔供 async 比较；不能在它之前用 N/SR/TB
                # 抹掉漂移证据。async=1 + max_soft_comp=0 只补缺口/裁重叠，
                # 不按两轨总时长比例伸缩、不通过变速改变音调。
                audio_filters.append(
                    f"aresample=async={AUDIO_ASYNC}:min_hard_comp={AUDIO_MIN_HARD_COMP_SECONDS}:max_soft_comp=0"
                )
            elif audio.sync_strategy == "async_soft":
                # Explicit factor overrides swr's async-derived default. INT_MAX
                # disables hard fill/trim; only tiny continuous compensation remains.
                samples_per_second = max(2.0, audio.sample_rate * audio.max_soft_compensation)
                audio_filters.append(
                    f"aresample=async={samples_per_second:g}:min_comp=0.001:"
                    f"min_hard_comp=2147483647:max_soft_comp={audio.max_soft_compensation:g}:comp_duration=1"
                )
            if normalize:
                audio_filters.append(f"asetpts=PTS+{audio.start_offset:.9f}/TB")
            if audio_filters:
                command.extend([f"-filter:a:{position}", ",".join(audio_filters)])
    else:
        command.append("-an")
    if timestamp:
        # 只消除开头的负 PTS/DTS；各轨整体平移同样的量，保留相对偏移。
        # 不使用 start_at_zero：它依赖 copyts，而本流程不保留原绝对时间戳。
        command.extend(["-avoid_negative_ts", "make_non_negative"])
    command.extend(["-map_chapters", "-1"])
    if plan.container.faststart:
        command.extend(["-movflags", "+faststart"])
    command.extend([
        "-progress", "pipe:1", "-stats_period", "0.5", "-nostats",
        "-f", plan.container.format, str(output_path.resolve()),
    ])
    return command


def validate_repair_plan(plan: RepairPlan) -> None:
    """Reject unrepresentable/conflicting plans rather than silently changing policy."""
    video = plan.video
    if video.fps_mode not in {"cfr", "preserve"}:
        raise MediaError("修复计划包含无效 fps_mode。")
    if video.fps_mode == "cfr":
        if type(video.target_fps) is not int or video.target_fps <= 0:
            raise MediaError("目标 FPS 必须为正整数。")
    elif video.target_fps is not None:
        raise MediaError("保持帧率的计划不能指定目标 FPS。")
    if video.timestamp_strategy not in {"preserve", "normalize", "regenerate_missing_pts"}:
        raise MediaError("修复计划包含无效 timestamp_strategy。")
    if video.timestamp_strategy == "preserve" and (
        video.fps_mode == "cfr" or any(a.sync_strategy != "preserve" for a in plan.audios)
    ):
        raise MediaError("CFR / async 计划需要明确的时间轴归一化策略。")
    if video.codec != "h264" or video.pixel_format != "yuv420p" or plan.container.format != "mp4":
        raise MediaError("当前 builder 仅支持 H.264 / yuv420p / MP4 计划。")
    if video.color_conversion.pixel_format != video.pixel_format:
        raise MediaError("视频与颜色计划的像素格式冲突。")
    color = video.color_conversion
    expected_input_range = {"full_to_limited": "pc", "preserve_limited": "tv"}.get(color.action)
    if expected_input_range is None or color.input_range != expected_input_range or color.output_range != "tv":
        raise MediaError("颜色计划的转换动作与输入/输出范围冲突。")
    for name, allowed in (("color_space", SDR_MATRICES), ("color_transfer", SDR_TRANSFERS),
                          ("color_primaries", COLOR_PRIMARIES)):
        value = getattr(color, name)
        if value is not None and value not in allowed:
            raise MediaError(f"颜色计划包含不支持的 {name}。")
    for stream in (video, *plan.audios):
        try:
            valid_offset = (type(stream.start_offset) in (int, float)
                            and stream.start_offset >= 0 and math.isfinite(stream.start_offset))
        except OverflowError:
            valid_offset = False
        if not valid_offset:
            raise MediaError("计划的相对起点必须为有限非负值。")
        if stream.stream_index is not None and (type(stream.stream_index) is not int or stream.stream_index < 0):
            raise MediaError("计划包含无效流编号。")
    for audio in plan.audios:
        if audio.codec != "aac" or audio.sync_strategy not in {"preserve", "async_gaps", "async_soft"}:
            raise MediaError("当前 builder 不支持该音频计划。")
        if audio.sample_rate is not None and (type(audio.sample_rate) is not int or audio.sample_rate <= 0):
            raise MediaError("音频采样率必须为正整数。")
        if audio.sync_strategy == "async_soft":
            if (not audio.sample_rate or type(audio.max_soft_compensation) not in (int, float)
                    or not 0 < audio.max_soft_compensation <= MAX_SOFT_DRIFT_PPM / 1e6):
                raise MediaError(f"软补偿计划需要采样率与不超过 {MAX_SOFT_DRIFT_PPM:g} ppm 的补偿比例。")
        elif audio.max_soft_compensation != 0:
            raise MediaError("未选择软补偿，却提供了软补偿比例。")


def build_color_parameters(plan: ColorConversionPlan) -> tuple[list[str], list[str]]:
    """范围变换用 scale；setparams 只在数值已正确时标记帧，编码标签与帧保持一致。"""
    if plan.action not in {"full_to_limited", "preserve_limited"}:
        raise MediaError(plan.reason)
    filters = []
    if plan.action == "full_to_limited":
        scale = "scale=w=iw:h=ih:in_range=full:out_range=limited"
        if plan.color_space:
            matrix = {"bt470bg": "bt601", "bt2020nc": "bt2020"}.get(plan.color_space, plan.color_space)
            scale += f":in_color_matrix={matrix}:out_color_matrix={matrix}"
        filters.extend([scale, f"format={plan.pixel_format}"])
    frame_tags = ["range=limited"]
    # x264 可能省略全为默认值的 VUI；显式写入范围位，保持其他未知标签未知。
    # 此处仅同步已转换好的像素标签，不能替代前面的 scale 数值转换。
    options = ["-color_range", plan.output_range, "-bsf:v", "h264_metadata=video_full_range_flag=0"]
    for field, flag in (("color_space", "colorspace"), ("color_transfer", "color_trc"), ("color_primaries", "color_primaries")):
        value = getattr(plan, field)
        if value is not None:
            # setparams 使用旧的 gamma 别名，编码选项继续使用 ffprobe 的名称。
            frame_value = {"gamma22": "bt470m", "gamma28": "bt470bg"}.get(value, value) if field == "color_transfer" else value
            frame_tags.append(f"{flag}={frame_value}")
            options.extend([f"-{flag}", value])
    filters.append("setparams=" + ":".join(frame_tags))
    return filters, options


def progress_seconds(line: str) -> float | None:
    """解析机器进度及传统 time=HH:MM:SS.xx；无效值不当作零。"""
    key, separator, value = line.strip().partition("=")
    try:
        if separator and key == "out_time_us":
            seconds = int(value) / 1_000_000
        else:
            match = re.search(r"(?:^|\s)(?:out_time|time)=\s*(-?)(\d+):([0-5]\d):([0-5]\d(?:\.\d+)?)(?=\s|$)", line)
            if match is None:
                return None
            sign, hours, minutes, seconds_text = match.groups()
            seconds = (int(hours) * 3600 + int(minutes) * 60 + float(seconds_text)) * (-1 if sign else 1)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except (ValueError, OverflowError):
        return None


def progress_percent(seconds: float, total: float | None) -> int | None:
    """CLI 和 GUI 共用估计百分比；100% 留给输出复查与发布成功后显示。"""
    if total is None or not math.isfinite(total) or total <= 0 or not math.isfinite(seconds):
        return None
    if seconds <= 0:
        return 0
    # 先比较再除，避免异常小的总时长让有限数相除溢出为 inf。
    if seconds >= total:
        return MAX_RUNNING_PROGRESS
    return min(MAX_RUNNING_PROGRESS, int(seconds / total * 100))


def run_ffmpeg(command: list[str], on_progress: Callable[[float], None] | None = None, *, input_validator=None) -> None:
    """同时读取进度和 stderr，避免长转码堵塞管道或将日志全部存入内存。"""
    _check_executable(command[0])
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=SUBPROCESS_CREATION_FLAGS,
        )
    except OSError as exc:
        raise MediaError(f"无法启动 FFmpeg：{exc}") from exc
    errors: deque[str] = deque(maxlen=80)
    validation_errors: deque[str] = deque(maxlen=1)

    def read_errors() -> None:
        try:
            while line := process.stderr.readline(16384):
                # Per-frame diagnostics must not evict the decoder's actual error.
                # They are consumed below, not retained as the error log tail.
                if not line.startswith("[" + FRAME_MONITOR_NAME + " @"):
                    errors.append(line.rstrip()[-1000:])
                if input_validator is not None:
                    input_validator.consume(line)
        except Exception as exc:
            validation_errors.append(f"输入帧检查/日志读取失败：{exc}")
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass  # Main-thread finally also reaps the process.

    reader: Thread | None = None
    try:
        try:
            reader = Thread(target=read_errors, daemon=True)
            reader.start()
        except RuntimeError as exc:
            raise MediaError(f"无法启动 FFmpeg 日志读取线程：{exc}") from exc
        for line in process.stdout:
            seconds = progress_seconds(line)
            if seconds is not None and on_progress is not None:
                on_progress(seconds)
        returncode = process.wait()
        reader.join()
        if validation_errors:
            raise MediaError(validation_errors[0])
        if returncode:
            detail = "\n".join(errors)[-4000:] or "FFmpeg 未返回错误详情"
            raise MediaError(f"FFmpeg 修复失败（退出码 {returncode}）：\n{detail}")
        if input_validator is not None:
            try:
                input_validator.finish()
            except ValueError as exc:
                raise MediaError(str(exc)) from exc
    finally:
        # Ctrl+C、回调异常和执行失败都必须回收进程后再清理临时输出。
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        # 线程创建/启动也在保护范围内；未启动的线程不能 join。
        if reader is not None and reader.ident is not None:
            reader.join()
        process.stdout.close()
        process.stderr.close()
