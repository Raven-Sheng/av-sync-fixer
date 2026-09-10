"""Exercise the private-file workflow with generated inputs, never counted as real evidence."""

import pytest

from app.analyzer import analyze
from app.ffmpeg_utils import MissingToolError, run_command
from tests.media_factory.catalog import MediaCase
from tests.media_factory.generate import MediaFactory
from tests.media_factory.regression import run_sample
from tests.media_factory.report import MatrixRecorder


@pytest.mark.parametrize("full,rotation", [(False, 0), (True, 0), (False, 90)])
def test_private_workflow_mechanics_with_synthetic_input(tmp_path, full, rotation):
    try:
        factory = MediaFactory(tmp_path / "generated")
    except MissingToolError as exc:
        pytest.skip(str(exc))
    case = MediaCase("private-mechanism-only", full=full)
    path = factory.create(case)
    if rotation:
        rotated = tmp_path / "中文 rotated source.mp4"
        run_command([factory.tools["ffmpeg"], "-v", "error", "-nostdin", "-n",
                     "-display_rotation:v:0", str(rotation), "-i", str(path),
                     "-map", "0", "-c", "copy", str(rotated)], 30)
        path = rotated
        assert abs(analyze(path).videos[0].rotation) == rotation
    recorder = MatrixRecorder(tmp_path / "mechanism-evidence")
    row = recorder.start("mechanism-only", "mechanism", "Synthetic workflow self-test, not real Steam", "general")
    run_sample(path, row, recorder, factory.tools, tmp_path)
    assert row.stages["Analyze"] == row.stages["Repair"] == row.stages["Validate"] == row.stages["Color"] == "PASS"
    assert row.stages["Source unchanged"] == "PASS"
    assert not list(tmp_path.glob("matrix-output-*"))
