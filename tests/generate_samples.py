"""运行 python -m tests.generate_samples，生成小型、可复现的人工样本。"""

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from app.cli import configure_output_encoding
from app.ffmpeg_utils import MediaError, PROJECT_ROOT, check_tools, probe_media
from tests.media_helpers import generate_media


SAMPLE_DIRECTORY = PROJECT_ROOT / "tests" / "generated_samples"
VIDEO_DURATION_SECONDS = 6.0
SAMPLE_FPS = 30
SAMPLE_RESOLUTION = (320, 180)
SAMPLE_RATE = 48000
AUDIO_LENGTH_OFFSET_SECONDS = 0.3


@dataclass(frozen=True)
class SampleSpec:
    name: str
    label: str
    audio_duration: float | None
    variable_frame_rate: bool = False
    expected_cfr_fps: int = SAMPLE_FPS


SAMPLES = (
    SampleSpec("cfr", "正常 CFR", VIDEO_DURATION_SECONDS),
    SampleSpec("vfr", "疑似 VFR", VIDEO_DURATION_SECONDS, True, 24),
    SampleSpec("audio_longer", "音频略长", VIDEO_DURATION_SECONDS + AUDIO_LENGTH_OFFSET_SECONDS),
    SampleSpec("audio_shorter", "音频略短", VIDEO_DURATION_SECONDS - AUDIO_LENGTH_OFFSET_SECONDS),
    SampleSpec("no_audio", "没有音频", None),
)


def sample_options(spec: SampleSpec) -> list[str]:
    width, height = SAMPLE_RESOLUTION
    options = [
        "-f", "lavfi", "-i",
        f"testsrc2=size={width}x{height}:rate={SAMPLE_FPS}:duration={VIDEO_DURATION_SECONDS:g}",
    ]
    if spec.audio_duration is not None:
        options.extend(["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={SAMPLE_RATE}:duration={spec.audio_duration:g}"])
    else:
        options.append("-an")
    if spec.variable_frame_rate:
        # 前半段每隔一帧保留一帧，后半段全部保留；不能 setpts 抹平间隔。
        options.extend([
            "-vf", f"select=if(lt(t\\,{VIDEO_DURATION_SECONDS / 2:g})\\,not(mod(n\\,2))\\,1)",
            "-fps_mode:v", "vfr",
        ])
    options.extend([
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p", "-color_range", "tv",
        "-bsf:v", "h264_metadata=video_full_range_flag=0",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", "-f", "mp4",
    ])
    return options


def generate_samples(directory: str | Path = SAMPLE_DIRECTORY) -> dict[str, Path]:
    """先生成并探测全部样本，再发布；拒绝覆盖已有文件。"""
    try:
        directory = Path(directory).expanduser().resolve()
        targets = {spec.name: directory / f"{spec.name}.mp4" for spec in SAMPLES}
        for target in targets.values():
            if os.path.lexists(target):
                raise MediaError(f"样本已存在，不会覆盖：{target}。请先移走样本，或使用 --output-dir 指定新目录。")
        executables = check_tools()
        directory.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".generating-", dir=directory) as staging:
            for spec in SAMPLES:
                staged = Path(staging) / targets[spec.name].name
                generate_media(executables["ffmpeg"], staged, sample_options(spec))
                probe_media(staged, executables["ffprobe"])
            for target in targets.values():
                # 与修复输出一样，不用覆盖式 rename；并发出现同名目标会失败。
                os.link(Path(staging) / target.name, target)
        return targets
    except OSError as exc:
        raise MediaError(f"生成样本时文件操作失败（请检查空间、权限及硬链接支持）：{exc}") from exc


def main(argv: list[str] | None = None) -> int:
    configure_output_encoding()
    parser = argparse.ArgumentParser(description="生成五类 6 秒合成测试样本，不覆盖已有文件")
    parser.add_argument("--output-dir", type=Path, default=SAMPLE_DIRECTORY, help="默认 tests/generated_samples（相对于项目位置）")
    args = parser.parse_args(argv)
    try:
        paths = generate_samples(args.output_dir)
        for spec in SAMPLES:
            path = paths[spec.name]
            print(f"{spec.label}：{path}（{path.stat().st_size / 1024:.1f} KiB）")
    except MediaError as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("生成已取消。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
