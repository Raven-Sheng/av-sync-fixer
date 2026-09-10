"""输出预设的复查和共享报告；CLI 与 GUI 不自行判断是否达标。"""

from app.analyzer import absolute_difference, fps_to_float
from app.models import AnalysisResult, FixPlan, OutputValidation


PRESET_LABELS = {"general": "通用", "bilibili": "Bilibili"}


def validate_bilibili_output(plan: FixPlan, after: AnalysisResult) -> OutputValidation:
    """Legacy metadata-only entry point; both presets now share the spec validator."""
    from app.repair_plan import expected_output_spec, plan_from_strategy
    from app.output_validation import validate_output
    repair = plan.repair or plan_from_strategy(plan.source, plan.strategy, preset=plan.preset)
    return validate_output(plan.expected or expected_output_spec(repair, plan.source), after)


def format_validation_report(result: OutputValidation) -> str:
    info = result.media
    summary = ["Post Repair Report", *[f"{name}: {level.value}" for name, level in result.sections.items()],
               f"Overall: {result.overall.value}"]
    if info is None:
        return "\n".join([*summary, "输出验证报告：输出不可读取。", *[f"不符合项：{e}" for e in result.errors]])
    video = info.videos[0] if info.videos else None
    def seconds(value):
        return "未知" if value is None else f"{value:.3f} 秒"
    def fps(value):
        number = fps_to_float(value)
        return "未知" if number is None else f"{number:g} FPS"
    lines = [
        *summary,
        f"输出验证报告 · {PRESET_LABELS[result.preset]}",
        f"容器：{info.container or '未知'}（major_brand：{info.major_brand or '未知'}）",
        f"视频编码：{video.codec if video and video.codec else '未知'}",
        f"像素格式：{video.pixel_format if video and video.pixel_format else '未知'}",
        f"颜色范围：{video.color_range if video and video.color_range else '未知'}（目标 {result.expected.video.color.output_range if result.expected else '未指定'}）",
        f"颜色矩阵：{video.color_space if video and video.color_space else '未知'}",
        f"传递特性：{video.color_transfer if video and video.color_transfer else '未知'}",
        f"原色：{video.color_primaries if video and video.color_primaries else '未知'}",
        f"分辨率：{video.width} × {video.height}" if video and video.width and video.height else "分辨率：未知",
        f"帧率：平均 {fps(video.avg_frame_rate if video else None)} / 标称 {fps(video.r_frame_rate if video else None)}；CFR 目标 {result.target_fps if result.target_fps is not None else '未启用'}",
        f"视频时长：{seconds(video.duration if video else None)}",
    ]
    if result.expected:
        spec = result.expected
        lines.extend([
            f"计划视频：{spec.video.codec} / {spec.video.pixel_format} / {spec.video.fps_mode}；目标 FPS {spec.video.target_fps or '保持'}",
            f"计划封装：{spec.container.format} / faststart={spec.container.faststart}",
            f"时长容差：{spec.duration_tolerance:.3f} 秒；长度差恶化容差：{spec.sync_regression_tolerance:.3f} 秒；相对起点容差：{spec.start_tolerance:.3f} 秒。",
        ])
        lines.extend(f"计划音轨 {i}：{a.codec} / {a.sample_rate or '输入（未知）'} Hz"
                     for i, a in enumerate(spec.audios, 1))
    if not info.audios:
        lines.append("音频编码 / 采样率 / 时长 / 轨道差异：无音频轨（输入无音轨时不自动生成）")
    for position, audio in enumerate(info.audios, 1):
        lines.extend([
            f"音轨 {position}：编码 {audio.codec or '未知'}；采样率 {audio.sample_rate or '未知'} Hz",
            f"  音频时长：{seconds(audio.duration)}；轨道差异：{seconds(absolute_difference(video.duration if video else None, audio.duration))}",
        ])
    if result.errors:
        lines.append("结论：不符合预设 / RepairPlan 输出要求，未发布最终文件。")
    else:
        lines.append("结论：预设编码要求通过，RepairPlan 输出验证通过。" if not result.warnings else "结论：预设编码要求通过，但仍有需要检查的颜色、时序或其他待核实项。")
    lines.extend(f"不符合项：{message}" for message in result.errors)
    lines.extend(f"提示：{message}" for message in result.warnings)
    if result.target_fps is not None:
        lines.append("CFR 由 fps 滤镜生成，复查平均/标称 FPS；元数据验证不能证明声音与画面内容同步。")
    else:
        lines.append("本次未强制 CFR；元数据验证不能证明声音与画面内容同步。")
    return "\n".join(lines)
