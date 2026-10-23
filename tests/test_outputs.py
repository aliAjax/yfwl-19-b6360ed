"""Behavior tests for anonymous-history recovery and shift scheduling."""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


ENTRY = os.environ.get("SHIFT_PLANNER_ENTRY", "/app/shift_planner.py")
REFUSED = "SCHEDULING-CASE-REFUSED: "
EQUIPMENT = [{"id": "M-A"}, {"id": "M-B"}, {"id": "M-C"}]
JOBS = [
    {"id": "J-ALPHA", "weight": 5.0},
    {"id": "J-BRAVO", "weight": 2.0},
    {"id": "J-CEDAR", "weight": 4.0},
    {"id": "J-DELTA", "weight": 3.0},
    {"id": "J-ECHO", "weight": 1.0},
]
DURATIONS = {
    "M-A": {
        "J-ALPHA": 3.0,
        "J-BRAVO": 0.0,
        "J-CEDAR": 2.0,
        "J-DELTA": 0.0,
        "J-ECHO": 4.0,
    },
    "M-B": {
        "J-ALPHA": 0.0,
        "J-BRAVO": 2.0,
        "J-CEDAR": 1.0,
        "J-DELTA": 3.0,
        "J-ECHO": 1.0,
    },
    "M-C": {
        "J-ALPHA": 4.0,
        "J-BRAVO": 3.0,
        "J-CEDAR": 0.0,
        "J-DELTA": 2.0,
        "J-ECHO": 0.0,
    },
}
QUALIFIED = {
    "M-A": ["J-ALPHA", "J-CEDAR", "J-ECHO"],
    "M-B": ["J-BRAVO", "J-CEDAR", "J-DELTA", "J-ECHO"],
    "M-C": ["J-ALPHA", "J-BRAVO", "J-DELTA"],
}
CHANGEOVER_JOBS = [
    "J-ALPHA",
    "J-BRAVO",
    "J-CEDAR",
    "J-DELTA",
    "J-ECHO",
    "J-FOXTROT",
    "J-GOLF",
]
CHANGEOVER_VALUES = {
    (source, target): float((source_index * 3 + target_index * 2) % 6 + 1)
    for source_index, source in enumerate(CHANGEOVER_JOBS)
    for target_index, target in enumerate(CHANGEOVER_JOBS)
    if source != target
}
BATCHES = [
    [3, 0, 0, 0, 0],
    [0, 3, 0, 0, 0],
    [0, 0, 3, 0, 0],
    [0, 0, 0, 3, 0],
    [0, 0, 0, 0, 3],
    [1, 2, 1, 2, 1],
    [2, 1, 0, 3, 1],
    [0, 2, 3, 1, 3],
    [3, 0, 2, 1, 2],
    [1, 3, 1, 0, 2],
    [2, 2, 1, 2, 0],
    [0, 1, 2, 3, 1],
]
LARGE_JOBS = [
    {"id": "J-ALPHA", "weight": 5.0},
    {"id": "J-BRAVO", "weight": 2.0},
    {"id": "J-CEDAR", "weight": 4.0},
    {"id": "J-DELTA", "weight": 3.0},
    {"id": "J-ECHO", "weight": 1.0},
    {"id": "J-FOXTROT", "weight": 6.0},
    {"id": "J-GOLF", "weight": 3.0},
]
LARGE_DURATIONS = {
    "M-A": {
        "J-ALPHA": 3.0,
        "J-BRAVO": 0.0,
        "J-CEDAR": 2.0,
        "J-DELTA": 0.0,
        "J-ECHO": 4.0,
        "J-FOXTROT": 2.0,
        "J-GOLF": 0.0,
    },
    "M-B": {
        "J-ALPHA": 0.0,
        "J-BRAVO": 2.0,
        "J-CEDAR": 1.0,
        "J-DELTA": 3.0,
        "J-ECHO": 1.0,
        "J-FOXTROT": 0.0,
        "J-GOLF": 2.0,
    },
    "M-C": {
        "J-ALPHA": 4.0,
        "J-BRAVO": 3.0,
        "J-CEDAR": 0.0,
        "J-DELTA": 2.0,
        "J-ECHO": 0.0,
        "J-FOXTROT": 3.0,
        "J-GOLF": 2.0,
    },
}
LARGE_QUALIFIED = {
    "M-A": ["J-ALPHA", "J-CEDAR", "J-ECHO", "J-FOXTROT"],
    "M-B": ["J-BRAVO", "J-CEDAR", "J-DELTA", "J-ECHO", "J-GOLF"],
    "M-C": ["J-ALPHA", "J-BRAVO", "J-DELTA", "J-FOXTROT", "J-GOLF"],
}
LARGE_BATCHES = [
    [3, 0, 0, 0, 0, 0, 0],
    [0, 3, 0, 0, 0, 0, 0],
    [0, 0, 3, 0, 0, 0, 0],
    [0, 0, 0, 3, 0, 0, 0],
    [0, 0, 0, 0, 3, 0, 0],
    [0, 0, 0, 0, 0, 3, 0],
    [0, 0, 0, 0, 0, 0, 3],
    [1, 2, 1, 2, 1, 2, 1],
    [2, 1, 0, 3, 1, 2, 1],
    [0, 2, 3, 1, 3, 0, 2],
    [3, 0, 2, 1, 2, 2, 1],
    [1, 3, 1, 2, 0, 1, 2],
]


