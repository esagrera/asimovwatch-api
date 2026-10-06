"""
AsimovWatch · Fase 5 · Peça 5.3 — Operacions d'ingesta (backend).

Router de la pantalla Ingesta → Operacions:
  GET  /ingest-ops/summary            Salut del pipeline i estat de la cua automàtica d'enriquiment.
  GET  /ingest-ops/entries            Llistes paginades (pendents, errors, descartades, incompletes) amb diagnòstic.
  POST /ingest-ops/reprocess-preview  Validació prèvia d'un reprocessament, amb l'estat de cada fase (no modifica res).
  GET  /ingest-ops/queue-config       Configuració de la cua automàtica d'enriquiment.
  PUT  /ingest-ops/queue-config       Desa la configuració (valida els rangs; només claus entry_enrichment_*).

El reprocessament real continua sent POST /api/batch/process (main.py): aquest mòdul NO llança res.

Disseny:
  - La classe d'error (permanent / retryable / unknown) i els marcadors surten d'app.enrichment_queue,
    perquè la pantalla vegi exactament el mateix que la cua automàtica.
  - Pestanya «Incompletes»: entrades ENRICHED a les quals falta alguna traducció (_ca / _en). Es reparen amb el
    mode «output-only» (vegeu app.output_repair), que només omple els camps buits.
  - Aquest mòdul NO importa app.main (evita imports circulars).
"""
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel

from app.db import get_connection
from app.enrichment_queue import (
    PERMANENT_ERROR_MARKERS,
    RETRYABLE_ERROR_MARKERS,
    classify_processing_error,
    get_enrichment_queue_config,
)

router_ingest_ops = APIRouter(prefix="/ingest-ops", tags=["ingest-ops"])

TABS = {"pending": "RAW", "errors": "ERROR", "discarded": "DISCARDED", "incomplete": "ENRICHED"}
INCOMPLETE_DIAGNOSES = ("translation_missing_ca", "translation_missing_en", "translation_missing_both")

# Idèntics a BATCH_MODES de main.py (fase requerida per mode).
VALID_MODES = ("input-only", "primary-only", "output-only", "semifull", "full")
REQUIRED_PHASE = {
    "input-only": None,
    "primary-only": "input",
    "output-only": "primary",
    "semifull": "input",
    "full": None,
}
# Crides LLM màximes per entrada segons les fases que executa cada mode.
LLM_CALLS_PER_ENTRY = {"full": 3, "semifull": 2, "input-only": 1, "primary-only": 1, "output-only": 1}
SUGGESTED_TIMEOUT_MS = {
    "full": 180000,
    "semifull": 120000,
    "input-only": 120000,
    "primary-only": 120000,
    "output-only": 120000,
}

UI_RECOMMENDED_MAX_IDS = 50
API_MAX_IDS = 500
ACTIVE_STATUSES = ("QUEUED", "RUNNING")

# Camps de text que han de tenir versió _ca i _en quan el base té text.
TRANSLATION_BASES = ("summary_factual", "why_it_matters", "human_protection_notes")

CONFIG_KEYS = (
    "entry_enrichment_frequency_minutes",
    "entry_enrichment_last_status",
    "entry_enrichment_last_run_at",
    "entry_enrichment_last_duration_seconds",
    "entry_enrichment_last_error",
)


# ---------------------------------------------------------------------------
# Helpers de BD
# ---------------------------------------------------------------------------

def _query(sql: str, params: Optional[Dict[str, Any]] = None, one: bool = False):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or {})
            return cur.fetchone() if one else cur.fetchall()
    finally:
        conn.close()


def _like_patterns(markers) -> List[str]:
    return [f"%{m.lower()}%" for m in markers]


def _marker_params(retry_max: int) -> Dict[str, Any]:
    return {
        "perm": _like_patterns(PERMANENT_ERROR_MARKERS),
        "retry": _like_patterns(RETRYABLE_ERROR_MARKERS),
        # «timeout» té diagnòstic propi; la resta de marcadors temporals = proveïdor saturat.
        "retry_other": _like_patterns(m for m in RETRYABLE_ERROR_MARKERS if m != "timeout"),
        "retry_max": retry_max,
    }


def _missing_sql(lang: str) -> str:
    """Expressió SQL: algun camp de text té base però no té la versió `lang` (ca | en)."""
    parts = [
        f"(COALESCE(btrim({b}), '') <> '' AND COALESCE(btrim({b}_{lang}), '') = '')"
        for b in TRANSLATION_BASES
    ]
    return "(" + " OR ".join(parts) + ")"


