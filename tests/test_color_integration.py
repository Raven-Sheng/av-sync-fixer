"""真实范围转换：验证 ffprobe 标签和 Y/U/V 数值，防止只有标签看起来正确。"""

import hashlib

import pytest

from app.analyzer import analyze
from app.ffmpeg_utils import check_tools, MissingToolError, run_command
from app.fixer import prepare_fix, execute_fix
from tests.media_helpers import generate_media


from tests.media_factory.pixels import (WIDTH, HEIGHT, FULL_LEVELS, LIMITED_LEVELS, PIXEL_TOLERANCE,
                                        raw_picture, decode_native_yuv, patch_means)


@pytest.fixture(scope="module")
def color_tools():
    try:
        return check_tools()
    except MissingToolError as exc:
        pytest.skip(str(exc))


@pytest.fixture(scope="module")
def color_samples(color_tools, tmp_path_factory):
    tools = color_tools
    if "libx265" not in run_command([tools["ffmpeg"], "-hide_banner", "-encoders"], 10):
        pytest.skip("生成 HEVC Full 样本需要 libx265")
    directory = tmp_path_factory.mktemp("full-limited-pixels")
    paths = {}
    for full, levels in ((True, FULL_LEVELS), (False, LIMITED_LEVELS)):
        raw = directory / f"{full}.yuv"
        raw.write_bytes(raw_picture(levels))
        path = directory / f"中文 色阶 {full}.mp4"
        pixel_range = "pc" if full else "tv"
        generate_media(tools["ffmpeg"], path, [
            "-f", "rawvideo", "-pixel_format", "yuv420p", "-video_size", f"{WIDTH}x{HEIGHT}",
            "-framerate", "1", "-color_range", pixel_range, "-i", str(raw),
            "-c:v", "libx265", "-preset", "ultrafast", "-color_range", pixel_range,
            "-x265-params", "lossless=1:pools=1:frame-threads=1:log-level=error:colorprim=bt709:transfer=bt709:colormatrix=bt709",
        ])
        paths[full] = path
    return tools, paths


@pytest.mark.parametrize("full", [True, False])
@pytest.mark.parametrize("preset", ["general", "bilibili"])
def test_actual_range_conversion_preserves_black_white_and_chroma(color_samples, tmp_path, full, preset):
    tools, paths = color_samples
    source = paths[full]
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    before = analyze(source)
    video = before.videos[0]
    assert video.color_range == ("pc" if full else "tv")
    assert video.pixel_format in ({"yuvj420p", "yuv420p"} if full else {"yuv420p"})
    assert video.color_space == video.color_transfer == video.color_primaries == "bt709"
    original_pixels = decode_native_yuv(tools["ffmpeg"], source, video.pixel_format)
    assert original_pixels == raw_picture(FULL_LEVELS if full else LIMITED_LEVELS)
    plan = prepare_fix(before, tmp_path / "out", preset=preset)
    assert plan.color.action == ("full_to_limited" if full else "preserve_limited")
    filters = plan.command[plan.command.index("-vf") + 1]
    assert ("scale=" in filters) is full
    result = execute_fix(plan)
    output = result.after.videos[0]
    assert output.pixel_format == "yuv420p" and output.color_range == "tv"
    assert output.color_space == output.color_transfer == output.color_primaries == "bt709"
    assert (output.width, output.height) == (WIDTH, HEIGHT)
    assert result.validation and not result.validation.errors
    decoded = patch_means(decode_native_yuv(tools["ffmpeg"], result.after.path, "yuv420p"))
    input_means = patch_means(original_pixels)
    for plane, (actual, original) in enumerate(zip(decoded, input_means)):
        span = 219 if plane == 0 else 224
        expected = [16 + value * span / 255 for value in original] if full else original
        assert actual == pytest.approx(expected, abs=PIXEL_TOLERANCE)
    assert decoded[0][0] == pytest.approx(16, abs=PIXEL_TOLERANCE)
    assert decoded[0][-1] == pytest.approx(235, abs=PIXEL_TOLERANCE)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash


def test_tag_only_fake_conversion_is_detected_by_pixel_assertions(color_tools, tmp_path):
    tools = color_tools
    # 反例：仅更改帧/编码标签，会保留 Full 数值；标签合格并不能证明数值转换正确。
    path = tmp_path / "tag-only.mp4"
    raw = tmp_path / "full-values.yuv"
    raw.write_bytes(raw_picture(FULL_LEVELS))
    generate_media(tools["ffmpeg"], path, ["-f", "rawvideo", "-pixel_format", "yuv420p",
        "-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", "1", "-color_range", "tv", "-i", str(raw),
        "-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv420p", "-color_range", "tv",
        "-bsf:v", "h264_metadata=video_full_range_flag=0"])
    info = analyze(path)
    assert info.videos[0].pixel_format == "yuv420p" and info.videos[0].color_range == "tv"
    y = patch_means(decode_native_yuv(tools["ffmpeg"], path, "yuv420p"))[0]
    assert abs(y[0] - 16) > PIXEL_TOLERANCE and abs(y[-1] - 235) > PIXEL_TOLERANCE


@pytest.mark.parametrize("full", [True, False])
def test_known_range_without_colorimetry_stays_explicit_without_invented_bt709(color_tools, tmp_path, full):
    tools = color_tools
    raw = tmp_path / "known-range.yuv"
    raw.write_bytes(raw_picture(FULL_LEVELS if full else LIMITED_LEVELS))
    path = tmp_path / "no-colorimetry.mp4"
    pixel_range = "pc" if full else "tv"
    generate_media(tools["ffmpeg"], path, ["-f", "rawvideo", "-pixel_format", "yuv420p",
        "-video_size", f"{WIDTH}x{HEIGHT}", "-framerate", "1", "-color_range", pixel_range, "-i", str(raw),
        "-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv420p", "-color_range", pixel_range,
        "-bsf:v", f"h264_metadata=video_full_range_flag={int(full)}"])
    before = analyze(path)
    assert before.videos[0].color_range == pixel_range
    assert decode_native_yuv(tools["ffmpeg"], path, before.videos[0].pixel_format) == raw.read_bytes()
    for field in ("color_space", "color_transfer", "color_primaries"):
        assert getattr(before.videos[0], field) is None
    result = execute_fix(prepare_fix(before, tmp_path / "out"))
    video = result.after.videos[0]
    assert video.color_range == "tv" and video.pixel_format == "yuv420p"
    for field in ("color_space", "color_transfer", "color_primaries"):
        assert getattr(video, field) is None
    actual = patch_means(decode_native_yuv(tools["ffmpeg"], result.after.path, "yuv420p"))[0]
    expected = [16 + value * 219 / 255 for value in FULL_LEVELS] if full else LIMITED_LEVELS
    assert actual == pytest.approx(expected, abs=PIXEL_TOLERANCE)