def compact(value: object) -> bytes:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def changeover(source: str, target: str) -> float:
    return 0.0 if source == target else CHANGEOVER_VALUES[(source, target)]


def equipment_orders(equipment_id: str, qualified: dict[str, list[str]], count: int, step: int) -> list:
    patterns = list(itertools.permutations(qualified[equipment_id]))
    return [list(patterns[(index * step) % len(patterns)]) for index in range(count)]


def build_payload(
    jobs: list[dict] | None = None,
    durations: dict[str, dict[str, float]] | list[float] | None = None,
    qualified: dict[str, list[str]] | None = None,
    batches: list[list[int]] | None = None,
    case_id: str = "PLANT-2026-NIGHT",
    equipment: list[dict] | None = None,
    steps: dict[str, int] | None = None,
) -> dict:
    selected_jobs = copy.deepcopy(JOBS if jobs is None else jobs)
    selected_equipment = copy.deepcopy(EQUIPMENT if equipment is None else equipment)
    selected_qualified = copy.deepcopy(QUALIFIED if qualified is None else qualified)
    selected_batches = copy.deepcopy(BATCHES if batches is None else batches)
    selected_steps = {"M-A": 1, "M-B": 1, "M-C": 1}
    if steps is not None:
        selected_steps.update(steps)

    job_ids = [item["id"] for item in selected_jobs]
    if durations is None:
        selected_durations = copy.deepcopy(DURATIONS)
    elif isinstance(durations, list):
        selected_durations = {
            equipment["id"]: {
                job_id: float(durations[index][job_index])
                for job_index, job_id in enumerate(job_ids)
            }
            for index, equipment in enumerate(EQUIPMENT)
        }
    else:
        selected_durations = copy.deepcopy(durations)

    orders_by_equipment = {
        equipment_id: equipment_orders(
            equipment_id,
            selected_qualified,
            len(selected_batches) * 4 // 3 + 4,
            selected_steps[equipment_id],
        )
        for equipment_id in selected_qualified
    }
    use_count = {equipment_id: 0 for equipment_id in selected_qualified}
    rotation = ["M-A", "M-B", "M-C"]
    observations = []
    for observation_index, batch in enumerate(selected_batches):
        machine_minutes = {
            equipment_id: math.fsum(
                batch[job_index] * selected_durations[equipment_id][job_id]
                for job_index, job_id in enumerate(job_ids)
            )
            for equipment_id in selected_durations
        }
        slots = [
            rotation[observation_index % 3],
            rotation[(observation_index + 1) % 3],
            rotation[(observation_index + 2) % 3],
            rotation[observation_index % 3],
        ]
        sequences = []
        for sequence_index, equipment_id in enumerate(slots):
            order = orders_by_equipment[equipment_id][use_count[equipment_id]]
            use_count[equipment_id] += 1
            makespan = math.fsum(selected_durations[equipment_id][job_id] for job_id in order)
            makespan += math.fsum(
                changeover(first, second)
                for first, second in zip(order, order[1:])
            )
            sequences.append(
                {
                    "makespan": makespan,
                    "order": order,
                    "sequence_id": f"HIST-{observation_index + 1:02d}-{sequence_index + 1}",
                }
            )
        observations.append(
            {
                "batch": {
                    job_id: batch[index] for index, job_id in enumerate(job_ids)
                },
                "machine_minutes": machine_minutes,
                "observation": f"SUMMARY-{observation_index + 1:02d}",
                "sequences": sequences,
            }
        )
    return {
        "case_id": case_id,
        "equipment": selected_equipment,
        "jobs": selected_jobs,
        "observations": observations,
        "schema_version": 1,
        "unit": "minute",
    }


def build_large_payload() -> dict:
    return build_payload(
        jobs=LARGE_JOBS,
        durations=LARGE_DURATIONS,
        qualified=LARGE_QUALIFIED,
        batches=LARGE_BATCHES,
        case_id="PLANT-2027-SWING",
        steps={"M-A": 1, "M-B": 7, "M-C": 7},
    )