# CTE comuna: calcula diagnòstic, classe d'error i estat respecte a la cua d'una sola vegada.
# NOTA: no hi ha cap '%' literal; tots els patrons LIKE van com a paràmetres.
CLASSIFIED_CTE = f"""
WITH base AS (
    SELECT id, source_title, source_domain, detected_at, ingested_at, updated_at,
           processing_status, review_status, processing_error, processing_retries,
           input_relevance, ready_for_primary, input_relevance_reason, enriched_model,
           (COALESCE(btrim(raw_content), '') <> '' OR COALESCE(btrim(raw_snippet), '') <> '') AS has_content,
           lower(COALESCE(processing_error, '')) AS pe,
           {_missing_sql('ca')} AS miss_ca,
           {_missing_sql('en')} AS miss_en
    FROM public.entries
    WHERE processing_status = %(status)s
),
classified AS (
    SELECT base.*,
        CASE
            WHEN processing_status = 'RAW' THEN
                CASE
                    WHEN input_relevance IS NULL AND ready_for_primary IS NULL THEN 'no_input_executed'
                    WHEN input_relevance IS NOT NULL
                         AND lower(COALESCE(ready_for_primary, '')) IN ('yes', 'unclear') THEN 'stalled_after_input'
                    ELSE 'raw_other'
                END
            WHEN processing_status = 'ERROR' THEN
                CASE
                    WHEN pe LIKE ANY(%(perm)s) THEN 'credentials_or_balance'
                    WHEN strpos(pe, 'max_tokens') > 0 AND strpos(pe, 'fase input') > 0 THEN 'max_tokens_input'
                    WHEN strpos(pe, 'max_tokens') > 0 AND strpos(pe, 'fase primary') > 0 THEN 'max_tokens_primary'
                    WHEN strpos(pe, 'max_tokens') > 0 AND strpos(pe, 'fase output') > 0 THEN 'max_tokens_output'
                    WHEN strpos(pe, 'json') > 0 THEN 'invalid_llm_json'
                    WHEN strpos(pe, 'timeout') > 0 THEN 'llm_timeout'
                    WHEN pe LIKE ANY(%(retry_other)s) THEN 'provider_overloaded'
                    ELSE 'pipeline_error_other'
                END
            WHEN processing_status = 'DISCARDED' THEN
                CASE WHEN has_content THEN 'discarded_by_input' ELSE 'discarded_no_content' END
            WHEN processing_status = 'ENRICHED' THEN
                CASE
                    WHEN miss_ca AND miss_en THEN 'translation_missing_both'
                    WHEN miss_ca THEN 'translation_missing_ca'
                    WHEN miss_en THEN 'translation_missing_en'
                    ELSE 'enrichment_complete'
                END
            ELSE 'other'
        END AS diagnosis,
        CASE
            WHEN processing_status = 'ERROR' THEN
                CASE
                    WHEN pe LIKE ANY(%(perm)s) THEN 'permanent'
                    WHEN pe LIKE ANY(%(retry)s) THEN 'retryable'
                    ELSE 'unknown'
                END
            ELSE NULL
        END AS error_class
    FROM base
),
final AS (
    SELECT classified.*,
        CASE
            WHEN processing_status = 'RAW' THEN 'will_be_picked'
            WHEN processing_status = 'ERROR' THEN
                CASE
                    WHEN COALESCE(processing_retries, 0) >= %(retry_max)s THEN 'blocked_retries'
                    WHEN error_class = 'retryable' THEN 'will_be_picked'
                    WHEN error_class = 'permanent' THEN 'blocked_permanent'
                    ELSE 'blocked_unknown'
                END
            ELSE 'not_applicable'
        END AS queue_state
    FROM classified
)
"""


def _retry_max() -> int:
    return int(get_enrichment_queue_config()["retry_max"])


