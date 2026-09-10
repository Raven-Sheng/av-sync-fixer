"""按诊断选择策略，生成可预览计划，再转码、复查与安全发布。"""

from collections.abc import Callable
from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from app.analyzer import analyze
from app.ffmpeg_utils import (
    MediaError, PROJECT_ROOT, build_repair_command, find_tool, run_ffmpeg,
    DEFAULT_OUTPUT_PRESET,
)
from app.models import AnalysisResult, FixPlan, FixResult, OutputValidation
from app.output_validation import validate_output_file
from app.frame_validation import InputFrameValidator
from app.repair_plan import (select_repair_plan, select_strategy, select_target_fps,
                             COMMON_FPS, DEFAULT_TARGET_FPS, REPAIR_MODES, DEFAULT_REPAIR_MODE,
                             expected_output_spec, plan_from_strategy)


OUTPUT_DIR = PROJECT_ROOT / "output"


def estimate_duration(source: AnalysisResult) -> float | None:
    """仅用于估计转码进度，不参与音频裁剪或速度调整。"""
    durations = [stream.duration for stream in (*source.videos[:1], *source.audios) if stream.duration is not None]
    return source.container_duration or (max(durations) if durations else None)


def prepare_fix(source: AnalysisResult, output_dir: Path | None = None, *, mode: str = DEFAULT_REPAIR_MODE,
                preset: str = DEFAULT_OUTPUT_PRESET) -> FixPlan:
    """只生成计划，不创建目录、不运行 FFmpeg。预览命令使用最终输出路径。"""
    try:
        repair = select_repair_plan(source, mode, preset=preset)
        strategy = repair.strategy
        directory = Path(output_dir if output_dir is not None else OUTPUT_DIR).resolve()
        output = directory / f"{source.path.stem}_fixed.mp4"
        command = build_repair_command(find_tool("ffmpeg"), repair, output)
    except OSError as exc:
        raise MediaError(f"无法准备输出路径：{exc}") from exc
    return FixPlan(source, output, strategy, tuple(command), preset, repair.video.color_conversion, repair, expected_output_spec(repair, source))


def fix_video(
    source: AnalysisResult,
    output_dir: Path | None = None,
    *,
    mode: str = DEFAULT_REPAIR_MODE,
    preset: str = DEFAULT_OUTPUT_PRESET,
    on_start: Callable[[Path, int | None, list[str]], None] | None = None,
    on_progress: Callable[[float], None] | None = None,
    on_validation: Callable[[OutputValidation], None] | None = None,
) -> FixResult:
    return execute_fix(prepare_fix(source, output_dir, mode=mode, preset=preset),
                       on_start=on_start, on_progress=on_progress, on_validation=on_validation)


def execute_fix(
    plan: FixPlan,
    *,
    on_start: Callable[[Path, int | None, list[str]], None] | None = None,
    on_progress: Callable[[float], None] | None = None,
    on_validation: Callable[[OutputValidation], None] | None = None,
) -> FixResult:
    """执行已选好的计划。预览与执行共享参数，只替换临时输出路径。"""
    source, output, strategy = plan.source, plan.output_path, plan.strategy
    target_fps = strategy.target_fps
    try:
        directory = output.parent
        directory.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(output):
            raise MediaError(f"输出文件已存在，不会覆盖：{output}。请先重命名或移走该文件。")
        with TemporaryDirectory(prefix=".avsync-", dir=directory) as temporary:
            staged = Path(temporary) / output.name
            command = [*plan.command[:-1], str(staged)]
            if on_start is not None:
                on_start(output, target_fps, command)
            repair = plan.repair or plan_from_strategy(source, strategy, preset=plan.preset)
            run_ffmpeg(command, on_progress,
                       input_validator=InputFrameValidator(source.videos[0], repair.video.color_conversion))
            spec = plan.expected or expected_output_spec(repair, source)
            validation = validate_output_file(spec, staged, probe=analyze)
            if on_validation is not None:
                on_validation(validation)
            if validation.errors:
                label = "不符合 Bilibili 输出计划" if plan.preset == "bilibili" else "未满足 RepairPlan"
                raise MediaError(f"输出复查失败：{label}，未发布最终文件：\n" + "\n".join(validation.errors))
            after = validation.media
            # Windows rename refuses an existing destination, including a race;
            # unlike hard links it also works on FAT/exFAT. POSIX rename replaces
            # existing files, so retain exclusive hard-link publication there.
            try:
                if os.name == "nt":
                    os.rename(staged, output)
                else:
                    os.link(staged, output)
            except FileExistsError as exc:
                raise MediaError(f"输出文件已被其他任务创建，不会覆盖：{output}") from exc
            after = replace(after, path=output)
            if validation is not None:
                validation = replace(validation, media=after)
            return FixResult(source, after, target_fps, strategy, validation)
    except OSError as exc:
        raise MediaError(f"修复文件操作失败（请检查空间、权限及文件系统支持）：{exc}") from exc
