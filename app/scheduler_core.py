from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple
import uuid

from app.scheduler_tracking import (
    append_scheduler_event,
    create_scheduler_run,
    update_scheduler_run,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def generate_scheduler_run_id() -> str:
    timestamp = utc_now().strftime("%Y%m%d_%H%M%S")
    return f"scheduler_{timestamp}_{uuid.uuid4().hex[:8]}"


def run_scheduler_cycle(
    *,
    dry_run: bool,
    force: bool,
    acquire_lock: Callable[[], Tuple[Any, Any]],
    release_lock: Callable[[Any, Any], None],
    get_config_map: Callable[[], Dict[str, Any]],
    parse_bool: Callable[..., bool],
    parse_int: Callable[..., int],
    is_due: Callable[..., Tuple[bool, str]],
    run_sources: Callable[[Dict[str, Any]], Dict[str, Any]],
    run_entries_rss: Callable[[Dict[str, Any]], Dict[str, Any]],
    run_enrichment_queue: Callable[[Dict[str, Any]], Dict[str, Any]],
    set_telemetry: Callable[[str, Optional[str]], None],
    telemetry_keys: Dict[str, str],
    worker_type: str = "scheduler",
    worker_instance_id: Optional[str] = None,
) -> Dict[str, Any]:
    run_id = generate_scheduler_run_id()
    started_at = utc_now()

    create_scheduler_run(
        run_id=run_id,
        mode="dry_run" if dry_run else "executed",
        dry_run=dry_run,
        force=force,
        worker_type=worker_type,
        worker_instance_id=worker_instance_id,
    )

    append_scheduler_event(
        run_id,
        "scheduler",
        "created",
        message="Execució del scheduler creada",
        metadata={
            "dry_run": dry_run,
            "force": force,
            "worker_type": worker_type,
        },
    )

    lock_conn = None
    lock_cur = None

    try:
        lock_conn, lock_cur = acquire_lock()

        if lock_conn is None:
            result = {
                "status": "skipped",
                "reason": "scheduler_already_running",
                "lock_acquired": False,
            }

            update_scheduler_run(
                run_id,
                status="SKIPPED_LOCKED",
                current_stage="scheduler",
                current_action="lock_not_acquired",
                result=result,
                finished=True,
            )

            append_scheduler_event(
                run_id,
                "scheduler",
                "lock_not_acquired",
                message="Ja hi ha un scheduler en execució",
            )

            return {
                "run_id": run_id,
                **result,
            }

        update_scheduler_run(
            run_id,
            status="RUNNING",
            current_stage="scheduler",
            current_action="lock_acquired",
            heartbeat=True,
        )

        append_scheduler_event(
            run_id,
            "scheduler",
            "lock_acquired",
            message="Lock del scheduler adquirit",
        )

        config_map = get_config_map()
        checked_at = utc_now()

        sources_enabled = parse_bool(
            config_map.get("crawler_enabled"),
            default=False,
        )
        entries_enabled = parse_bool(
            config_map.get("entry_crawler_enabled"),
            default=False,
        )
        enrichment_enabled = parse_bool(
            config_map.get("entry_enrichment_enabled"),
            default=True,
        )

        sources_frequency = parse_int(
            config_map.get("crawler_frequency_minutes"),
            default=1440,
        )
        entries_frequency = parse_int(
            config_map.get("entry_crawler_frequency_minutes"),
            default=1440,
        )
        enrichment_frequency = parse_int(
            config_map.get("entry_enrichment_frequency_minutes"),
            default=15,
        )

        sources_due_raw, sources_reason = is_due(
            config_map.get("crawler_last_run_at"),
            sources_frequency,
            force,
        )
        entries_due_raw, entries_reason = is_due(
            config_map.get("entry_crawler_last_run_at"),
            entries_frequency,
            force,
        )
        enrichment_due_raw, enrichment_reason = is_due(
            config_map.get("entry_enrichment_last_run_at"),
            enrichment_frequency,
            force,
        )

        decision = {
            "sources": {
                "enabled": sources_enabled,
                "due": sources_enabled and sources_due_raw,
                "reason": (
                    sources_reason
                    if sources_enabled
                    else "disabled"
                ),
                "result": None,
            },
            "entries_rss": {
                "enabled": entries_enabled,
                "due": entries_enabled and entries_due_raw,
                "reason": (
                    entries_reason
                    if entries_enabled
                    else "disabled"
                ),
                "result": None,
            },
            "enrichment_queue": {
                "enabled": enrichment_enabled,
                "due": enrichment_enabled and enrichment_due_raw,
                "reason": (
                    enrichment_reason
                    if enrichment_enabled
                    else "disabled"
                ),
                "result": None,
            },
        }

        append_scheduler_event(
            run_id,
            "scheduler",
            "decision_completed",
            message="Decisió de periodicitat completada",
            metadata={
                "sources": decision["sources"],
                "entries_rss": decision["entries_rss"],
                "enrichment_queue": decision["enrichment_queue"],
            },
        )

        update_scheduler_run(
            run_id,
            current_stage="scheduler",
            current_action="decision_completed",
            stages=decision,
            heartbeat=True,
        )

        if dry_run:
            result = {
                "status": "ok",
                "mode": "dry_run",
                "lock_acquired": True,
                "checked_at": checked_at.isoformat(),
                "force": force,
                "sources": decision["sources"],
                "entries_rss": decision["entries_rss"],
                "enrichment_queue": decision["enrichment_queue"],
            }

            update_scheduler_run(
                run_id,
                status="COMPLETED",
                current_stage="scheduler",
                current_action="finished",
                stages=decision,
                result=result,
                heartbeat=True,
                finished=True,
            )

            append_scheduler_event(
                run_id,
                "scheduler",
                "finished",
                message="Dry run finalitzat",
            )

            return {
                "run_id": run_id,
                **result,
            }

        executed = []
        had_errors = False

        if decision["sources"]["due"]:
            update_scheduler_run(
                run_id,
                current_stage="sources",
                current_action="started",
                heartbeat=True,
            )

            append_scheduler_event(
                run_id,
                "sources",
                "started",
                message="Crawler de fonts iniciat",
            )

            try:
                result = run_sources(config_map)
                decision["sources"]["result"] = result
                executed.append("sources")

                append_scheduler_event(
                    run_id,
                    "sources",
                    "completed",
                    message="Crawler de fonts finalitzat",
                    metadata={"result": result},
                )

            except Exception as exc:
                had_errors = True
                decision["sources"]["reason"] = str(exc)[:500]

                append_scheduler_event(
                    run_id,
                    "sources",
                    "failed",
                    message="Error al crawler de fonts",
                    metadata={
                        "error_type": type(exc).__name__,
                    },
                )

        if decision["entries_rss"]["due"]:
            update_scheduler_run(
                run_id,
                current_stage="entries_rss",
                current_action="started",
                heartbeat=True,
            )

            append_scheduler_event(
                run_id,
                "entries_rss",
                "started",
                message="Crawler RSS iniciat",
            )

            try:
                result = run_entries_rss(config_map)
                decision["entries_rss"]["result"] = result
                executed.append("entries_rss")

                append_scheduler_event(
                    run_id,
                    "entries_rss",
                    "completed",
                    message="Crawler RSS finalitzat",
                    metadata={"result": result},
                )

            except Exception as exc:
                had_errors = True
                decision["entries_rss"]["reason"] = str(exc)[:500]

                append_scheduler_event(
                    run_id,
                    "entries_rss",
                    "failed",
                    message="Error al crawler RSS",
                    metadata={
                        "error_type": type(exc).__name__,
                    },
                )

        if decision["enrichment_queue"]["due"]:
            update_scheduler_run(
                run_id,
                current_stage="enrichment_queue",
                current_action="started",
                heartbeat=True,
            )

            append_scheduler_event(
                run_id,
                "enrichment_queue",
                "started",
                message="Cua d'enriquiment iniciada",
            )

            try:
                result = run_enrichment_queue(config_map)
                decision["enrichment_queue"]["result"] = result
                executed.append("enrichment_queue")

                summary = {}
                if isinstance(result, dict):
                    summary = result.get("result", {}) or {}

                update_scheduler_run(
                    run_id,
                    current_stage="enrichment_queue",
                    current_action="completed",
                    progress={
                        "attempted": summary.get("attempted", 0),
                        "total": summary.get("attempted", 0),
                        "enriched": summary.get("enriched", 0),
                        "discarded": summary.get("discarded", 0),
                        "failed": summary.get("failed", 0),
                        "skipped": summary.get("skipped", 0),
                    },
                    stages=decision,
                    heartbeat=True,
                )

                append_scheduler_event(
                    run_id,
                    "enrichment_queue",
                    "completed",
                    message="Cua d'enriquiment finalitzada",
                    metadata={"result": summary},
                )

            except Exception as exc:
                had_errors = True
                decision["enrichment_queue"]["reason"] = str(exc)[:500]

                append_scheduler_event(
                    run_id,
                    "enrichment_queue",
                    "failed",
                    message="Error a la cua d'enriquiment",
                    metadata={
                        "error_type": type(exc).__name__,
                    },
                )

        finished_at = utc_now()
        duration_seconds = round(
            (finished_at - started_at).total_seconds(),
            3,
        )

        scheduler_status = (
            "COMPLETED_WITH_ERRORS"
            if had_errors
            else "COMPLETED"
        )

        result = {
            "status": (
                "completed_with_errors"
                if had_errors
                else "ok"
            ),
            "mode": "executed",
            "lock_acquired": True,
            "checked_at": checked_at.isoformat(),
            "force": force,
            "executed": executed,
            "duration_seconds": duration_seconds,
            "sources": decision["sources"],
            "entries_rss": decision["entries_rss"],
            "enrichment_queue": decision["enrichment_queue"],
        }

        update_scheduler_run(
            run_id,
            status=scheduler_status,
            current_stage="scheduler",
            current_action="finished",
            stages=decision,
            result=result,
            heartbeat=True,
            finished=True,
        )

        append_scheduler_event(
            run_id,
            "scheduler",
            "finished",
            message="Execució finalitzada",
            metadata={
                "status": scheduler_status,
                "executed": executed,
                "duration_seconds": duration_seconds,
            },
        )

        set_telemetry(
            telemetry_keys["last_run_at"],
            started_at.isoformat(),
        )
        set_telemetry(
            telemetry_keys["last_status"],
            "OK" if not had_errors else "COMPLETED_WITH_ERRORS",
        )
        set_telemetry(
            telemetry_keys["last_duration_seconds"],
            str(duration_seconds),
        )
        set_telemetry(
            telemetry_keys["last_error"],
            "Errors en un o més mòduls"
            if had_errors
            else "",
        )

        return {
            "run_id": run_id,
            **result,
        }

    except Exception as exc:
        error_message = str(exc)[:2000]

        try:
            update_scheduler_run(
                run_id,
                status="FAILED",
                current_stage="scheduler",
                current_action="error",
                error_message=error_message,
                heartbeat=True,
                finished=True,
            )

            append_scheduler_event(
                run_id,
                "scheduler",
                "failed",
                message="Error general del scheduler",
                metadata={
                    "error_type": type(exc).__name__,
                },
            )
        except Exception:
            pass

        raise

    finally:
        release_lock(lock_conn, lock_cur)