def _where(params: Dict[str, Any], diagnosis=None, queue_state=None, source_domain=None, q=None,
           only_incomplete: bool = False) -> str:
    conds: List[str] = []
    if only_incomplete:
        conds.append(
            "diagnosis IN ('translation_missing_ca', 'translation_missing_en', "
            "'translation_missing_both')"
        )
        conds.append("COALESCE(review_status, 'NEW') <> 'REJECTED'")
    if diagnosis:
        conds.append("diagnosis = %(diagnosis)s")
        params["diagnosis"] = diagnosis
    if queue_state == "blocked":
        conds.append("queue_state IN ('blocked_retries', 'blocked_permanent', 'blocked_unknown')")
    elif queue_state:
        conds.append("queue_state = %(queue_state)s")
        params["queue_state"] = queue_state
    if source_domain:
        conds.append("lower(COALESCE(source_domain, '')) = %(source_domain)s")
        params["source_domain"] = source_domain.strip().lower()
    if q and q.strip():
        text = q.strip().lower()
        conds.append("(lower(COALESCE(source_title, '')) LIKE %(q_like)s OR id::text = %(q_exact)s)")
        params["q_like"] = f"%{text}%"
        params["q_exact"] = text
    return ("WHERE " + " AND ".join(conds)) if conds else ""


def _group_counts(status: str, retry_max: int, column: str, **filters) -> Dict[str, int]:
    params = {"status": status, **_marker_params(retry_max)}
    where = _where(params, **filters)
    rows = _query(
        f"{CLASSIFIED_CTE} SELECT {column} AS k, COUNT(*) AS n FROM final {where} "
        f"GROUP BY {column} ORDER BY n DESC",
        params,
    )
    return {str(r["k"]): int(r["n"]) for r in rows if r["k"] is not None}


# ---------------------------------------------------------------------------
# GET /ingest-ops/summary
# ---------------------------------------------------------------------------

@router_ingest_ops.get("/summary")
def ingest_summary():
    """Comptadors globals, desglossament per diagnòstic i estat de cua, i telemetria de la cua automàtica."""
    cfg = get_enrichment_queue_config()
    retry_max = int(cfg["retry_max"])

    status_rows = _query(
        "SELECT processing_status AS s, COUNT(*) AS n FROM public.entries GROUP BY processing_status"
    )
    by_status = {str(r["s"]): int(r["n"]) for r in status_rows}
    pipeline = {
        "raw": by_status.get("RAW", 0),
        "error": by_status.get("ERROR", 0),
        "enriched": by_status.get("ENRICHED", 0),
        "discarded": by_status.get("DISCARDED", 0),
        "total": sum(by_status.values()),
        "other": sum(v for k, v in by_status.items() if k not in ("RAW", "ERROR", "ENRICHED", "DISCARDED")),
    }

    error_by_queue = _group_counts("ERROR", retry_max, "queue_state")
    error_by_class = _group_counts("ERROR", retry_max, "error_class")
    blocked = sum(v for k, v in error_by_queue.items() if k.startswith("blocked"))
    error_picked = error_by_queue.get("will_be_picked", 0)
    incomplete_by_diag = _group_counts("ENRICHED", retry_max, "diagnosis", only_incomplete=True)

    cfg_rows = _query(
        "SELECT key, value FROM public.config WHERE key = ANY(%(keys)s)", {"keys": list(CONFIG_KEYS)}
    )
    raw_cfg = {r["key"]: r["value"] for r in cfg_rows}
    try:
        frequency = int(str(raw_cfg.get("entry_enrichment_frequency_minutes", "15")).strip())
    except ValueError:
        frequency = 15

    active_run = _query(
        "SELECT run_id, status, current_stage, current_action, started_at, last_heartbeat_at "
        "FROM public.scheduler_runs WHERE status IN ('QUEUED','RUNNING') ORDER BY started_at DESC LIMIT 1",
        one=True,
    )
    last_run = _query(
        "SELECT run_id, status, started_at, finished_at, progress "
        "FROM public.scheduler_runs ORDER BY started_at DESC LIMIT 1",
        one=True,
    )

    return {
        "generated_at": datetime.now(timezone.utc),
        "pipeline": pipeline,
        "raw": {"by_diagnosis": _group_counts("RAW", retry_max, "diagnosis")},
        "error": {
            "by_diagnosis": _group_counts("ERROR", retry_max, "diagnosis"),
            "by_queue_state": error_by_queue,
            "by_class": error_by_class,
            "blocked": blocked,
            "will_be_picked": error_picked,
        },
        "discarded": {"by_diagnosis": _group_counts("DISCARDED", retry_max, "diagnosis")},
        "incomplete": {"by_diagnosis": incomplete_by_diag, "total": sum(incomplete_by_diag.values())},
        "queue": {
            "config": cfg,
            "frequency_minutes": frequency,
            "telemetry": {
                "last_status": raw_cfg.get("entry_enrichment_last_status") or None,
                "last_run_at": raw_cfg.get("entry_enrichment_last_run_at") or None,
                "last_duration_seconds": raw_cfg.get("entry_enrichment_last_duration_seconds") or None,
                "last_error": raw_cfg.get("entry_enrichment_last_error") or None,
            },
            # RAW (tots) + ERROR que la cua pot reintentar. Si la cua està desactivada, no s'agafarà res.
            "eligible_now": (pipeline["raw"] + error_picked) if cfg["enabled"] else 0,
            "max_per_cycle": int(cfg["max_per_run"]),
        },
        "scheduler": {"active_run": active_run, "last_run": last_run},
    }


