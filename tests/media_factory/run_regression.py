"""Run the three suites into one fresh, auditable report directory."""

from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import uuid

from .report import MatrixRecorder

PROJECT_ROOT = Path(__file__).resolve().parents[2]

def main():
    root = PROJECT_ROOT
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    directory = root / "tmp" / "compatibility-reports" / stamp
    directory.mkdir(parents=True)
    temporary_root = root / "tmp" / "regression-tests" / stamp
    temporary_root.mkdir(parents=True)
    phases = (("unit", "not integration"), ("integration", "integration and not private"), ("private", "private"))
    failed = False
    for phase, selection in phases:
        print(f"\nRunning {phase} tests", flush=True)
        result = subprocess.run([sys.executable, "-X", "utf8", "-m", "pytest", "-q", "-ra", "-m", selection,
            "--regression-phase", phase, "--matrix-report-dir", str(directory),
            "--basetemp", str(temporary_root / phase)], cwd=root)
        failed |= result.returncode != 0
        # Startup/collection failures can happen before pytest's reporting hook.
        # Still emit a report with no claimed coverage and the failing exit code.
        if not (directory / f"{phase}.json").exists():
            MatrixRecorder(directory).finish(phase, {"passed": 0, "failed": 1, "skipped": 0,
                "exit_code": result.returncode, "infrastructure_error": True})
            failed = True
    print(f"\nCompatibility Matrix: {directory / 'matrix.md'}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
