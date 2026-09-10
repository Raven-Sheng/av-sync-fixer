"""Opt-in by file presence: complete real recordings, not synthetic substitutes."""

from pathlib import Path

import pytest

from app.ffmpeg_utils import MissingToolError, check_tools
from tests.media_factory.catalog import discover_private
from tests.media_factory.regression import run_sample

PRIVATE_DIRECTORY = Path(__file__).resolve().parents[1] / "samples" / "private"
PRIVATE_FILES = discover_private(PRIVATE_DIRECTORY)


@pytest.mark.parametrize("path", PRIVATE_FILES or (None,), ids=[p.relative_to(PRIVATE_DIRECTORY).as_posix() for p in PRIVATE_FILES] or ["no-private-samples"])
def test_real_sample_regression(path, matrix_recorder, tmp_path):
    label = path.relative_to(PRIVATE_DIRECTORY).as_posix() if path else "Real Sample Regression: no files in samples/private"
    row = matrix_recorder.start(f"private:{label}", "private", label, "general")
    if path is None:
        row.notes.append("NOT TESTED: samples/private contains no supported media files; synthetic samples are not real Steam evidence.")
        pytest.skip(row.notes[-1])
    try:
        run_sample(path, row, matrix_recorder, check_tools(), tmp_path)
    except MissingToolError as exc:
        row.notes.append(str(exc))
        pytest.skip(str(exc))
    except Exception as exc:
        row.notes.append(f"{type(exc).__name__}: {str(exc)[:2000]}")
        raise