# ---------------------------------------------------------------------------
# GET /ingest-ops/entries
# ---------------------------------------------------------------------------

@router_ingest_ops.get("/entries")
def list_ingest_entries(
    tab: str = Query("pending", description="pending (RAW) | errors (ERROR) | discarded (DISCARDED) | incomplete (ENRICHED sense alguna traducció)"),
    diagnosis: Optional[str] = Query(None),
    queue_state: Optional[str] = Query(None, description="Un valor concret o «blocked» (totes les bloquejades)"),
    source_domain: Optional[str] = Query(None),
    q: Optional[str] = Query(None, description="Text al títol o ID exacte"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    sort: str = Query("oldest", description="oldest | newest (per data de detecció)"),
):
    if tab not in TABS:
        raise HTTPException(status_code=400, detail=f"tab no vàlid: {tab}")
    if sort not in ("oldest", "newest"):
        raise HTTPException(status_code=400, detail=f"sort no vàlid: {sort}")

    status = TABS[tab]
    incomplete = tab == "incomplete"
    retry_max = _retry_max()
    base_params = {"status": status, **_marker_params(retry_max)}

    list_params = dict(base_params)
    where = _where(list_params, diagnosis, queue_state, source_domain, q, only_incomplete=incomplete)
    order = (
        "detected_at ASC NULLS LAST, id ASC" if sort == "oldest" else "detected_at DESC NULLS LAST, id DESC"
    )
    total = int(
        _query(f"{CLASSIFIED_CTE} SELECT COUNT(*) AS n FROM final {where}", list_params, one=True)["n"]
    )
    rows = _query(
        f"""{CLASSIFIED_CTE}
        SELECT id, source_title, source_domain, detected_at, ingested_at, updated_at,
               processing_status, review_status, LEFT(processing_error, 300) AS processing_error,
               COALESCE(processing_retries, 0) AS processing_retries,
               input_relevance, ready_for_primary,
               LEFT(input_relevance_reason, 300) AS input_relevance_reason,
               enriched_model, has_content, diagnosis, error_class, queue_state, miss_ca, miss_en
        FROM final {where}
        ORDER BY {order}
        LIMIT %(limit)s OFFSET %(offset)s""",
        {**list_params, "limit": limit, "offset": offset},
    )

    # Facetes: cada una ignora el seu propi filtre perquè el desplegable mostri totes les opcions.
    facets = {
        "diagnosis": _group_counts(
            status, retry_max, "diagnosis", queue_state=queue_state, source_domain=source_domain, q=q,
            only_incomplete=incomplete,
        ),
        "queue_state": _group_counts(
            status, retry_max, "queue_state", diagnosis=diagnosis, source_domain=source_domain, q=q,
            only_incomplete=incomplete,
        ),
    }
    dom_params = dict(base_params)
    dom_where = _where(dom_params, diagnosis, queue_state, None, q, only_incomplete=incomplete)
    dom_rows = _query(
        f"{CLASSIFIED_CTE} SELECT lower(COALESCE(source_domain, '(desconeguda)')) AS k, COUNT(*) AS n "
        f"FROM final {dom_where} GROUP BY 1 ORDER BY n DESC, k ASC LIMIT 10",
        dom_params,
    )
    facets["source_domain"] = {str(r["k"]): int(r["n"]) for r in dom_rows}

    return {
        "tab": tab,
        "processing_status": status,
        "items": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "facets": facets,
        "retry_max": retry_max,
    }


# ---------------------------------------------------------------------------
# POST /ingest-ops/reprocess-preview
# ---------------------------------------------------------------------------

class ReprocessPreviewRequest(BaseModel):
    entry_ids: List[int]
    mode: str = "full"
    skip_existing: bool = True


_PHASE_IN_ERROR = re.compile(r"fase\s+(input|primary|output)", re.IGNORECASE)


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, (list, dict)) and not value)


