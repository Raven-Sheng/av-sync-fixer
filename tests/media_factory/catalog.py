from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


PRESETS = ("general", "bilibili")
CATEGORIES = {
    "A": "H264 / yuv420p / limited / CFR", "B": "H264 / yuv420p / full (literal pix_fmt)",
    "C": "HEVC / yuv420p / limited", "D": "HEVC / yuvj420p / full (literal pix_fmt)",
    "E": "30 FPS", "F": "60 FPS", "G": "59.94 FPS (60000/1001)", "H": "VFR (frame timestamps)",
    "I": "Audio shorter than video", "J": "Audio longer than video", "K": "No audio",
    "L": "44.1 kHz AAC", "M": "48 kHz AAC", "N": "Chinese filename", "O": "Path contains spaces",
}


@dataclass(frozen=True)
class MediaCase:
    id: str
    codec: str = "h264"
    full: bool = False
    fps: str = "30"
    audio_seconds: float | None = 4.0
    sample_rate: int = 48000
    vfr: bool = False
    filename: str = "input.mp4"
    seconds: float = 4.0


CASES = (
    MediaCase("h264_limited_30", filename="中文 录屏.mp4"),
    MediaCase("h264_full_60", full=True, fps="60", sample_rate=44100),
    MediaCase("hevc_limited_5994", codec="hevc", fps="60000/1001"),
    MediaCase("hevc_full_60", codec="hevc", full=True, fps="60"),
    MediaCase("vfr", fps="60", vfr=True),
    MediaCase("audio_shorter", audio_seconds=3.0),
    MediaCase("audio_longer", audio_seconds=5.0, sample_rate=44100),
    MediaCase("silent", audio_seconds=None),
)
MEDIA_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm", ".ts", ".mts", ".m2ts", ".nut"})


def discover_private(directory: Path) -> tuple[Path, ...]:
    if not directory.is_dir():
        return ()
    return tuple(sorted((p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS),
                        key=lambda p: str(p).casefold()))


def observed_categories(info, *, cadence: str | None) -> list[str]:
    """Observed fields only: a requested generator format is not proof of coverage."""
    result = []
    v = info.videos[0] if info.videos else None
    if v:
        if (v.codec, v.pixel_format, v.color_range) == ("h264", "yuv420p", "tv") and cadence == "CFR":
            result.append("A")
        for key, expected in (("B", ("h264", "yuv420p", "pc")), ("C", ("hevc", "yuv420p", "tv")),
                              ("D", ("hevc", "yuvj420p", "pc"))):
            if (v.codec, v.pixel_format, v.color_range) == expected:
                result.append(key)
        for key, rate in (("E", Fraction(30)), ("F", Fraction(60)), ("G", Fraction(60000, 1001))):
            if v.avg_frame_rate == rate and cadence == "CFR":
                result.append(key)
        if cadence == "VFR":
            result.append("H")
        if v.duration is not None:
            for audio in info.audios:
                if audio.duration is not None and audio.duration < v.duration - 0.2:
                    result.append("I")
                if audio.duration is not None and audio.duration > v.duration + 0.2:
                    result.append("J")
    if not info.audios and info.stream_list_complete:
        result.append("K")
    for key, rate in (("L", 44100), ("M", 48000)):
        if any(a.codec == "aac" and a.sample_rate == rate for a in info.audios):
            result.append(key)
    if any("\u4e00" <= char <= "\u9fff" for char in info.path.name):
        result.append("N")
    if " " in str(info.path):
        result.append("O")
    return sorted(set(result))