def invoke(
    tmp_path: Path,
    payload: dict | None = None,
    *,
    raw: str | None = None,
    journal_name: str = "dispatch.jsonl",
    report_name: str = "dispatch.json",
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    scenario = tmp_path / "input" / "scenario.json"
    scenario.parent.mkdir(parents=True, exist_ok=True)
    if raw is None:
        scenario.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    else:
        scenario.write_text(raw, encoding="utf-8")
    journal = tmp_path / "state" / journal_name
    report = tmp_path / "out" / report_name
    process = subprocess.run(
        [
            sys.executable,
            ENTRY,
            "--scenario",
            str(scenario),
            "--journal",
            str(journal),
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        timeout=4,
    )
    return process, scenario, journal, report


def read_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def expected_schedule() -> tuple[dict[str, str], dict[str, list[str]], float, float]:
    equipment_ids = [item["id"] for item in EQUIPMENT]
    job_ids = [item["id"] for item in JOBS]
    weights = {item["id"]: item["weight"] for item in JOBS}
    best_key: tuple | None = None
    best_assignment: tuple[str, ...] | None = None
    best_schedule: dict[str, list[str]] | None = None
    best_total = 0.0
    best_makespan = 0.0
    for order in itertools.permutations(job_ids):
        for assignment in itertools.product(equipment_ids, repeat=len(job_ids)):
            assignment_lookup = dict(zip(job_ids, assignment))
            if any(DURATIONS[assignment_lookup[job_id]][job_id] <= 0.0 for job_id in job_ids):
                continue
            schedule = {equipment_id: [] for equipment_id in equipment_ids}
            for job_id in order:
                schedule[assignment_lookup[job_id]].append(job_id)
            total = 0.0
            makespan = 0.0
            for equipment_id in equipment_ids:
                elapsed = 0.0
                previous = None
                for job_id in schedule[equipment_id]:
                    elapsed += 0.0 if previous is None else changeover(previous, job_id)
                    elapsed += DURATIONS[equipment_id][job_id]
                    total += weights[job_id] * elapsed
                    previous = job_id
                makespan = max(makespan, elapsed)
            assignment_vector = tuple(assignment_lookup[job_id] for job_id in job_ids)
            sequence = tuple(tuple(schedule[equipment_id]) for equipment_id in equipment_ids)
            key = (round(total, 9), round(makespan, 9), assignment_vector, sequence)
            if best_key is None or key < best_key:
                best_key = key
                best_assignment = assignment_vector
                best_schedule = schedule
                best_total = total
                best_makespan = makespan
    assert best_assignment is not None
    assert best_schedule is not None
    assignments = {
        job_id: best_assignment[index] for index, job_id in enumerate(job_ids)
    }
    return assignments, best_schedule, best_total, best_makespan


def test_existing_inspect_mode_returns_zero() -> None:
    """题面：「原有的 `--inspect` 检查仍要返回 0，不读取业务文件」"""
    process = subprocess.run(
        [sys.executable, ENTRY, "--inspect"],
        capture_output=True,
        text=True,
        timeout=4,
    )
    assert process.returncode == 0
    assert process.stdout == ""
    assert process.stderr == ""


def test_success_is_silent_and_creates_report(tmp_path: Path) -> None:
    """题面：「成功运行时控制台保持安静，退出码为 0」"""
    process, _, journal, report = invoke(tmp_path, build_payload())
    assert process.returncode == 0
    assert process.stdout == ""
    assert process.stderr == ""
    assert report.is_file()
    assert journal.is_file()


def test_missing_argument_returns_three(tmp_path: Path) -> None:
    """题面：「诊断只占标准错误的一行」"""
    report_path = tmp_path / "out" / "case_03.json"
    journal_path = tmp_path / "state" / "case_03.jsonl"
    process = subprocess.run(
        [sys.executable, ENTRY, "--scenario", str(tmp_path / "case_03.json")],
        capture_output=True,
        text=True,
        timeout=4,
    )
    assert process.returncode == 3
    assert process.stdout == ""
    assert process.stderr.startswith(REFUSED)
    assert len(process.stderr.splitlines()) == 1
    assert report_path.exists() is False
    assert journal_path.exists() is False


def test_malformed_json_returns_three(tmp_path: Path) -> None:
    """题面：「交接单最外层只使用下表六格，表外的扩展名一律不接收：」"""
    process, _, journal, report = invoke(tmp_path, raw="{")
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stdout == ""


def test_refusal_prefix_is_exact(tmp_path: Path) -> None:
    """题面：「且该行以 `SCHEDULING-CASE-REFUSED: ` 开头」"""
    process, _, journal, report = invoke(tmp_path, raw="{")
    assert process.returncode == 3
    assert process.stdout == ""
    assert process.stderr.startswith(REFUSED)
    assert report.exists() is False
    assert journal.exists() is False


def test_duplicate_top_level_key_returns_three(tmp_path: Path) -> None:
    """题面：「交接单最外层只使用下表六格，表外的扩展名一律不接收：」"""
    raw = (
        '{"case_id":"A-2026-NIGHT","case_id":"B-2026-DAY","equipment":[],"jobs":[],'
        '"observations":[],"schema_version":1,"unit":"minute"}'
    )
    process, _, journal, report = invoke(tmp_path, raw=raw)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_top_level_array_returns_three(tmp_path: Path) -> None:
    """题面：「交接单最外层只使用下表六格，表外的扩展名一律不接收：」"""
    process, _, journal, report = invoke(tmp_path, raw="[]")
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_missing_top_level_key_returns_three(tmp_path: Path) -> None:
    """题面：「交接单最外层只使用下表六格，表外的扩展名一律不接收：」"""
    payload = build_payload()
    del payload["unit"]
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_extra_top_level_key_returns_three(tmp_path: Path) -> None:
    """题面：「交接单最外层只使用下表六格，表外的扩展名一律不接收：」"""
    payload = build_payload()
    payload["draft"] = True
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_schema_version_bool_returns_three(tmp_path: Path) -> None:
    """题面：「版本位写整数 1；计量单位写 `"minute"`；班次编号要有年份段和班次段」"""
    payload = build_payload()
    payload["schema_version"] = False
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_unit_value_returns_three(tmp_path: Path) -> None:
    """题面：「版本位写整数 1；计量单位写 `"minute"`；班次编号要有年份段和班次段」"""
    payload = build_payload()
    payload["unit"] = "hour"
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_case_id_without_shift_returns_three(tmp_path: Path) -> None:
    """题面：「班次编号要有年份段和班次段」"""
    process, _, journal, report = invoke(tmp_path, build_payload(case_id="PLANT-2026"))
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_equipment_count_returns_three(tmp_path: Path) -> None:
    """题面：「`equipment` 正好三条，每条只有 `id`」"""
    payload = build_payload()
    payload["equipment"] = payload["equipment"][:2]
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_equipment_extra_key_returns_three(tmp_path: Path) -> None:
    """题面：「`equipment` 正好三条，每条只有 `id`」"""
    payload = build_payload()
    payload["equipment"][0]["label"] = "drill"
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_duplicate_equipment_returns_three(tmp_path: Path) -> None:
    """题面：「设备编号、工单编号和汇总回执号各自不能重样」"""
    payload = build_payload()
    payload["equipment"][1]["id"] = payload["equipment"][0]["id"]
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_too_few_jobs_returns_three(tmp_path: Path) -> None:
    """题面：「`jobs` 写本班 4 到 7 张工单」"""
    payload = build_payload()
    payload["jobs"] = payload["jobs"][:3]
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_too_many_jobs_returns_three(tmp_path: Path) -> None:
    """题面：「`jobs` 写本班 4 到 7 张工单」"""
    payload = build_large_payload()
    payload["jobs"].append({"id": "J-HOTEL", "weight": 1.0})
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_job_extra_key_returns_three(tmp_path: Path) -> None:
    """题面：「每张只有 `id` 和 `weight`」"""
    payload = build_payload()
    payload["jobs"][0]["due"] = "early"
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_duplicate_job_returns_three(tmp_path: Path) -> None:
    """题面：「设备编号、工单编号和汇总回执号各自不能重样」"""
    payload = build_payload()
    payload["jobs"][1]["id"] = payload["jobs"][0]["id"]
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_weight_bool_returns_three(tmp_path: Path) -> None:
    """题面：「权重是大于零的有限实数；布尔值不能冒充数字」"""
    payload = build_payload()
    payload["jobs"][0]["weight"] = True
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_weight_zero_returns_three(tmp_path: Path) -> None:
    """题面：「权重是大于零的有限实数；布尔值不能冒充数字」"""
    payload = build_payload()
    payload["jobs"][0]["weight"] = 0.0
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_too_few_observations_returns_three(tmp_path: Path) -> None:
    """题面：「`observations` 是 6 到 12 份历史汇总」"""
    payload = build_payload()
    payload["observations"] = payload["observations"][:5]
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_too_many_observations_returns_three(tmp_path: Path) -> None:
    """题面：「`observations` 是 6 到 12 份历史汇总」"""
    payload = build_payload()
    payload["observations"].append(copy.deepcopy(payload["observations"][-1]))
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_duplicate_observation_returns_three(tmp_path: Path) -> None:
    """题面：「设备编号、工单编号和汇总回执号各自不能重样」"""
    payload = build_payload()
    payload["observations"][1]["observation"] = payload["observations"][0]["observation"]
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_batch_missing_job_returns_three(tmp_path: Path) -> None:
    """题面：「`batch` 的键要逐一覆盖工单」"""
    payload = build_payload()
    del payload["observations"][2]["batch"]["J-CEDAR"]
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_batch_extra_job_returns_three(tmp_path: Path) -> None:
    """题面：「`batch` 的键要逐一覆盖工单」"""
    payload = build_payload()
    payload["observations"][2]["batch"]["J-HOTEL"] = 1
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_batch_bool_returns_three(tmp_path: Path) -> None:
    """题面：「值只能是 0、1、2、3 这几个整数，布尔值和小数都不算批次」"""
    payload = build_payload()
    payload["observations"][2]["batch"]["J-CEDAR"] = False
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_batch_above_three_returns_three(tmp_path: Path) -> None:
    """题面：「值只能是 0、1、2、3 这几个整数，布尔值和小数都不算批次」"""
    payload = build_payload()
    payload["observations"][2]["batch"]["J-CEDAR"] = 4
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_machine_minutes_missing_equipment_returns_three(tmp_path: Path) -> None:
    """题面：「`machine_minutes` 的键要逐一覆盖设备」"""
    payload = build_payload()
    del payload["observations"][0]["machine_minutes"]["M-C"]
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_machine_minutes_negative_returns_three(tmp_path: Path) -> None:
    """题面：「数值为非负有限分钟」"""
    payload = build_payload()
    payload["observations"][0]["machine_minutes"]["M-A"] = -1.0
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_sequences_count_returns_three(tmp_path: Path) -> None:
    """题面：「`sequences` 固定放四条匿名历史顺序」"""
    payload = build_payload()
    payload["observations"][0]["sequences"].pop()
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_sequence_extra_key_returns_three(tmp_path: Path) -> None:
    """题面：「每条只有 `makespan`、`order`、`sequence_id`」"""
    payload = build_payload()
    payload["observations"][0]["sequences"][0]["machine"] = "M-A"
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_duplicate_sequence_id_returns_three(tmp_path: Path) -> None:
    """题面：「`sequence_id` 在同一次场景内唯一」"""
    payload = build_payload()
    payload["observations"][1]["sequences"][0]["sequence_id"] = payload["observations"][0][
        "sequences"
    ][0]["sequence_id"]
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_sequence_repeats_job_returns_three(tmp_path: Path) -> None:
    """题面：「`order` 是不重复的已知工单编号」"""
    payload = build_payload()
    payload["observations"][0]["sequences"][0]["order"] = [
        payload["observations"][0]["sequences"][0]["order"][0],
        payload["observations"][0]["sequences"][0]["order"][0],
    ]
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_sequence_unknown_job_returns_three(tmp_path: Path) -> None:
    """题面：「`order` 是不重复的已知工单编号」"""
    payload = build_payload()
    payload["observations"][0]["sequences"][0]["order"] = ["J-ALPHA", "J-HOTEL"]
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_sequence_negative_makespan_returns_three(tmp_path: Path) -> None:
    """题面：「`makespan` 是非负有限分钟」"""
    payload = build_payload()
    payload["observations"][0]["sequences"][0]["makespan"] = -1.0
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_duration_matrix_recovered(tmp_path: Path) -> None:
    """题面：「工时接近零表示这台设备没有该工单资格」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    matrix = read_report(report_path)["duration_matrix"]
    assert process.returncode == 0
    assert matrix["M-A"]["J-ALPHA"] == pytest.approx(3.0, abs=1e-7)
    assert matrix["M-B"]["J-DELTA"] == pytest.approx(3.0, abs=1e-7)
    assert matrix["M-C"]["J-BRAVO"] == pytest.approx(3.0, abs=1e-7)


def test_qualification_matrix_recovered(tmp_path: Path) -> None:
    """题面：「工时接近零表示这台设备没有该工单资格」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    qualification = read_report(report_path)["qualification"]
    assert process.returncode == 0
    assert qualification["M-A"]["J-ALPHA"] is True
    assert qualification["M-A"]["J-BRAVO"] is False
    assert qualification["M-C"]["J-CEDAR"] is False


def test_directed_changeover_matrix_recovered(tmp_path: Path) -> None:
    """题面：「`changeover[a][b]` 表示同一设备先做 `a` 再做 `b` 的准备分钟」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    matrix = read_report(report_path)["changeover_matrix"]
    assert process.returncode == 0
    assert matrix["J-ALPHA"]["J-BRAVO"] == pytest.approx(3.0, abs=1e-7)
    assert matrix["J-BRAVO"]["J-ALPHA"] == pytest.approx(4.0, abs=1e-7)
    assert matrix["J-ALPHA"]["J-BRAVO"] != matrix["J-BRAVO"]["J-ALPHA"]


def test_changeover_diagonal_is_zero(tmp_path: Path) -> None:
    """题面：「`changeover[a][a]` 为 0」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    matrix = read_report(report_path)["changeover_matrix"]
    assert process.returncode == 0
    assert all(
        matrix[job_id][job_id] == pytest.approx(0.0, abs=1e-9)
        for job_id in matrix
    )


def test_history_mapping_recovered(tmp_path: Path) -> None:
    """题面：「就把 `sequence_id` 定到该设备」"""
    payload = build_payload()
    process, _, _, report_path = invoke(tmp_path, payload)
    mapping = read_report(report_path)["history_mapping"]
    assert process.returncode == 0
    assert len(mapping) == len(payload["observations"]) * 4
    assert mapping["HIST-01-1"] == "M-A"
    assert mapping["HIST-01-2"] == "M-B"
    assert mapping["HIST-01-3"] == "M-C"


def test_history_makespan_uses_directed_changeover(tmp_path: Path) -> None:
    """题面：「所有有方向的换型未知量要从这些顺序和 `makespan` 中一起回归」"""
    payload = build_payload()
    process, _, _, report_path = invoke(tmp_path, payload)
    report = read_report(report_path)
    sequence = payload["observations"][3]["sequences"][1]
    equipment_id = report["history_mapping"][sequence["sequence_id"]]
    reconstructed = math.fsum(
        report["duration_matrix"][equipment_id][job_id] for job_id in sequence["order"]
    )
    reconstructed += math.fsum(
        report["changeover_matrix"][first][second]
        for first, second in zip(sequence["order"], sequence["order"][1:])
    )
    assert process.returncode == 0
    assert reconstructed == pytest.approx(sequence["makespan"], abs=1e-7)


def test_ambiguous_history_order_returns_three(tmp_path: Path) -> None:
    """题面：「出现零台或超过一台可承接设备时拒绝，不能猜机台」"""
    payload = build_payload()
    payload["observations"][0]["sequences"][0]["order"] = ["J-CEDAR", "J-ECHO"]
    payload["observations"][0]["sequences"][0]["makespan"] = 9.0
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_summary_missing_equipment_returns_three(tmp_path: Path) -> None:
    """题面：「四条匿名顺序定完后，每份汇总中三台设备都不能缺席」"""
    payload = build_payload()
    for sequence in payload["observations"][0]["sequences"]:
        sequence["order"] = ["J-ALPHA", "J-CEDAR", "J-ECHO"]
        sequence["makespan"] = 9.0
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_duration_rank_deficiency_returns_three(tmp_path: Path) -> None:
    """题面：「全部汇总的批次数矩阵必须满列秩」"""
    payload = build_payload()
    for observation in payload["observations"]:
        observation["batch"] = {
            "J-ALPHA": 1,
            "J-BRAVO": 0,
            "J-CEDAR": 0,
            "J-DELTA": 0,
            "J-ECHO": 0,
        }
        observation["machine_minutes"] = {
            "M-A": 3.0,
            "M-B": 0.0,
            "M-C": 4.0,
        }
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_recovered_duration_values_remain_nonnegative(tmp_path: Path) -> None:
    """题面：「反演出的工时应一致且非负」"""
    payload = build_payload()
    payload["observations"][0]["machine_minutes"]["M-A"] = 0.0
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_equipment_without_work_returns_three(tmp_path: Path) -> None:
    """题面：「某台设备若没有任何可做工单，整份场景不能用」"""
    payload = build_payload()
    for observation in payload["observations"]:
        observation["machine_minutes"]["M-A"] = 0.0
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_changeover_rank_deficiency_returns_three(tmp_path: Path) -> None:
    """题面：「换型方程秩不足、解出负数或复算不一致都要拒绝」"""
    payload = build_payload()
    short_orders = {
        "M-A": ["J-ALPHA", "J-CEDAR"],
        "M-B": ["J-BRAVO", "J-CEDAR"],
        "M-C": ["J-ALPHA", "J-BRAVO"],
    }
    for observation in payload["observations"]:
        for sequence_index, equipment_id in enumerate(("M-A", "M-B", "M-C")):
            order = short_orders[equipment_id]
            sequence = observation["sequences"][sequence_index]
            sequence["order"] = order
            sequence["makespan"] = (
                math.fsum(DURATIONS[equipment_id][job_id] for job_id in order)
                + changeover(order[0], order[1])
            )
    process, _, journal, report = invoke(tmp_path, payload)
    assert process.returncode == 3
    assert report.exists() is False
    assert journal.exists() is False
    assert process.stderr.startswith(REFUSED)


def test_assignments_match_global_optimum(tmp_path: Path) -> None:
    """题面：「每张工单只做一次，只能落到具资格设备」"""
    expected_assignments, _, _, _ = expected_schedule()
    process, _, _, report_path = invoke(tmp_path, build_payload())
    report = read_report(report_path)
    assert process.returncode == 0
    assert report["assignments"] == expected_assignments


def test_schedule_sequence_matches_optimum(tmp_path: Path) -> None:
    """题面：「同一设备顺序加工两张不同工单时，后一张开始前要等对应的有向换型」"""
    _, expected_sequence, _, _ = expected_schedule()
    process, _, _, report_path = invoke(tmp_path, build_payload())
    report = read_report(report_path)
    assert process.returncode == 0
    assert report["schedule"] == expected_sequence


def test_total_weighted_completion_matches_optimum(tmp_path: Path) -> None:
    """题面：「优化目标是最小化 `sum(weight × completion_time)`」"""
    _, _, expected_total, _ = expected_schedule()
    process, _, _, report_path = invoke(tmp_path, build_payload())
    report = read_report(report_path)
    assert process.returncode == 0
    assert report["total_weighted_completion"] == pytest.approx(expected_total, abs=1e-9)


def test_makespan_matches_schedule(tmp_path: Path) -> None:
    """题面：「所有设备结束时刻的最大值」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    report = read_report(report_path)
    elapsed = {}
    for equipment_id, sequence in report["schedule"].items():
        elapsed[equipment_id] = math.fsum(
            report["duration_matrix"][equipment_id][job_id] for job_id in sequence
        )
        elapsed[equipment_id] += math.fsum(
            report["changeover_matrix"][first][second]
            for first, second in zip(sequence, sequence[1:])
        )
    assert process.returncode == 0
    assert report["makespan"] == pytest.approx(max(elapsed.values()), abs=1e-9)


def test_qualified_assignments_only(tmp_path: Path) -> None:
    """题面：「每张工单只做一次，只能落到具资格设备」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    report = read_report(report_path)
    assert process.returncode == 0
    assert all(
        report["qualification"][equipment_id][job_id]
        for job_id, equipment_id in report["assignments"].items()
    )


def test_report_exact_keys(tmp_path: Path) -> None:
    """题面：「最外层字段与下表完全一致，没有表外字段」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    report = read_report(report_path)
    assert process.returncode == 0
    assert set(report) == {
        "assignments",
        "case_id",
        "changeover_matrix",
        "checksum",
        "duration_matrix",
        "history_mapping",
        "journal_entry",
        "makespan",
        "qualification",
        "schedule",
        "schema_version",
        "solver_nodes",
        "total_weighted_completion",
    }


def test_nested_report_key_sets(tmp_path: Path) -> None:
    """题面：「| `schema_version` | 固定整数 1 |」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    report = read_report(report_path)
    equipment_ids = {item["id"] for item in EQUIPMENT}
    job_ids = {item["id"] for item in JOBS}
    assert process.returncode == 0
    assert set(report["duration_matrix"]) == equipment_ids
    assert all(set(row) == job_ids for row in report["duration_matrix"].values())
    assert set(report["qualification"]) == equipment_ids
    assert set(report["changeover_matrix"]) == job_ids
    assert set(report["assignments"]) == job_ids
    assert set(report["schedule"]) == equipment_ids
    assert type(report["schema_version"]) is int
    assert report["schema_version"] == 1


def test_journal_entry_exact_keys(tmp_path: Path) -> None:
    """题面：「流水行只有下表四列」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    entry = read_report(report_path)["journal_entry"]
    assert process.returncode == 0
    assert set(entry) == {"case_id", "input_sha256", "report_sha256", "status"}
    assert entry["status"] == "completed"


def test_journal_status_is_completed(tmp_path: Path) -> None:
    """题面：「完成态，只写 `"completed"`」"""
    process, _, journal, _ = invoke(tmp_path, build_payload())
    first_line = journal.read_text(encoding="utf-8").splitlines()[0]
    entry = json.loads(first_line)
    assert process.returncode == 0
    assert entry["status"] == "completed"


def test_solver_nodes_is_positive_integer(tmp_path: Path) -> None:
    """题面：「排程搜索实际考察的分配节点数，为正整数」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    nodes = read_report(report_path)["solver_nodes"]
    assert process.returncode == 0
    assert type(nodes) is int
    assert nodes > 0


def test_compact_json_and_trailing_newline(tmp_path: Path) -> None:
    """题面：「键名按字母序，冒号与逗号后不留空格，末字节是一个换行符」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    raw = report_path.read_bytes()
    assert process.returncode == 0
    assert raw == compact(json.loads(raw))
    assert raw.endswith(b"\n")


def test_checksum_matches_result(tmp_path: Path) -> None:
    """题面：「`checksum` 从一份临时视图计算：先摘掉 `checksum` 与 `journal_entry`，再对剩余紧凑字节取摘要。」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    report = read_report(report_path)
    without_checksums = {
        key: value
        for key, value in report.items()
        if key not in {"checksum", "journal_entry"}
    }
    assert process.returncode == 0
    assert report["checksum"] == hashlib.sha256(compact(without_checksums)).hexdigest()


def test_report_digest_excludes_only_journal_entry(tmp_path: Path) -> None:
    """题面：「报告摘要从摘掉 `journal_entry` 的报告副本开始算。」"""
    process, _, _, report_path = invoke(tmp_path, build_payload())
    report = read_report(report_path)
    without_entry = {
        key: value for key, value in report.items() if key != "journal_entry"
    }
    assert process.returncode == 0
    assert report["journal_entry"]["report_sha256"] == hashlib.sha256(
        compact(without_entry)
    ).hexdigest()


def test_journal_repeat_is_idempotent(tmp_path: Path) -> None:
    """题面：「相同场景字节和相同报告字节再次进入时，流水不增加行」"""
    first, scenario, journal, report = invoke(tmp_path, build_payload())
    before = report.read_bytes()
    journal_before = journal.read_bytes()
    second = subprocess.run(
        [
            sys.executable,
            ENTRY,
            "--scenario",
            str(scenario),
            "--journal",
            str(journal),
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        timeout=4,
    )
    assert first.returncode == 0
    assert second.returncode == 0
    assert journal.read_bytes() == journal_before
    assert report.read_bytes() == before


def test_changed_case_appends_journal(tmp_path: Path) -> None:
    """题面：「场景摘要变化并产出新报告时，只追加一条对应的 `journal_entry`」"""
    first, scenario, journal, report = invoke(tmp_path, build_payload())
    scenario.write_text(
        json.dumps(build_payload(case_id="PLANT-2026-DAY"), sort_keys=True),
        encoding="utf-8",
    )
    second = subprocess.run(
        [
            sys.executable,
            ENTRY,
            "--scenario",
            str(scenario),
            "--journal",
            str(journal),
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        timeout=4,
    )
    assert first.returncode == 0
    assert second.returncode == 0
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 2
    assert read_report(report)["case_id"] == "PLANT-2026-DAY"


def test_failure_preserves_existing_outputs(tmp_path: Path) -> None:
    """题面：「校验失败、反演退化或写盘失败都不得改写旧报告和旧流水」"""
    success, scenario, journal, report = invoke(tmp_path, build_payload())
    journal_before = journal.read_bytes()
    report_before = report.read_bytes()
    scenario.write_text("{broken", encoding="utf-8")
    failed = subprocess.run(
        [
            sys.executable,
            ENTRY,
            "--scenario",
            str(scenario),
            "--journal",
            str(journal),
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        timeout=4,
    )
    assert success.returncode == 0
    assert failed.returncode == 3
    assert journal.read_bytes() == journal_before
    assert report.read_bytes() == report_before
    assert sorted(path.name for path in journal.parent.iterdir()) == [journal.name, journal.name + ".lock"]
    assert sorted(path.name for path in report.parent.iterdir()) == [report.name]


def test_report_replace_failure_rolls_back_journal(tmp_path: Path) -> None:
    """题面：「写盘失败都不得改写旧报告和旧流水」"""
    scenario = tmp_path / "input" / "scenario.json"
    scenario.parent.mkdir(parents=True)
    scenario.write_text(json.dumps(build_payload(), sort_keys=True), encoding="utf-8")
    journal = tmp_path / "state" / "rollback.jsonl"
    initial_report = tmp_path / "out" / "initial.json"
    first = subprocess.run(
        [
            sys.executable,
            ENTRY,
            "--scenario",
            str(scenario),
            "--journal",
            str(journal),
            "--report",
            str(initial_report),
        ],
        capture_output=True,
        text=True,
        timeout=4,
    )
    journal_before = journal.read_bytes()
    scenario.write_text(
        json.dumps(build_payload(case_id="PLANT-2026-DAY"), sort_keys=True),
        encoding="utf-8",
    )
    blocked_report = tmp_path / "out" / "blocked.json"
    blocked_report.mkdir()
    second = subprocess.run(
        [
            sys.executable,
            ENTRY,
            "--scenario",
            str(scenario),
            "--journal",
            str(journal),
            "--report",
            str(blocked_report),
        ],
        capture_output=True,
        text=True,
        timeout=4,
    )
    assert first.returncode == 0
    assert second.returncode == 3
    assert journal.read_bytes() == journal_before
    assert blocked_report.is_dir()
    assert sorted(path.name for path in blocked_report.parent.iterdir()) == [
        "blocked.json",
        "initial.json",
    ]


def test_sequence_presentation_order_is_irrelevant(tmp_path: Path) -> None:
    """题面：「结果在重排 `equipment` 或 `sequences` 的呈现顺序后仍应指向同一组工单和设备」"""
    original = build_payload()
    shuffled = copy.deepcopy(original)
    for observation in shuffled["observations"]:
        observation["sequences"].reverse()
    shuffled["observations"].reverse()
    first, _, _, first_report = invoke(tmp_path, original, report_name="first.json")
    second, _, _, second_report = invoke(tmp_path, shuffled, report_name="second.json")
    first_result = read_report(first_report)
    second_result = read_report(second_report)
    assert first.returncode == 0
    assert second.returncode == 0
    assert first_result["history_mapping"] == second_result["history_mapping"]
    assert first_result["assignments"] == second_result["assignments"]
    assert first_result["schedule"] == second_result["schedule"]


def test_equipment_presentation_order_is_irrelevant(tmp_path: Path) -> None:
    """题面：「结果在重排 `equipment` 或 `sequences` 的呈现顺序后仍应指向同一组工单和设备」"""
    original = build_payload()
    shuffled = build_payload(equipment=list(reversed(copy.deepcopy(EQUIPMENT))))
    first, _, _, first_report = invoke(tmp_path, original, report_name="first.json")
    second, _, _, second_report = invoke(tmp_path, shuffled, report_name="second.json")
    first_result = read_report(first_report)
    second_result = read_report(second_report)
    assert first.returncode == 0
    assert second.returncode == 0
    assert first_result["history_mapping"] == second_result["history_mapping"]
    assert first_result["assignments"] == second_result["assignments"]


def test_normal_case_finishes_within_three_seconds(tmp_path: Path) -> None:
    """题面：「常规输入从启动到报告落盘要在 3 秒内结束」"""
    scenario = tmp_path / "input" / "scenario.json"
    scenario.parent.mkdir(parents=True)
    scenario.write_text(json.dumps(build_payload(), sort_keys=True), encoding="utf-8")
    journal = tmp_path / "state" / "timed.jsonl"
    report = tmp_path / "out" / "timed.json"
    start = time.monotonic()
    process = subprocess.run(
        [
            sys.executable,
            ENTRY,
            "--scenario",
            str(scenario),
            "--journal",
            str(journal),
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        timeout=4,
    )
    elapsed = time.monotonic() - start
    assert process.returncode == 0
    assert report.is_file()
    assert elapsed < 3.0


def test_maximum_public_scale_finishes_within_three_seconds(tmp_path: Path) -> None:
    """题面：「达到 7 张工单、12 份汇总的公开上限时也按这个时限处理」"""
    payload = build_large_payload()
    scenario = tmp_path / "input" / "scenario.json"
    scenario.parent.mkdir(parents=True)
    scenario.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    journal = tmp_path / "state" / "upper.jsonl"
    report = tmp_path / "out" / "upper.json"
    start = time.monotonic()
    process = subprocess.run(
        [
            sys.executable,
            ENTRY,
            "--scenario",
            str(scenario),
            "--journal",
            str(journal),
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        timeout=4,
    )
    elapsed = time.monotonic() - start
    assert process.returncode == 0
    assert len(payload["jobs"]) == 7
    assert len(payload["observations"]) == 12
    assert report.is_file()
    assert elapsed < 3.0