def _phase_persisted(row: Dict[str, Any], phase: str) -> bool:
    """Mateixa regla que phase_is_persisted de main.py."""
    if phase == "input":
        return row.get("input_relevance") is not None and row.get("ready_for_primary") is not None
    if phase == "primary":
        return (
            row.get("summary_factual") is not None
            or row.get("why_it_matters") is not None
            or row.get("enriched_at") is not None
        )
    return False


def _translation_gaps(row: Dict[str, Any]) -> Dict[str, List[str]]:
    """Camps de text amb base però sense la versió _ca / _en."""
    gaps: Dict[str, List[str]] = {"ca": [], "en": []}
    for base in TRANSLATION_BASES:
        if _is_empty(row.get(base)):
            continue
        for lang in ("ca", "en"):
            if _is_empty(row.get(f"{base}_{lang}")):
                gaps[lang].append(base)
    return gaps


def _any_translation(row: Dict[str, Any]) -> bool:
    return any(
        not _is_empty(row.get(f"{b}_{lang}"))
        for b in ("summary_factual", "why_it_matters") for lang in ("ca", "en")
    )


def _failed_phase(row: Dict[str, Any]) -> Optional[str]:
    """Fase on ha fallat una entrada ERROR, si el missatge ho diu («La fase Output ha retornat JSON invàlid…»)."""
    if (row.get("processing_status") or "").upper() != "ERROR":
        return None
    match = _PHASE_IN_ERROR.search(row.get("processing_error") or "")
    return match.group(1).lower() if match else None


def _phase_states(row: Dict[str, Any]) -> Dict[str, str]:
    """
    Estat de cada fase d'una entrada:
      done        feta i desada
      incomplete  Output ha corregut però falten traduccions (entrada ENRICHED incompleta)
      unsaved     feta però no desada (Primary quan Output ha fallat: el pipeline no la desa fins al final)
      failed      és on ha fallat
      pending     encara no feta
      n/a         no s'executarà (descartada per Input)
    """
    status = (row.get("processing_status") or "").upper()
    input_done = _phase_persisted(row, "input")
    primary_saved = _phase_persisted(row, "primary")
    failed = _failed_phase(row)

    if status == "DISCARDED":
        return {"input": "done" if input_done else "n/a", "primary": "n/a", "output": "n/a"}
    if status == "ENRICHED":
        gaps = _translation_gaps(row)
        if gaps["ca"] or gaps["en"]:
            output = "incomplete" if _any_translation(row) else "pending"
        else:
            output = "done" if _any_translation(row) else "pending"
        return {
            "input": "done" if input_done else "n/a",
            "primary": "done" if primary_saved else "n/a",
            "output": output,
        }
    return {
        "input": "done" if input_done else ("failed" if failed == "input" else "pending"),
        "primary": (
            "done" if primary_saved
            else "failed" if failed == "primary"
            else "unsaved" if failed == "output"
            else "pending"
        ),
        "output": "done" if _any_translation(row) else ("failed" if failed == "output" else "pending"),
    }


def _skip_reason(row: Dict[str, Any], mode: str, skip_existing: bool) -> Optional[str]:
    """Motiu pel qual process_batch_job SALTARIA l'entrada en aquest mode (o None si s'executaria)."""
    required = REQUIRED_PHASE[mode]
    if required and not _phase_persisted(row, required):
        return f"missing_required_phase_{required}"
    if skip_existing:
        if mode == "input-only" and _phase_persisted(row, "input"):
            return "input_already_persisted"
        if mode == "primary-only" and _phase_persisted(row, "primary"):
            return "primary_already_persisted"
        # Una entrada ENRICHED només se salta en output-only si les
        # traduccions requerides ja són completes. Si en falta una,
        # output-only és precisament la reparació segura.
        if mode == "output-only" and row.get("enriched_at") is not None:
            gaps = _translation_gaps(row)
            if not gaps["ca"] and not gaps["en"]:
                return "output_already_persisted"
    return None


def _would_queue_pick(row: Dict[str, Any], retry_max: int) -> bool:
    status = (row.get("processing_status") or "").upper()
    if status == "RAW":
        return True
    if status == "ERROR":
        return (
            int(row.get("processing_retries") or 0) < retry_max
            and classify_processing_error(row.get("processing_error")) == "retryable"
        )
    return False


