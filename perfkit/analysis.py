"""Analyze same-host benchmark CSVs, using only the Python standard library.

Invalid CSVs or impossible timestamp order raise ValueError; samples are never
silently dropped to make a latency distribution appear valid. CSV timestamps
must originate from the same host's monotonic clock. Ordering checks cannot
prove that clock provenance, which the runner must record in its environment.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from statistics import mean


QUANTILE_METHOD = "nearest-rank: sorted[ceil(p*n)-1]; no interpolation"
SENDER_COLUMNS = ("seq", "scheduled_ns", "generated_ns", "publish_ns",
                  "publish_return_ns", "measured")
RECEIVER_COLUMNS = ("seq", "receive_ns", "finish_ns", "cpu_ns", "payload_valid")
SAMPLE_COLUMNS = ("seq", "scheduled_ns", "start_ns", "finish_ns", "cpu_ns", "measured")


def _read_csv(path: Path, columns: tuple[str, ...]) -> list[dict[str, int]]:
    with Path(path).open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream, strict=True)
        rows = []
        try:
            if reader.fieldnames != list(columns):
                raise ValueError(f"{path}: expected CSV columns {','.join(columns)}")
            for line, raw in enumerate(reader, 2):
                if None in raw or any(raw[c] is None for c in columns):
                    raise ValueError(f"{path}:{line}: incorrect field count")
                row = {}
                for column in columns:
                    value = raw[column]
                    # Avoid silently accepting floats, whitespace or exponent notation.
                    if not value or not value.isascii() or not value.isdecimal():
                        raise ValueError(f"{path}:{line}: {column} must be a nonnegative integer")
                    row[column] = int(value)
                for flag in ("measured", "payload_valid"):
                    if flag in row and row[flag] not in (0, 1):
                        raise ValueError(f"{path}:{line}: {flag} must be 0 or 1")
                rows.append(row)
        except csv.Error as error:
            raise ValueError(f"{path}: malformed CSV: {error}") from error
    return rows


def _deadline(value: int | None) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("deadline_ns must be a nonnegative integer or None")


def _order(row: dict[str, int], *keys: str) -> None:
    if any(row[a] > row[b] for a, b in zip(keys, keys[1:])):
        raise ValueError(f"seq {row['seq']}: invalid chronological order: {' <= '.join(keys)}")


def _monotonic(rows: list[dict[str, int]], key: str, *, strict: bool = False) -> None:
    for a, b in zip(rows, rows[1:]):
        if b[key] < a[key] or (strict and b[key] == a[key]):
            raise ValueError(f"seq {b['seq']}: {key} is not {'strictly ' if strict else ''}monotonic")


def _unique(rows: list[dict[str, int]]) -> dict[int, dict[str, int]]:
    indexed = {row["seq"]: row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError("duplicate seq in sender/sample manifest")
    return indexed


def _single_thread_intervals(rows: list[dict[str, int]], start: str) -> None:
    for row in rows:
        if row['cpu_ns'] > row['finish_ns'] - row[start]:
            raise ValueError(f"seq {row['seq']}: CPU duration exceeds enclosing wall interval")
    for previous, current in zip(rows, rows[1:]):
        if previous['finish_ns'] > current[start]:
            raise ValueError(f"seq {current['seq']}: overlapping single-thread intervals")


def _distribution(values: list[int]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return dict(n=0, min=None, mean=None, p50=None, p95=None, p99=None, max=None)
    return dict(n=len(ordered), min=ordered[0], mean=mean(ordered),
                p50=ordered[math.ceil(0.50 * len(ordered)) - 1],
                p95=ordered[math.ceil(0.95 * len(ordered)) - 1],
                p99=ordered[math.ceil(0.99 * len(ordered)) - 1], max=ordered[-1])


def _deadline_metrics(total: int, completed: list[tuple[int, int]],
                      deadline_ns: int | None) -> dict:
    missing = total - len(completed)
    late = None if deadline_ns is None else sum(
        finish > scheduled + deadline_ns for scheduled, finish in completed)
    violated = None if late is None else late + missing
    return {
        "deadline_ns": deadline_ns,
        "deadline_anchor": "scheduled_ns",
        "denominator": total,
        "completed": len(completed),
        "late": late,
        "missing": missing,
        "violated": violated,
        "violation_fraction": None if violated is None or total == 0 else violated / total,
        "verdict": "not_evaluated" if deadline_ns is None else (
            "no_measured_samples" if total == 0 else "violated" if violated else "met"),
    }


def analyze_c01(sender_path: Path, receiver_path: Path,
                deadline_ns: int | None) -> dict:
    """Analyze C01; conditional latencies use first valid reception per measured ID.

    Duplicate events count every reception after the first for a measured ID,
    including invalid payload events. Invalid events and duplicates can overlap.
    A valid event following an invalid event can still complete the task. Warmup
    receptions never enter measured counts or distributions. Unexpected IDs are
    receiver IDs absent from the complete sender manifest. The manifest includes
    warmup IDs, so warmup reception is not an unexpected-ID event.
    """
    _deadline(deadline_ns)
    senders = _read_csv(sender_path, SENDER_COLUMNS)
    receivers = _read_csv(receiver_path, RECEIVER_COLUMNS)
    manifest = _unique(senders)
    for row in senders:
        _order(row, "scheduled_ns", "generated_ns", "publish_ns", "publish_return_ns")
    _monotonic(senders, "scheduled_ns", strict=True)
    _monotonic(senders, "generated_ns")
    _monotonic(senders, "publish_ns")
    _monotonic(senders, "publish_return_ns")
    _monotonic(receivers, "receive_ns")
    _single_thread_intervals(receivers, 'receive_ns')
    measured = {seq: row for seq, row in manifest.items() if row["measured"]}
    first_valid = {}
    planned_ranks = {seq: rank for rank, seq in enumerate(measured)}
    highest_delivered_rank = -1
    out_of_order = 0
    seen = set()
    duplicates = invalid = unexpected = warmup = 0
    invalid_all = sum(not row["payload_valid"] for row in receivers)
    for row in receivers:
        _order(row, "receive_ns", "finish_ns")
        seq = row["seq"]
        if seq not in manifest:
            unexpected += 1
            continue
        if row["receive_ns"] < manifest[seq]["publish_ns"]:
            raise ValueError(f"seq {seq}: receive_ns precedes publish_ns; clock/order invalid")
        if seq not in measured:
            warmup += 1
            continue
        duplicates += seq in seen
        seen.add(seq)
        invalid += not row["payload_valid"]
        if row["payload_valid"] and seq not in first_valid:
            rank = planned_ranks[seq]
            out_of_order += rank < highest_delivered_rank
            highest_delivered_rank = max(highest_delivered_rank, rank)
            first_valid[seq] = row
    values = {name: [] for name in (
        "publish_to_callback_ns", "data_age_ns", "chain_latency_ns",
        "callback_wall_time_ns", "callback_cpu_time_ns")}
    completed = []
    for seq, receiver in first_valid.items():
        sender = measured[seq]
        values["publish_to_callback_ns"].append(receiver["receive_ns"] - sender["publish_ns"])
        values["data_age_ns"].append(receiver["receive_ns"] - sender["generated_ns"])
        values["chain_latency_ns"].append(receiver["finish_ns"] - sender["generated_ns"])
        values["callback_wall_time_ns"].append(receiver["finish_ns"] - receiver["receive_ns"])
        values["callback_cpu_time_ns"].append(receiver["cpu_ns"])
        completed.append((sender["scheduled_ns"], receiver["finish_ns"]))
    planned_rows = list(measured.values())
    planned_periods = [b["scheduled_ns"] - a["scheduled_ns"]
                       for a, b in zip(planned_rows, planned_rows[1:])]
    period = (planned_periods[0] if planned_periods and
              all(value == planned_periods[0] for value in planned_periods) else None)
    planned_interval = None if period is None else len(measured) * period
    last_release = planned_rows[-1]["scheduled_ns"] if planned_rows else None
    planned_end = None if period is None else last_release + period
    return {
        "scenario": "C01", "quantile_method": QUANTILE_METHOD,
        "clock_assumption": "same-host monotonic_ns origin; not provable from CSV",
        "distribution_population": "first valid delivery per measured sender ID; conditional on delivery",
        "counts": {
            "sender_rows": len(senders), "warmup_sent": len(senders) - len(measured),
            "measured_sent": len(measured), "receiver_events": len(receivers),
            "valid_delivered": len(first_valid), "missing_delivery": len(measured) - len(first_valid),
            "invalid_payload_events": invalid, "invalid_payload_events_all": invalid_all,
            "duplicate_events": duplicates, "unexpected_id_events": unexpected,
            "warmup_receiver_events": warmup,
            "out_of_order_first_valid_deliveries": out_of_order,
            "delivered_after_last_planned_release": sum(
                row["receive_ns"] > last_release for row in first_valid.values()) if planned_rows else 0,
            "delivered_after_planned_window_end": None if planned_end is None else sum(
                row["receive_ns"] > planned_end for row in first_valid.values()),
        },
        "planned_interval_accounting": {
            "planned_period_ns": period,
            "planned_measurement_interval_ns": planned_interval,
            "delivered_tasks_per_planned_second": None if planned_interval is None else
                len(first_valid) * 1_000_000_000 / planned_interval,
            "basis": "measured count * uniform scheduled period; unavailable if fewer than two or nonuniform releases",
            "interpretation": "delivery accounting rate, not on-time receiver throughput; includes finite-drain deliveries",
        },
        "distributions": {key: _distribution(data) for key, data in values.items()},
        "deadline": _deadline_metrics(len(measured), completed, deadline_ns),
    }


def analyze_s01(samples_path: Path, deadline_ns: int | None) -> dict:
    """Analyze scheduled worker starts; lateness is not kernel dispatch latency."""
    _deadline(deadline_ns)
    rows = _read_csv(samples_path, SAMPLE_COLUMNS)
    _unique(rows)
    for row in rows:
        _order(row, "scheduled_ns", "start_ns", "finish_ns")
    _monotonic(rows, "scheduled_ns", strict=True)
    _monotonic(rows, "start_ns")
    _single_thread_intervals(rows, 'start_ns')
    measured = [row for row in rows if row["measured"]]
    period_errors = [(b["start_ns"] - a["start_ns"]) -
                     (b["scheduled_ns"] - a["scheduled_ns"])
                     for a, b in zip(measured, measured[1:])]
    values = {
        "start_lateness_ns": [row["start_ns"] - row["scheduled_ns"] for row in measured],
        "wall_time_ns": [row["finish_ns"] - row["start_ns"] for row in measured],
        "cpu_time_ns": [row["cpu_ns"] for row in measured],
        "period_error_ns": period_errors,
        "absolute_period_error_ns": [abs(value) for value in period_errors],
    }
    return {
        "scenario": "S01", "quantile_method": QUANTILE_METHOD,
        "clock_assumption": "same-host monotonic_ns origin; not provable from CSV",
        "distribution_population": "measured samples; period errors between consecutive measured starts",
        "counts": {"sample_rows": len(rows), "warmup_samples": len(rows) - len(measured),
                   "measured_samples": len(measured), "period_intervals": len(period_errors)},
        "distributions": {key: _distribution(data) for key, data in values.items()},
        "deadline": _deadline_metrics(len(measured),
            [(row["scheduled_ns"], row["finish_ns"]) for row in measured], deadline_ns),
    }


MEASUREMENT_LIMITS = [
    "所有时间戳须来自同一主机的同一 monotonic_ns 时钟；CSV 顺序检查不能证明时钟来源。跨主机时间戳不可直接相减。",
    "C01 publish_to_callback_ns 包含 ROS 中间件、传输及回调派发延迟；它不是纯网络时延。",
    "C01 分位数仅针对有效送达样本；必须同时查看缺失、无效载荷、重复和异常 ID。缺失原因未知，不可称为网络丢包。",
    "首次有效接收决定 C01 完成；重复不增加送达数。无效接收后仍可由有效接收完成。事件计数可能重叠。",
    "deadline 基于 scheduled_ns；C01 分母是全部 measured 发送任务，超期与缺失分开，违约数为二者之和。未配置 deadline 时不作业务期限判断。",
    "S01 start_lateness_ns 是计划时刻到工作开始的偏移，不是内核调度派发延迟。period_error_ns 是相邻 measured 开始间隔减去对应计划间隔，可为负数。",
    "queue wait 与 runnable_wait 未观测，不能由这些汇总反推出。线程 CPU 时间与墙钟时间分别统计。",
    "送达记账速率的分母是 measured 数乘以均匀计划周期，至少需两次释放；不能推断时为 null。它包含计划区间后排空期的送达，不是按时接收吞吐率。乱序计数按首次有效接收顺序相对计划发送顺序判断。",
    "运行器排空等待有限；排空结束后仍可能到达。释放延迟与追赶行为受运行器实现约束，须结合 config/environment 和原始时间戳解释结果，不能由本报告推断长期稳定性。",
    "分位数使用 nearest-rank（升序第 ceil(p*n) 项）；空样本为 null。重复运行分别报告，不对各次运行的分位数取平均。",
    "Docker 环境结果不等价于 Jetson 实机性能，也不证明端到端控制系统或硬实时保证。",
]


def write_report(output_root: Path, config: dict, environment: dict,
                 results: list[dict]) -> None:
    """Write machine-readable evidence and a Chinese Markdown report per run.

    No pooled percentiles are manufactured from per-repetition summaries.
    Config/environment are copied as provided; callers must exclude secrets.
    """
    summary = {"schema_version": 1, "config": config, "environment": environment,
               "quantile_method": QUANTILE_METHOD,
               "measurement_limits": MEASUREMENT_LIMITS, "results": results}
    # Validate JSON before creating any output, retaining integer timestamps.
    encoded = json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    lines = ["# 具身性能基准报告", "", "## 配置与环境", "", "```json",
             json.dumps({"config": config, "environment": environment},
                        ensure_ascii=False, indent=2, allow_nan=False), "```", "",
             "## 测量口径与边界", ""]
    lines.extend(f"- {item}" for item in MEASUREMENT_LIMITS)
    for result in results:
        scenario = result["scenario"]
        metrics = result["metrics"]
        if scenario not in ("C01", "S01") or metrics["scenario"] != scenario:
            raise ValueError("report scenario and metrics scenario must agree (C01 or S01)")
        lines.extend(["", f"## {scenario} · 第 {result['repetition']} 次运行", "",
                      f"原始数据目录：`{result['raw_directory']}`", "",
                      "### 计数与期限", "", "```json",
                      json.dumps({"counts": metrics["counts"], "deadline": metrics["deadline"],
                                  **({"planned_interval_accounting": metrics["planned_interval_accounting"]}
                                     if scenario == "C01" else {})},
                                 ensure_ascii=False, indent=2, allow_nan=False), "```", "",
                      "### 分布（单位 ns）", "", f"样本口径：{metrics['distribution_population']}", "",
                      "| 指标 | n | min | mean | p50 | p95 | p99 | max |",
                      "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
        for name, distribution in metrics["distributions"].items():
            cells = ["null" if distribution[key] is None else str(distribution[key])
                     for key in ("n", "min", "mean", "p50", "p95", "p99", "max")]
            lines.append(f"| {name} | " + " | ".join(cells) + " |")
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "summary.json").write_text(encoded, encoding="utf-8")
    (output_root / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
