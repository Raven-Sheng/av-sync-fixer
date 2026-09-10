"""只验证新输入分析，不对真实用户录屏执行修复。"""

import hashlib

import pytest

from app.analyzer import analyze
from app.ffmpeg_utils import MissingToolError, check_tools, run_command
from app.media_profile import format_media_profile
from app.compatibility import diagnose_compatibility
from app.models import CompatibilitySeverity
from tests.media_helpers import generate_media


@pytest.fixture(scope="module")
def hevc_tools():
    try:
        tools = check_tools()
    except MissingToolError as exc:
        pytest.skip(str(exc))
    if "libx265" not in run_command([tools["ffmpeg"], "-hide_banner", "-encoders"], timeout=10):
        pytest.skip("FFmpeg 构建未包含 libx265，无法生成 HEVC 分析样本")
    return tools


@pytest.mark.parametrize("color_range,label,pixel_format", [("pc", "Full", "yuvj420p"), ("tv", "Limited", "yuv420p")])
def test_real_hevc_input_profile_and_range(hevc_tools, tmp_path, color_range, label, pixel_format):
    path = tmp_path / f"HEVC 中文 {color_range}.mp4"
    generate_media(hevc_tools["ffmpeg"], path, [
        "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=60:duration=1",
        "-f", "lavfi", "-i", "sine=sample_rate=48000:duration=1",
        "-vf", f"scale=in_range=tv:out_range={color_range},format=yuv420p",
        "-c:v", "libx265", "-preset", "ultrafast", "-x265-params",
        "pools=1:frame-threads=1:log-level=error:colorprim=bt709:transfer=bt709:colormatrix=bt709",
        "-color_range", color_range, "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-c:a", "aac", "-ac", "2",
    ])
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    info = analyze(path)
    video, audio = info.videos[0], info.audios[0]
    assert video.codec == "hevc" and video.profile == "Main"
    assert video.color_range == color_range and video.color_range_name == label
    # 新版 ffprobe 也可能把 Full 表示为 yuv420p + pc；范围语义单独断言。
    assert video.pixel_format in {pixel_format, "yuv420p"}
    assert video.color_space == video.color_transfer == video.color_primaries == "bt709"
    assert video.avg_frame_rate == video.r_frame_rate == 60
    assert video.start_pts == 0 and video.duration_ts is not None and video.nb_frames == 60
    assert audio.codec == "aac" and audio.profile == "LC" and audio.sample_rate == 48000
    assert audio.channels == 2 and audio.channel_layout == "stereo"
    assert video.is_default and audio.is_default
    assert label in format_media_profile(info)
    findings = {item.code: item for item in diagnose_compatibility(info).findings if item.scope == "video:0"}
    assert findings["video.codec"].severity is CompatibilitySeverity.INFO
    assert findings["video.bit_depth"].evidence["interpreted_depth"] == 8
    assert findings["video.color_range"].severity is (
        CompatibilitySeverity.REPAIR_RECOMMENDED if color_range == "pc" else CompatibilitySeverity.INFO)
    assert findings["video.dynamic_range"].severity is CompatibilitySeverity.INFO
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original


@pytest.mark.parametrize("transfer,hdr", [("bt709", False), ("smpte2084", True), ("arib-std-b67", True)])
def test_real_10bit_hevc_transfer_tags(hevc_tools, tmp_path, transfer, hdr):
    # 合成输入验证标签解析和风险规则，不代表生成了真实 HDR 场景或验证了色调映射。
    path = tmp_path / f"10bit {transfer}.mp4"
    generate_media(hevc_tools["ffmpeg"], path, [
        "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=1",
        "-vf", "format=yuv420p10le", "-c:v", "libx265", "-preset", "ultrafast",
        "-x265-params", f"pools=1:frame-threads=1:log-level=error:colorprim=bt2020:transfer={transfer}:colormatrix=bt2020nc",
        "-color_range", "tv", "-colorspace", "bt2020nc", "-color_primaries", "bt2020", "-color_trc", transfer,
    ])
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    info = analyze(path)
    assert info.videos[0].pixel_format == "yuv420p10le"
    assert info.videos[0].color_transfer == transfer
    findings = {item.code: item for item in diagnose_compatibility(info).findings if item.scope == "video:0"}
    assert findings["video.bit_depth"].evidence["interpreted_depth"] == 10
    assert findings["video.colorimetry"].severity is CompatibilitySeverity.WARNING
    assert findings["video.dynamic_range"].severity is (
        CompatibilitySeverity.UNSUPPORTED if hdr else CompatibilitySeverity.INFO)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original
