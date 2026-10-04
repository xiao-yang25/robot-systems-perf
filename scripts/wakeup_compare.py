"""Finite same-host S01/cyclictest wakeup comparisons.

Process ownership and raw log capture belong to the supplied execute callback.
This adapter changes no host scheduler, affinity, memory lock or power setting.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Callable

from perfkit.analysis import analyze_s01


ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _binary_evidence(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "path": str(path.resolve()),
        "sha256": digest.hexdigest(),
        "version": None,
        "version_reason": "external tool help/version evidence is recorded by the common preflight",
    }


def _runtime_settings(cpu: int | None) -> tuple[dict, list[str]]:
    if not all(hasattr(os, name) for name in ("sched_getscheduler", "sched_getparam", "sched_getaffinity", "SCHED_OTHER")):
        raise RuntimeError("wakeup comparison requires Linux scheduler and affinity inspection")
    policy = os.sched_getscheduler(0)
    priority = os.sched_getparam(0).sched_priority
    if policy != os.SCHED_OTHER or priority != 0:
        raise RuntimeError("wakeup comparison requires current SCHED_OTHER with priority 0")
    affinity = sorted(os.sched_getaffinity(0))
    if not affinity:
        raise RuntimeError("current CPU affinity is empty")
    wrapper: list[str] = []
    if cpu is not None:
        if type(cpu) is not int or cpu not in affinity:
            raise ValueError("cpu must be an integer in the current allowed affinity")
        taskset = shutil.which("taskset")
        if not taskset:
            raise RuntimeError("taskset is required for an explicitly selected cpu")
        wrapper = [taskset, "-c", str(cpu)]
    settings = {
        "clock": "CLOCK_MONOTONIC",
        "sleep_mode": "absolute clock_nanosleep",
        "frequency_hz": 1000,
        "period_ns": 1_000_000,
        "worker_threads": 1,
        "scheduler": "SCHED_OTHER",
        "priority": 0,
        "inherited_affinity": affinity,
        "effective_requested_affinity": [cpu] if cpu is not None else affinity,
        "affinity_mode": "taskset wraps both tools" if cpu is not None else "both tools inherit caller affinity",
        "power_management": "inherited; cyclictest --default-system preserves system power management",
        "memory_lock": "no mlock requested; allocations, inherited state and tool internals may differ",
        "work_ns": 0,
        "warmup_samples": 0,
        "nice": os.getpriority(os.PRIO_PROCESS, 0) if hasattr(os, "getpriority") else None,
        "population_limit": "S01 uses a fixed count; cyclictest uses duration and may have a different sample count",
        "interpretation": "observed userspace wakeup/start deviation; not kernel dispatch latency or an SLA verdict",
    }
    return settings, wrapper


def wakeup_runs(output: Path, seconds: int, repetitions: int,
                execute: Callable, cpu: int | None = None) -> list[dict]:
    """Run each tool once per repetition, alternating order, with no retries.

    execute(command, folder, timeout, env=None, discover=False) must capture full
    stdout/stderr in folder and raise for nonzero exit or timeout. It owns all
    launched processes. Tool capability failures propagate to the common caller.
    """
    if type(seconds) is not int or not 1 <= seconds <= 1000:
        raise ValueError("seconds must be an integer from 1 to 1000 (S01 supports at most 1000000 samples)")
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    settings, wrapper = _runtime_settings(cpu)
    s01 = ROOT / "build" / "periodic_bench"
    if not s01.is_file() or not os.access(s01, os.X_OK):
        raise RuntimeError("built executable missing: build/periodic_bench")
    cyclictest_name = shutil.which("cyclictest")
    if not cyclictest_name:
        raise RuntimeError("cyclictest is unavailable; install rt-tests before wakeup comparison")
    cyclictest = Path(cyclictest_name)
    evidence = {"s01": _binary_evidence(s01), "cyclictest": _binary_evidence(cyclictest)}
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Check all run names before starting: reruns must never truncate raw data.
    for repetition in range(1, repetitions + 1):
        for tool in ("s01", "cyclictest"):
            folder = output / f"wakeup-{repetition:02d}-{tool}"
            if folder.exists():
                raise FileExistsError(f"run output already exists: {folder}")
    records = []
    for repetition in range(1, repetitions + 1):
        order = ("s01", "cyclictest") if repetition % 2 else ("cyclictest", "s01")
        for position, tool in enumerate(order, 1):
            folder = output / f"wakeup-{repetition:02d}-{tool}"
            folder.mkdir()
            if tool == "s01":
                command = wrapper + [str(s01), "--period-ns", "1000000", "--work-ns", "0",
                                     "--count", str(seconds * 1000), "--warmup", "0",
                                     "--output", str(folder / "samples.csv")]
            else:
                # rt-tests 2.11 OPT_PRIORITY can reset OTHER to FIFO. Apply the
                # explicit policy last so priority 0 remains non-realtime.
                command = wrapper + [str(cyclictest), "--priority=0", "--policy=other",
                                     "--default-system", "--threads=1", "--clock=0",
                                     "--interval=1000", f"--duration={seconds}", "--quiet",
                                     "--histogram=100000"]
            run_settings = {
                **settings, "duration_seconds": seconds, "repetition": repetition,
                "order_position": position, "execution_order": list(order),
                "tool": tool, "tool_evidence": evidence[tool], "command": command,
                "termination": "fixed sample count" if tool == "s01" else "duration",
                "requested_sample_count": seconds * 1000 if tool == "s01" else None,
                "raw_output": "full stdout/stderr captured by common execute; histogram overflow retained",
            }
            _write_json(folder / "settings.json", run_settings)
            execution = execute(command, folder, timeout=seconds + 30)
            if tool == "s01":
                analysis = analyze_s01(folder / "samples.csv", deadline_ns=None)
                counts = analysis['counts']
                if counts['measured_samples'] != seconds * 1000 or counts['warmup_samples'] != 0:
                    raise RuntimeError('S01 sample counts do not match requested fixed-count window')
                metrics = {
                    "start_deviation_ns": analysis["distributions"]["start_lateness_ns"],
                    "metric_boundary": "start_ns - scheduled_ns; measured completed samples",
                    "population": "fixed count S01 samples with zero work and zero warmup",
                    "quantile_method": analysis["quantile_method"],
                    "response_time": {
                        "distribution": None,
                        "boundary": "finish_ns - scheduled_ns; distinct from start deviation",
                        "reason": "not part of this wakeup comparison",
                    },
                }
            else:
                metrics = {
                    "start_deviation_ns": None,
                    "metric_boundary": "cyclictest timer wakeup latency reported in microseconds in raw histogram",
                    "population": "duration-limited cyclictest samples; count may differ from S01",
                    "unavailable_reason": "raw histogram is retained; no trusted parser or percentile conversion is claimed",
                    "response_time": {
                        "distribution": None,
                        "boundary": "finish_ns - scheduled_ns",
                        "reason": "cyclictest does not measure S01 task completion response time",
                    },
                }
            _write_json(folder / "metrics.json", metrics)
            record = {**execution, "tool": tool, "repetition": repetition,
                      "order_position": position, "run_dir": str(folder),
                      "settings": run_settings, "metrics": metrics}
            _write_json(folder / "comparison.json", record)
            records.append(record)
    return records
