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

from app.models import AnalysisResult, RepairStrategy


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_BIN = PROJECT_ROOT / "tools" / "ffmpeg" / "bin"
VIDEO_CODEC = "libx264"
VIDEO_PRESET = "medium"
VIDEO_CRF = 18
VIDEO_PIXEL_FORMAT = "yuv420p"
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "192k"
AUDIO_ASYNC = 1
AUDIO_MIN_HARD_COMP_SECONDS = 0.1
MAX_RUNNING_PROGRESS = 99
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
    output = run_command(
        [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format",
         "-show_streams", str(path.resolve())],
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
    """仅构建选中策略的参数。旧式传入 target_fps 的调用表示显式 CFR。"""
    if not source.videos:
        raise MediaError("没有可修复的视频轨（封面图片不算视频）。")
    if preset not in OUTPUT_PRESETS:
        raise MediaError(f"不支持的输出预设：{preset}")
    if strategy is None:
        strategy = RepairStrategy("cfr", True, False, (), target_fps)
    target_fps = strategy.target_fps
    if strategy.cfr and (type(target_fps) is not int or target_fps <= 0):
        raise MediaError("目标 FPS 必须为正整数。")
    if any(type(index) is not int or not 0 <= index < len(source.audios) for index in strategy.audio_sync_tracks):
        raise MediaError("音频同步策略包含无效音轨编号。")
    if source.path.resolve() == output_path.resolve():
        raise MediaError("输出路径不能与输入路径相同。")
    video = source.videos[0]
    if preset == "bilibili":
        if not strategy.cfr:
            raise MediaError("Bilibili 预设必须启用 CFR。")
        if video.width is None or video.height is None:
            raise MediaError("Bilibili 预设无法确认原始分辨率，不能验证保持原尺寸。")
        if video.width % 2 or video.height % 2:
            raise MediaError(
                f"原始分辨率 {video.width} × {video.height} 含奇数边长，"
                f"无法同时保持原尺寸并使用 {VIDEO_CODEC} / {VIDEO_PIXEL_FORMAT}；Bilibili 预设不会自动缩放、裁剪或补边。"
            )
    streams = (video, *source.audios)
    # 同时已知所有起点时，保留它们相对于最早轨道的偏移。
    starts = [stream.start_time for stream in streams]
    offsets = [0.0] * len(streams)
    if all(start is not None for start in starts):
        origin = min(starts)
        offsets = [start - origin for start in starts]
    # 默认 FFmpeg 可能容忍损坏包并返回成功，不能把丢失内容的输出当作修复成功。
    command = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-xerror", "-nostdin", "-n"]
    if strategy.timestamp:
        # genpts 只补缺失 PTS，不会修复所有不单调时间戳。
        command.extend(["-fflags", "+genpts"])
    if preset == "bilibili":
        # 保持编码宽高，旋转仍由 MP4 显示矩阵表达，不把旋转烘焙进像素。
        command.append("-noautorotate")
    command.extend([
        "-i", str(source.path.resolve()),
        "-map", f"0:{video.index}" if video.index is not None else "0:V:0",
    ])
    for position, audio in enumerate(source.audios):
        command.extend(["-map", f"0:{audio.index}" if audio.index is not None else f"0:a:{position}"])
    normalize = strategy.cfr or strategy.timestamp or bool(strategy.audio_sync_tracks)
    video_filters = []
    if strategy.cfr:
        video_filters.extend([
            "setpts=PTS-STARTPTS", f"fps=fps={target_fps}",
            f"setpts=N/({target_fps}*TB)+{offsets[0]:.9f}/TB",
        ])
    elif normalize:
        video_filters.append(f"setpts=PTS-STARTPTS+{offsets[0]:.9f}/TB")
    if preset != "bilibili":
        video_filters.append("pad=ceil(iw/2)*2:ceil(ih/2)*2")
    command.extend([
        "-vf", ",".join(video_filters),
        # fps 滤镜已强制 CFR。后级保留滤镜生成的时间戳，避免 cfr 输出同步
        # 将晚开始的视频补帧至零，从而抹掉原本合法的音视频起始偏移。
        "-fps_mode:v", "passthrough",
        "-c:v", VIDEO_CODEC, "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF),
        "-pix_fmt", VIDEO_PIXEL_FORMAT,
    ])
    if not strategy.cfr:
        # 不强制 CFR 的模式保留原帧间隔，避免编码器按平均 FPS 量化时间戳。
        command.extend(["-enc_time_base:v", "filter"])
    if source.audios:
        command.extend(["-c:a", AUDIO_CODEC, "-b:a", AUDIO_BITRATE])
        if preset == "bilibili":
            command.extend(["-ar", str(BILIBILI_SAMPLE_RATE)])
        for position in range(len(source.audios)):
            audio_filters = []
            if normalize:
                audio_filters.append("asetpts=PTS-STARTPTS")
            if position in strategy.audio_sync_tracks:
                # 必须保留原 PTS 间隔供 async 比较；不能在它之前用 N/SR/TB
                # 抹掉漂移证据。async=1 + max_soft_comp=0 只补缺口/裁重叠，
                # 不按两轨总时长比例伸缩、不通过变速改变音调。
                audio_filters.append(
                    f"aresample=async={AUDIO_ASYNC}:min_hard_comp={AUDIO_MIN_HARD_COMP_SECONDS}:max_soft_comp=0"
                )
            if normalize:
                audio_filters.append(f"asetpts=PTS+{offsets[position + 1]:.9f}/TB")
            if audio_filters:
                command.extend([f"-filter:a:{position}", ",".join(audio_filters)])
    else:
        command.append("-an")
    if strategy.timestamp:
        # 只消除开头的负 PTS/DTS；各轨整体平移同样的量，保留相对偏移。
        # 不使用 start_at_zero：它依赖 copyts，而本流程不保留原绝对时间戳。
        command.extend(["-avoid_negative_ts", "make_non_negative"])
    command.extend([
        "-map_chapters", "-1", "-movflags", "+faststart",
        "-progress", "pipe:1", "-stats_period", "0.5", "-nostats",
        "-f", "mp4", str(output_path.resolve()),
    ])
    return command


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


def run_ffmpeg(command: list[str], on_progress: Callable[[float], None] | None = None) -> None:
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

    def read_errors() -> None:
        for line in process.stderr:
            errors.append(line.rstrip()[-1000:])

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
        if returncode:
            detail = "\n".join(errors)[-4000:] or "FFmpeg 未返回错误详情"
            raise MediaError(f"FFmpeg 修复失败（退出码 {returncode}）：\n{detail}")
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
