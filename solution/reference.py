#!/usr/bin/env python3
"""Recover production capabilities and solve the finite shift schedule."""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import itertools
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any


REFUSED = "SCHEDULING-CASE-REFUSED: "
TOP_KEYS = {"case_id", "equipment", "jobs", "observations", "schema_version", "unit"}
EQUIPMENT_KEYS = {"id"}
JOB_KEYS = {"id", "weight"}
OBSERVATION_KEYS = {"batch", "machine_minutes", "observation", "sequences"}
SEQUENCE_KEYS = {"makespan", "order", "sequence_id"}
ENTRY_KEYS = {"case_id", "input_sha256", "report_sha256", "status"}
QUALIFICATION_EPSILON = 1e-7
FIT_EPSILON = 1e-6
ROUND_EPSILON = 1e-7


class Refusal(ValueError):
    """The shift cannot be reconstructed or scheduled."""


def is_number(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(float(value))


def close(left: float, right: float, tolerance: float = FIT_EPSILON) -> bool:
    return abs(float(left) - float(right)) <= tolerance


def compact_bytes(value: Any) -> bytes:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Refusal(f"duplicate key: {key}")
        result[key] = value
    return result


def read_scenario(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Refusal(f"cannot read scenario: {exc}") from exc
    try:
        value = json.loads(text, object_pairs_hook=reject_duplicates)
    except json.JSONDecodeError as exc:
        raise Refusal(f"invalid scenario JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise Refusal("scenario root must be an object")
    return value


def exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise Refusal(f"{label} keys differ; missing={missing} extra={extra}")


def valid_case_id(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    parts = value.split("-")
    if len(parts) < 3 or any(not part for part in parts):
        return False
    year_indexes = [
        index
        for index, part in enumerate(parts)
        if re.fullmatch(r"(?:19|20)\d{2}", part)
    ]
    return bool(year_indexes and year_indexes[0] < len(parts) - 1)


def validate(raw: dict[str, Any]) -> dict[str, Any]:
    exact_keys(raw, TOP_KEYS, "scenario")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise Refusal("schema_version must be integer 1")
    if raw["unit"] != "minute":
        raise Refusal("unit must be minute")
    if not valid_case_id(raw["case_id"]):
        raise Refusal("case_id must contain a year segment and a following shift segment")

    equipment = raw["equipment"]
    if not isinstance(equipment, list) or len(equipment) != 3:
        raise Refusal("equipment must contain exactly three entries")
    equipment_ids: list[str] = []
    for index, item in enumerate(equipment):
        if not isinstance(item, dict):
            raise Refusal(f"equipment[{index}] must be an object")
        exact_keys(item, EQUIPMENT_KEYS, f"equipment[{index}]")
        equipment_id = item["id"]
        if not isinstance(equipment_id, str) or not equipment_id.strip():
            raise Refusal("equipment id must be non-empty")
        if equipment_id in equipment_ids:
            raise Refusal("equipment ids must be unique")
        equipment_ids.append(equipment_id)

    jobs = raw["jobs"]
    if not isinstance(jobs, list) or not 4 <= len(jobs) <= 7:
        raise Refusal("jobs must contain between four and seven entries")
    job_ids: list[str] = []
    normalized_jobs: list[dict[str, Any]] = []
    for index, item in enumerate(jobs):
        if not isinstance(item, dict):
            raise Refusal(f"jobs[{index}] must be an object")
        exact_keys(item, JOB_KEYS, f"jobs[{index}]")
        job_id = item["id"]
        if not isinstance(job_id, str) or not job_id.strip():
            raise Refusal("job id must be non-empty")
        if job_id in job_ids:
            raise Refusal("job ids must be unique")
        weight = item["weight"]
        if not is_number(weight) or float(weight) <= 0.0:
            raise Refusal("job weight must be positive and finite")
        job_ids.append(job_id)
        normalized_jobs.append({"id": job_id, "weight": float(weight)})

    observations = raw["observations"]
    if not isinstance(observations, list) or not 6 <= len(observations) <= 12:
        raise Refusal("observations must contain between six and twelve entries")
    job_set = set(job_ids)
    equipment_set = set(equipment_ids)
    observation_ids: set[str] = set()
    sequence_ids: set[str] = set()
    normalized_observations: list[dict[str, Any]] = []

    for index, item in enumerate(observations):
        if not isinstance(item, dict):
            raise Refusal(f"observations[{index}] must be an object")
        exact_keys(item, OBSERVATION_KEYS, f"observations[{index}]")

        observation_id = item["observation"]
        if not isinstance(observation_id, str) or not observation_id.strip():
            raise Refusal("observation id must be non-empty")
        if observation_id in observation_ids:
            raise Refusal("observation ids must be unique")
        observation_ids.add(observation_id)

        batch = item["batch"]
        if not isinstance(batch, dict) or set(batch) != job_set:
            raise Refusal("batch keys must exactly cover jobs")
        clean_batch: dict[str, int] = {}
        for job_id in job_ids:
            count = batch[job_id]
            if type(count) is not int or not 0 <= count <= 3:
                raise Refusal("batch values must be integers from zero through three")
            clean_batch[job_id] = count

        minutes = item["machine_minutes"]
        if not isinstance(minutes, dict) or set(minutes) != equipment_set:
            raise Refusal("machine_minutes keys must exactly cover equipment")
        clean_minutes: dict[str, float] = {}
        for equipment_id in equipment_ids:
            value = minutes[equipment_id]
            if not is_number(value) or float(value) < 0.0:
                raise Refusal("machine minutes must be non-negative finite numbers")
            clean_minutes[equipment_id] = float(value)

        sequences = item["sequences"]
        if not isinstance(sequences, list) or len(sequences) != 4:
            raise Refusal("sequences must contain exactly four historical runs")
        clean_sequences: list[dict[str, Any]] = []
        for sequence_index, sequence in enumerate(sequences):
            if not isinstance(sequence, dict):
                raise Refusal(
                    f"observations[{index}].sequences[{sequence_index}] must be an object"
                )
            exact_keys(
                sequence,
                SEQUENCE_KEYS,
                f"observations[{index}].sequences[{sequence_index}]",
            )
            sequence_id = sequence["sequence_id"]
            if not isinstance(sequence_id, str) or not sequence_id.strip():
                raise Refusal("sequence_id must be non-empty")
            if sequence_id in sequence_ids:
                raise Refusal("sequence ids must be unique")
            sequence_ids.add(sequence_id)

            order = sequence["order"]
            if not isinstance(order, list) or not 2 <= len(order) <= len(job_ids):
                raise Refusal("sequence order must contain between two and all jobs")
            if any(not isinstance(job_id, str) or job_id not in job_set for job_id in order):
                raise Refusal("sequence order contains an unknown job")
            if len(set(order)) != len(order):
                raise Refusal("sequence order must not repeat jobs")

            makespan = sequence["makespan"]
            if not is_number(makespan) or float(makespan) < 0.0:
                raise Refusal("sequence makespan must be non-negative and finite")
            clean_sequences.append(
                {
                    "makespan": float(makespan),
                    "order": list(order),
                    "sequence_id": sequence_id,
                }
            )

        normalized_observations.append(
            {
                "batch": clean_batch,
                "machine_minutes": clean_minutes,
                "observation": observation_id,
                "sequences": clean_sequences,
            }
        )

    return {
        "case_id": raw["case_id"],
        "equipment": [{"id": equipment_id} for equipment_id in equipment_ids],
        "jobs": normalized_jobs,
        "observations": normalized_observations,
        "schema_version": 1,
        "unit": "minute",
    }


def matrix_rank(matrix: list[list[float]]) -> int:
    if not matrix:
        return 0
    work = [list(map(float, row)) for row in matrix]
    rows = len(work)
    columns = len(work[0])
    scale = max(1.0, max(abs(value) for row in work for value in row))
    rank = 0
    for column in range(columns):
        pivot = max(range(rank, rows), key=lambda row: abs(work[row][column]))
        if abs(work[pivot][column]) <= 1e-10 * scale:
            continue
        work[rank], work[pivot] = work[pivot], work[rank]
        divisor = work[rank][column]
        for cell in range(column, columns):
            work[rank][cell] /= divisor
        for row in range(rows):
            if row == rank:
                continue
            factor = work[row][column]
            if abs(factor) <= 1e-12:
                continue
            for cell in range(column, columns):
                work[row][cell] -= factor * work[rank][cell]
        rank += 1
        if rank == rows:
            break
    return rank


def solve_square(matrix: list[list[float]], right: list[float]) -> list[float]:
    size = len(right)
    work = [list(map(float, matrix[row])) + [float(right[row])] for row in range(size)]
    scale = max(1.0, max(abs(value) for row in matrix for value in row))
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(work[row][column]))
        if abs(work[pivot][column]) <= 1e-10 * scale:
            raise Refusal("linear system is rank deficient")
        if pivot != column:
            work[column], work[pivot] = work[pivot], work[column]
        divisor = work[column][column]
        for cell in range(column, size + 1):
            work[column][cell] /= divisor
        for row in range(size):
            if row == column:
                continue
            factor = work[row][column]
            if abs(factor) <= 1e-12:
                continue
            for cell in range(column, size + 1):
                work[row][cell] -= factor * work[column][cell]
    return [work[row][size] for row in range(size)]


def least_squares(rows: list[list[float]], targets: list[float]) -> list[float]:
    if not rows:
        raise Refusal("no historical transition equations")
    width = len(rows[0])
    if len(rows) < width:
        raise Refusal("not enough historical transition equations")
    normal = [[0.0 for _ in range(width)] for _ in range(width)]
    right = [0.0 for _ in range(width)]
    for row, target in zip(rows, targets):
        for left in range(width):
            for other in range(width):
                normal[left][other] += row[left] * row[other]
            right[left] += row[left] * target
    if matrix_rank(normal) != width:
        raise Refusal("historical changeover equations are rank deficient")
    return solve_square(normal, right)


def normalize_measure(value: float, label: str) -> float:
    if not math.isfinite(value):
        raise Refusal(f"{label} is not finite")
    if value < -FIT_EPSILON:
        raise Refusal(f"{label} is negative")
    value = max(0.0, value)
    nearest = round(value)
    if abs(value - nearest) <= ROUND_EPSILON:
        return float(nearest)
    return value


def recover_durations(case: dict[str, Any]) -> dict[str, dict[str, float]]:
    job_ids = [item["id"] for item in case["jobs"]]
    equipment_ids = [item["id"] for item in case["equipment"]]
    batch_rows = [
        [float(observation["batch"][job_id]) for job_id in job_ids]
        for observation in case["observations"]
    ]
    design_transpose = [
        [batch_rows[row][column] for row in range(len(batch_rows))]
        for column in range(len(job_ids))
    ]
    if matrix_rank(design_transpose) != len(job_ids):
        raise Refusal("batch history is rank deficient for duration recovery")

    normal = [[0.0 for _ in job_ids] for _ in job_ids]
    for left in range(len(job_ids)):
        for right in range(len(job_ids)):
            normal[left][right] = math.fsum(
                observation_row[left] * observation_row[right]
                for observation_row in batch_rows
            )

    recovered: dict[str, dict[str, float]] = {}
    for equipment_id in equipment_ids:
        minutes = [
            observation["machine_minutes"][equipment_id]
            for observation in case["observations"]
        ]
        right = [
            math.fsum(row[index] * value for row, value in zip(batch_rows, minutes))
            for index in range(len(job_ids))
        ]
        coefficients = solve_square(normal, right)
        duration_row = {
            job_id: normalize_measure(coefficients[index], f"duration {equipment_id}/{job_id}")
            for index, job_id in enumerate(job_ids)
        }
        for observation, measured in zip(case["observations"], minutes):
            reconstructed = math.fsum(
                observation["batch"][job_id] * duration_row[job_id]
                for job_id in job_ids
            )
            if not close(reconstructed, measured):
                raise Refusal("recovered durations do not reproduce machine minutes")
        if not any(value > QUALIFICATION_EPSILON for value in duration_row.values()):
            raise Refusal(f"equipment {equipment_id} has no observable qualification")
        recovered[equipment_id] = duration_row
    return recovered


def qualification_matrix(
    case: dict[str, Any],
    durations: dict[str, dict[str, float]],
) -> dict[str, dict[str, bool]]:
    return {
        equipment["id"]: {
            job["id"]: durations[equipment["id"]][job["id"]] > QUALIFICATION_EPSILON
            for job in case["jobs"]
        }
        for equipment in case["equipment"]
    }


def recover_history_mapping(
    case: dict[str, Any],
    qualification: dict[str, dict[str, bool]],
) -> dict[str, str]:
    equipment_ids = [item["id"] for item in case["equipment"]]
    mapping: dict[str, str] = {}
    for observation in case["observations"]:
        assigned_equipment: list[str] = []
        for sequence in observation["sequences"]:
            eligible = [
                equipment_id
                for equipment_id in equipment_ids
                if all(qualification[equipment_id][job_id] for job_id in sequence["order"])
            ]
            if len(eligible) != 1:
                raise Refusal(
                    f"sequence {sequence['sequence_id']} does not have one qualified equipment"
                )
            mapping[sequence["sequence_id"]] = eligible[0]
            assigned_equipment.append(eligible[0])
        if set(assigned_equipment) != set(equipment_ids):
            raise Refusal("one historical summary omits an equipment")
    return mapping


def recover_changeover_model(
    case: dict[str, Any],
    durations: dict[str, dict[str, float]],
    history_mapping: dict[str, str],
) -> dict[str, dict[str, float]]:
    job_ids = [item["id"] for item in case["jobs"]]
    directions = [
        (source, target)
        for source in job_ids
        for target in job_ids
        if source != target
    ]
    direction_index = {
        direction: index for index, direction in enumerate(directions)
    }
    rows: list[list[float]] = []
    targets: list[float] = []
    for observation in case["observations"]:
        for sequence in observation["sequences"]:
            equipment_id = history_mapping[sequence["sequence_id"]]
            row = [0.0 for _ in directions]
            for first, second in zip(sequence["order"], sequence["order"][1:]):
                row[direction_index[(first, second)]] += 1.0
            processing = math.fsum(
                durations[equipment_id][job_id] for job_id in sequence["order"]
            )
            setup_target = sequence["makespan"] - processing
            if setup_target < -FIT_EPSILON:
                raise Refusal("history makespan is shorter than its processing time")
            rows.append(row)
            targets.append(setup_target)

    coefficients = least_squares(rows, targets)
    if any(value < -FIT_EPSILON for value in coefficients):
        raise Refusal("recovered directed changeover time is negative")
    normalized = {
        direction: normalize_measure(coefficients[index], f"changeover {direction[0]}->{direction[1]}")
        for index, direction in enumerate(directions)
    }
    changeover = {
        job_id: {other: 0.0 for other in job_ids}
        for job_id in job_ids
    }
    for (source, target), value in normalized.items():
        changeover[source][target] = value

    for observation in case["observations"]:
        for sequence in observation["sequences"]:
            equipment_id = history_mapping[sequence["sequence_id"]]
            reconstructed = math.fsum(
                durations[equipment_id][job_id] for job_id in sequence["order"]
            )
            reconstructed += math.fsum(
                changeover[first][second]
                for first, second in zip(sequence["order"], sequence["order"][1:])
            )
            if not close(reconstructed, sequence["makespan"]):
                raise Refusal("recovered changeover model does not reproduce history")
    return changeover


def insert_pareto(
    states: dict[tuple[int, int], list[tuple[float, float, tuple[str, ...]]]],
    key: tuple[int, int],
    candidate: tuple[float, float, tuple[str, ...]],
) -> None:
    bucket = states.setdefault(key, [])
    candidate_cost, candidate_elapsed, _ = candidate
    for cost, elapsed, _ in bucket:
        if cost <= candidate_cost + FIT_EPSILON and elapsed <= candidate_elapsed + FIT_EPSILON:
            return
    bucket[:] = [
        existing
        for existing in bucket
        if not (
            candidate_cost <= existing[0] + FIT_EPSILON
            and candidate_elapsed <= existing[1] + FIT_EPSILON
        )
    ]
    bucket.append(candidate)


def equipment_order_table(
    case: dict[str, Any],
    equipment_id: str,
    durations: dict[str, dict[str, float]],
    qualification: dict[str, dict[str, bool]],
    changeover: dict[str, dict[str, float]],
) -> dict[int, tuple[float, float, tuple[str, ...]]]:
    jobs = case["jobs"]
    size = len(jobs)
    states: dict[
        tuple[int, int],
        list[tuple[float, float, tuple[str, ...]]],
    ] = {}
    for index, job in enumerate(jobs):
        job_id = job["id"]
        if not qualification[equipment_id][job_id]:
            continue
        duration = durations[equipment_id][job_id]
        insert_pareto(
            states,
            (1 << index, index),
            (job["weight"] * duration, duration, (job_id,)),
        )

    for mask in range(1, 1 << size):
        for last in range(size):
            for cost, elapsed, order in list(states.get((mask, last), [])):
                last_job = jobs[last]["id"]
                for following in range(size):
                    if mask & (1 << following):
                        continue
                    next_job = jobs[following]["id"]
                    if not qualification[equipment_id][next_job]:
                        continue
                    next_elapsed = (
                        elapsed
                        + changeover[last_job][next_job]
                        + durations[equipment_id][next_job]
                    )
                    next_cost = cost + jobs[following]["weight"] * next_elapsed
                    insert_pareto(
                        states,
                        (mask | (1 << following), following),
                        (next_cost, next_elapsed, order + (next_job,)),
                    )

    best: dict[int, tuple[float, float, tuple[str, ...]]] = {
        0: (0.0, 0.0, ())
    }
    for (mask, _), values in states.items():
        candidate = min(values)
        existing = best.get(mask)
        if existing is None or candidate < existing:
            best[mask] = candidate
    return best


def optimize_schedule(
    case: dict[str, Any],
    durations: dict[str, dict[str, float]],
    qualification: dict[str, dict[str, bool]],
    changeover: dict[str, dict[str, float]],
) -> tuple[dict[str, str], dict[str, list[str]], float, float, int]:
    equipment_ids = [item["id"] for item in case["equipment"]]
    job_ids = [item["id"] for item in case["jobs"]]
    tables = {
        equipment_id: equipment_order_table(
            case,
            equipment_id,
            durations,
            qualification,
            changeover,
        )
        for equipment_id in equipment_ids
    }
    choices = [
        [
            equipment_id
            for equipment_id in equipment_ids
            if qualification[equipment_id][job_id]
        ]
        for job_id in job_ids
    ]
    if any(not options for options in choices):
        raise Refusal("at least one job has no qualified equipment")

    best_key: tuple[Any, ...] | None = None
    best_assignment: tuple[str, ...] | None = None
    best_schedule: dict[str, list[str]] | None = None
    best_total = 0.0
    best_makespan = 0.0
    nodes = 0

    for assignment in itertools.product(*choices):
        nodes += 1
        masks = {equipment_id: 0 for equipment_id in equipment_ids}
        for index, equipment_id in enumerate(assignment):
            masks[equipment_id] |= 1 << index
        entries = [tables[equipment_id].get(masks[equipment_id]) for equipment_id in equipment_ids]
        if any(entry is None for entry in entries):
            continue
        total = math.fsum(entry[0] for entry in entries if entry is not None)
        makespan = max(
            (entry[1] for entry in entries if entry is not None),
            default=0.0,
        )
        schedule = {
            equipment_id: list(tables[equipment_id][masks[equipment_id]][2])
            for equipment_id in equipment_ids
        }
        sequence_key = tuple(tuple(schedule[equipment_id]) for equipment_id in equipment_ids)
        key = (total, makespan, assignment, sequence_key)
        if best_key is None or key < best_key:
            best_key = key
            best_assignment = assignment
            best_schedule = schedule
            best_total = total
            best_makespan = makespan

    if best_assignment is None or best_schedule is None:
        raise Refusal("no complete qualified schedule exists")
    assignments = {
        job_id: best_assignment[index] for index, job_id in enumerate(job_ids)
    }
    return assignments, best_schedule, best_total, best_makespan, nodes


def build_report(case: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    durations = recover_durations(case)
    qualification = qualification_matrix(case, durations)
    history_mapping = recover_history_mapping(case, qualification)
    changeover = recover_changeover_model(case, durations, history_mapping)
    assignments, schedule, total, makespan, nodes = optimize_schedule(
        case,
        durations,
        qualification,
        changeover,
    )
    base = {
        "assignments": assignments,
        "case_id": case["case_id"],
        "changeover_matrix": changeover,
        "duration_matrix": durations,
        "history_mapping": history_mapping,
        "makespan": makespan,
        "qualification": qualification,
        "schedule": schedule,
        "schema_version": 1,
        "solver_nodes": nodes,
        "total_weighted_completion": total,
    }
    checksum = hashlib.sha256(compact_bytes(base)).hexdigest()
    report_core = {**base, "checksum": checksum}
    entry = {
        "case_id": case["case_id"],
        "input_sha256": hashlib.sha256(compact_bytes(case)).hexdigest(),
        "report_sha256": hashlib.sha256(compact_bytes(report_core)).hexdigest(),
        "status": "completed",
    }
    result = {**report_core, "journal_entry": entry}
    return result, compact_bytes(result)


def read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Refusal(f"cannot read journal: {exc}") from exc
    if text and not text.endswith("\n"):
        raise Refusal("journal must end with one newline")
    records: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line:
            raise Refusal(f"journal line {number} is empty")
        try:
            value = json.loads(line, object_pairs_hook=reject_duplicates)
        except (json.JSONDecodeError, Refusal) as exc:
            raise Refusal(f"journal line {number} is invalid: {exc}") from exc
        if not isinstance(value, dict):
            raise Refusal("journal entries must be objects")
        exact_keys(value, ENTRY_KEYS, f"journal line {number}")
        if value["status"] != "completed":
            raise Refusal("journal status must be completed")
        records.append(value)
    return records


def stage(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


def restore_path(path: Path, old_bytes: bytes | None) -> None:
    if old_bytes is None:
        path.unlink(missing_ok=True)
        return
    staged = stage(path, old_bytes)
    try:
        os.replace(staged, path)
        staged = None
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def publish(report_path: Path, journal_path: Path, result: dict[str, Any], payload: bytes) -> None:
    if report_path == journal_path:
        raise Refusal("report and journal paths must differ")
    lock_path = journal_path.with_name(journal_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    report_parent = report_path.parent
    report_parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        records = read_journal(journal_path)
        existing = next(
            (
                record
                for record in records
                if all(record[key] == result["journal_entry"][key] for key in ENTRY_KEYS)
            ),
            None,
        )
        old_journal = journal_path.read_bytes() if journal_path.exists() else None
        old_report = report_path.read_bytes() if report_path.is_file() else None
        new_journal = None
        if existing is None:
            new_journal = b"".join(
                compact_bytes(record) for record in [*records, result["journal_entry"]]
            )

        report_stage = stage(report_path, payload) if old_report != payload else None
        journal_stage = stage(journal_path, new_journal) if new_journal is not None else None
        journal_replaced = False
        try:
            if journal_stage is not None:
                os.replace(journal_stage, journal_path)
                journal_stage = None
                journal_replaced = True
            if report_stage is not None:
                os.replace(report_stage, report_path)
                report_stage = None
        except Exception:
            if journal_replaced:
                restore_path(journal_path, old_journal)
            raise
        finally:
            if report_stage is not None:
                report_stage.unlink(missing_ok=True)
            if journal_stage is not None:
                journal_stage.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario")
    parser.add_argument("--journal")
    parser.add_argument("--report")
    parser.add_argument("--inspect", action="store_true")
    return parser.parse_args()


def one_line(message: object) -> str:
    return " ".join(str(message).splitlines())


def main() -> int:
    args = parse_args()
    if args.inspect:
        return 0
    if not args.scenario or not args.journal or not args.report:
        print(REFUSED + "the shift needs a scenario, journal, and report path", file=sys.stderr)
        return 3
    try:
        case = validate(read_scenario(Path(args.scenario)))
        result, payload = build_report(case)
        publish(Path(args.report), Path(args.journal), result, payload)
        return 0
    except (Refusal, OSError, ValueError, TypeError, KeyError) as exc:
        print(REFUSED + one_line(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
