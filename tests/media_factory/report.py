"""Evidence-based matrix reporting. Unexecuted stages never default to PASS."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path

from .catalog import CASES, CATEGORIES, PRESETS

STAGES = ("Analyze", "Compatibility", "Repair", "Validate", "Color", "Duration", "Source unchanged")
STATUS_PRIORITY = ("FAIL", "NOT TESTED", "WARNING", "PASS")


@dataclass
class MatrixRow:
    id: str
    kind: str
    label: str
    preset: str
    stages: dict = field(default_factory=lambda: dict.fromkeys(STAGES, "NOT TESTED"))
    observed: list = field(default_factory=list)
    input: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    artifacts: str = ""


def aggregate(values):
    values = list(values)
    if not values:
        return "NOT TESTED"
    if any(value not in STATUS_PRIORITY for value in values):
        raise ValueError(f"Unknown statuses: {values}")
    # An uncovered part of a category cannot be hidden by another passing row.
    for status in STATUS_PRIORITY:
        if status in values:
            return status


def coverage(rows):
    # Each generated input is reused across presets. A missing preset must count
    # as untested once a sibling establishes the input's actual categories. Keep
    # the missing row's observed list empty: it has no probe evidence of its own.
    synthetic_categories = {}
    for row in rows:
        if row["kind"] == "synthetic":
            synthetic_categories.setdefault(row["label"], set()).update(row["observed"])
    def relevant(row, category):
        return category in (synthetic_categories[row["label"]] if row["kind"] == "synthetic" else row["observed"])
    return {key: {stage: aggregate(row["stages"][stage] if key in row["observed"] else "NOT TESTED"
                                  for row in rows if relevant(row, key))
                  for stage in STAGES} for key in CATEGORIES}


class MatrixRecorder:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.rows = {}

    def start(self, key, kind, label, preset):
        row = MatrixRow(key, kind, label, preset)
        self.rows[key] = row
        return row

    def finish(self, phase, summary):
        self.directory.mkdir(parents=True, exist_ok=True)
        data = {"phase": phase, "utc": datetime.now(timezone.utc).isoformat(), "tests": summary,
                "rows": [asdict(row) for row in self.rows.values()]}
        (self.directory / f"{phase}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return write_report(self.directory)


def write_report(directory):
    phases, executed = [], {}
    for name in ("unit", "integration", "private", "all"):
        path = directory / f"{name}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            phases.append({key: data[key] for key in ("phase", "utc", "tests")})
            executed.update((row["id"], row) for row in data["rows"])
    rows = []
    for case in CASES:
        for preset in PRESETS:
            key = f"synthetic:{case.id}:{preset}"
            rows.append(executed.pop(key, asdict(MatrixRow(key, "synthetic", case.id, preset))))
    private = list(executed.values())
    if not private:
        private = [asdict(MatrixRow("private:none", "private", "Real samples absent or private phase not run", "general"))]
    rows.extend(private)
    result = {"phases": phases, "coverage": coverage(rows), "rows": rows,
              "limitations": ["Synthetic motion and color bars do not reproduce Steam recorder clocks, capture stalls, or driver behavior.",
                              "Literal pix_fmt coverage uses ffprobe values; full yuvj420p is not counted as full yuv420p.",
                              "Color PASS means decoded calibration patches (synthetic) or a sampled first-frame reference (private), not exhaustive color verification.",
                              "Compatibility PASS means the input diagnostic agrees with observed metadata, not that the input has no risks.",
                              "Private tests repair complete files; timing uses the existing output spec and cannot prove audiovisual content alignment."]}
    (directory / "matrix.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    lines = ["# Compatibility Matrix", "", "PASS / WARNING / FAIL / NOT TESTED are distinct. Only observed input fields establish category coverage.", "",
             "## Test runs", "", "| Phase | Passed | Failed | Skipped | Exit code |", "|---|---:|---:|---:|---:|"]
    for phase in phases:
        counts = phase["tests"]
        lines.append(f"| {phase['phase']} | {counts['passed']} | {counts['failed']} | {counts['skipped']} | {counts['exit_code']} |")
    for phase in phases:
        if phase["tests"].get("infrastructure_error"):
            lines.append(f"\n{phase['phase']}: pytest did not produce a session report (startup/collection failure); this phase has no confirmed coverage.\n")
    lines += ["", "## A–O observed coverage", "", "| Category | Input | Analyze | Repair | Validate | Color |", "|---|---|---|---|---|---|"]
    for key, label in CATEGORIES.items():
        levels = result["coverage"][key]
        lines.append(f"| {key} | {label} | " + " | ".join(levels[s] for s in ("Analyze", "Repair", "Validate", "Color")) + " |")
    lines += ["", "## Sample executions", "", "| Input | Preset | Observed input | Categories | Analyze | Compatibility | Repair | Validate | Color | Duration | Source unchanged |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        values = [row["label"], row["preset"], row["input"].get("description", "NOT TESTED"), ", ".join(row["observed"]) or "NOT TESTED"]
        values += [row["stages"][stage] for stage in STAGES]
        lines.append("| " + " | ".join(map(cell, values)) + " |")
    lines += ["", "## Evidence and limitations", ""]
    lines.extend(f"- {message}" for message in result["limitations"])
    for row in rows:
        if row["notes"] or row["artifacts"]:
            lines.append(f"- **{cell(row['label'])} / {row['preset']}**: " + "; ".join(map(cell, row["notes"])))
            if row["artifacts"]:
                lines.append(f"  Evidence: [{row['artifacts']}]({row['artifacts']}/post-repair.txt)")
    path = directory / "matrix.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