@router_ingest_ops.post("/reprocess-preview")
def reprocess_preview(body: ReprocessPreviewRequest):
    """Valida un reprocessament abans de llançar-lo i informa de l'estat de cada fase. No modifica res."""
    blockers: List[Dict[str, str]] = []
    warnings: List[Dict[str, Any]] = []

    ids = list(body.entry_ids or [])
    mode = (body.mode or "").strip()
    if not ids:
        blockers.append({"code": "empty", "message": "No s'ha seleccionat cap entrada."})
    if len(ids) > API_MAX_IDS:
        blockers.append({"code": "too_many", "message": f"Màxim {API_MAX_IDS} entrades per batch."})
    if len(set(ids)) != len(ids):
        blockers.append({"code": "duplicates", "message": "Hi ha IDs duplicats."})
    if mode not in VALID_MODES:
        blockers.append({"code": "invalid_mode", "message": f"Mode no vàlid: {mode}."})
    if blockers:
        return {"ok": False, "blockers": blockers, "warnings": warnings, "items": []}

    retry_max = _retry_max()
    rows = _query(
        "SELECT id, processing_status, review_status, processing_error, processing_retries, input_relevance, "
        "ready_for_primary, summary_factual, why_it_matters, human_protection_notes, enriched_at, "
        "summary_factual_ca, summary_factual_en, why_it_matters_ca, why_it_matters_en, "
        "human_protection_notes_ca, human_protection_notes_en "
        "FROM public.entries WHERE id = ANY(%(ids)s)",
        {"ids": ids},
    )
    by_id = {r["id"]: r for r in rows}

    items: List[Dict[str, Any]] = []
    counts_by_status: Dict[str, int] = {}
    requires_full_ids: List[int] = []
    enriched_ids: List[int] = []
    complete_enriched_ids: List[int] = []
    queue_ids: List[int] = []
    will_run = 0
    phase_summary = {
        "total": 0,
        "input_done": 0,
        "primary_saved": 0,
        "output_done": 0,
        "output_incomplete": 0,
        "unsaved_primary": 0,
        "failed_by_phase": {"input": 0, "primary": 0, "output": 0, "unknown": 0},
    }

    for entry_id in ids:
        row = by_id.get(entry_id)
        if row is None:
            items.append({
                "id": entry_id,
                "processing_status": None,
                "will_run": False,
                "skip_reason": "entry_not_found",
                "phases": None,
                "failed_phase": None,
                "error": None,
                "retries": None,
            })
            continue

        status = (row.get("processing_status") or "").upper()
        review_status = (row.get("review_status") or "NEW").upper()
        counts_by_status[status] = counts_by_status.get(status, 0) + 1

        # Una entrada rebutjada editorialment no ha de consumir tokens.
        # Es mostra com a no executable i després bloqueja el batch sencer.
        if review_status == "REJECTED":
            phases = _phase_states(row)
            failed = _failed_phase(row)

            phase_summary["total"] += 1
            phase_summary["input_done"] += 1 if phases["input"] == "done" else 0
            phase_summary["primary_saved"] += 1 if phases["primary"] == "done" else 0
            phase_summary["output_done"] += 1 if phases["output"] == "done" else 0
            phase_summary["output_incomplete"] += 1 if phases["output"] == "incomplete" else 0
            phase_summary["unsaved_primary"] += 1 if phases["primary"] == "unsaved" else 0
            if status == "ERROR":
                phase_summary["failed_by_phase"][failed or "unknown"] += 1

            items.append({
                "id": entry_id,
                "processing_status": status,
                "review_status": review_status,
                "will_run": False,
                "skip_reason": "editorially_rejected",
                "phases": phases,
                "failed_phase": failed,
                "error": (
                    "Entrada rebutjada editorialment: no es reprocessa "
                    "ni es consumeixen tokens."
                ),
                "retries": int(row.get("processing_retries") or 0),
            })
            continue

        # Només cal forçar full si no hi ha cap fase que permeti continuar.
        # Si Primary ja està desada, output-only pot reparar traduccions
        # encara que l'entrada vingui d'un flux històric o de cerca temàtica
        # sense traça estructurada d'Input.
        if (
            not _phase_persisted(row, "input")
            and not _phase_persisted(row, "primary")
        ):
            requires_full_ids.append(entry_id)

        phases = _phase_states(row)
        if status == "ENRICHED":
            enriched_ids.append(entry_id)
            if phases["output"] == "done":
                complete_enriched_ids.append(entry_id)
        if _would_queue_pick(row, retry_max):
            queue_ids.append(entry_id)

        failed = _failed_phase(row)
        phase_summary["total"] += 1
        phase_summary["input_done"] += 1 if phases["input"] == "done" else 0
        phase_summary["primary_saved"] += 1 if phases["primary"] == "done" else 0
        phase_summary["output_done"] += 1 if phases["output"] == "done" else 0
        phase_summary["output_incomplete"] += 1 if phases["output"] == "incomplete" else 0
        phase_summary["unsaved_primary"] += 1 if phases["primary"] == "unsaved" else 0
        if status == "ERROR":
            phase_summary["failed_by_phase"][failed or "unknown"] += 1

        reason = _skip_reason(row, mode, body.skip_existing)
        runs = reason is None
        will_run += 1 if runs else 0
        error_text = (row.get("processing_error") or "")[:300] or None
        items.append({
            "id": entry_id,
            "processing_status": status,
            "review_status": review_status,
            "will_run": runs,
            "skip_reason": reason,
            "phases": phases,
            "failed_phase": failed,
            "error": error_text,
            "retries": int(row.get("processing_retries") or 0),
        })

    rejected_ids = [
        item["id"]
        for item in items
        if item.get("skip_reason") == "editorially_rejected"
    ]
    if rejected_ids:
        blockers.append({
            "code": "editorially_rejected",
            "message": (
                f"{len(rejected_ids)} entrada/es estan rebutjades editorialment. "
                "No es poden reprocessar."
            ),
        })

    skipped = len(items) - will_run
    if will_run == 0:
        blockers.append({
            "code": "nothing_to_run",
            "message": "Amb aquest mode no s'executaria cap entrada.",
        })

    if len(ids) > UI_RECOMMENDED_MAX_IDS:
        warnings.append({
            "code": "over_recommended",
            "message": (
                f"Més de {UI_RECOMMENDED_MAX_IDS} entrades: "
                "risc de batch llarg i de reinici del servidor."
            ),
        })
    if skipped and will_run:
        warnings.append({
            "code": "skipped_entries",
            "count": skipped,
            "message": f"{skipped} entrades es saltarien en aquest mode.",
        })
    if requires_full_ids and mode != "full":
        warnings.append({
            "code": "mode_skips_without_input",
            "count": len(requires_full_ids),
            "message": "Hi ha entrades sense la fase Input: només el mode «full» les processa.",
        })
    if enriched_ids and mode in ("full", "semifull", "input-only", "primary-only"):
        warnings.append({
            "code": "overwrites_enriched",
            "count": len(enriched_ids),
            "entry_ids": enriched_ids[:50],
            "message": f"{len(enriched_ids)} entrades ja estan ENRICHED: aquest mode en sobreescriuria el resultat.",
        })
    if mode == "output-only" and complete_enriched_ids:
        warnings.append({
            "code": "output_already_complete",
            "count": len(complete_enriched_ids),
            "entry_ids": complete_enriched_ids[:50],
            "message": (
                f"{len(complete_enriched_ids)} entrades ja tenen totes les traduccions: "
                "no s'hi canviarà res, però es gastaria una crida."
            ),
        })
    if queue_ids:
        warnings.append({
            "code": "queue_will_pick",
            "count": len(queue_ids),
            "entry_ids": queue_ids[:50],
            "message": f"{len(queue_ids)} entrades les agafaria igualment la cua automàtica.",
        })

    active_runs = _query(
        "SELECT run_id, status, current_stage, started_at FROM public.scheduler_runs "
        "WHERE status IN ('QUEUED','RUNNING') ORDER BY started_at DESC"
    )
    active_batches = _query(
        "SELECT batch_id, mode, status, processed, total, "
        "COALESCE(array_length(ARRAY(SELECT x FROM unnest(entry_ids) AS x "
        "WHERE x = ANY(%(ids)s)), 1), 0) AS overlap_count "
        "FROM public.batch_jobs "
        "WHERE status IN ('QUEUED','RUNNING') AND mode = ANY(%(modes)s)",
        {"ids": ids, "modes": list(VALID_MODES)},
    )

    if active_runs:
        stage = active_runs[0].get("current_stage") or "—"
        warnings.append({
            "code": "scheduler_active",
            "count": len(active_runs),
            "message": (
                f"Hi ha un cicle del scheduler en marxa (etapa: {stage}); "
                "pot processar les mateixes entrades."
            ),
        })
    if active_batches:
        warnings.append({
            "code": "batch_active",
            "count": len(active_batches),
            "message": "Hi ha un batch d'enriquiment en marxa.",
        })

    overlapping = sum(int(b["overlap_count"] or 0) for b in active_batches)
    if overlapping:
        warnings.append({
            "code": "batch_overlap",
            "count": overlapping,
            "message": f"{overlapping} d'aquestes entrades ja són en un batch en marxa.",
        })

    recommended_mode = "full" if requires_full_ids else mode
    return {
        "ok": not blockers,
        "mode": mode,
        "requested": len(ids),
        "will_run": will_run,
        "skipped": skipped,
        "counts_by_status": counts_by_status,
        "phase_summary": phase_summary,
        "items": items,
        "blockers": blockers,
        "warnings": warnings,
        "requires_full": bool(requires_full_ids),
        "requires_full_ids": requires_full_ids[:100],
        "recommended": {
            "mode": recommended_mode,
            "timeout_per_entry_ms": SUGGESTED_TIMEOUT_MS[recommended_mode],
        },
        "estimate": {"llm_calls_max": will_run * LLM_CALLS_PER_ENTRY[mode]},
        "overlap": {
            "scheduler_active": active_runs,
            "batches_active": active_batches,
            "queue_will_pick_ids": queue_ids[:100],
        },
        "queue_retry_max": retry_max,
    }


