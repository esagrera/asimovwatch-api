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

from typing import Any, Dict, List, Optional, Tuple

import psycopg2.extras

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


def run_enrichment_queue(
    max_per_run: Optional[int] = None,
    max_per_source: Optional[int] = None,
    retry_max: Optional[int] = None,
    timeout_seconds: Optional[int] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Executa la primera versió segura de la cua: només selecció.

    En aquesta iteració, dry_run no modifica el comportament perquè el mòdul
    encara no escriu ni executa LLM. El paràmetre es retorna explícitament
    per mantenir el contracte de la futura fase d'execució.

    entry_enrichment_enabled és informatiu en aquesta capa: no bloqueja una
    invocació manual directa. El futur scheduler serà qui respectarà aquest
    flag abans de cridar la cua automàtica.
    """
    config = get_enrichment_queue_config()

    effective_max_per_run = (
        config["max_per_run"]
        if max_per_run is None
        else _parse_int(max_per_run, config["max_per_run"], minimum=1, maximum=100)
    )
    effective_max_per_source = (
        config["max_per_source"]
        if max_per_source is None
        else _parse_int(max_per_source, config["max_per_source"], minimum=1, maximum=100)
    )
    effective_retry_max = (
        config["retry_max"]
        if retry_max is None
        else _parse_int(retry_max, config["retry_max"], minimum=0, maximum=20)
    )
    effective_timeout_seconds = (
        config["timeout_seconds"]
        if timeout_seconds is None
        else _parse_int(timeout_seconds, config["timeout_seconds"], minimum=1, maximum=3600)
    )

    selection_result = select_enrichment_candidates(
        max_per_run=effective_max_per_run,
        max_per_source=effective_max_per_source,
        retry_max=effective_retry_max,
    )

    return {
        "status": "ok",
        "mode": "selection_only",
        "dry_run": dry_run,
        "config": {
            "enabled": config["enabled"],
            "max_per_run": effective_max_per_run,
            "max_per_source": effective_max_per_source,
            "retry_max": effective_retry_max,
            "timeout_seconds": effective_timeout_seconds,
        },
        **selection_result,
        "result": {
            "attempted": 0,
            "enriched": 0,
            "discarded": 0,
            "stopped": 0,
            "failed": 0,
            "skipped": 0,
        },
    }
