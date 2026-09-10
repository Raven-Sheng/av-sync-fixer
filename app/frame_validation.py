"""Validate decoded input properties before filters erase their color evidence.

showinfo checksums are disabled. Only a baseline and counters are retained, so
validation runs alongside encoding without a second full-file decode or frame JSON.
"""

import re

from app.color import HDR_TRANSFERS, HDR_SIDE_DATA_MARKERS

FRAME_MONITOR_NAME = "showinfo@avsync_input"
FRAME_MONITOR_FILTER = FRAME_MONITOR_NAME + "=checksum=0"


class InputFrameValidator:
    def __init__(self, video, color):
        self.expected = {"color_range": color.input_range, "color_space": color.color_space,
            "color_primaries": color.color_primaries, "color_trc": color.color_transfer}
        self.pixel_format = (video.pixel_format or "").replace("yuvj", "yuv")
        self.baseline = self.geometry = None
        self.frames = self.colors = 0
        self.pending = False

    def consume(self, line):
        if not line.startswith("[" + FRAME_MONITOR_NAME + " @"):
            return
        if "side data" in line.lower() and any(marker in line.lower() for marker in HDR_SIDE_DATA_MARKERS):
            raise ValueError("转码帧出现 HDR 附加证据；当前 SDR 计划不能安全处理，停止发布。")
        if re.search(r"\bn:\s*\d+", line):
            if self.pending:
                raise ValueError("输入帧颜色检查记录不完整，不能确认当前计划安全。")
            fmt = re.search(r"\bfmt:(\S+)", line)
            size = re.search(r"\bs:(\d+x\d+)", line)
            if not fmt or not size:
                raise ValueError("无法解析输入帧属性；请检查 FFmpeg 版本。")
            normalized = fmt[1].replace("yuvj", "yuv")
            if normalized != self.pixel_format:
                raise ValueError(f"输入帧像素格式与分析不一致：{self.pixel_format} → {normalized}。")
            geometry = (normalized, size[1])
            if self.geometry is not None and geometry != self.geometry:
                raise ValueError("视频中途改变像素格式或尺寸，固定 RepairPlan 无法安全处理。")
            self.geometry = geometry
            self.frames += 1
            self.pending = True
        if "color_range:" in line:
            fields = dict(re.findall(r"(color_range|color_space|color_primaries|color_trc):(\S+)", line))
            if not self.pending or fields.keys() != self.expected.keys():
                raise ValueError("无法完整关联输入帧颜色记录；请检查 FFmpeg 版本。")
            fields = {k: None if v in {"unknown", "unspecified", "reserved"} else v for k, v in fields.items()}
            if fields["color_trc"] in HDR_TRANSFERS:
                raise ValueError("转码帧出现 PQ/HLG HDR，当前 SDR 计划不能安全处理。")
            for key, expected in self.expected.items():
                if expected is not None and fields[key] != expected:
                    raise ValueError(f"输入帧颜色与 RepairPlan 不一致：{key} 计划 {expected}，实际 {fields[key]}；停止发布。")
            if self.baseline is not None and fields != self.baseline:
                raise ValueError("视频中途改变颜色属性，固定 RepairPlan 无法安全处理；停止发布。")
            self.baseline = fields
            self.colors += 1
            self.pending = False

    def finish(self):
        if not self.frames or self.pending or self.frames != self.colors:
            raise ValueError("未获得完整输入帧颜色检查记录，不能发布未经检查的输出；请检查 FFmpeg 版本。")
