"""
AsimovWatch · Fase 5 · Peça 5.2 (Processos en curs, fase MANUAL) i 5.2d (neteja d'historial).

Router de supervisió dels processos de fons del servidor:
  - public.batch_jobs      (enriquiment, cerca temàtica, avaluació BIHP)
  - public.scheduler_runs  (cicles del scheduler, només lectura)

Regles de seguretat:
  - Cap canvi d'estat automàtic. L'únic canvi d'estat és POST /processes/{id}/mark-failed,
    que és manual i només s'accepta si el procés no té cap thread viu i compleix el criteri
    d'estancament (silenci > llindar) o és orfe (la darrera activitat és anterior a l'arrencada
    d'aquest procés del servidor).
  - La neteja (5.2d) només toca batch_jobs en estat final amb més de 30 dies.
  - Aquest mòdul NO importa app.main (evita imports circulars).

Suposició verificada amb el pla Free de Render: una sola instància. Si el servei passa a diverses
instàncies, `thread_alive` i `orphaned_after_restart` deixen de ser fiables.
"""
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.errors
from fastapi import APIRouter, HTTPException, Query
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, Field

from app.db import get_connection
from app.scheduler_tracking import (
    QUEUED_STALE_AFTER_SECONDS as SCHEDULER_QUEUED_STALE_SECONDS,
    RUNNING_STALE_AFTER_SECONDS as SCHEDULER_RUNNING_STALE_SECONDS,
    get_scheduler_run_events,
)

router_processes = APIRouter(prefix="/processes", tags=["processes"])

# Moment d'arrencada d'aquest procés (el mòdul s'importa en arrencar l'API).
PROCESS_STARTED_AT = datetime.now(timezone.utc)

# Llindars de silenci (decisió D-B, Q2). Mesuren el temps sense cap actualització d'updated_at.
BATCH_QUEUED_STALE_SECONDS = 15 * 60
BATCH_RUNNING_STALE_SECONDS = 20 * 60

# Neteja d'historial (5.2d)
PURGE_MIN_DAYS = 30
PURGE_DEFAULT_DAYS = 90

ACTIVE_STATUSES = ("QUEUED", "RUNNING")
FINAL_STATUSES = ("COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED")
ALL_STATUSES = set(ACTIVE_STATUSES) | set(FINAL_STATUSES) | {"SKIPPED_LOCKED"}

SCHEDULER_PREFIX = "scheduler_"
THREAD_PREFIX = "asimovwatch-"

MODE_THEMATIC = "thematic_search"
MODE_BIHP = "candidates_bihp_evaluate"
ENRICH_MODES = ("input-only", "primary-only", "output-only", "semifull", "full")
KNOWN_MODES = ENRICH_MODES + (MODE_THEMATIC, MODE_BIHP)
MODE_LABELS = {
    "input-only": "Input",
    "primary-only": "Primary",
    "output-only": "Output",
    "semifull": "Semifull",
    "full": "Full",
}

TYPE_ENRICH = "entries_batch_enrich"
TYPE_THEMATIC = "thematic_search"
TYPE_BIHP = "bihp_batch_evaluate"
TYPE_SCHEDULER = "scheduler_cycle"
TYPE_UNKNOWN = "unknown"
BATCH_TYPES = {TYPE_ENRICH, TYPE_THEMATIC, TYPE_BIHP, TYPE_UNKNOWN}
ALL_TYPES = BATCH_TYPES | {TYPE_SCHEDULER}

PURGE_WHERE = (
    "status IN ('COMPLETED','COMPLETED_WITH_ERRORS','FAILED') "
    "AND finished_at IS NOT NULL "
    "AND finished_at < NOW() - make_interval(days => %(days)s)"
)

