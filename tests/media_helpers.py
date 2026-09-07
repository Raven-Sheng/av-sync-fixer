"""合成媒体与逐帧读取的共享测试工具，不参与产品修复逻辑。"""

import json
from pathlib import Path

from app.ffmpeg_utils import find_tool, run_command


MEDIA_COMMAND_TIMEOUT_SECONDS = 30


def generate_media(ffmpeg: str, path: Path, options: list[str]) -> None:
    run_command([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
        *options, str(path.resolve()),
    ], timeout=MEDIA_COMMAND_TIMEOUT_SECONDS)


def frame_times(path: Path) -> list[float]:
    output = run_command([
        find_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(path.resolve()),
    ], timeout=MEDIA_COMMAND_TIMEOUT_SECONDS)
    return [float(frame["best_effort_timestamp_time"]) for frame in json.loads(output)["frames"]]
