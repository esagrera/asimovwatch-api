"""
Cua d'enriquiment d'AsimovWatch.

Aquest mòdul és responsable de seleccionar entries candidates per a la cua
d'enriquiment. No exposa endpoints FastAPI i, en aquesta primera versió, no
executa el pipeline LLM ni modifica la base de dades.

Responsabilitats:
- Llegir la configuració de la cua des de public.config amb defaults segurs.
- Seleccionar entries RAW i ERROR explícitament reintentables.
- Excloure DISCARDED, ENRICHED, errors permanents i errors desconeguts.
- Aplicar una quota inicial per source_domain.
- Omplir la capacitat sobrant per antiguitat global.
- Retornar una selecció explicable i apta per a dry-run.
"""

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2.extras

from app.crawler import run_entry_enrichment
from app.db import get_connection


DEFAULT_ENTRY_ENRICHMENT_ENABLED = True
DEFAULT_MAX_PER_RUN = 10
DEFAULT_MAX_PER_SOURCE = 2
DEFAULT_RETRY_MAX = 3
DEFAULT_TIMEOUT_SECONDS = 180

PERMANENT_ERROR_MARKERS = (
    "credit balance",
    "api key",
    "credential",
    "missing provider credentials",
)

RETRYABLE_ERROR_MARKERS = (
    "overloaded",
    "529",
    "rate limit",
    "timeout",
    "connection reset",
)