# Columnes lleugeres (sense items ni entry_ids) + indicadors calculats a la BD.
BATCH_LIST_COLUMNS = """
    batch_id, mode, status, total, processed, succeeded, failed, skipped, error_message,
    created_at, started_at, finished_at, updated_at,
    COALESCE(array_length(entry_ids, 1), 0) AS entry_count,
    options->>'brief' AS opt_brief,
    options->>'requested_by' AS opt_requested_by,
    options->>'dry_run' AS opt_dry_run,
    CASE WHEN jsonb_typeof(options->'candidate_ids') = 'array'
         THEN jsonb_array_length(options->'candidate_ids') END AS opt_candidate_count,
    EXTRACT(EPOCH FROM (NOW() - COALESCE(updated_at, created_at)))::int AS silence_seconds,
    (COALESCE(updated_at, created_at) < %(started)s) AS before_start
"""

SCHEDULER_LIST_COLUMNS = """
    run_id, status, mode, dry_run, force, current_stage, current_action,
    started_at, updated_at, last_heartbeat_at, finished_at, duration_seconds, error_message,
    EXTRACT(EPOCH FROM (NOW() - COALESCE(last_heartbeat_at, updated_at, started_at)))::int AS silence_seconds,
    (COALESCE(last_heartbeat_at, updated_at, started_at) < %(started)s) AS before_start
"""


# ---------------------------------------------------------------------------
# Helpers de BD i de threads
# ---------------------------------------------------------------------------

def _query(sql: str, params: Optional[Dict[str, Any]] = None, one: bool = False):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or {})
            return cur.fetchone() if one else cur.fetchall()
    finally:
        conn.close()


def _live_thread_names() -> List[str]:
    return [
        t.name
        for t in threading.enumerate()
        if t.is_alive() and (t.name or "").startswith(THREAD_PREFIX)
    ]


def _has_live_thread(process_id: str, live_names: List[str]) -> bool:
    return any(name.endswith(process_id) for name in live_names)


def _is_scheduler_id(process_id: str) -> bool:
    return process_id.startswith(SCHEDULER_PREFIX)


# ---------------------------------------------------------------------------
# Classificació i serialització
# ---------------------------------------------------------------------------

def _process_type(mode: Optional[str]) -> str:
    if mode in ENRICH_MODES:
        return TYPE_ENRICH
    if mode == MODE_THEMATIC:
        return TYPE_THEMATIC
    if mode == MODE_BIHP:
        return TYPE_BIHP
    return TYPE_UNKNOWN


def _batch_title(row: Dict[str, Any], ptype: str) -> str:
    if ptype == TYPE_ENRICH:
        count = row.get("total") or row.get("entry_count") or 0
        label = MODE_LABELS.get(row.get("mode"), row.get("mode"))
        return f"{label} · {count} entrades"
    if ptype == TYPE_THEMATIC:
        brief = (row.get("opt_brief") or "").strip()
        if len(brief) > 80:
            brief = brief[:77] + "..."
        return f"Cerca temàtica · {brief}" if brief else "Cerca temàtica"
    if ptype == TYPE_BIHP:
        count = row.get("opt_candidate_count") or row.get("total") or 0
        return f"Avaluació BIHP · {count} candidates"
    return str(row.get("mode") or "Procés desconegut")


def _batch_item(row: Dict[str, Any], live_names: List[str]) -> Dict[str, Any]:
    status = row.get("status")
    active = status in ACTIVE_STATUSES
    silence = row.get("silence_seconds")
    limit = BATCH_QUEUED_STALE_SECONDS if status == "QUEUED" else BATCH_RUNNING_STALE_SECONDS
    is_stale = bool(active and silence is not None and silence > limit)
    orphaned = bool(active and row.get("before_start"))
    alive = bool(active and _has_live_thread(row["batch_id"], live_names))
    ptype = _process_type(row.get("mode"))
    return {
        "process_id": row["batch_id"],
        "process_source": "batch_jobs",
        "process_type": ptype,
        "mode": row.get("mode"),
        "title": _batch_title(row, ptype),
        "status": status,
        "total": row.get("total") or 0,
        "processed": row.get("processed") or 0,
        "succeeded": row.get("succeeded") or 0,
        "failed": row.get("failed") or 0,
        "skipped": row.get("skipped") or 0,
        "created_at": row.get("created_at"),
        "started_at": row.get("started_at"),
        "updated_at": row.get("updated_at"),
        "finished_at": row.get("finished_at"),
        "error_message": row.get("error_message"),
        "requested_by": row.get("opt_requested_by"),
        "dry_run": row.get("opt_dry_run") == "true",
        "seconds_since_update": silence if active else None,
        "stale_after_seconds": limit,
        "indeterminate": bool(active and ptype == TYPE_THEMATIC),
        "is_stale": is_stale,
        "orphaned_after_restart": orphaned,
        "thread_alive": alive,
        "can_mark_failed": bool(active and not alive and (is_stale or orphaned)),
    }


