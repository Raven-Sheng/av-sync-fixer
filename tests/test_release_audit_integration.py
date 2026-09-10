"""Small encoded adversarial fixtures; these are not real Steam regression evidence."""

import hashlib
import json
import subprocess

import pytest

from app.analyzer import analyze
from app.compatibility import diagnose_compatibility
from app.ffmpeg_utils import MediaError, MissingToolError, check_tools, run_command
from app.fixer import execute_fix, prepare_fix
from tests.media_helpers import generate_media
from tests.media_factory.pixels import (WIDTH, HEIGHT, FULL_LEVELS, LIMITED_LEVELS,
    PIXEL_TOLERANCE, raw_picture, decode_native_yuv, patch_means)


@pytest.fixture(scope="module")
def audit_tools():
    try:
        tools = check_tools()
    except MissingToolError as exc:
        pytest.skip(str(exc))
    encoders = run_command([tools["ffmpeg"], "-hide_banner", "-encoders"], 10)
    if "libx265" not in encoders or "libx264" not in encoders:
        pytest.skip("NOT TESTED: audit fixtures require libx264 and libx265")
    return tools


@pytest.mark.parametrize("transfer", ["unknown", "bt709", "smpte2084", "arib-std-b67"])
def test_frame_only_hdr_is_read_and_refused_before_encoding(audit_tools, tmp_path, transfer):
    path = tmp_path / "HDR 中文 输入.mp4"
    generate_media(audit_tools["ffmpeg"], path, [
        "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=2",
        "-vf", "format=yuv420p10le", "-c:v", "libx265", "-preset", "ultrafast",
        "-x265-params", f"pools=1:frame-threads=1:log-level=error:colorprim=bt2020:transfer={transfer}:colormatrix=bt2020nc:"
        "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):max-cll=1000,400",
        "-color_range", "tv", "-colorspace", "bt2020nc", "-color_primaries", "bt2020", "-color_trc", transfer])
    digest = hashlib.sha256(path.read_bytes()).digest()
    source = analyze(path)
    assert "Mastering display metadata" in source.videos[0].sampled_hdr_side_data_types
    finding = next(f for f in diagnose_compatibility(source).findings if f.code == "video.dynamic_range")
    assert "Mastering display metadata" in finding.evidence["sampled_hdr_side_data_types"]
    for preset in ("general", "bilibili"):
        with pytest.raises(MediaError, match="HDR"):
            prepare_fix(source, tmp_path / "out", preset=preset)
    assert not (tmp_path / "out").exists()
    assert hashlib.sha256(path.read_bytes()).digest() == digest


@pytest.mark.parametrize("codec", ["libx264", "libx265"])
@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("matrix,primaries,transfer", [("bt709", "bt709", "bt709"), ("bt2020nc", "bt2020", "bt2020-10")])
def test_10bit_sdr_numeric_range_conversion(audit_tools, tmp_path, codec, full, matrix, primaries, transfer):
    levels = FULL_LEVELS if full else LIMITED_LEVELS
    # Known 10-bit numeric pixels, not an 8-bit input merely relabelled as 10-bit.
    to_10 = lambda value: round(value * 1023 / 255) if full else value * 4
    raw = b"".join(to_10(value).to_bytes(2, "little") for value in raw_picture(levels))
    raw_path = tmp_path / "10bit.yuv"
    raw_path.write_bytes(raw)
    path = tmp_path / "10-bit 中文 空格.mp4"
    options = ["-stream_loop", "-1", "-f", "rawvideo", "-pixel_format", "yuv420p10le", "-video_size", f"{WIDTH}x{HEIGHT}",
        "-framerate", "30", "-color_range", "pc" if full else "tv",
        "-colorspace", matrix, "-color_primaries", primaries, "-color_trc", transfer,
        "-i", str(raw_path), "-t", "2",
        "-c:v", codec, "-preset", "ultrafast", "-pix_fmt", "yuv420p10le",
        "-color_range", "pc" if full else "tv", "-colorspace", matrix, "-color_primaries", primaries, "-color_trc", transfer]
    if codec == "libx265":
        options += ["-x265-params", "lossless=1:pools=1:frame-threads=1:log-level=error"]
    else:
        options += ["-qp", "0"]
    generate_media(audit_tools["ffmpeg"], path, options)
    source = analyze(path)
    assert source.videos[0].pixel_format == "yuv420p10le"
    assert decode_native_yuv(audit_tools["ffmpeg"], path, "yuv420p10le") == raw
    original = hashlib.sha256(path.read_bytes()).digest()
    for preset in ("general", "bilibili"):
        result = execute_fix(prepare_fix(source, tmp_path / preset, preset=preset))
        assert not result.validation.errors
        output = result.after.videos[0]
        assert (output.color_space, output.color_primaries, output.color_transfer) == (matrix, primaries, transfer)
        actual = patch_means(decode_native_yuv(audit_tools["ffmpeg"], result.after.path, "yuv420p"))
        for plane, values in enumerate(actual):
            span = 219 if plane == 0 else 224
            expected = [16 + to_10(v) * span / 1023 for v in levels] if full else list(levels)
            if plane == 2:
                expected.reverse()
            assert values == pytest.approx(expected, abs=PIXEL_TOLERANCE)
    assert hashlib.sha256(path.read_bytes()).digest() == original