def _parse_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(
    value: Optional[str],
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default

    return min(max(parsed, minimum), maximum)


def get_enrichment_queue_config() -> Dict[str, Any]:
    """
    Llegeix la configuració de public.config sense escriure cap valor.

    Les claus encara poden no existir mentre la UI de la cua no s'hagi
    implementat. En aquest cas, s'apliquen els defaults acordats. Aquest
    comportament fa el mòdul segur per a dry-run i evita una migració SQL
    només per introduir valors de configuració.
    """
    conn = get_connection()

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT key, value
                FROM public.config
                WHERE key = ANY(%s)
                """,
                ([
                    "entry_enrichment_enabled",
                    "entry_enrichment_max_per_run",
                    "entry_enrichment_max_per_source",
                    "entry_enrichment_retry_max",
                    "entry_enrichment_timeout_seconds",
                ],),
            )
            config_map = {
                row["key"]: row["value"]
                for row in cur.fetchall()
            }
    finally:
        conn.close()

    return {
        "enabled": _parse_bool(
            config_map.get("entry_enrichment_enabled"),
            DEFAULT_ENTRY_ENRICHMENT_ENABLED,
        ),
        "max_per_run": _parse_int(
            config_map.get("entry_enrichment_max_per_run"),
            DEFAULT_MAX_PER_RUN,
            minimum=1,
            maximum=100,
        ),
        "max_per_source": _parse_int(
            config_map.get("entry_enrichment_max_per_source"),
            DEFAULT_MAX_PER_SOURCE,
            minimum=1,
            maximum=100,
        ),
        "retry_max": _parse_int(
            config_map.get("entry_enrichment_retry_max"),
            DEFAULT_RETRY_MAX,
            minimum=0,
            maximum=20,
        ),
        "timeout_seconds": _parse_int(
            config_map.get("entry_enrichment_timeout_seconds"),
            DEFAULT_TIMEOUT_SECONDS,
            minimum=1,
            maximum=3600,
        ),
    }


def classify_processing_error(processing_error: Optional[str]) -> str:
    """
    Classifica un error de pipeline per decidir si una entry ERROR pot tornar
    a entrar a la cua automàtica.

    - permanent: saldo, API key o credencial/configuració coneguda.
    - retryable: incidència temporal explícita.
    - unknown: qualsevol altre error; no es reintenta automàticament.

    Es prioritza permanent si, excepcionalment, un mateix missatge conté
    marcadors contradictoris. Això evita reintents automàtics imprudents.
    """
    normalized = (processing_error or "").strip().lower()

    if not normalized:
        return "unknown"

    if any(marker in normalized for marker in PERMANENT_ERROR_MARKERS):
        return "permanent"

    if any(marker in normalized for marker in RETRYABLE_ERROR_MARKERS):
        return "retryable"

    return "unknown"


def _load_candidate_entries(retry_max: int) -> List[Dict[str, Any]]:
    """
    Carrega RAW i ERROR potencialment seleccionables en ordre estable.

    Les entries ERROR no es filtren exclusivament en SQL segons el text de
    l'error: la classificació es fa en Python amb una política única,
    reutilitzable i fàcil de provar. SQL només aplica el límit de reintents.
    """
    conn = get_connection()

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    id,
                    source_domain,
                    detected_at,
                    processing_status,
                    processing_error,
                    processing_retries,
                    raw_content,
                    raw_snippet
                FROM public.entries
                WHERE processing_status = 'RAW'
                   OR (
                        processing_status = 'ERROR'
                    AND COALESCE(processing_retries, 0) < %s
                   )
                ORDER BY detected_at ASC NULLS LAST, id ASC
                """,
                (retry_max,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def _candidate_eligibility(candidate: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Determina si una entry carregada és elegible per a la cua.

    Les RAW són elegibles; si no tenen contingut, el pipeline compartit a
    crawler.py les descartarà sense consumir LLM. Les ERROR només ho són si
    classify_processing_error() identifica explícitament un error temporal.
    """
    processing_status = (candidate.get("processing_status") or "").upper()

    if processing_status == "RAW":
        return True, "raw"

    if processing_status == "ERROR":
        error_class = classify_processing_error(candidate.get("processing_error"))
        if error_class == "retryable":
            return True, "retryable_error"
        return False, f"error_{error_class}"

    return False, "unsupported_processing_status"


def select_enrichment_candidates(
    max_per_run: int,
    max_per_source: int,
    retry_max: int,
) -> Dict[str, Any]:
    """
    Selecciona entries per a la cua amb fairness condicionada per font.

    Fase A: selecciona fins a max_per_source entries per source_domain.
    Fase B: omple places sobrants amb les entries elegibles més antigues,
    sense aplicar cap topall de domini.

    La selecció no modifica cap fila. Les candidates es retornen amb els
    seus metadades mínimes perquè el caller pugui explicar el resultat.
    """
    if max_per_run < 1:
        raise ValueError("max_per_run ha de ser >= 1")
    if max_per_source < 1:
        raise ValueError("max_per_source ha de ser >= 1")
    if retry_max < 0:
        raise ValueError("retry_max ha de ser >= 0")

    loaded_candidates = _load_candidate_entries(retry_max=retry_max)

    eligible_candidates: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []

    for candidate in loaded_candidates:
        eligible, reason = _candidate_eligibility(candidate)
        if eligible:
            candidate["queue_reason"] = reason
            candidate["queue_source_domain"] = (
                (candidate.get("source_domain") or "").strip().lower()
                or "(unknown)"
            )
            eligible_candidates.append(candidate)
        else:
            excluded.append({
                "id": candidate["id"],
                "processing_status": candidate.get("processing_status"),
                "reason": reason,
            })

    selected: List[Dict[str, Any]] = []
    selected_ids = set()
    selected_per_domain: Dict[str, int] = {}

    # Fase A: quota inicial per domini, mantenint l'ordre global establert per SQL.
    for candidate in eligible_candidates:
        if len(selected) >= max_per_run:
            break

        domain = candidate["queue_source_domain"]
        if selected_per_domain.get(domain, 0) >= max_per_source:
            continue

        selected.append(candidate)
        selected_ids.add(candidate["id"])
        selected_per_domain[domain] = selected_per_domain.get(domain, 0) + 1

    quota_selected = len(selected)

    # Fase B: omplir places restants sense topall per font.
    if len(selected) < max_per_run:
        for candidate in eligible_candidates:
            if len(selected) >= max_per_run:
                break
            if candidate["id"] in selected_ids:
                continue

            selected.append(candidate)
            selected_ids.add(candidate["id"])

    overflow_selected = len(selected) - quota_selected

    selected_items = [
        {
            "id": candidate["id"],
            "source_domain": candidate["queue_source_domain"],
            "detected_at": candidate.get("detected_at"),
            "processing_status": candidate.get("processing_status"),
            "processing_retries": candidate.get("processing_retries"),
            "queue_reason": candidate["queue_reason"],
        }
        for candidate in selected
    ]

    return {
        "selected_ids": [candidate["id"] for candidate in selected],
        "selected_items": selected_items,
        "selection": {
            "max_per_run": max_per_run,
            "max_per_source": max_per_source,
            "retry_max": retry_max,
            "loaded_candidates": len(loaded_candidates),
            "eligible_candidates": len(eligible_candidates),
            "excluded_candidates": len(excluded),
            "quota_selected": quota_selected,
            "overflow_selected": overflow_selected,
        },
        "excluded": excluded,
    }

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _set_config_value(cur: Any, key: str, value: Optional[str]) -> None:
    """
    Desa telemetria del worker a public.config.

    Aquesta funció s'utilitza només per a entry_enrichment_last_*.
    No crea ni modifica la configuració de política (enabled, límits,
    retry_max o timeout), que s'ha de gestionar explícitament des de
    la UI o SQL administratiu.
    """
    safe_value = "" if value is None else str(value)

    cur.execute(
        """
        INSERT INTO public.config (key, value, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (key) DO UPDATE
        SET value = EXCLUDED.value,
            updated_at = NOW()
        """,
        (key, safe_value),
    )


def _persist_queue_run_status(
    run_started_at: datetime,
    status: str,
    duration_seconds: Optional[float] = None,
    error: Optional[str] = None,
) -> None:
    """
    Persisteix només telemetria de l'execució real de la cua.

    S'obre una connexió curta i independent perquè el pipeline de cada
    entry també obre/gestiona la seva connexió pròpia.
    """
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            _set_config_value(
                cur,
                "entry_enrichment_last_status",
                status,
            )
            _set_config_value(
                cur,
                "entry_enrichment_last_run_at",
                run_started_at.isoformat(),
            )
            _set_config_value(
                cur,
                "entry_enrichment_last_duration_seconds",
                "" if duration_seconds is None else str(duration_seconds),
            )
            _set_config_value(
                cur,
                "entry_enrichment_last_error",
                error,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def _run_selected_entries(
    selected_items: List[Dict[str, Any]],
    timeout_seconds: int,
) -> Dict[str, Any]:
    """
    Executa seqüencialment el pipeline complet sobre cada entry seleccionada.

    timeout_seconds és un llindar d'observabilitat en aquesta versió:
    es mesura la durada real després de cada crida, però no s'intenta
    cancel·lar un LLM que ja està en curs. Els timeouts efectius de xarxa
    continuen controlats per la configuració LLM de cada fase.
    """
    result_summary = {
        "attempted": 0,
        "enriched": 0,
        "discarded": 0,
        "stopped": 0,
        "failed": 0,
        "skipped": 0,
    }
    item_results: List[Dict[str, Any]] = []

    for selected_item in selected_items:
        entry_id = selected_item["id"]
        item_started_at = time.monotonic()

        try:
            pipeline_result = run_entry_enrichment(
                entry_id=entry_id,
                skip_input=False,
                run_input=True,
                run_primary=True,
                run_output=True,
                persist=True,
            )
            elapsed_seconds = round(time.monotonic() - item_started_at, 3)
            pipeline_status = pipeline_result.get("status", "error")

            item_result = {
                "entry_id": entry_id,
                "status": pipeline_status,
                "elapsed_seconds": elapsed_seconds,
                "exceeded_timeout_seconds": elapsed_seconds > timeout_seconds,
            }

            if pipeline_status == "enriched":
                result_summary["enriched"] += 1
            elif pipeline_status == "discarded":
                result_summary["discarded"] += 1
            elif pipeline_status == "stopped":
                result_summary["stopped"] += 1
            elif pipeline_status == "skipped":
                result_summary["skipped"] += 1
            else:
                result_summary["failed"] += 1
                item_result["detail"] = pipeline_result.get(
                    "detail",
                    "Error de pipeline sense detall.",
                )

        except Exception as exc:
            elapsed_seconds = round(time.monotonic() - item_started_at, 3)
            item_result = {
                "entry_id": entry_id,
                "status": "error",
                "elapsed_seconds": elapsed_seconds,
                "exceeded_timeout_seconds": elapsed_seconds > timeout_seconds,
                "detail": str(exc)[:2000],
            }
            result_summary["failed"] += 1

        result_summary["attempted"] += 1
        item_results.append(item_result)

    return {
        "summary": result_summary,
        "items": item_results,
    }

def run_enrichment_queue(
    max_per_run: Optional[int] = None,
    max_per_source: Optional[int] = None,
    retry_max: Optional[int] = None,
    timeout_seconds: Optional[int] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Selecciona i, només en execució real, processa la cua d'enriquiment.

    dry_run=True:
    - Selecciona candidates.
    - No crida LLM.
    - No actualitza public.entries.
    - No escriu entry_enrichment_last_*.

    dry_run=False:
    - Selecciona candidates.
    - Executa una entry rere l'altra amb Input -> Primary -> Output.
    - Continua si una entry falla.
    - Desa només telemetria entry_enrichment_last_*.
    """
    run_started_at = _utc_now()
    config = get_enrichment_queue_config()

    effective_max_per_run = (
        config["max_per_run"]
        if max_per_run is None
        else _parse_int(
            max_per_run,
            config["max_per_run"],
            minimum=1,
            maximum=100,
        )
    )
    effective_max_per_source = (
        config["max_per_source"]
        if max_per_source is None
        else _parse_int(
            max_per_source,
            config["max_per_source"],
            minimum=1,
            maximum=100,
        )
    )
    effective_retry_max = (
        config["retry_max"]
        if retry_max is None
        else _parse_int(
            retry_max,
            config["retry_max"],
            minimum=0,
            maximum=20,
        )
    )
    effective_timeout_seconds = (
        config["timeout_seconds"]
        if timeout_seconds is None
        else _parse_int(
            timeout_seconds,
            config["timeout_seconds"],
            minimum=1,
            maximum=3600,
        )
    )

    try:
        selection_result = select_enrichment_candidates(
            max_per_run=effective_max_per_run,
            max_per_source=effective_max_per_source,
            retry_max=effective_retry_max,
        )

        base_response = {
            "status": "ok",
            "mode": "dry_run" if dry_run else "executed",
            "dry_run": dry_run,
            "config": {
                "enabled": config["enabled"],
                "max_per_run": effective_max_per_run,
                "max_per_source": effective_max_per_source,
                "retry_max": effective_retry_max,
                "timeout_seconds": effective_timeout_seconds,
            },
            **selection_result,
        }

        if dry_run:
            return {
                **base_response,
                "result": {
                    "attempted": 0,
                    "enriched": 0,
                    "discarded": 0,
                    "stopped": 0,
                    "failed": 0,
                    "skipped": 0,
                },
                "items": [],
            }

        if not selection_result["selected_items"]:
            duration_seconds = round(
                (_utc_now() - run_started_at).total_seconds(),
                3,
            )
            _persist_queue_run_status(
                run_started_at=run_started_at,
                status="OK_EMPTY",
                duration_seconds=duration_seconds,
                error=None,
            )

            return {
                **base_response,
                "result": {
                    "attempted": 0,
                    "enriched": 0,
                    "discarded": 0,
                    "stopped": 0,
                    "failed": 0,
                    "skipped": 0,
                },
                "items": [],
            }

        execution_result = _run_selected_entries(
            selected_items=selection_result["selected_items"],
            timeout_seconds=effective_timeout_seconds,
        )
        duration_seconds = round(
            (_utc_now() - run_started_at).total_seconds(),
            3,
        )

        failures = [
            item
            for item in execution_result["items"]
            if item["status"] == "error"
        ]

        if failures:
            worker_status = "COMPLETED_WITH_ERRORS"
            worker_error = "; ".join(
                f"entry {item['entry_id']}: {str(item.get('detail', 'error'))[:300]}"
                for item in failures[:5]
            )
        else:
            worker_status = "OK"
            worker_error = None

        _persist_queue_run_status(
            run_started_at=run_started_at,
            status=worker_status,
            duration_seconds=duration_seconds,
            error=worker_error,
        )

        return {
            **base_response,
            "result": execution_result["summary"],
            "items": execution_result["items"],
        }

    except Exception as exc:
        duration_seconds = round(
            (_utc_now() - run_started_at).total_seconds(),
            3,
        )

        if not dry_run:
            try:
                _persist_queue_run_status(
                    run_started_at=run_started_at,
                    status="ERROR",
                    duration_seconds=duration_seconds,
                    error=str(exc)[:2000],
                )
            except Exception:
                pass

        raise