def _scheduler_item(row: Dict[str, Any]) -> Dict[str, Any]:
    status = row.get("status")
    active = status in ACTIVE_STATUSES
    silence = row.get("silence_seconds")
    limit = (
        SCHEDULER_QUEUED_STALE_SECONDS if status == "QUEUED" else SCHEDULER_RUNNING_STALE_SECONDS
    )
    title = "Cicle del scheduler" + (" (prova)" if row.get("dry_run") else "")
    return {
        "process_id": row["run_id"],
        "process_source": "scheduler_runs",
        "process_type": TYPE_SCHEDULER,
        "mode": row.get("mode"),
        "title": title,
        "status": status,
        "total": 0,
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
        "created_at": row.get("started_at"),
        "started_at": row.get("started_at"),
        "updated_at": row.get("updated_at"),
        "last_heartbeat_at": row.get("last_heartbeat_at"),
        "finished_at": row.get("finished_at"),
        "duration_seconds": row.get("duration_seconds"),
        "error_message": row.get("error_message"),
        "current_stage": row.get("current_stage"),
        "current_action": row.get("current_action"),
        "requested_by": None,
        "dry_run": bool(row.get("dry_run")),
        "seconds_since_update": silence if active else None,
        "stale_after_seconds": limit,
        "indeterminate": active,
        "is_stale": bool(active and silence is not None and silence > limit),
        "orphaned_after_restart": bool(active and row.get("before_start")),
        "thread_alive": None,
        # El scheduler es recupera sol (recover_stale_scheduler_runs): cap acció manual.
        "can_mark_failed": False,
    }


def _sort_key(item: Dict[str, Any]):
    value = item.get("started_at") or item.get("created_at")
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# ---------------------------------------------------------------------------
# Consultes
# ---------------------------------------------------------------------------

