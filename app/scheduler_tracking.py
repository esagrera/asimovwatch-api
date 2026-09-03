import os
import socket
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import Json, RealDictCursor

from app.db import get_connection


DEFAULT_WORKER_TYPE = "scheduler"
DEFAULT_EVENT_LIMIT = 200

def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, dict):
        return {
            key: _json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [
            _json_safe(item)
            for item in value
        ]

    return value

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def default_worker_instance_id() -> str:
    hostname = socket.gethostname()
    pid = os.getpid()
    return f"{hostname}:{pid}"


def create_scheduler_run(
    run_id: str,
    mode: str,
    dry_run: bool = False,
    force: bool = False,
    worker_type: str = DEFAULT_WORKER_TYPE,
    worker_instance_id: Optional[str] = None,
) -> Dict[str, Any]:
    instance_id = worker_instance_id or default_worker_instance_id()

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO public.scheduler_runs (
                    run_id,
                    status,
                    mode,
                    dry_run,
                    force,
                    worker_type,
                    worker_instance_id,
                    started_at,
                    updated_at,
                    last_heartbeat_at,
                    progress,
                    stages
                )
                VALUES (
                    %s,
                    'QUEUED',
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    NOW(),
                    NOW(),
                    NOW(),
                    '{}'::jsonb,
                    '{}'::jsonb
                )
                RETURNING *
                """,
                (
                    run_id,
                    mode,
                    dry_run,
                    force,
                    worker_type,
                    instance_id,
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_scheduler_run(
    run_id: str,
    *,
    status: Optional[str] = None,
    current_stage: Optional[str] = None,
    current_action: Optional[str] = None,
    current_entry_id: Optional[int] = None,
    current_source_domain: Optional[str] = None,
    progress: Optional[Dict[str, Any]] = None,
    stages: Optional[Dict[str, Any]] = None,
    result: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
    heartbeat: bool = False,
    finished: bool = False,
) -> Optional[Dict[str, Any]]:
    fields = []
    values = []

    optional_fields = {
        "status": status,
        "current_stage": current_stage,
        "current_action": current_action,
        "current_entry_id": current_entry_id,
        "current_source_domain": current_source_domain,
        "error_message": error_message,
    }

    for column, value in optional_fields.items():
        if value is not None:
            fields.append(f"{column} = %s")
            values.append(value)

    if progress is not None:
        fields.append("progress = %s")
        values.append(Json(_json_safe(progress)))

    if stages is not None:
        fields.append("stages = %s")
        values.append(Json(_json_safe(stages)))

    if result is not None:
        fields.append("result = %s")
        values.append(Json(_json_safe(result)))

    fields.append("updated_at = NOW()")

    if heartbeat:
        fields.append("last_heartbeat_at = NOW()")

    if finished:
        fields.append("finished_at = NOW()")
        fields.append(
            "duration_seconds = EXTRACT(EPOCH FROM (NOW() - started_at))"
        )

    values.append(run_id)

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                UPDATE public.scheduler_runs
                SET {", ".join(fields)}
                WHERE run_id = %s
                RETURNING *
                """,
                values,
            )
            row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def append_scheduler_event(
    run_id: str,
    stage: str,
    event: str,
    *,
    entry_id: Optional[int] = None,
    source_domain: Optional[str] = None,
    message: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT run_id
                FROM public.scheduler_runs
                WHERE run_id = %s
                FOR UPDATE
                """,
                (run_id,),
            )

            if cur.fetchone() is None:
                raise ValueError(f"Scheduler run no trobat: {run_id}")

            cur.execute(
                """
                SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
                FROM public.scheduler_run_events
                WHERE run_id = %s
                """,
                (run_id,),
            )

            sequence = cur.fetchone()["next_sequence"]

            cur.execute(
                """
                INSERT INTO public.scheduler_run_events (
                    run_id,
                    sequence,
                    stage,
                    event,
                    entry_id,
                    source_domain,
                    message,
                    metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    run_id,
                    sequence,
                    stage,
                    event,
                    entry_id,
                    source_domain,
                    message,
                    Json(_json_safe(metadata))
                    if metadata is not None
                    else None
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_scheduler_run(run_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM public.scheduler_runs
                WHERE run_id = %s
                """,
                (run_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_scheduler_run_events(
    run_id: str,
    *,
    limit: int = DEFAULT_EVENT_LIMIT,
    after_sequence: Optional[int] = None,
) -> List[Dict[str, Any]]:
    safe_limit = min(max(int(limit), 1), 1000)

    filters = ["run_id = %s"]
    values: List[Any] = [run_id]

    if after_sequence is not None:
        filters.append("sequence > %s")
        values.append(after_sequence)

    values.append(safe_limit)

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT *
                FROM public.scheduler_run_events
                WHERE {" AND ".join(filters)}
                ORDER BY sequence ASC
                LIMIT %s
                """,
                values,
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()