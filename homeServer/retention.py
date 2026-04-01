#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import signal
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app_config import get_active_env_path, get_bool_env, get_int_env, load_app_env
from db import get_conn, get_db_path

RUNNING = True


@dataclass(frozen=True)
class RetentionConfig:
    enabled: bool
    retention_days: int
    batch_size: int
    max_batches: int
    dry_run: bool


@dataclass(frozen=True)
class RetentionTarget:
    table_name: str
    count_sql: str
    delete_sql: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def retention_cutoff_utc(retention_days: int, *, now: datetime | None = None) -> str:
    current_time = now or datetime.now(timezone.utc)
    return (current_time - timedelta(days=retention_days)).isoformat()


def log_event(event: str, **fields: Any) -> None:
    record = {
        "ts": utc_now(),
        "event": event,
        **fields,
    }
    print(json.dumps(record, ensure_ascii=False), flush=True)


def handle_signal(signum: int, frame: Any) -> None:
    global RUNNING
    RUNNING = False
    log_event("retention_shutdown_requested", signal=signum)


def build_simple_target(table_name: str) -> RetentionTarget:
    count_sql = f"SELECT COUNT(*) FROM {table_name} WHERE received_at_utc < ?"
    delete_sql = f"""
        DELETE FROM {table_name}
        WHERE id IN (
            SELECT id FROM (
                SELECT id
                FROM {table_name}
                WHERE received_at_utc < ?
                ORDER BY received_at_utc ASC, id ASC
                LIMIT ?
            )
        )
    """
    return RetentionTarget(
        table_name=table_name,
        count_sql=count_sql,
        delete_sql=delete_sql,
    )


RETENTION_TARGETS = (
    RetentionTarget(
        table_name="weather_readings",
        count_sql="""
            SELECT COUNT(*)
            FROM weather_readings w
            LEFT JOIN aws_delivery_queue q ON q.reading_id = w.id
            WHERE w.received_at_utc < ?
              AND (q.reading_id IS NULL OR q.status = 'delivered')
        """,
        delete_sql="""
            DELETE FROM weather_readings
            WHERE id IN (
                SELECT id FROM (
                    SELECT w.id
                    FROM weather_readings w
                    LEFT JOIN aws_delivery_queue q ON q.reading_id = w.id
                    WHERE w.received_at_utc < ?
                      AND (q.reading_id IS NULL OR q.status = 'delivered')
                    ORDER BY w.received_at_utc ASC, w.id ASC
                    LIMIT ?
                )
            )
        """,
    ),
    build_simple_target("device_health_events"),
    build_simple_target("weather_events"),
    build_simple_target("device_telemetry_events"),
    build_simple_target("ingest_events"),
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Delete expired SQLite rows in bounded batches.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report eligible row counts without deleting anything.",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help="Override DB_RETENTION_DAYS for this run.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Override DB_RETENTION_BATCH_SIZE for this run.",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=None,
        help="Override DB_RETENTION_MAX_BATCHES for this run.",
    )
    return parser


def load_retention_config(args: argparse.Namespace) -> RetentionConfig:
    load_app_env()

    retention_days = args.retention_days
    if retention_days is None:
        retention_days = get_int_env("DB_RETENTION_DAYS", 180, minimum=1)
    elif retention_days < 1:
        raise RuntimeError(f"Invalid --retention-days value: {retention_days}")

    batch_size = args.batch_size
    if batch_size is None:
        batch_size = get_int_env("DB_RETENTION_BATCH_SIZE", 1000, minimum=1)
    elif batch_size < 1:
        raise RuntimeError(f"Invalid --batch-size value: {batch_size}")

    max_batches = args.max_batches
    if max_batches is None:
        max_batches = get_int_env("DB_RETENTION_MAX_BATCHES", 100, minimum=1)
    elif max_batches < 1:
        raise RuntimeError(f"Invalid --max-batches value: {max_batches}")

    return RetentionConfig(
        enabled=get_bool_env("DB_RETENTION_ENABLED", False),
        retention_days=retention_days,
        batch_size=batch_size,
        max_batches=max_batches,
        dry_run=args.dry_run,
    )


def current_env_path() -> str:
    return str(get_active_env_path())


def eligible_row_count(conn: sqlite3.Connection, target: RetentionTarget, cutoff_utc: str) -> int:
    row = conn.execute(target.count_sql, (cutoff_utc,)).fetchone()
    if row is None:
        return 0
    return int(row[0])