@pytest.mark.parametrize("preset", ["general", "bilibili"])
def test_mixed_rate_multitrack_preserves_default_language_and_surround(audit_tools, tmp_path, preset):
    path = tmp_path / "多音轨 & 空格.mp4"
    generate_media(audit_tools["ffmpeg"], path, [
        "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2",
        "-f", "lavfi", "-i", "aevalsrc=0.1*sin(2*PI*500*t)|0.1*sin(2*PI*600*t)|0.1*sin(2*PI*700*t)|0.1*sin(2*PI*80*t)|0.1*sin(2*PI*900*t)|0.1*sin(2*PI*1000*t):s=48000:d=2:c=5.1",
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "libx264", "-c:a", "aac",
        "-color_range", "tv", "-colorspace", "bt709", "-color_trc", "bt709", "-color_primaries", "bt709",
        "-disposition:a:0", "0", "-disposition:a:1", "default", "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=zho"])
    source = analyze(path)
    result = execute_fix(prepare_fix(source, tmp_path / "out", preset=preset))
    assert not result.validation.errors
    assert [a.channels for a in result.after.audios] == [1, 6]
    assert [a.channel_layout for a in result.after.audios] == ["mono", "5.1"]
    assert [a.is_default for a in result.after.audios] == [False, True]
    assert [a.metadata.get("language") for a in result.after.audios] == ["eng", "zho"]
    assert [a.sample_rate for a in result.after.audios] == ([48000, 48000] if preset == "bilibili" else [44100, 48000])
    # Decode every mapped output, not just the ffprobe headers.
    subprocess.run([audit_tools["ffmpeg"], "-v", "error", "-xerror", "-nostdin", "-i", str(result.after.path),
        "-map", "0:v", "-map", "0:a", "-f", "null", "-"], capture_output=True, check=True, timeout=30)


@pytest.mark.parametrize("preset", ["general", "bilibili"])
@pytest.mark.parametrize("change", ["range", "matrix", "hdr", "size"])
def test_midstream_input_change_cannot_publish_mislabelled_success(audit_tools, tmp_path, preset, change):
    for i in (0, 1):
        matrix = "bt2020nc" if i and change == "matrix" else "bt709"
        transfer = "smpte2084" if i and change == "hdr" else "bt709"
        pixel_range = "full" if i and change == "range" else "limited"
        size = "192x96" if i and change == "size" else "160x90"
        generate_media(audit_tools["ffmpeg"], tmp_path / f"part{i}.ts", [
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={6 if i == 0 else 2}",
            "-vf", f"setparams=range={pixel_range}:colorspace={matrix}:color_primaries=bt709:color_trc={transfer}",
            "-c:v", "libx264", "-qp", "0", "-g", "30", "-pix_fmt", "yuv420p",
            "-color_range", "pc" if pixel_range == "full" else "tv", "-colorspace", matrix,
            "-color_primaries", "bt709", "-color_trc", transfer])
    listing = tmp_path / "segments.txt"
    listing.write_text("file 'part0.ts'\nfile 'part1.ts'\n", encoding="utf-8")
    path = tmp_path / "中途 变化.mp4"
    generate_media(audit_tools["ffmpeg"], path, ["-f", "concat", "-i", str(listing), "-c", "copy"])
    frames = json.loads(run_command([audit_tools["ffprobe"], "-v", "error", "-show_entries",
        "frame=color_range,color_space,color_transfer,width,height", "-of", "json", str(path)], 30))["frames"]
    field = {"range": "color_range", "matrix": "color_space", "hdr": "color_transfer", "size": "width"}[change]
    assert len({f[field] for f in frames}) == 2  # Prove the encoded fixture really changes.
    original = hashlib.sha256(path.read_bytes()).digest()
    source = analyze(path)
    plan = prepare_fix(source, tmp_path / "out", preset=preset)
    with pytest.raises(MediaError, match="输入帧|中途|HDR"):
        execute_fix(plan)
    assert not plan.output_path.exists()
    assert not list((tmp_path / "out").glob(".avsync-*"))
    assert hashlib.sha256(path.read_bytes()).digest() == original