def _parse_statuses(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    values = [s.strip().upper() for s in raw.split(",") if s.strip()]
    invalid = [s for s in values if s not in ALL_STATUSES]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Estats no vàlids: {', '.join(invalid)}")
    return values


def _batch_filter(statuses: List[str], ptype: Optional[str]):
    where, params = [], {"started": PROCESS_STARTED_AT}
    if statuses:
        where.append("status = ANY(%(statuses)s)")
        params["statuses"] = statuses
    if ptype == TYPE_ENRICH:
        where.append("mode = ANY(%(modes)s)")
        params["modes"] = list(ENRICH_MODES)
    elif ptype == TYPE_THEMATIC:
        where.append("mode = %(mode)s")
        params["mode"] = MODE_THEMATIC
    elif ptype == TYPE_BIHP:
        where.append("mode = %(mode)s")
        params["mode"] = MODE_BIHP
    elif ptype == TYPE_UNKNOWN:
        where.append("NOT (mode = ANY(%(known)s))")
        params["known"] = list(KNOWN_MODES)
    return ("WHERE " + " AND ".join(where)) if where else "", params


def _scheduler_filter(statuses: List[str]):
    params = {"started": PROCESS_STARTED_AT}
    if statuses:
        params["statuses"] = statuses
        return "WHERE status = ANY(%(statuses)s)", params
    return "", params


def _fetch_batches(where_sql: str, params: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    sql = (
        f"SELECT {BATCH_LIST_COLUMNS} FROM public.batch_jobs {where_sql} "
        "ORDER BY COALESCE(started_at, created_at) DESC LIMIT %(limit)s"
    )
    return _query(sql, {**params, "limit": limit})


def _fetch_runs(where_sql: str, params: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    sql = (
        f"SELECT {SCHEDULER_LIST_COLUMNS} FROM public.scheduler_runs {where_sql} "
        "ORDER BY started_at DESC LIMIT %(limit)s"
    )
    return _query(sql, {**params, "limit": limit})


def _count(table: str, where_sql: str, params: Dict[str, Any]) -> int:
    row = _query(
        f"SELECT COUNT(*) AS n FROM public.{table} {where_sql}",
        {k: v for k, v in params.items() if k != "started"},
        one=True,
    )
    return int(row["n"])


def _global_counts(live_names: List[str]) -> Dict[str, int]:
    where_b, params_b = _batch_filter(list(ACTIVE_STATUSES), None)
    where_s, params_s = _scheduler_filter(list(ACTIVE_STATUSES))
    batches = [_batch_item(r, live_names) for r in _fetch_batches(where_b, params_b, 500)]
    runs = [_scheduler_item(r) for r in _fetch_runs(where_s, params_s, 100)]
    attention = sum(
        1 for b in batches if (b["is_stale"] or b["orphaned_after_restart"]) and not b["thread_alive"]
    ) + sum(1 for r in runs if r["is_stale"] or r["orphaned_after_restart"])
    problems = _query(
        """
        SELECT
          (SELECT COUNT(*) FROM public.batch_jobs
            WHERE status IN ('FAILED','COMPLETED_WITH_ERRORS')
              AND COALESCE(finished_at, updated_at) > NOW() - INTERVAL '24 hours')
          +
          (SELECT COUNT(*) FROM public.scheduler_runs
            WHERE status IN ('FAILED','COMPLETED_WITH_ERRORS')
              AND COALESCE(finished_at, updated_at) > NOW() - INTERVAL '24 hours') AS n
        """,
        one=True,
    )
    return {
        "active": len(batches) + len(runs),
        "stale": attention,
        "failed_recent": int(problems["n"]),
    }


# ---------------------------------------------------------------------------
# Endpoints de lectura
# ---------------------------------------------------------------------------

@router_processes.get("")
def list_processes(
    status: Optional[str] = Query(None, description="Estats separats per coma"),
    type: Optional[str] = Query(None, description="Tipus de procés"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Llista unificada de batch_jobs i scheduler_runs, ordenada per data d'inici descendent."""
    statuses = _parse_statuses(status)
    if type and type not in ALL_TYPES:
        raise HTTPException(status_code=400, detail=f"Tipus no vàlid: {type}")

    live_names = _live_thread_names()
    fetch_limit = offset + limit
    items: List[Dict[str, Any]] = []
    total = 0

    if not type or type in BATCH_TYPES:
        where_b, params_b = _batch_filter(statuses, type)
        total += _count("batch_jobs", where_b, params_b)
        items += [_batch_item(r, live_names) for r in _fetch_batches(where_b, params_b, fetch_limit)]

    if not type or type == TYPE_SCHEDULER:
        where_s, params_s = _scheduler_filter(statuses)
        total += _count("scheduler_runs", where_s, params_s)
        items += [_scheduler_item(r) for r in _fetch_runs(where_s, params_s, fetch_limit)]

    items.sort(key=_sort_key, reverse=True)
    return {
        "items": items[offset: offset + limit],
        "total": total,
        "limit": limit,
        "offset": offset,
        "counts": _global_counts(live_names),
        "process_started_at": PROCESS_STARTED_AT,
    }


@router_processes.get("/purge-preview")
def purge_preview(older_than_days: int = Query(PURGE_DEFAULT_DAYS, ge=PURGE_MIN_DAYS, le=3650)):
    """Previsualitza què esborraria la neteja (5.2d). No modifica res."""
    rows = _query(
        f"""
        SELECT b.status, COUNT(*) AS n, MIN(b.finished_at) AS oldest, MAX(b.finished_at) AS newest,
               COALESCE(SUM(pg_column_size(b)), 0) AS bytes
        FROM public.batch_jobs b
        WHERE {PURGE_WHERE}
        GROUP BY b.status
        """,
        {"days": older_than_days},
    )
    oldest = min((r["oldest"] for r in rows), default=None)
    newest = max((r["newest"] for r in rows), default=None)
    return {
        "older_than_days": older_than_days,
        "count": sum(int(r["n"]) for r in rows),
        "bytes": sum(int(r["bytes"]) for r in rows),
        "oldest": oldest,
        "newest": newest,
        "by_status": {r["status"]: int(r["n"]) for r in rows},
    }


@router_processes.get("/export")
def export_purgeable(older_than_days: int = Query(PURGE_DEFAULT_DAYS, ge=PURGE_MIN_DAYS, le=3650)):
    """Còpia JSON de les mateixes files que esborraria la neteja (per descarregar abans d'esborrar)."""
    rows = _query(
        f"SELECT * FROM public.batch_jobs WHERE {PURGE_WHERE} ORDER BY finished_at ASC",
        {"days": older_than_days},
    )
    return {
        "exported_at": datetime.now(timezone.utc),
        "older_than_days": older_than_days,
        "count": len(rows),
        "items": [dict(r) for r in rows],
    }


@router_processes.get("/{process_id}")
def get_process(process_id: str):
    """Detall d'un procés. Batches: items i options. Scheduler: events del run."""
    live_names = _live_thread_names()

    if _is_scheduler_id(process_id):
        row = _query(
            f"SELECT *, "
            "EXTRACT(EPOCH FROM (NOW() - COALESCE(last_heartbeat_at, updated_at, started_at)))::int AS silence_seconds, "
            "(COALESCE(last_heartbeat_at, updated_at, started_at) < %(started)s) AS before_start "
            "FROM public.scheduler_runs WHERE run_id = %(id)s",
            {"id": process_id, "started": PROCESS_STARTED_AT},
            one=True,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Scheduler run no trobat")
        return {
            "item": _scheduler_item(row),
            "stages": row.get("stages"),
            "progress": row.get("progress"),
            "result": row.get("result"),
            "events": get_scheduler_run_events(process_id, limit=200),
        }

    row = _query(
        "SELECT *, "
        "COALESCE(array_length(entry_ids, 1), 0) AS entry_count, "
        "options->>'brief' AS opt_brief, options->>'requested_by' AS opt_requested_by, "
        "options->>'dry_run' AS opt_dry_run, "
        "CASE WHEN jsonb_typeof(options->'candidate_ids') = 'array' "
        "     THEN jsonb_array_length(options->'candidate_ids') END AS opt_candidate_count, "
        "EXTRACT(EPOCH FROM (NOW() - COALESCE(updated_at, created_at)))::int AS silence_seconds, "
        "(COALESCE(updated_at, created_at) < %(started)s) AS before_start "
        "FROM public.batch_jobs WHERE batch_id = %(id)s",
        {"id": process_id, "started": PROCESS_STARTED_AT},
        one=True,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Batch no trobat")
    return {
        "item": _batch_item(row, live_names),
        "options": row.get("options"),
        "items": row.get("items"),
        "entry_ids": row.get("entry_ids"),
    }


# ---------------------------------------------------------------------------
# Acció manual: marcar com a FAILED
# ---------------------------------------------------------------------------

class MarkFailedRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=500)
    requested_by: Optional[str] = Field(default="admin-ui", max_length=100)


@router_processes.post("/{process_id}/mark-failed")
def mark_failed(process_id: str, body: MarkFailedRequest):
    """
    Marca com a FAILED un batch penjat. No atura cap thread: per això es rebutja si n'hi ha un de viu.
    La sentència és atòmica i només s'aplica si updated_at no ha canviat des de la validació.
    """
    if _is_scheduler_id(process_id):
        raise HTTPException(
            status_code=409,
            detail="Els cicles del scheduler es recuperen automàticament; no admeten acció manual.",
        )

    live_names = _live_thread_names()
    row = _query(
        f"SELECT {BATCH_LIST_COLUMNS} FROM public.batch_jobs WHERE batch_id = %(id)s",
        {"id": process_id, "started": PROCESS_STARTED_AT},
        one=True,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Batch no trobat")

    item = _batch_item(row, live_names)
    if row["status"] not in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail=f"El procés ja no és actiu (estat {row['status']}).")
    if item["thread_alive"]:
        raise HTTPException(
            status_code=409,
            detail="El procés segueix viu al servidor: marcar-lo com a FAILED no l'aturaria.",
        )
    if not (item["is_stale"] or item["orphaned_after_restart"]):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Encara no compleix el criteri d'estancament: {item['seconds_since_update']} s "
                f"sense activitat (llindar {item['stale_after_seconds']} s)."
            ),
        )

    requested_by = (body.requested_by or "admin-ui").strip() or "admin-ui"
    reason = body.reason.strip()
    message = f"[Marcat manualment com a FAILED per {requested_by}] {reason}"[:2000]
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE public.batch_jobs
                SET status = 'FAILED',
                    error_message = %(message)s,
                    finished_at = NOW(),
                    updated_at = NOW(),
                    options = COALESCE(options, '{}'::jsonb) || jsonb_build_object(
                        'manual_failure',
                        jsonb_build_object('reason', %(reason)s::text,
                                           'by', %(by)s::text,
                                           'at', %(at)s::text))
                WHERE batch_id = %(id)s
                  AND status IN ('QUEUED','RUNNING')
                  AND updated_at IS NOT DISTINCT FROM %(seen)s
                RETURNING batch_id, status
                """,
                {
                    "message": message,
                    "reason": reason,
                    "by": requested_by,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "id": process_id,
                    "seen": _seen_updated_at(process_id, row),
                },
            )
            updated = cur.fetchone()
        if not updated:
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail="El procés ha canviat mentre es validava. Refresca la llista i torna-ho a provar.",
            )
        conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"No s'ha pogut marcar el procés: {exc}")
    finally:
        conn.close()

    return {"status": "marked_failed", "process_id": process_id, "new_status": "FAILED"}


def _seen_updated_at(process_id: str, row: Dict[str, Any]):
    """Retorna l'updated_at exacte llegit (el SELECT lleuger ja el porta)."""
    return row.get("updated_at")


# ---------------------------------------------------------------------------
# 5.2d · Neteja de l'historial
# ---------------------------------------------------------------------------

class PurgeRequest(BaseModel):
    older_than_days: int = Field(PURGE_DEFAULT_DAYS, ge=PURGE_MIN_DAYS, le=3650)
    confirm_count: int = Field(..., ge=0, description="Recompte vist a la vista prèvia")


@router_processes.post("/purge")
def purge_history(body: PurgeRequest):
    """
    Esborra batch_jobs finalitzats amb més de `older_than_days` dies. Mai toca QUEUED ni RUNNING.
    Exigeix `confirm_count` igual al recompte actual, perquè la vista prèvia no hagi quedat obsoleta.
    """
    params = {"days": body.older_than_days}
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM public.batch_jobs WHERE {PURGE_WHERE}", params)
            current = int(cur.fetchone()["n"])
            if current != body.confirm_count:
                conn.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"El recompte ha canviat ({current} ara, {body.confirm_count} a la vista prèvia). "
                        "Torna a previsualitzar."
                    ),
                )
            cur.execute(f"DELETE FROM public.batch_jobs WHERE {PURGE_WHERE}", params)
            deleted = cur.rowcount
        conn.commit()
    except HTTPException:
        raise
    except psycopg2.errors.ForeignKeyViolation:
        conn.rollback()
        raise HTTPException(
            status_code=409,
            detail="Hi ha dades que depenen d'aquests batches; no s'ha esborrat res.",
        )
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"No s'ha pogut netejar l'historial: {exc}")
    finally:
        conn.close()

    return {"status": "purged", "deleted": deleted, "older_than_days": body.older_than_days}