def delete_batch(conn: sqlite3.Connection, target: RetentionTarget, cutoff_utc: str, batch_size: int) -> int:
    before_changes = conn.total_changes
    conn.execute(target.delete_sql, (cutoff_utc, batch_size))
    conn.commit()
    return conn.total_changes - before_changes


def process_target(
    conn: sqlite3.Connection,
    target: RetentionTarget,
    config: RetentionConfig,
    cutoff_utc: str,
    *,
    logger: Callable[..., None],
) -> dict[str, Any]:
    if config.dry_run:
        eligible_rows = eligible_row_count(conn, target, cutoff_utc)
        result = {
            "table_name": target.table_name,
            "eligible_rows": eligible_rows,
            "deleted_rows": 0,
            "batches_run": 0,
            "remaining_eligible_rows": eligible_rows,
        }
        logger(
            "retention_table_dry_run",
            table_name=target.table_name,
            eligible_rows=eligible_rows,
        )
        return result

    deleted_rows = 0
    batches_run = 0

    while RUNNING and batches_run < config.max_batches:
        deleted_in_batch = delete_batch(conn, target, cutoff_utc, config.batch_size)
        if deleted_in_batch == 0:
            break

        batches_run += 1
        deleted_rows += deleted_in_batch

        logger(
            "retention_batch_deleted",
            table_name=target.table_name,
            batch_number=batches_run,
            deleted_rows=deleted_in_batch,
        )

        if deleted_in_batch < config.batch_size:
            break

    remaining_eligible_rows = eligible_row_count(conn, target, cutoff_utc)
    result = {
        "table_name": target.table_name,
        "eligible_rows": deleted_rows + remaining_eligible_rows,
        "deleted_rows": deleted_rows,
        "batches_run": batches_run,
        "remaining_eligible_rows": remaining_eligible_rows,
    }

    logger(
        "retention_table_complete",
        table_name=target.table_name,
        deleted_rows=deleted_rows,
        batches_run=batches_run,
        remaining_eligible_rows=remaining_eligible_rows,
    )
    return result


def apply_retention(
    conn: sqlite3.Connection,
    config: RetentionConfig,
    cutoff_utc: str,
    *,
    logger: Callable[..., None] = log_event,
) -> list[dict[str, Any]]:
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")

    results = []
    for target in RETENTION_TARGETS:
        results.append(process_target(conn, target, config, cutoff_utc, logger=logger))

    if not config.dry_run:
        conn.execute("PRAGMA optimize")

    return results


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        config = load_retention_config(args)
    except Exception as exc:
        log_event("retention_config_error", error=str(exc), env_path=current_env_path())
        return 1

    db_path = get_db_path()
    if not db_path.exists():
        log_event("retention_db_missing", db_path=str(db_path), env_path=current_env_path())
        return 1

    if not config.enabled and not config.dry_run:
        log_event(
            "retention_disabled",
            db_path=str(db_path),
            env_path=current_env_path(),
        )
        return 0

    cutoff_utc = retention_cutoff_utc(config.retention_days)
    log_event(
        "retention_start",
        db_path=str(db_path),
        env_path=current_env_path(),
        dry_run=config.dry_run,
        retention_days=config.retention_days,
        batch_size=config.batch_size,
        max_batches=config.max_batches,
        cutoff_utc=cutoff_utc,
    )

    try:
        with get_conn(timeout_sec=60.0) as conn:
            results = apply_retention(conn, config, cutoff_utc)

            total_deleted_rows = sum(int(result["deleted_rows"]) for result in results)
            total_remaining_eligible_rows = sum(int(result["remaining_eligible_rows"]) for result in results)

            checkpoint_result = None
            if not config.dry_run and total_deleted_rows > 0:
                checkpoint_row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                if checkpoint_row is not None:
                    checkpoint_result = {
                        "busy": int(checkpoint_row[0]),
                        "log_frames": int(checkpoint_row[1]),
                        "checkpointed_frames": int(checkpoint_row[2]),
                    }

    except Exception as exc:
        log_event("retention_error", error=str(exc), db_path=str(db_path))
        return 1

    log_event(
        "retention_complete",
        db_path=str(db_path),
        dry_run=config.dry_run,
        cutoff_utc=cutoff_utc,
        total_deleted_rows=total_deleted_rows,
        total_remaining_eligible_rows=total_remaining_eligible_rows,
        table_results=results,
        checkpoint=checkpoint_result,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
