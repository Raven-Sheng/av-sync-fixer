"""Shared calibration pixels and native-range decoding for media regression."""

import subprocess
from statistics import mean

WIDTH, HEIGHT = 256, 64
FULL_LEVELS = (0, 1, 15, 16, 32, 64, 96, 128, 160, 192, 224, 235, 240, 250, 254, 255)
LIMITED_LEVELS = (16, 17, 24, 32, 48, 64, 96, 126, 160, 192, 210, 220, 230, 233, 234, 235)
PIXEL_TOLERANCE = 3  # CRF 18 有损编码；取均匀色块内部，容纳量化但不能掩盖重复范围压缩。


def raw_picture(levels):
    y = bytes(value for value in levels for _ in range(WIDTH // len(levels))) * HEIGHT
    chroma = bytes(value for value in levels for _ in range(WIDTH // (2 * len(levels)))) * (HEIGHT // 2)
    return y + chroma + chroma[::-1]


def decode_native_yuv(ffmpeg, path, pixel_format):
    # 与探测结果同像素布局，避免测试读取阶段又偷偷做范围变换。
    result = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-frames:v", "1",
        "-pix_fmt", pixel_format, "-f", "rawvideo", "pipe:1"], capture_output=True, check=True, timeout=30)
    return result.stdout


def patch_means(data):
    values = []
    offset = 0
    for width, height in ((WIDTH, HEIGHT), (WIDTH // 2, HEIGHT // 2), (WIDTH // 2, HEIGHT // 2)):
        plane = data[offset:offset + width * height]
        step = width // len(FULL_LEVELS)
        values.append([mean(plane[y * width + x] for y in range(height // 2 - 2, height // 2 + 2)
                            for x in range(i * step + 2, (i + 1) * step - 2)) for i in range(len(FULL_LEVELS))])
        offset += width * height
    return values
