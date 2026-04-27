"""Shared helpers for analyze_gateway_logs.py.

Extracted to keep the main script under 400 lines.  All functions are
stdlib-only so the script stays dependency-free.
"""

from __future__ import annotations

import csv
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AggRow:
    count: int = 0
    success: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    use_time_sum: int = 0
    use_time_values: list[int] | None = None


# ---------------------------------------------------------------------------
# Scalar helpers
# ---------------------------------------------------------------------------

def to_local_time(epoch_seconds: int | None) -> str:
    if epoch_seconds is None:
        return ""
    return (
        datetime.fromtimestamp(int(epoch_seconds), tz=UTC)
        .astimezone()
        .isoformat(timespec="seconds")
    )


def safe_int(value: Any) -> int:
    try:
        return 0 if value is None else int(value)
    except Exception:
        return 0


def safe_float(value: Any) -> float:
    try:
        return 0.0 if value is None else float(value)
    except Exception:
        return 0.0


def is_success(error_value: Any) -> bool:
    if error_value is None:
        return True
    return str(error_value).strip() == ""


def percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    if p <= 0:
        return min(values)
    if p >= 100:
        return max(values)
    sorted_vals = sorted(values)
    k = int(math.ceil((p / 100.0) * len(sorted_vals))) - 1
    k = max(0, min(k, len(sorted_vals) - 1))
    return int(sorted_vals[k])


# ---------------------------------------------------------------------------
# Attempt parsing
# ---------------------------------------------------------------------------

_HTTP_CODE_RE = re.compile(r"\b(\d{3})\b")


def extract_http_code(msg: str) -> str | None:
    m = re.search(r"\b(\d{3})\s*:\s*", msg)
    if m:
        return m.group(1)
    m = _HTTP_CODE_RE.search(msg)
    return m.group(1) if m else None


def parse_attempts(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except Exception:
        return []
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def update_agg(agg: AggRow, row: sqlite3.Row) -> AggRow:
    ok = is_success(row["error"])
    use_time = safe_int(row["use_time"])
    values = agg.use_time_values if agg.use_time_values is not None else []
    values.append(use_time)
    return AggRow(
        count=agg.count + 1,
        success=agg.success + (1 if ok else 0),
        failed=agg.failed + (0 if ok else 1),
        input_tokens=agg.input_tokens + safe_int(row["input_tokens"]),
        output_tokens=agg.output_tokens + safe_int(row["output_tokens"]),
        cost=agg.cost + safe_float(row["cost"]),
        use_time_sum=agg.use_time_sum + use_time,
        use_time_values=values,
    )


def agg_to_dict(agg: AggRow) -> dict[str, Any]:
    values = agg.use_time_values or []
    avg = agg.use_time_sum / agg.count if agg.count else 0.0
    return {
        "count": agg.count,
        "success": agg.success,
        "failed": agg.failed,
        "failure_rate": (agg.failed / agg.count) if agg.count else 0.0,
        "input_tokens": agg.input_tokens,
        "output_tokens": agg.output_tokens,
        "cost": agg.cost,
        "use_time_avg": avg,
        "use_time_p50": percentile(values, 50),
        "use_time_p90": percentile(values, 90),
        "use_time_p99": percentile(values, 99),
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def export_csv(path: Path, db_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
          id, time, request_model_name, request_api_key_name,
          channel_name, actual_model_name, input_tokens, output_tokens,
          use_time, cost, error, total_attempts
        FROM relay_logs
        ORDER BY time ASC
        """
    ).fetchall()
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "id", "time_epoch", "time_local", "request_model_name",
            "request_api_key_name", "channel_name", "actual_model_name",
            "input_tokens", "output_tokens", "use_time", "cost",
            "error", "total_attempts",
        ])
        for row in rows:
            epoch = safe_int(row["time"])
            writer.writerow([
                row["id"], epoch,
                to_local_time(epoch) if epoch else "",
                row["request_model_name"], row["request_api_key_name"],
                row["channel_name"], row["actual_model_name"],
                row["input_tokens"], row["output_tokens"],
                row["use_time"], row["cost"],
                row["error"], row["total_attempts"],
            ])
    conn.close()
