#!/usr/bin/env python3
"""Legacy planner with no recovered capability or setup model."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


REFUSED = "SCHEDULING-CASE-REFUSED: "


def compact_bytes(value: object) -> bytes:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def blank_model(scenario: dict) -> dict:
    equipment_ids = [item["id"] for item in scenario["equipment"]]
    job_ids = [item["id"] for item in scenario["jobs"]]
    sequence_ids = [
        sequence["sequence_id"]
        for observation in scenario["observations"]
        for sequence in observation["sequences"]
    ]
    return {
        "assignments": {job_id: equipment_ids[0] for job_id in job_ids},
        "case_id": scenario["case_id"],
        "changeover_matrix": {
            first: {second: 0.0 for second in job_ids}
            for first in job_ids
        },
        "duration_matrix": {
            equipment_id: {job_id: 0.0 for job_id in job_ids}
            for equipment_id in equipment_ids
        },
        "history_mapping": {
            sequence_id: equipment_ids[0] for sequence_id in sequence_ids
        },
        "makespan": 0.0,
        "qualification": {
            equipment_id: {job_id: False for job_id in job_ids}
            for equipment_id in equipment_ids
        },
        "schedule": {equipment_id: [] for equipment_id in equipment_ids},
        "schema_version": 1,
        "solver_nodes": 0,
        "total_weighted_completion": 0.0,
    }


def create_report(scenario: dict) -> dict:
    base = blank_model(scenario)
    checksum = hashlib.sha256(compact_bytes(base)).hexdigest()
    core = {**base, "checksum": checksum}
    entry = {
        "case_id": scenario["case_id"],
        "input_sha256": hashlib.sha256(compact_bytes(scenario)).hexdigest(),
        "report_sha256": hashlib.sha256(compact_bytes(core)).hexdigest(),
        "status": "completed",
    }
    return {**core, "journal_entry": entry}


def write_result(report_path: Path, journal_path: Path, report: dict) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(compact_bytes(report))
    with journal_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report["journal_entry"], sort_keys=True, separators=(",", ":")))
        handle.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario")
    parser.add_argument("--journal")
    parser.add_argument("--report")
    parser.add_argument("--inspect", action="store_true")
    args = parser.parse_args()
    if args.inspect:
        return 0
    if not args.scenario or not args.journal or not args.report:
        print(REFUSED + "the shift needs a scenario, journal, and report path", file=sys.stderr)
        return 3
    try:
        scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
        report = create_report(scenario)
        write_result(Path(args.report), Path(args.journal), report)
        return 0
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(REFUSED + " ".join(str(exc).splitlines()), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
