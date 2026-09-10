import pytest

from app.ffmpeg_utils import MissingToolError
from tests.media_factory.catalog import CASES, PRESETS
from tests.media_factory.generate import MediaFactory, MissingEncoder
from tests.media_factory.regression import run_sample


@pytest.fixture(scope="session")
def synthetic_factory_cache(tmp_path_factory):
    return {"directory": tmp_path_factory.mktemp("compatibility-matrix-media")}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
@pytest.mark.parametrize("preset", PRESETS)
def test_input_compatibility_matrix(case, preset, synthetic_factory_cache, matrix_recorder, tmp_path):
    row = matrix_recorder.start(f"synthetic:{case.id}:{preset}", "synthetic", case.id, preset)
    try:
        cache = synthetic_factory_cache
        if "factory" not in cache:
            cache["factory"] = MediaFactory(cache["directory"])
        factory = cache["factory"]
        path = factory.create(case)
        run_sample(path, row, matrix_recorder, factory.tools, tmp_path, case=case)
    except (MissingToolError, MissingEncoder) as exc:
        row.notes.append(str(exc))
        pytest.skip(str(exc))
    except Exception as exc:
        row.notes.append(f"{type(exc).__name__}: {str(exc)[:2000]}")
        raise
