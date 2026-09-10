"""Coverage accounting and pixel-oracle negative controls (no FFmpeg required)."""

from dataclasses import replace
from fractions import Fraction
import json
from pathlib import Path

import pytest

from tests.test_strategies import media
from tests.media_factory.catalog import CASES, CATEGORIES, discover_private, observed_categories
from tests.media_factory.pixels import FULL_LEVELS
from tests.media_factory.regression import assert_calibration, cadence_of, file_hash
from tests.media_factory.report import MatrixRecorder, aggregate


@pytest.mark.parametrize("values,expected", [([], "NOT TESTED"), (["PASS"], "PASS"),
    (["PASS", "NOT TESTED"], "NOT TESTED"), (["PASS", "WARNING"], "WARNING"),
    (["NOT TESTED", "FAIL"], "FAIL")])
def test_aggregation_never_promotes_untested_to_pass(values, expected):
    assert aggregate(values) == expected


def test_aggregation_rejects_unknown_status_even_alongside_pass():
    with pytest.raises(ValueError):
        aggregate(["PASS", "PASSS"])


def test_partial_preset_run_cannot_claim_complete_category_coverage(tmp_path):
    recorder = MatrixRecorder(tmp_path)
    row = recorder.start("synthetic:h264_limited_30:general", "synthetic", "h264_limited_30", "general")
    row.observed = ["A"]
    row.stages = dict.fromkeys(row.stages, "PASS")
    recorder.finish("integration", {"passed": 1, "failed": 0, "skipped": 0, "exit_code": 0})
    report = json.loads((tmp_path / "matrix.json").read_text(encoding="utf-8"))
    assert set(report["coverage"]["A"].values()) == {"NOT TESTED"}
    missing = next(r for r in report["rows"] if r["id"] == "synthetic:h264_limited_30:bilibili")
    assert missing["observed"] == []  # Do not invent probe evidence for the missing run.


def test_full_range_alias_does_not_fabricate_literal_pixel_format_coverage():
    source = media(video={"pix_fmt": "yuvj420p", "color_range": "pc"})
    assert "B" not in observed_categories(source, cadence="CFR")
    actual_literal = replace(source, videos=(replace(source.videos[0], pixel_format="yuv420p"),))
    assert "B" in observed_categories(actual_literal, cadence="CFR")
    assert "D" in observed_categories(replace(source, videos=(replace(source.videos[0], codec="hevc"),)), cadence="CFR")


def test_cfr_coverage_requires_frame_evidence_not_equal_fps_metadata():
    source = media()
    assert not {"A", "E", "F", "G"} & set(observed_categories(source, cadence=None))
    assert {"A", "E"} <= set(observed_categories(source, cadence="CFR"))


def test_fractional_fps_is_not_counted_as_sixty():
    source = media(video={"avg_frame_rate": "60000/1001", "r_frame_rate": "60000/1001"})
    assert "G" in observed_categories(source, cadence="CFR")
    assert "F" not in observed_categories(source, cadence="CFR")


def test_private_discovery_is_optional_recursive_and_extension_filtered(tmp_path):
    folder = tmp_path / "private"
    assert discover_private(folder) == ()
    folder.mkdir()
    nested = folder / "中文 folder"
    nested.mkdir()
    paths = [folder / "recording.mp4", nested / "真实.MKV"]
    for path in paths:
        path.write_bytes(b"fixture")
    (folder / "README.md").write_text("not a video", encoding="utf-8")
    assert set(discover_private(folder)) == set(paths)


def test_empty_report_marks_every_category_and_real_samples_not_tested(tmp_path):
    recorder = MatrixRecorder(tmp_path)
    recorder.finish("unit", {"passed": 1, "failed": 0, "skipped": 0, "exit_code": 0})
    data = json.loads((tmp_path / "matrix.json").read_text(encoding="utf-8"))
    assert set(data["coverage"]) == set(CATEGORIES)
    assert all(level == "NOT TESTED" for row in data["coverage"].values() for level in row.values())
    assert all(level == "NOT TESTED" for row in data["rows"] if row["kind"] == "private" for level in row["stages"].values())


def test_phase_merge_keeps_executed_rows_but_not_prior_run_results(tmp_path):
    summary = {"passed": 1, "failed": 0, "skipped": 0, "exit_code": 0}
    run = tmp_path / "run-one"
    integration = MatrixRecorder(run)
    row = integration.start("synthetic:h264_limited_30:general", "synthetic", "h264_limited_30", "general")
    row.observed = ["A"]
    row.stages.update(Analyze="PASS", Repair="PASS", Validate="WARNING")
    integration.finish("integration", summary)
    private = MatrixRecorder(run)
    private.finish("private", {**summary, "passed": 0, "skipped": 1})
    merged = json.loads((run / "matrix.json").read_text(encoding="utf-8"))
    assert merged["coverage"]["A"]["Validate"] == "NOT TESTED"
    assert next(row for row in merged["rows"] if row["id"] == "synthetic:h264_limited_30:general")["stages"]["Validate"] == "WARNING"
    assert merged["coverage"]["A"]["Color"] == "NOT TESTED"
    fresh = MatrixRecorder(tmp_path / "run-two")
    fresh.finish("unit", summary)
    data = json.loads((fresh.directory / "matrix.json").read_text(encoding="utf-8"))
    assert data["coverage"]["A"]["Analyze"] == "NOT TESTED"