# ---------------------------------------------------------------------------
# Configuració de la cua automàtica (5.3-C)
# ---------------------------------------------------------------------------

QUEUE_CONFIG_KEYS = {
    "enabled": "entry_enrichment_enabled",
    "max_per_run": "entry_enrichment_max_per_run",
    "max_per_source": "entry_enrichment_max_per_source",
    "retry_max": "entry_enrichment_retry_max",
    "timeout_seconds": "entry_enrichment_timeout_seconds",
    "frequency_minutes": "entry_enrichment_frequency_minutes",
}
# Els quatre primers rangs són els de enrichment_queue.py. La freqüència admet des de 5 min: el scheduler
# s'avalua cada 15 min i amb 15 la cua s'executa a la pràctica cada ~30 (vegeu la guia).
QUEUE_CONFIG_LIMITS = {
    "max_per_run": (1, 100),
    "max_per_source": (1, 100),
    "retry_max": (0, 20),
    "timeout_seconds": (1, 3600),
    "frequency_minutes": (5, 10080),
}


class QueueConfigUpdate(BaseModel):
    enabled: bool
    max_per_run: int
    max_per_source: int
    retry_max: int
    timeout_seconds: int
    frequency_minutes: int


def _read_queue_config() -> Dict[str, Any]:
    cfg = get_enrichment_queue_config()
    rows = _query(
        "SELECT key, value FROM public.config WHERE key = ANY(%(keys)s)",
        {"keys": [QUEUE_CONFIG_KEYS["frequency_minutes"]] + list(CONFIG_KEYS)},
    )
    raw = {r["key"]: r["value"] for r in rows}
    try:
        frequency = int(str(raw.get(QUEUE_CONFIG_KEYS["frequency_minutes"], "15")).strip())
    except ValueError:
        frequency = 15
    return {
        "enabled": bool(cfg["enabled"]),
        "max_per_run": int(cfg["max_per_run"]),
        "max_per_source": int(cfg["max_per_source"]),
        "retry_max": int(cfg["retry_max"]),
        "timeout_seconds": int(cfg["timeout_seconds"]),
        "frequency_minutes": frequency,
        "limits": {k: {"min": lo, "max": hi} for k, (lo, hi) in QUEUE_CONFIG_LIMITS.items()},
    }


@router_ingest_ops.get("/queue-config")
def get_queue_config():
    """Configuració actual de la cua automàtica d'enriquiment (amb els rangs vàlids)."""
    return _read_queue_config()


@router_ingest_ops.put("/queue-config")
def put_queue_config(body: QueueConfigUpdate):
    """
    Desa la configuració de la cua. Valida els rangs i escriu NOMÉS les claus entry_enrichment_*,
    en una sola transacció. El canvi s'aplica al cicle següent del scheduler.
    """
    values = body.dict() if hasattr(body, "dict") else body.model_dump()
    errors = []
    for field, (lo, hi) in QUEUE_CONFIG_LIMITS.items():
        if not (lo <= values[field] <= hi):
            errors.append(f"{field} ha d'estar entre {lo} i {hi}")
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for field, key in QUEUE_CONFIG_KEYS.items():
                value = str(values[field]).lower() if field == "enabled" else str(values[field])
                cur.execute(
                    """
                    INSERT INTO public.config (key, value, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                    """,
                    (key, value),
                )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"No s'ha pogut desar la configuració: {exc}")
    finally:
        conn.close()
    return {"status": "saved", "config": _read_queue_config()}
