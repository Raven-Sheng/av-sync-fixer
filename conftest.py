"""Root pytest configuration: register CLI options before test-path discovery."""

from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest

from tests.media_factory.report import MatrixRecorder

INTEGRATION_MODULES = {"test_integration.py", "test_gui.py", "test_generated_samples.py",
                       "test_sample_generation.py", "test_compatibility_matrix.py"}


def pytest_addoption(parser):
    parser.addoption("--matrix-report-dir", default=None, help="Directory for matrix JSON/Markdown; defaults to a fresh run under tmp/")
    parser.addoption("--regression-phase", choices=("unit", "integration", "private", "all"), default="all")


def pytest_configure(config):
    directory = config.getoption("--matrix-report-dir")
    if directory is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        directory = Path(config.rootpath) / "tmp" / "compatibility-reports" / stamp
    config.matrix_recorder = MatrixRecorder(directory)


def pytest_collection_modifyitems(items):
    for item in items:
        name = item.path.name
        if name.endswith("_integration.py") or name in INTEGRATION_MODULES:
            item.add_marker(pytest.mark.integration)
        if name == "test_private_samples.py":
            item.add_marker(pytest.mark.integration)
            item.add_marker(pytest.mark.private)


@pytest.fixture(scope="session")
def matrix_recorder(pytestconfig):
    return pytestconfig.matrix_recorder


def pytest_sessionfinish(session, exitstatus):
    reporter = session.config.pluginmanager.getplugin("terminalreporter")
    stats = reporter.stats if reporter else {}
    summary = {"passed": len(stats.get("passed", ())), "failed": len(stats.get("failed", ())) + len(stats.get("error", ())),
               "skipped": len(stats.get("skipped", ())), "exit_code": int(exitstatus)}
    session.config.matrix_path = session.config.matrix_recorder.finish(session.config.getoption("--regression-phase"), summary)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    terminalreporter.write_line(f"Compatibility Matrix: {config.matrix_path}")
