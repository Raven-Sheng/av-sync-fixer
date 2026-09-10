"""颜色范围决策与复查：像素数值转换不同于像素布局和颜色标签。"""

from app.models import ColorConversionPlan, StreamInfo


OUTPUT_PIXEL_FORMAT = "yuv420p"
OUTPUT_COLOR_RANGE = "tv"
FULL_YUV_FORMATS = frozenset({"yuvj420p", "yuvj422p", "yuvj444p", "yuvj440p", "yuvj411p"})
SDR_MATRICES = frozenset({"bt709", "fcc", "bt470bg", "smpte170m", "smpte240m", "bt2020nc"})
SDR_TRANSFERS = frozenset({"bt709", "smpte170m", "smpte240m", "gamma22", "gamma28", "iec61966-2-1", "bt2020-10", "bt2020-12"})
COLOR_PRIMARIES = frozenset({"bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "film", "bt2020", "smpte431", "smpte432", "jedec-p22"})
HDR_TRANSFERS = {"smpte2084": "PQ", "arib-std-b67": "HLG"}
HDR_SIDE_DATA_MARKERS = ("mastering display", "content light", "hdr", "dovi", "dolby vision")


def hdr_side_data_types(items) -> tuple[str, ...]:
    return tuple(dict.fromkeys(name for item in items if isinstance(item, dict)
        and isinstance(name := item.get("side_data_type"), str)
        and any(marker in name.lower() for marker in HDR_SIDE_DATA_MARKERS)))


def hdr_evidence(video: StreamInfo) -> tuple[str, ...]:
    """Positive stream/frame evidence only; neither bit depth nor BT.2020 proves HDR."""
    transfers = ((video.color_transfer or "").strip().lower(), *video.sampled_color_transfers)
    return tuple(dict.fromkeys((*(t for t in transfers if t in HDR_TRANSFERS),
        *hdr_side_data_types(video.side_data_list or ()), *video.sampled_hdr_side_data_types)))


def select_color_plan(video: StreamInfo) -> ColorConversionPlan:
    """未知范围不猜测；只有已知 full 才压缩数值范围。未实现 HDR 色调映射。"""
    pixel = (video.pixel_format or "").strip().lower()
    raw_range = (video.color_range or "").strip().lower()
    input_range = {"pc": "pc", "jpeg": "pc", "tv": "tv", "mpeg": "tv"}.get(raw_range)
    range_source = "color_range" if input_range else "unknown"
    if input_range is None and not raw_range and pixel in FULL_YUV_FORMATS:
        input_range, range_source = "pc", "pix_fmt"
    metadata = {name: (getattr(video, name) or "").strip().lower() or None
                for name in ("color_space", "color_transfer", "color_primaries")}
    def plan(action, reason, warnings=()):
        return ColorConversionPlan(action, input_range, range_source, reason,
            OUTPUT_PIXEL_FORMAT, OUTPUT_COLOR_RANGE, **metadata, warnings=tuple(warnings))
    if input_range is None:
        return plan("blocked", "输入颜色范围未知或无效，不能安全选择 Full/Limited；停止转码，需先核实输入范围。")
    if pixel in FULL_YUV_FORMATS and input_range == "tv":
        return plan("blocked", "yuvj 像素格式与 Limited 范围标签冲突；停止转码，避免错误压缩黑白位。")
    # 两种范围都必须先确认是 YUV；Limited 标签不能证明 RGB→YUV 的矩阵转换安全。
    if not (pixel.startswith("yuv") or pixel in {"nv12", "nv21", "p010le", "p010be"}):
        return plan("blocked", "输入的 YUV 像素格式未知或不受支持；不能只改标签代替颜色转换。")
    if evidence := hdr_evidence(video):
        return plan("blocked", "输入具有 HDR 或冲突的 HDR 附加证据（" + ", ".join(evidence)
                    + "）；当前未实现 HDR 色调映射，核实色彩体系前不能安全输出本预设。")
    for name, known in (("color_space", SDR_MATRICES), ("color_transfer", SDR_TRANSFERS), ("color_primaries", COLOR_PRIMARIES)):
        if metadata[name] is not None and metadata[name] not in known:
            return plan("blocked", f"输入 {name}={metadata[name]} 未在受支持颜色标记内；不猜测或改写色彩体系。")
    warnings = [f"输入 {name} 未指定；不根据分辨率猜测 BT.709，输出仅复查可用标记。"
                for name, value in metadata.items() if value is None]
    if input_range == "pc":
        return plan("full_to_limited", "显式将 Full YUV 数值转换到 Limited，保留已知色彩体系和分辨率。", warnings)
    return plan("preserve_limited", "输入已为 Limited，不重复进行颜色范围压缩。", warnings)


def color_validation_errors(plan: ColorConversionPlan, video: StreamInfo | None) -> tuple[str, ...]:
    from app.output_validation import color_checks
    from app.models import ValidationLevel
    if video is None:
        return ("缺少输出视频轨，无法验证颜色。",)
    return tuple(c.message for c in color_checks(plan, video) if c.level == ValidationLevel.FAIL)


def color_plan_text(plan: ColorConversionPlan) -> str:
    return f"颜色策略：{plan.action}；{plan.input_range or '未知'} → {plan.output_range} / {plan.pixel_format}；{plan.reason}"
