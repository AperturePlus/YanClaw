"""Analyze gateway relay_logs stored in gateway/data/data.db.

This script is intentionally dependency-free (stdlib only) so it can be run in
any environment that already runs Yanclaw.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from _gateway_log_helpers import (
    AggRow,
    agg_to_dict,
    export_csv,
    export_json,
    extract_http_code,
    is_success,
    parse_attempts,
    percentile,
    safe_float,
    safe_int,
    to_local_time,
    update_agg,
)


def _print_header(title: str) -> None:
    print("=" * 78)
    print(title)
    print("=" * 78)


def _print_use_time_stats(values: list[int]) -> None:
    if not values:
        print("use_time: (no data)")
        return
    avg = sum(values) / max(1, len(values))
    print(
        "use_time (ms): "
        f"avg={avg:.2f} p50={percentile(values, 50)} "
        f"p90={percentile(values, 90)} p99={percentile(values, 99)} "
        f"min={min(values)} max={max(values)}"
    )


def analyze(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    time_range = conn.execute(
        "SELECT MIN(time) AS min_time, MAX(time) AS max_time, COUNT(*) AS cnt FROM relay_logs"
    ).fetchone()
    min_time = safe_int(time_range["min_time"]) if time_range else 0
    max_time = safe_int(time_range["max_time"]) if time_range else 0
    total = safe_int(time_range["cnt"]) if time_range else 0

    rows = conn.execute(
        """
        SELECT
          id, time, request_model_name, request_api_key_name,
          channel_name, actual_model_name, input_tokens, output_tokens,
          use_time, cost, error, attempts, total_attempts
        FROM relay_logs
        ORDER BY time ASC
        """
    ).fetchall()

    overall = AggRow(use_time_values=[])
    by_model: dict[str, AggRow] = defaultdict(lambda: AggRow(use_time_values=[]))
    by_channel: dict[str, AggRow] = defaultdict(lambda: AggRow(use_time_values=[]))
    by_key: dict[str, AggRow] = defaultdict(lambda: AggRow(use_time_values=[]))

    total_attempts_dist: Counter[int] = Counter()
    fallback_attempted = 0
    fallback_success = 0
    attempt_fail_reasons: Counter[str] = Counter()
    attempt_fail_codes: Counter[str] = Counter()

    for row in rows:
        overall = update_agg(overall, row)
        model_key = str(row["actual_model_name"] or "")
        by_model[model_key] = update_agg(by_model[model_key], row)
        channel_key = str(row["channel_name"] or "")
        by_channel[channel_key] = update_agg(by_channel[channel_key], row)
        key_key = str(row["request_api_key_name"] or "")
        by_key[key_key] = update_agg(by_key[key_key], row)

        total_attempts_value = safe_int(row["total_attempts"])
        if total_attempts_value <= 0:
            total_attempts_value = max(1, len(parse_attempts(row["attempts"])))
        total_attempts_dist[total_attempts_value] += 1

        if total_attempts_value > 1:
            fallback_attempted += 1
            if is_success(row["error"]):
                fallback_success += 1

        for attempt in parse_attempts(row["attempts"]):
            status = str(attempt.get("status") or "").lower()
            msg = str(attempt.get("msg") or "").strip()
            if status != "failed" or not msg:
                continue
            code = extract_http_code(msg)
            if code:
                attempt_fail_codes[code] += 1
            prefix = msg
            for sep in ("{", "\n"):
                if sep in prefix:
                    prefix = prefix.split(sep, 1)[0].strip()
            if len(prefix) > 140:
                prefix = prefix[:140].rstrip() + "…"
            attempt_fail_reasons[prefix] += 1

    use_time_vals = overall.use_time_values or []
    summary: dict[str, Any] = {
        "db": str(db_path),
        "time_range": {
            "min_epoch": min_time,
            "max_epoch": max_time,
            "min_local": to_local_time(min_time) if min_time else "",
            "max_local": to_local_time(max_time) if max_time else "",
        },
        "overall": {
            "total": overall.count,
            "success": overall.success,
            "failed": overall.failed,
            "input_tokens": overall.input_tokens,
            "output_tokens": overall.output_tokens,
            "cost": overall.cost,
            "use_time": {
                "avg": (overall.use_time_sum / overall.count) if overall.count else 0.0,
                "p50": percentile(use_time_vals, 50),
                "p90": percentile(use_time_vals, 90),
                "p99": percentile(use_time_vals, 99),
                "min": min(use_time_vals) if use_time_vals else 0,
                "max": max(use_time_vals) if use_time_vals else 0,
            },
        },
        "fallback": {
            "attempted": fallback_attempted,
            "success": fallback_success,
            "success_rate": (fallback_success / fallback_attempted) if fallback_attempted else 0.0,
            "total_attempts_distribution": dict(total_attempts_dist),
        },
        "top_attempt_fail_http_codes": attempt_fail_codes.most_common(10),
        "top_attempt_fail_reasons": attempt_fail_reasons.most_common(15),
        "by_actual_model_name": {},
        "by_channel_name": {},
        "by_request_api_key_name": {},
        "raw_rows_count": total,
    }

    for key, agg in sorted(by_model.items(), key=lambda kv: kv[1].count, reverse=True):
        if key:
            summary["by_actual_model_name"][key] = agg_to_dict(agg)
    for key, agg in sorted(by_channel.items(), key=lambda kv: kv[1].count, reverse=True):
        if key:
            summary["by_channel_name"][key] = agg_to_dict(agg)
    for key, agg in sorted(by_key.items(), key=lambda kv: kv[1].count, reverse=True):
        if key:
            summary["by_request_api_key_name"][key] = agg_to_dict(agg)

    conn.close()
    return summary


def _print_group(title: str, key: str, payload: dict[str, Any]) -> None:
    _print_header(title)
    items = payload[key]
    for name, agg in list(items.items())[:15]:
        print(
            f"{name:35.35s} "
            f"count={agg['count']:6d} "
            f"fail_rate={agg['failure_rate']:.3f} "
            f"tokens_in={agg['input_tokens']:9d} "
            f"tokens_out={agg['output_tokens']:9d} "
            f"cost={agg['cost']:.6f}"
        )
    if not items:
        print("  (none)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze gateway relay_logs.")
    parser.add_argument(
        "--db",
        default=str(Path("gateway") / "data" / "data.db"),
        help="Path to gateway data.db (default: gateway/data/data.db)",
    )
    parser.add_argument("--export-json", default="", help="Write analysis JSON to this path.")
    parser.add_argument("--export-csv", default="", help="Export relay_logs rows to CSV at this path.")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    payload = analyze(db_path)

    _print_header("TIME RANGE")
    tr = payload["time_range"]
    print(f"min: {tr['min_epoch']}  {tr['min_local']}")
    print(f"max: {tr['max_epoch']}  {tr['max_local']}")

    _print_header("OVERALL")
    ov = payload["overall"]
    print(f"total:   {ov['total']}")
    print(f"success: {ov['success']}")
    print(f"failed:  {ov['failed']}")
    print(f"tokens:  in={ov['input_tokens']} out={ov['output_tokens']}")
    print(f"cost:    {ov['cost']:.6f}")
    ut = ov["use_time"]
    print(
        "use_time (ms): "
        f"avg={ut['avg']:.2f} p50={ut['p50']} p90={ut['p90']} p99={ut['p99']} "
        f"min={ut['min']} max={ut['max']}"
    )

    _print_header("FALLBACK / RETRIES (attempts)")
    fb = payload["fallback"]
    print(f"rows_with_total_attempts>1: {fb['attempted']}")
    print(f"fallback_success:           {fb['success']}")
    print(f"fallback_success_rate:      {fb['success_rate']:.3f}")
    print("total_attempts distribution:")
    for k, v in sorted(fb["total_attempts_distribution"].items(), key=lambda kv: int(kv[0])):
        print(f"  {k}: {v}")

    _print_header("TOP ATTEMPT FAILURES (HTTP codes)")
    for code, cnt in payload["top_attempt_fail_http_codes"]:
        print(f"  {code}: {cnt}")
    if not payload["top_attempt_fail_http_codes"]:
        print("  (none)")

    _print_header("TOP ATTEMPT FAILURES (reason prefixes)")
    for reason, cnt in payload["top_attempt_fail_reasons"]:
        print(f"  {cnt:5d}  {reason}")
    if not payload["top_attempt_fail_reasons"]:
        print("  (none)")

    _print_group("BY actual_model_name", "by_actual_model_name", payload)
    _print_group("BY channel_name", "by_channel_name", payload)
    _print_group("BY request_api_key_name", "by_request_api_key_name", payload)

    if args.export_json:
        export_json(Path(args.export_json), payload)
        print(f"\nWrote JSON: {args.export_json}")
    if args.export_csv:
        export_csv(Path(args.export_csv), db_path)
        print(f"Wrote CSV: {args.export_csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
