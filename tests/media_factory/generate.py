from pathlib import Path

from app.ffmpeg_utils import check_tools, run_command
from tests.media_helpers import generate_media
from .pixels import raw_picture, FULL_LEVELS, LIMITED_LEVELS, WIDTH, HEIGHT
from .catalog import MediaCase


class MissingEncoder(RuntimeError):
    pass


class MediaFactory:
    def __init__(self, directory: Path):
        self.directory = directory
        self.tools = check_tools()
        self.encoders = run_command([self.tools["ffmpeg"], "-hide_banner", "-encoders"], 10)
        self.paths = {}

    def create(self, case: MediaCase) -> Path:
        if case.id in self.paths:
            return self.paths[case.id]
        encoder = {"h264": "libx264", "hevc": "libx265"}[case.codec]
        if encoder not in self.encoders:
            raise MissingEncoder(f"生成 {case.id} 需要 {encoder}；该类别 NOT TESTED")
        directory = self.directory / case.id
        directory.mkdir(parents=True)
        raw = directory / "calibration.yuv"
        raw.write_bytes(raw_picture(FULL_LEVELS if case.full else LIMITED_LEVELS))
        path = directory / case.filename
        range_name = "pc" if case.full else "tv"
        filters = (f"[1:v]scale=in_range=tv:out_range={range_name},format=yuv420p[scene];"
                   "[0:v][scene]vstack=inputs=2")
        if case.vfr:
            filters += ",select=if(lt(t\\,2)\\,not(mod(n\\,2))\\,1)"
        filters += f",setparams=range={'full' if case.full else 'limited'}:colorspace=bt709:color_trc=bt709:color_primaries=bt709[video]"
        options = ["-stream_loop", "-1", "-f", "rawvideo", "-pixel_format", "yuv420p",
            "-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", case.fps, "-color_range", range_name,
            "-t", str(case.seconds), "-i", str(raw), "-f", "lavfi", "-i",
            f"testsrc2=size={WIDTH}x{HEIGHT}:rate={case.fps}:duration={case.seconds}"]
        if case.audio_seconds is not None:
            options.extend(["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={case.sample_rate}:duration={case.audio_seconds}"])
        options.extend(["-filter_complex", filters, "-map", "[video]", "-fps_mode", "vfr", "-c:v", encoder,
                        "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-color_range", range_name,
                        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"])
        if case.codec == "hevc":
            options.extend(["-x265-params", "lossless=1:pools=1:frame-threads=1:log-level=error:colorprim=bt709:transfer=bt709:colormatrix=bt709"])
        else:
            options.extend(["-crf", "0", "-bsf:v", f"h264_metadata=video_full_range_flag={int(case.full)}"])
        options.extend(["-map", "2:a", "-c:a", "aac", "-ac", "2"] if case.audio_seconds is not None else ["-an"])
        generate_media(self.tools["ffmpeg"], path, options)
        if path.stat().st_size > 5 * 1024 * 1024:
            raise AssertionError("合成样本超过 5 MiB 预算")
        self.paths[case.id] = path
        return path