def test_all_factory_samples_are_short_and_category_axes_exist():
    assert all(2 <= c.seconds <= 10 and (c.audio_seconds is None or 2 <= c.audio_seconds <= 10) for c in CASES)
    assert {c.codec for c in CASES} == {"h264", "hevc"}
    assert {Fraction(c.fps) for c in CASES} >= {Fraction(30), Fraction(60), Fraction(60000, 1001)}
    assert any(c.full for c in CASES) and any(c.vfr for c in CASES)
    assert any(c.audio_seconds is None for c in CASES)


@pytest.mark.parametrize("incorrect", ["tag-only", "double-conversion"])
def test_calibration_oracle_rejects_incorrect_pixel_values(incorrect):
    original = [list(FULL_LEVELS)] * 3
    correct = [[16 + value * span / 255 for value in FULL_LEVELS] for span in (219, 224, 224)]
    assert_calibration(original, correct, True)
    bad = original if incorrect == "tag-only" else [[16 + value * span / 255 for value in plane] for plane, span in zip(correct, (219, 224, 224))]
    with pytest.raises(AssertionError):
        assert_calibration(original, bad, True)


@pytest.mark.parametrize("missing", [0, 1, 2])
def test_calibration_rejects_missing_color_planes(missing):
    complete = [list(FULL_LEVELS)] * 3
    with pytest.raises(AssertionError):
        assert_calibration(complete, complete[:missing], False)


def test_calibration_rejects_nonfinite_patch_even_after_valid_patch():
    complete = [list(FULL_LEVELS) for _ in range(3)]
    bad = [plane.copy() for plane in complete]
    bad[2][5] = float("nan")
    with pytest.raises(AssertionError):
        assert_calibration(complete, bad, False)


@pytest.mark.parametrize("failure", ["validation", "publication", "reprobe"])
def test_failed_repair_retains_validation_and_duration_evidence(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace
    from app.models import OutputValidation, ValidationCheck, ValidationLevel
    from tests.media_factory import regression

    path = tmp_path / "中文 source.mp4"
    path.write_bytes(b"untouched source")
    before = replace(media(), path=path)
    failing = OutputValidation("general", before, None, errors=("Duration worsened",),
        checks=(ValidationCheck("Timing", "audio.0.difference", ValidationLevel.FAIL, "Duration worsened"),))
    passing = OutputValidation("general", before, None,
        checks=(ValidationCheck("Timing", "Video.duration", ValidationLevel.PASS, "Duration preserved"),))
    monkeypatch.setattr(regression, "analyze", lambda path: before)
    monkeypatch.setattr(regression, "prepare_fix", lambda *args, **kwargs: SimpleNamespace(expected=CASES[0]))
    def execute(plan, on_validation):
        on_validation(failing if failure == "validation" else passing)
        if failure != "reprobe":
            raise RuntimeError(failure)
        return SimpleNamespace(after=before)
    monkeypatch.setattr(regression, "execute_fix", execute)
    monkeypatch.setattr(regression, "validate_output_file", lambda *args, **kwargs: failing)
    recorder = MatrixRecorder(tmp_path / "report")
    row = recorder.start("private:test", "private", "test", "general")
    with pytest.raises((RuntimeError, AssertionError)):
        regression.run_sample(path, row, recorder, {}, tmp_path)
    assert row.stages["Repair"] == ("PASS" if failure == "reprobe" else "FAIL")
    assert row.stages["Validate"] == ("PASS" if failure == "publication" else "FAIL")
    assert row.stages["Duration"] == ("PASS" if failure == "publication" else "FAIL")
    assert row.stages["Color"] == "NOT TESTED"
    assert row.stages["Source unchanged"] == "PASS"
    assert not list(tmp_path.glob("matrix-output-*"))
    if failure != "publication":
        assert "Duration worsened" in (recorder.directory / row.artifacts / "post-repair.txt").read_text(encoding="utf-8")


def test_cadence_uses_deltas_and_rejects_discontinuity():
    assert cadence_of([0, 0.033333, 0.066667, 0.1]) == "CFR"
    assert cadence_of([0, 0.033333, 0.1, 0.133333]) == "VFR"
    with pytest.raises(AssertionError):
        cadence_of([0, 0.1, 0.05])


def test_source_hash_changes_when_media_changes(tmp_path):
    path = tmp_path / "source.bin"
    path.write_bytes(b"a" * (1024 * 1024 + 7))
    first = file_hash(path)
    with path.open("ab") as stream:
        stream.write(b"x")
    assert file_hash(path) != first


def test_regression_runner_stays_in_this_repository():
    from tests.media_factory.run_regression import PROJECT_ROOT
    assert PROJECT_ROOT == Path(__file__).resolve().parents[1]


def test_runner_startup_errors_still_write_not_tested_report(tmp_path, monkeypatch):
    from subprocess import CompletedProcess
    from tests.media_factory import run_regression
    monkeypatch.setattr(run_regression, "PROJECT_ROOT", tmp_path)
    temporary_paths = []
    def fail_startup(args, **kwargs):
        temporary = Path(args[args.index("--basetemp") + 1])
        assert temporary.parent.is_dir()
        assert temporary not in temporary_paths
        temporary_paths.append(temporary)
        return CompletedProcess(args, 2)
    monkeypatch.setattr(run_regression.subprocess, "run", fail_startup)
    assert run_regression.main() == 1
    path = next((tmp_path / "tmp" / "compatibility-reports").glob("*/matrix.json"))
    report = json.loads(path.read_text(encoding="utf-8"))
    assert len(report["phases"]) == 3
    assert all(phase["tests"]["infrastructure_error"] for phase in report["phases"])
    assert all(level == "NOT TESTED" for category in report["coverage"].values() for level in category.values())
    markdown = path.with_suffix(".md").read_text(encoding="utf-8")
    assert markdown.index("| private |") < markdown.index("pytest did not produce a session report")
