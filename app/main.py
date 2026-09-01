from dotenv import load_dotenv
load_dotenv()

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from functools import lru_cache

import psycopg2
import psycopg2.errors
from psycopg2.extras import RealDictCursor, Json
from fastapi import FastAPI, HTTPException, status, Security, Depends, Request, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, AnyUrl, Field
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from app.db import get_connection
from app.source_candidates import router_candidates
from app.llm_admin import router_llm_admin
from app.llm_clients import get_supported_providers
from app.crawler import run as run_entries_crawler, run_entry_enrichment


# ─── AUTH ─────────────────────────────────────────────────────────────────────

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def verify_api_key(api_key: str = Security(api_key_header)):
    expected = os.environ.get("API_KEY", "")
    if not expected:
        raise HTTPException(status_code=500, detail="API_KEY no configurada al servidor")
    if not secrets.compare_digest(api_key or "", expected):
        raise HTTPException(status_code=401, detail="API Key invàlida o absent")
    return api_key

# ─── APP ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Asimovwatch API",
    version="2.0.0",
    swagger_ui_parameters={"persistAuthorization": True}
)
protected_router = APIRouter(dependencies=[Depends(verify_api_key)])
protected_router.include_router(router_candidates)
protected_router.include_router(router_llm_admin)

@app.middleware("http")
async def protect_docs(request: Request, call_next):
    if request.url.path in ("/docs", "/openapi.json", "/redoc"):
        docs_user = os.environ.get("DOCS_USER", "")
        docs_password = os.environ.get("DOCS_PASSWORD", "")
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                user, pwd = decoded.split(":", 1)
                if secrets.compare_digest(user, docs_user) and secrets.compare_digest(pwd, docs_password):
                    return await call_next(request)
            except Exception:
                pass
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="AsimovWatch Docs"'},
            content="Accés no autoritzat"
        )
    return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://asimovwatch.com",
        "https://api.asimovwatch.com",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── UTILS ────────────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def get_config_map():
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT key, value FROM public.config")
        rows = cur.fetchall()
        return {row["key"]: row["value"] for row in rows}
    finally:
        cur.close()
        conn.close()

# ─── MODELS ───────────────────────────────────────────────────────────────────

class EntryIngest(BaseModel):
    source_url: AnyUrl
    source_domain: str
    source_title: str
    source_type: Optional[str] = None
    source_language: Optional[str] = None
    ingest_method: Optional[str] = None
    external_id: Optional[str] = None
    author_name: Optional[str] = None
    canonical_url: Optional[AnyUrl] = None
    published_date: Optional[datetime] = None
    detected_at: Optional[datetime] = None
    country_region: Optional[str] = None
    institution_type: Optional[str] = None
    raw_snippet: Optional[str] = None
    raw_content: Optional[str] = None
    raw_content_format: Optional[str] = None
    raw_payload: Optional[dict] = None
    summary_factual: Optional[str] = None
    why_it_matters: Optional[str] = None
    theme_tags: Optional[list] = None
    affected_principles: Optional[list] = None
    risk_level: Optional[str] = None
    debate_questions: Optional[list] = None
    confidence_notes: Optional[str] = None
    human_protection_declared: Optional[str] = None
    human_protection_verifiable: Optional[str] = None
    human_protection_depth: Optional[str] = None
    human_protection_notes: Optional[str] = None
    review_status: Optional[str] = "NEW"
    reviewer: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    editor_notes: Optional[str] = None
    validation_notes: Optional[str] = None

class EntryReview(BaseModel):
    review_status: str
    reviewer: Optional[str] = None
    editor_notes: Optional[str] = None
    validation_notes: Optional[str] = None

class EntryEnrich(BaseModel):
    processing_status: Optional[str] = None
    processing_error: Optional[str] = None
    processing_retries: Optional[int] = None
    relevance_score: Optional[str] = None
    relevance_reason: Optional[str] = None
    translated_summary_ca: Optional[str] = None
    translated_whyitmatters_ca: Optional[str] = None
    translated_debatequestions_ca: Optional[str] = None
    enriched_model: Optional[str] = None
    raw_snippet_original: Optional[str] = None
    source_language: Optional[str] = None
    summary_factual: Optional[str] = None
    why_it_matters: Optional[str] = None
    debate_questions: Optional[list] = None
    theme_tags: Optional[list] = None
    affected_principles: Optional[list] = None
    risk_level: Optional[str] = None
    human_protection_declared: Optional[str] = None
    human_protection_verifiable: Optional[str] = None
    human_protection_depth: Optional[str] = None
    human_protection_notes: Optional[str] = None
    confidence_notes: Optional[str] = None
    input_relevance: Optional[str] = None
    input_relevance_reason: Optional[str] = None
    ready_for_primary: Optional[str] = None
    clean_input_text: Optional[str] = None
    input_summary: Optional[str] = None
    input_quality: Optional[str] = None
    input_quality_notes: Optional[str] = None
    entry_category: Optional[str] = None
    analyzed_provider: Optional[str] = None
    analyzed_model: Optional[str] = None
    bihp_directives: Optional[list] = None

class EntryReenrichRequest(BaseModel):
    """
    Control de fases per a POST /entries/{entry_id}/reenrich.

    persist=True (defecte): mode operatiu; escriu el resultat a public.entries.
    persist=False: mode de prova; no modifica l'entry ni en cas d'èxit ni en
    cas d'error, i retorna les sortides de les fases executades.
    """
    skip_input: bool = False
    run_input: bool = True
    run_primary: bool = True
    run_output: bool = True
    persist: bool = True

class ConfigUpdate(BaseModel):
    value: str

class CrawlerLogCreate(BaseModel):
    sources_checked: int = 0
    items_found: int = 0
    items_relevant: int = 0
    items_enriched: int = 0
    items_failed: int = 0
    duration_seconds: Optional[float] = None
    notes: Optional[str] = None

# ─── BATCH CONSTANTS & MODELS ───────────────────────────────────────────────

BATCH_MODES = {
    "input-only": {
        "run_input": True,
        "run_primary": False,
        "run_output": False,
        "skip_input": False,
        "required_phase": None,
    },
    "primary-only": {
        "run_input": False,
        "run_primary": True,
        "run_output": False,
        "skip_input": True,
        "required_phase": "input",
    },
    "output-only": {
        "run_input": False,
        "run_primary": False,
        "run_output": True,
        "skip_input": True,
        "required_phase": "primary",
    },
    "semifull": {
        "run_input": False,
        "run_primary": True,
        "run_output": True,
        "skip_input": True,
        "required_phase": "input",
    },
    "full": {
        "run_input": True,
        "run_primary": True,
        "run_output": True,
        "skip_input": False,
        "required_phase": None,
    },
}

class BatchOptions(BaseModel):
    persist: bool = True
    skip_existing: bool = True
    max_concurrent: int = 1
    timeout_per_entry_ms: int = 120000
    on_error: str = "continue"
    max_retries: int = 0

class EntryBatchEnrich(BaseModel):
    entry_ids: list[int]

    processing_status: Optional[str] = None
    processing_error: Optional[str] = None
    processing_retries: Optional[int] = None

    relevance_score: Optional[str] = None
    relevance_reason: Optional[str] = None

    translated_summary_ca: Optional[str] = None
    translated_whyitmatters_ca: Optional[str] = None
    translated_debatequestions_ca: Optional[list] = None

    enriched_model: Optional[str] = None
    raw_snippet_original: Optional[str] = None
    source_language: Optional[str] = None

    summary_factual: Optional[str] = None
    why_it_matters: Optional[str] = None
    debate_questions: Optional[list] = None
    theme_tags: Optional[list] = None
    affected_principles: Optional[list] = None
    risk_level: Optional[str] = None

    human_protection_declared: Optional[str] = None
    human_protection_verifiable: Optional[str] = None
    human_protection_depth: Optional[str] = None
    human_protection_notes: Optional[str] = None
    confidence_notes: Optional[str] = None

    input_relevance: Optional[str] = None
    input_relevance_reason: Optional[str] = None
    ready_for_primary: Optional[str] = None
    clean_input_text: Optional[str] = None
    input_summary: Optional[str] = None
    input_quality: Optional[str] = None
    input_quality_notes: Optional[str] = None

    entry_category: Optional[str] = None
    analyzed_provider: Optional[str] = None
    analyzed_model: Optional[str] = None
    bihp_directives: Optional[list] = None


class BatchProcessRequest(BaseModel):
    entry_ids: List[int]
    mode: str = "full"
    options: BatchOptions = Field(default_factory=BatchOptions)


# ─── BATCH HELPERS ────────────────────────────────────────────────────────────

def validate_batch_request(body: BatchProcessRequest) -> None:
    if not body.entry_ids:
        raise HTTPException(
            status_code=400,
            detail="entry_ids no pot estar buit",
        )

    if len(body.entry_ids) > 500:
        raise HTTPException(
            status_code=400,
            detail="Màxim de 500 entry_ids per batch",
        )

    if len(set(body.entry_ids)) != len(body.entry_ids):
        raise HTTPException(
            status_code=400,
            detail="entry_ids conté IDs duplicats",
        )

    if body.mode not in BATCH_MODES:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "mode no vàlid",
                "allowed_modes": sorted(BATCH_MODES.keys()),
            },
        )

    if body.options.max_concurrent != 1:
        raise HTTPException(
            status_code=400,
            detail=(
                "La primera versió només permet processament seqüencial: "
                "max_concurrent ha de ser 1"
            ),
        )

    if body.options.timeout_per_entry_ms < 1000:
        raise HTTPException(
            status_code=400,
            detail="timeout_per_entry_ms ha de ser com a mínim 1000",
        )

    if body.options.on_error not in {"continue", "abort"}:
        raise HTTPException(
            status_code=400,
            detail="on_error ha de ser 'continue' o 'abort'",
        )


def generate_batch_id() -> str:
    return f"batch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def get_batch_job(batch_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cur.execute(
            """
            SELECT *
            FROM public.batch_jobs
            WHERE batch_id = %s
            """,
            (batch_id,),
        )
        return cur.fetchone()
    finally:
        cur.close()
        conn.close()


def update_batch_job(batch_id: str, **fields: Any) -> None:
    if not fields:
        return

    allowed_fields = {
        "status",
        "processed",
        "succeeded",
        "failed",
        "skipped",
        "items",
        "error_message",
        "started_at",
        "finished_at",
        "updated_at",
    }

    unknown = set(fields) - allowed_fields
    if unknown:
        raise ValueError(f"Camps de batch no permesos: {sorted(unknown)}")

    assignments = []
    values = []

    for field, value in fields.items():
        assignments.append(f"{field} = %s")
        if field == "items":
            values.append(Json(value))
        else:
            values.append(value)

    assignments.append("updated_at = NOW()")
    values.append(batch_id)

    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            f"""
            UPDATE public.batch_jobs
            SET {", ".join(assignments)}
            WHERE batch_id = %s
            """,
            values,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def get_entry_phase_state(cur: Any, entry_id: int) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT
            id,
            processing_status,
            input_relevance,
            ready_for_primary,
            summary_factual,
            why_it_matters,
            enriched_at
        FROM public.entries
        WHERE id = %s
        """,
        (entry_id,),
    )
    return cur.fetchone()


def phase_is_persisted(entry: Dict[str, Any], phase: str) -> bool:
    if phase == "input":
        return (
            entry.get("input_relevance") is not None
            and entry.get("ready_for_primary") is not None
        )

    if phase == "primary":
        return (
            entry.get("summary_factual") is not None
            or entry.get("why_it_matters") is not None
            or entry.get("enriched_at") is not None
        )

    return False

# ─── BATCH WORKER ─────────────────────────────────────────────────────────────

def process_batch_job(
    batch_id: str,
    entry_ids: List[int],
    mode: str,
    options: Dict[str, Any],
) -> None:
    batch_config = BATCH_MODES[mode]
    timeout_seconds = options["timeout_per_entry_ms"] / 1000.0

    items: List[Dict[str, Any]] = []
    processed = 0
    succeeded = 0
    failed = 0
    skipped = 0

    update_batch_job(
        batch_id,
        status="RUNNING",
        started_at=utc_now(),
    )

    for entry_id in entry_ids:
        item_started_at = time.monotonic()
        item_result: Dict[str, Any] = {
            "entry_id": entry_id,
            "status": "pending",
        }

        try:
            conn = get_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)

            try:
                entry = get_entry_phase_state(cur, entry_id)
            finally:
                cur.close()
                conn.close()

            if not entry:
                item_result.update(
                    status="skipped",
                    reason="entry_not_found",
                )
                skipped += 1
                processed += 1
                items.append(item_result)

                update_batch_job(
                    batch_id,
                    processed=processed,
                    succeeded=succeeded,
                    failed=failed,
                    skipped=skipped,
                    items=items,
                )
                continue

            required_phase = batch_config["required_phase"]

            if required_phase and not phase_is_persisted(
                entry,
                required_phase,
            ):
                item_result.update(
                    status="skipped",
                    reason=f"missing_{required_phase}_phase",
                )
                skipped += 1
                processed += 1
                items.append(item_result)

                update_batch_job(
                    batch_id,
                    processed=processed,
                    succeeded=succeeded,
                    failed=failed,
                    skipped=skipped,
                    items=items,
                )
                continue

            if options["skip_existing"]:
                if mode == "input-only" and phase_is_persisted(entry, "input"):
                    item_result.update(
                        status="skipped",
                        reason="input_already_persisted",
                    )
                    skipped += 1
                    processed += 1
                    items.append(item_result)

                    update_batch_job(
                        batch_id,
                        processed=processed,
                        succeeded=succeeded,
                        failed=failed,
                        skipped=skipped,
                        items=items,
                    )
                    continue

                if mode == "primary-only" and phase_is_persisted(
                    entry,
                    "primary",
                ):
                    item_result.update(
                        status="skipped",
                        reason="primary_already_persisted",
                    )
                    skipped += 1
                    processed += 1
                    items.append(item_result)

                    update_batch_job(
                        batch_id,
                        processed=processed,
                        succeeded=succeeded,
                        failed=failed,
                        skipped=skipped,
                        items=items,
                    )
                    continue

                if mode == "output-only" and entry.get("enriched_at"):
                    item_result.update(
                        status="skipped",
                        reason="output_already_persisted",
                    )
                    skipped += 1
                    processed += 1
                    items.append(item_result)

                    update_batch_job(
                        batch_id,
                        processed=processed,
                        succeeded=succeeded,
                        failed=failed,
                        skipped=skipped,
                        items=items,
                    )
                    continue

            if time.monotonic() - item_started_at > timeout_seconds:
                raise TimeoutError(
                    "Timeout abans d'executar l'entry"
                )

            result = run_entry_enrichment(
                entry_id=entry_id,
                skip_input=batch_config["skip_input"],
                run_input=batch_config["run_input"],
                run_primary=batch_config["run_primary"],
                run_output=batch_config["run_output"],
                persist=options["persist"],
            )

            elapsed = time.monotonic() - item_started_at

            if elapsed > timeout_seconds:
                raise TimeoutError(
                    f"Timeout processant entry {entry_id}"
                )

            result_status = result.get("status")

            if result_status in {"enriched", "stopped"}:
                item_result.update(
                    status="succeeded",
                    result_status=result_status,
                    elapsed_seconds=round(elapsed, 3),
                )
                succeeded += 1
            elif result_status == "discarded":
                item_result.update(
                    status="succeeded",
                    result_status="discarded",
                    elapsed_seconds=round(elapsed, 3),
                )
                succeeded += 1
            else:
                item_result.update(
                    status="failed",
                    reason=result.get("detail", "unknown_error"),
                    elapsed_seconds=round(elapsed, 3),
                )
                failed += 1

        except Exception as exc:
            item_result.update(
                status="failed",
                reason=str(exc)[:2000],
            )
            failed += 1

            if options["on_error"] == "abort":
                processed += 1
                items.append(item_result)

                update_batch_job(
                    batch_id,
                    processed=processed,
                    succeeded=succeeded,
                    failed=failed,
                    skipped=skipped,
                    items=items,
                    status="FAILED",
                    error_message=str(exc)[:2000],
                    finished_at=utc_now(),
                )
                return

        processed += 1
        items.append(item_result)

        update_batch_job(
            batch_id,
            processed=processed,
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
            items=items,
        )

    final_status = (
        "COMPLETED"
        if failed == 0
        else "COMPLETED_WITH_ERRORS"
    )

    update_batch_job(
        batch_id,
        status=final_status,
        processed=processed,
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        items=items,
        finished_at=utc_now(),
    )

# ─── DEDUP ────────────────────────────────────────────────────────────────────

def build_dedup_key(entry: EntryIngest) -> str:
    base = {
        "source_url": str(entry.source_url),
        "canonical_url": str(entry.canonical_url) if entry.canonical_url else None,
        "source_title": entry.source_title.strip(),
        "published_date": ensure_utc(entry.published_date).isoformat() if entry.published_date else None,
        "external_id": entry.external_id.strip() if entry.external_id else None,
        "source_domain": entry.source_domain.strip().lower(),
    }
    normalized = json.dumps(base, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

# ─── HEALTH (públic, sense API Key) ───────────────────────────────────────────

@app.get("/")
def read_root():
    return {"message": "Asimovwatch API v2 is alive"}

@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.get("/db-check")
def db_check():
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1;")
        result = cur.fetchone()
        cur.close()
        conn.close()
        return {"database": "connected", "result": result[0]}
    except Exception as e:
        return {"database": "error", "detail": str(e)}    

# ─── ENTRIES LIST ─────────────────────────────────────────────────────────────

@protected_router.get("/entries")
def list_entries(
    limit: int = 20,
    offset: int = 0,
    status: Optional[str] = None,
    risk_level: Optional[str] = None,
    source_type: Optional[str] = None,
    country_region: Optional[str] = None,
    reviewer: Optional[str] = None,
    processing_status: Optional[str] = None,
    relevance_score: Optional[str] = None,
    entry_category: Optional[str] = None,
    analyzed_provider: Optional[str] = None,
    q: Optional[str] = None,
):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        safe_limit = min(max(limit, 1), 100)
        safe_offset = max(offset, 0)

        filters = []
        params = []

        if status:
            filters.append("review_status = %s")
            params.append(status.upper())

        if risk_level:
            filters.append("risk_level = %s")
            params.append(risk_level.lower())

        if source_type:
            filters.append("source_type = %s")
            params.append(source_type.lower())

        if country_region:
            filters.append("LOWER(country_region) = LOWER(%s)")
            params.append(country_region)

        if reviewer:
            filters.append("LOWER(reviewer) = LOWER(%s)")
            params.append(reviewer)

        if processing_status:
            filters.append("processing_status = %s")
            params.append(processing_status.upper())

        if relevance_score:
            filters.append("relevance_score = %s")
            params.append(relevance_score.lower())

        if entry_category:
            filters.append("entry_category = %s")
            params.append(entry_category)

        if analyzed_provider:
            filters.append("LOWER(analyzed_provider) = LOWER(%s)")
            params.append(analyzed_provider)   

        if q:
            filters.append("""
                (LOWER(source_title) LIKE LOWER(%s)
                OR LOWER(raw_snippet) LIKE LOWER(%s)
                OR LOWER(summary_factual) LIKE LOWER(%s)
                OR LOWER(translated_summary_ca) LIKE LOWER(%s))
            """)
            like_q = f"%{q}%"
            params.extend([like_q, like_q, like_q, like_q])

        where_clause = ""
        if filters:
            where_clause = "WHERE " + " AND ".join(filters)

        count_query = f"SELECT COUNT(*) AS total FROM public.entries {where_clause}"
        cur.execute(count_query, params)
        total = cur.fetchone()["total"]

        data_query = f"""
            SELECT
                id, source_url, source_domain, source_title, source_type,
                source_language, country_region, risk_level, review_status,
                reviewer, published_date, detected_at, ingested_at, ingest_status,
                summary_factual, theme_tags, affected_principles, processing_status,
                relevance_score, relevance_reason, enriched_at, enriched_model,
                translated_summary_ca
            FROM public.entries
            {where_clause}
            ORDER BY detected_at DESC NULLS LAST, id DESC
            LIMIT %s OFFSET %s
        """
        params_data = params + [safe_limit, safe_offset]
        cur.execute(data_query, params_data)
        rows = cur.fetchall()

        return {
            "total": total,
            "limit": safe_limit,
            "offset": safe_offset,
            "count": len(rows),
            "items": rows
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        cur.close()
        conn.close()

# ─── ENTRIES PENDING ENRICHMENT ───────────────────────────────────────────────

@protected_router.get("/entries/pending/enrichment")
def list_pending_enrichment(limit: int = 50):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        safe_limit = min(max(limit, 1), 200)
        cur.execute("""
            SELECT
                id, source_url, source_domain, source_title,
                source_language, raw_snippet, published_date,
                detected_at, processing_status, processing_retries
            FROM public.entries
            WHERE processing_status IN ('RAW', 'ERROR')
            AND processing_retries < 3
            ORDER BY detected_at ASC NULLS LAST
            LIMIT %s
        """, (safe_limit,))
        rows = cur.fetchall()
        return {"count": len(rows), "items": rows}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

@protected_router.get("/entries/diagnostics")
def get_entry_diagnostics(
    status: Optional[str] = None,
    limit: int = 50,
    source_domain: Optional[str] = None,
):
    """
    Diagnò···stic d'entries penjades o incompletes al pipeline
    (RAW, ERROR, o ENRICHED amb camps clau buits).

    No modifica cap dada; nomé··s informa. Pensat per al procé··s
    de validació··· humana i per decidir si cal reprocessar amb
    POST /entries/{entry_id}/reenrich.
    """
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        safe_limit = min(max(limit, 1), 200)
        filters = []
        params: list = []

        if status:
            filters.append("processing_status = %s")
            params.append(status.upper())
        else:
            filters.append("processing_status IN ('RAW', 'ERROR', 'ENRICHED')")

        if source_domain:
            filters.append("LOWER(source_domain) = LOWER(%s)")
            params.append(source_domain)

        where_sql = " AND ".join(filters)

        base_query = (
            "SELECT "
            "id, source_title, source_domain, ingested_at, updated_at, "
            "processing_status, processing_error, processing_retries, "
            "input_relevance, ready_for_primary, entry_category, "
            "enriched_model, enriched_at, "
            "CASE "
            "WHEN processing_status = 'RAW' "
            "AND input_relevance IS NULL "
            "AND ready_for_primary IS NULL "
            "THEN 'no_input_executed' "
            "WHEN processing_status = 'RAW' "
            "AND input_relevance IS NOT NULL "
            "AND ready_for_primary IN ('yes', 'unclear') "
            "THEN 'stalled_after_input_before_primary' "
            "WHEN processing_status = 'ERROR' "
            "AND processing_error ILIKE %s "
            "THEN 'missing_provider_credentials' "
            "WHEN processing_status = 'ERROR' "
            "AND processing_error ILIKE %s "
            "THEN 'llm_timeout' "
            "WHEN processing_status = 'ERROR' "
            "AND processing_error ILIKE %s "
            "THEN 'invalid_llm_json_output' "
            "WHEN processing_status = 'ERROR' "
            "THEN 'pipeline_error_other' "
            "WHEN processing_status = 'ENRICHED' "
            "AND summary_factual IS NOT NULL "
            "AND why_it_matters IS NOT NULL "
            "THEN 'enrichment_complete' "
            "WHEN processing_status = 'ENRICHED' "
            "AND (summary_factual IS NULL OR why_it_matters IS NULL) "
            "THEN 'incomplete_enrichment' "
            "ELSE 'unknown' "
            "END AS diagnosis "
            "FROM public.entries "
            "WHERE " + where_sql + " "
            "ORDER BY ingested_at DESC NULLS LAST, id DESC "
            "LIMIT %s"
        )

        like_anthropic = "%ANTHROPIC%"
        like_timeout = "%timeout%"
        like_json = "%JSON%"

        full_params = [like_anthropic, like_timeout, like_json] + params + [safe_limit]

        cur.execute(base_query, full_params)
        rows = cur.fetchall()

        summary: dict = {}
        for row in rows:
            key = row["diagnosis"]
            summary[key] = summary.get(key, 0) + 1

        return {
            "count": len(rows),
            "summary": summary,
            "items": rows,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

# ─── ENTRIES AGGREGATE ────────────────────────────────────────────────────────────

@protected_router.get("/entries/aggregate")
def aggregate_entries(
    group_by: str = "source_domain",
    source_domain: Optional[str] = None,
    entry_category: Optional[str] = None,
    processing_status: Optional[str] = None,
    review_status: Optional[str] = None,
    analyzed_provider: Optional[str] = None,
    analyzed_model: Optional[str] = None,
    risk_level: Optional[str] = None,
    relevance_score: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = 50,
):
    """
    Agregació d'entries per un camp específic amb filtres opcionals.
    
    Retorna una llista d'agregacions amb el valor del grup i el comptador.
    """
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Validar group_by
        allowed_group_by = {
            "source_domain",
            "entry_category",
            "processing_status",
            "review_status",
            "analyzed_provider",
            "analyzed_model",
            "risk_level",
            "relevance_score",
            "input_relevance",
            "human_protection_declared",
            "human_protection_verifiable",
            "human_protection_depth",
        }
        
        if group_by not in allowed_group_by:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": f"group_by no vàlid. Permès: {sorted(allowed_group_by)}",
                    "provided": group_by,
                },
            )
        
        # Construir filtres
        filters = []
        params = []
        
        if source_domain:
            filters.append("LOWER(source_domain) = LOWER(%s)")
            params.append(source_domain)
        
        if entry_category:
            filters.append("entry_category = %s")
            params.append(entry_category)
        
        if processing_status:
            filters.append("processing_status = %s")
            params.append(processing_status.upper())
        
        if review_status:
            filters.append("review_status = %s")
            params.append(review_status.upper())
        
        if analyzed_provider:
            filters.append("LOWER(analyzed_provider) = LOWER(%s)")
            params.append(analyzed_provider)
        
        if analyzed_model:
            filters.append("LOWER(analyzed_model) = LOWER(%s)")
            params.append(analyzed_model)
        
        if risk_level:
            filters.append("risk_level = %s")
            params.append(risk_level.lower())
        
        if relevance_score:
            filters.append("relevance_score = %s")
            params.append(relevance_score.lower())
        
        if date_from:
            filters.append("detected_at >= %s")
            params.append(ensure_utc(date_from))
        
        if date_to:
            filters.append("detected_at <= %s")
            params.append(ensure_utc(date_to))
        
        where_clause = ""
        if filters:
            where_clause = "WHERE " + " AND ".join(filters)
        
        # Consulta de total
        count_query = f"SELECT COUNT(*) AS total FROM public.entries {where_clause}"
        cur.execute(count_query, params)
        total = cur.fetchone()["total"]
        
        # Consulta d'agregació.
        # Alguns camps poden ser NULL o strings buits. Els agrupem com
        # "(not set)" per distingir absència tècnica de dades del valor
        # metodològic explícit "unknown".
        nullable_group_fields = {
            "analyzed_provider",
            "analyzed_model",
            "input_relevance",
            "human_protection_declared",
            "human_protection_verifiable",
            "human_protection_depth",
        }

        if group_by in nullable_group_fields:
            group_expr = (
                f"COALESCE(NULLIF(BTRIM({group_by}), ''), '(not set)')"
            )
        else:
            group_expr = group_by

        safe_limit = min(max(limit, 1), 100)

        aggregate_query = f"""
        SELECT
            {group_expr} AS group_value,
            COUNT(*) AS count
        FROM public.entries
        {where_clause}
        GROUP BY {group_expr}
        ORDER BY count DESC, group_value ASC
        LIMIT %s
        """

        params_with_limit = params + [safe_limit]
        cur.execute(aggregate_query, params_with_limit)
        rows = cur.fetchall()
        
        return {
            "group_by": group_by,
            "total": total,
            "limit": safe_limit,
            "count": len(rows),
            "groups": [
                {
                    "group_value": row["group_value"],
                    "count": row["count"],
                }
                for row in rows
            ],
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

# ─── ENTRY DETAIL ─────────────────────────────────────────────────────────────

@protected_router.get("/entries/{entry_id}")
def get_entry(entry_id: int):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cur.execute("""
            SELECT  id, source_url, source_domain, source_title, source_type, source_language,
                    ingest_method, external_id, author_name, canonical_url, published_date,
                    detected_at, country_region, institution_type, raw_snippet,
                    raw_snippet_original, raw_content, raw_content_format, raw_payload,
                    summary_factual, why_it_matters, theme_tags, affected_principles,
                    risk_level, debate_questions, confidence_notes,
                    human_protection_declared, human_protection_verifiable,
                    human_protection_depth, human_protection_notes,
                    input_relevance, input_relevance_reason, ready_for_primary,
                    clean_input_text, input_summary, input_quality, input_quality_notes,
                    entry_category, analyzed_provider, analyzed_model, bihp_directives,
                    review_status, reviewer, reviewed_at, editor_notes, validation_notes,
                    dedup_key, ingest_status, ingested_at, updated_at,
                    processing_status, processing_error, processing_retries,
                    relevance_score, relevance_reason, enriched_at, enriched_model,
                    translated_summary_ca, translated_whyitmatters_ca, translated_debatequestions_ca
            FROM public.entries
            WHERE id = %s
        """, (entry_id,))

        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Entry not found")
        return row

    finally:
        cur.close()
        conn.close()

# ─── CREATE ENTRY ─────────────────────────────────────────────────────────────

@protected_router.post("/entries", status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "Duplicate entry"}})
def create_entry(entry: EntryIngest):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        dedup_key = build_dedup_key(entry)
        published_date = ensure_utc(entry.published_date)
        detected_at = ensure_utc(entry.detected_at) or utc_now()
        reviewed_at = ensure_utc(entry.reviewed_at)
        source_url = str(entry.source_url)
        canonical_url = str(entry.canonical_url) if entry.canonical_url else None

        cur.execute("""
            SELECT id, dedup_key FROM public.entries WHERE dedup_key = %s LIMIT 1
        """, (dedup_key,))
        existing = cur.fetchone()

        if existing:
            return JSONResponse(
                status_code=409,
                content={"status": "duplicate", "id": existing["id"], "dedup_key": existing["dedup_key"]}
            )

        cur.execute("""
            INSERT INTO public.entries (
                source_url, source_domain, source_title, source_type, source_language,
                ingest_method, external_id, author_name, canonical_url, published_date,
                detected_at, country_region, institution_type, raw_snippet, raw_content,
                raw_content_format, raw_payload, summary_factual, why_it_matters,
                theme_tags, affected_principles, risk_level, debate_questions,
                confidence_notes, human_protection_declared, human_protection_verifiable,
                human_protection_depth, human_protection_notes, review_status, reviewer,
                reviewed_at, editor_notes, validation_notes, dedup_key, ingested_at,
                updated_at, ingest_status, processing_status
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, now(),
                now(), 'ingested', 'RAW'
            )
            RETURNING
                id, source_url, source_domain, source_title, source_type,
                ingest_method, review_status, ingest_status, processing_status,
                dedup_key, ingested_at
        """, (
            source_url, entry.source_domain.strip().lower(), entry.source_title.strip(),
            entry.source_type, entry.source_language, entry.ingest_method,
            entry.external_id.strip() if entry.external_id else None,
            entry.author_name, canonical_url, published_date, detected_at,
            entry.country_region, entry.institution_type, entry.raw_snippet,
            entry.raw_content, entry.raw_content_format, Json(entry.raw_payload),
            entry.summary_factual, entry.why_it_matters, entry.theme_tags,
            entry.affected_principles, entry.risk_level, entry.debate_questions,
            entry.confidence_notes, entry.human_protection_declared,
            entry.human_protection_verifiable, entry.human_protection_depth,
            entry.human_protection_notes, entry.review_status, entry.reviewer, reviewed_at, 
            entry.editor_notes, entry.validation_notes, dedup_key
        ))

        created = cur.fetchone()
        conn.commit()
        return {"status": "created", "item": created}

    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise HTTPException(status_code=409, detail="Duplicate entry")

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create entry: {str(e)}")

    finally:
        cur.close()
        conn.close()

# ─── REVIEW ENTRY ─────────────────────────────────────────────────────────────

@protected_router.put("/entries/{entry_id}/review",
    responses={404: {"description": "Entry not found"}})
def review_entry(entry_id: int, review: EntryReview):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cur.execute("SELECT id FROM public.entries WHERE id = %s", (entry_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Entry not found")

        reviewed_at = utc_now() if review.review_status != "NEW" else None

        cur.execute("""
            UPDATE public.entries
            SET
                review_status = %s,
                reviewer = %s,
                editor_notes = %s,
                validation_notes = %s,
                reviewed_at = COALESCE(%s, reviewed_at),
                updated_at = now()
            WHERE id = %s
            RETURNING
                id, source_url, source_title, source_domain,
                review_status, reviewer, editor_notes,
                validation_notes, reviewed_at, updated_at
        """, (
            review.review_status, review.reviewer, review.editor_notes,
            review.validation_notes, reviewed_at, entry_id
        ))

        updated = cur.fetchone()
        conn.commit()
        return {"status": "updated", "item": updated}

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update entry: {str(e)}")
    finally:
        cur.close()
        conn.close()

# ─── ENRICH ENTRY ─────────────────────────────────────────────────────────────

@protected_router.put("/entries/{entry_id}/enrich",
    responses={404: {"description": "Entry not found"}})
def enrich_entry(entry_id: int, enrich: EntryEnrich):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cur.execute("SELECT id FROM public.entries WHERE id = %s", (entry_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Entry not found")

        fields = []
        values = []

        field_map = {
            "processing_status": enrich.processing_status,
            "processing_error": enrich.processing_error,
            "processing_retries": enrich.processing_retries,
            "relevance_score": enrich.relevance_score,
            "relevance_reason": enrich.relevance_reason,
            "translated_summary_ca": enrich.translated_summary_ca,
            "translated_whyitmatters_ca": enrich.translated_whyitmatters_ca,
            "translated_debatequestions_ca": enrich.translated_debatequestions_ca,
            "enriched_model": enrich.enriched_model,
            "raw_snippet_original": enrich.raw_snippet_original,
            "source_language": enrich.source_language,
            "summary_factual": enrich.summary_factual,
            "why_it_matters": enrich.why_it_matters,
            "debate_questions": enrich.debate_questions,
            "theme_tags": enrich.theme_tags,
            "affected_principles": enrich.affected_principles,
            "risk_level": enrich.risk_level,
            "human_protection_declared": enrich.human_protection_declared,
            "human_protection_verifiable": enrich.human_protection_verifiable,
            "human_protection_depth": enrich.human_protection_depth,
            "human_protection_notes": enrich.human_protection_notes,
            "confidence_notes": enrich.confidence_notes,
            "input_relevance": enrich.input_relevance,
            "input_relevance_reason": enrich.input_relevance_reason,
            "ready_for_primary": enrich.ready_for_primary,
            "clean_input_text": enrich.clean_input_text,
            "input_summary": enrich.input_summary,
            "input_quality": enrich.input_quality,
            "input_quality_notes": enrich.input_quality_notes,
            "entry_category": enrich.entry_category,
            "analyzed_provider": enrich.analyzed_provider,
            "analyzed_model": enrich.analyzed_model,
        }

        for col, val in field_map.items():
            if val is not None:
                fields.append(f"{col} = %s")
                values.append(val)

        if enrich.bihp_directives is not None:
            fields.append("bihp_directives = %s")
            values.append(Json(enrich.bihp_directives))        

        if not fields:
            raise HTTPException(status_code=400, detail="No fields to update")

        fields.append("enriched_at = now()")
        fields.append("updated_at = now()")
        values.append(entry_id)

        cur.execute(f"""
            UPDATE public.entries
            SET {", ".join(fields)}
            WHERE id = %s
            RETURNING
                id, processing_status, relevance_score, enriched_at,
                enriched_model, translated_summary_ca, updated_at
        """, values)

        updated = cur.fetchone()
        conn.commit()
        return {"status": "enriched", "item": updated}

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to enrich entry: {str(e)}")
    finally:
        cur.close()
        conn.close()

# ─── BATCH ENRICH ENTRIES ─────────────────────────────────────────────────────

@protected_router.post("/entries/batch-enrich")
def batch_enrich_entries(enrich: EntryBatchEnrich):
    entry_ids = enrich.entry_ids or []

    if not entry_ids:
        raise HTTPException(
            status_code=400,
            detail="entry_ids no pot estar buit"
        )

    if len(entry_ids) > 100:
        raise HTTPException(
            status_code=400,
            detail="Màxim de 100 entry_ids per operació batch"
        )

    if len(set(entry_ids)) != len(entry_ids):
        raise HTTPException(
            status_code=400,
            detail="entry_ids conté IDs duplicats"
        )

    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        scalar_fields = {
            "processing_status": enrich.processing_status,
            "processing_error": enrich.processing_error,
            "processing_retries": enrich.processing_retries,
            "relevance_score": enrich.relevance_score,
            "relevance_reason": enrich.relevance_reason,
            "translated_summary_ca": enrich.translated_summary_ca,
            "translated_whyitmatters_ca": enrich.translated_whyitmatters_ca,
            "enriched_model": enrich.enriched_model,
            "raw_snippet_original": enrich.raw_snippet_original,
            "source_language": enrich.source_language,
            "summary_factual": enrich.summary_factual,
            "why_it_matters": enrich.why_it_matters,
            "risk_level": enrich.risk_level,
            "human_protection_declared": enrich.human_protection_declared,
            "human_protection_verifiable": enrich.human_protection_verifiable,
            "human_protection_depth": enrich.human_protection_depth,
            "human_protection_notes": enrich.human_protection_notes,
            "confidence_notes": enrich.confidence_notes,
            "input_relevance": enrich.input_relevance,
            "input_relevance_reason": enrich.input_relevance_reason,
            "ready_for_primary": enrich.ready_for_primary,
            "clean_input_text": enrich.clean_input_text,
            "input_summary": enrich.input_summary,
            "input_quality": enrich.input_quality,
            "input_quality_notes": enrich.input_quality_notes,
            "entry_category": enrich.entry_category,
            "analyzed_provider": enrich.analyzed_provider,
            "analyzed_model": enrich.analyzed_model,
        }

        json_fields = {
            "translated_debatequestions_ca": enrich.translated_debatequestions_ca,
            "debate_questions": enrich.debate_questions,
            "theme_tags": enrich.theme_tags,
            "affected_principles": enrich.affected_principles,
            "bihp_directives": enrich.bihp_directives,
        }

        fields = []
        values = []

        for column, value in scalar_fields.items():
            if value is not None:
                fields.append(f"{column} = %s")
                values.append(value)

        for column, value in json_fields.items():
            if value is not None:
                fields.append(f"{column} = %s")
                values.append(Json(value))

        if not fields:
            raise HTTPException(
                status_code=400,
                detail="No s'ha proporcionat cap camp per actualitzar"
            )

        cur.execute(
            "SELECT id FROM public.entries WHERE id = ANY(%s)",
            (entry_ids,)
        )
        existing_ids = {row["id"] for row in cur.fetchall()}
        missing_ids = [
            entry_id for entry_id in entry_ids
            if entry_id not in existing_ids
        ]

        fields.append("enriched_at = now()")
        fields.append("updated_at = now()")
        values.append(entry_ids)

        cur.execute(
            f"""
            UPDATE public.entries
            SET {", ".join(fields)}
            WHERE id = ANY(%s)
            RETURNING
                id,
                processing_status,
                input_relevance,
                ready_for_primary,
                updated_at
            """,
            values,
        )

        updated = cur.fetchall()
        conn.commit()

        return {
            "status": "updated",
            "requested_count": len(entry_ids),
            "updated_count": len(updated),
            "updated_ids": sorted(row["id"] for row in updated),
            "missing_ids": missing_ids,
            "items": updated,
        }

    except HTTPException:
        conn.rollback()
        raise

    except Exception as e:
        conn.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to batch enrich entries: {str(e)}"
        )

    finally:
        cur.close()
        conn.close()

# ─── REENRICH ENTRY TEST ──────────────────────────────────────────────────────────────

@protected_router.post("/entries/{entry_id}/reenrich")
def reenrich_entry(entry_id: int, body: EntryReenrichRequest):
    """
    Re-executa el pipeline d'enriquiment (Input -> Primary -> Output) sobre
    una entry existent, sense passar pel crawler RSS ni per la cerca temàtica.

    Permet executar fases individualment o en combinació:
    - run_input=False: no executa Input.
    - run_primary=False: s'atura després d'Input.
    - run_output=False: s'atura després de Primary.
    - skip_input=True: equival al comportament de cerca temàtica; salta Input.

    persist=False és segur per a proves de prompts, providers, models, parsers
    i classificacions: no actualitza public.entries, no canvia processing_status,
    no escriu processing_error i no incrementa processing_retries.
    """
    from app.crawler import run_entry_enrichment

    result = run_entry_enrichment(
        entry_id=entry_id,
        skip_input=body.skip_input,
        run_input=body.run_input,
        run_primary=body.run_primary,
        run_output=body.run_output,
        persist=body.persist,
    )

    if result.get("status") == "error" and result.get("detail") == "Entry not found":
        raise HTTPException(status_code=404, detail="Entry not found")

    return result

# ─── DELETE ENTRY ─────────────────────────────────────────────────────────────

@protected_router.delete("/entries/{entry_id}",
    responses={404: {"description": "Entry not found"}})
def delete_entry(entry_id: int):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cur.execute("SELECT id, source_title FROM public.entries WHERE id = %s", (entry_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Entry not found")

        cur.execute("DELETE FROM public.entries WHERE id = %s RETURNING id, source_title", (entry_id,))
        deleted = cur.fetchone()
        conn.commit()
        return {"status": "deleted", "item": deleted}

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to delete entry: {str(e)}")
    finally:
        cur.close()
        conn.close()

# ─── CONFIG ───────────────────────────────────────────────────────────────────

@protected_router.get("/config")
def get_config():
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT key, value, updated_at FROM public.config ORDER BY key")
        rows = cur.fetchall()
        return {"items": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

@protected_router.put("/config/{key}")
def update_config(key: str, body: ConfigUpdate):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            INSERT INTO public.config (key, value, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated_at = now()
            RETURNING key, value, updated_at
        """, (key, body.value))
        row = cur.fetchone()
        conn.commit()
        return {"status": "ok", "item": row}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

class PromptUpdate(BaseModel):
    value: str
    category: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    use_fallback: Optional[bool] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    timeout_secs: Optional[int] = None


@protected_router.get("/prompts")
def get_prompts():
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT
                p.key,
                p.category,
                p.value,
                p.updated_at,
                c.provider,
                c.model,
                c.use_fallback,
                c.max_tokens,
                c.temperature,
                c.timeout_secs
            FROM public.prompts p
            LEFT JOIN public.llm_runtime_config c
              ON c.scope_type = 'prompt'
             AND c.scope_key = 'prompt'
             AND c.prompt_key = p.key
            ORDER BY p.key
        """)
        rows = cur.fetchall()
        return {"items": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


@protected_router.get("/prompts/{key}")
def get_prompt_versions(key: str):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT id, prompt_key, version, value, change_note, changed_by, changed_at, is_active
            FROM public.prompt_versions
            WHERE prompt_key = %s
            ORDER BY version DESC, changed_at DESC
        """, (key,))
        versions = cur.fetchall()

        cur.execute("""
            SELECT
                p.key,
                p.category,
                p.value,
                p.updated_at,
                c.provider,
                c.model,
                c.use_fallback,
                c.max_tokens,
                c.temperature,
                c.timeout_secs
            FROM public.prompts p
            LEFT JOIN public.llm_runtime_config c
              ON c.scope_type = 'prompt'
             AND c.scope_key = 'prompt'
             AND c.prompt_key = p.key
            WHERE p.key = %s
            LIMIT 1
        """, (key,))
        prompt = cur.fetchone()

        if not prompt:
            raise HTTPException(status_code=404, detail="Prompt not found")

        return {
            "item": prompt,
            "versions": versions
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


@protected_router.put("/prompts/{key}")
def update_prompt(key: str, body: PromptUpdate):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        clean_key = (key or "").strip()
        if not clean_key:
            raise HTTPException(status_code=400, detail="Prompt key buit")

        clean_value = (body.value or "").strip()
        if not clean_value:
            raise HTTPException(status_code=400, detail="Prompt value buit")

        clean_category = (body.category or "").strip() or None
        clean_provider = (body.provider or "").strip().lower()
        clean_model = (body.model or "").strip()

        if not clean_provider:
            raise HTTPException(status_code=400, detail="Provider és obligatori")

        if not clean_model:
            raise HTTPException(status_code=400, detail="Model és obligatori")

        if clean_provider not in get_supported_providers():
            raise HTTPException(status_code=400, detail="Provider no suportat")

        if body.max_tokens is not None and body.max_tokens <= 0:
            raise HTTPException(status_code=400, detail="max_tokens ha de ser > 0")

        if body.temperature is not None and (body.temperature < 0 or body.temperature > 2):
            raise HTTPException(status_code=400, detail="temperature ha d'estar entre 0 i 2")

        if body.timeout_secs is not None and body.timeout_secs <= 0:
            raise HTTPException(status_code=400, detail="timeout_secs ha de ser > 0")

        cur.execute("""
            SELECT key, category, value, updated_at
            FROM public.prompts
            WHERE key = %s
            LIMIT 1
        """, (clean_key,))
        existing_prompt = cur.fetchone()

        old_value = existing_prompt["value"] if existing_prompt and existing_prompt["value"] is not None else ""
        value_changed = (not existing_prompt) or (old_value != clean_value)

        old_category = existing_prompt["category"] if existing_prompt else None
        category_changed = (not existing_prompt) or (old_category != clean_category)

        if existing_prompt:
            if value_changed:
                cur.execute("""
                    UPDATE public.prompts
                    SET
                        category = %s,
                        value = %s,
                        updated_at = now()
                    WHERE key = %s
                    RETURNING key, category, value, updated_at
                """, (clean_category, clean_value, clean_key))
                prompt_row = cur.fetchone()
            elif category_changed:
                cur.execute("""
                    UPDATE public.prompts
                    SET
                        category = %s,
                        updated_at = now()
                    WHERE key = %s
                    RETURNING key, category, value, updated_at
                """, (clean_category, clean_key))
                prompt_row = cur.fetchone()
            else:
                prompt_row = existing_prompt
        else:
            cur.execute("""
                INSERT INTO public.prompts (key, category, value, updated_at)
                VALUES (%s, %s, %s, now())
                RETURNING key, category, value, updated_at
            """, (clean_key, clean_category, clean_value))
            prompt_row = cur.fetchone()

        cur.execute("""
            INSERT INTO public.llm_runtime_config (
                scope_type,
                scope_key,
                prompt_key,
                provider,
                model,
                enabled,
                use_fallback,
                max_tokens,
                temperature,
                timeout_secs,
                notes,
                created_at,
                updated_at
            )
            VALUES (
                'prompt',
                'prompt',
                %s,
                %s,
                %s,
                true,
                COALESCE(%s, true),
                %s,
                %s,
                %s,
                NULL,
                now(),
                now()
            )
            ON CONFLICT (scope_type, scope_key, prompt_key) DO UPDATE
            SET
                provider = EXCLUDED.provider,
                model = EXCLUDED.model,
                use_fallback = EXCLUDED.use_fallback,
                max_tokens = EXCLUDED.max_tokens,
                temperature = EXCLUDED.temperature,
                timeout_secs = EXCLUDED.timeout_secs,
                updated_at = now()
            RETURNING
                provider,
                model,
                use_fallback,
                max_tokens,
                temperature,
                timeout_secs
        """, (
            clean_key,
            clean_provider,
            clean_model,
            body.use_fallback,
            body.max_tokens,
            body.temperature,
            body.timeout_secs
        ))
        llm_row = cur.fetchone()

        conn.commit()

        return {
            "status": "ok",
            "item": {
                "key": prompt_row["key"],
                "category": prompt_row["category"],
                "value": prompt_row["value"],
                "updated_at": prompt_row["updated_at"],
                "provider": llm_row["provider"],
                "model": llm_row["model"],
                "use_fallback": llm_row["use_fallback"],
                "max_tokens": llm_row["max_tokens"],
                "temperature": float(llm_row["temperature"]) if llm_row["temperature"] is not None else None,
                "timeout_secs": llm_row["timeout_secs"],
            }
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

# ─── CRAWLER LOG ──────────────────────────────────────────────────────────────

@protected_router.get("/crawler-log")
def get_crawler_log(limit: int = 20):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        safe_limit = min(max(limit, 1), 100)
        cur.execute("""
            SELECT * FROM public.crawler_log
            ORDER BY executed_at DESC
            LIMIT %s
        """, (safe_limit,))
        rows = cur.fetchall()
        return {"count": len(rows), "items": rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

@protected_router.post("/crawler-log", status_code=status.HTTP_201_CREATED)
def create_crawler_log(log: CrawlerLogCreate):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            INSERT INTO public.crawler_log (
                sources_checked, items_found, items_relevant,
                items_enriched, items_failed, duration_seconds, notes
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (
            log.sources_checked, log.items_found, log.items_relevant,
            log.items_enriched, log.items_failed, log.duration_seconds, log.notes
        ))
        row = cur.fetchone()
        conn.commit()
        return {"status": "created", "item": row}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

# ─── MODELS CRAWLER OPS ───────────────────────────────────────────────────────

class SourceCrawlerRunRequest(BaseModel):
    brief: Optional[str] = None
    dry_run: bool = True
    proposed_by: Optional[str] = "admin-run-now"
    prompt_key: str = "Source candidates discovery"


class EntryCrawlerRunRequest(BaseModel):
    source_ids: Optional[list[int]] = None
    hours_back: int = 24
    max_items_per_source: int = 20
    run_enrichment: bool = True
    retry_errors: bool = False
    force: bool = False
    dry_run: bool = True
    requested_by: Optional[str] = "admin-entry-crawler"


class ThematicEntrySearchRequest(BaseModel):
    brief: str
    date_range: Optional[str] = "Últim any"
    source_scope: Optional[str] = "web_and_monitored_sources"
    source_types: Optional[list[str]] = None
    max_results: int = 10
    run_enrichment: bool = True
    dry_run: bool = True
    requested_by: Optional[str] = "admin-thematic-search"
    prompt_key: str = "Thematic entry discovery"

class EntryCrawlerConfigUpdate(BaseModel):
    enabled: bool
    frequency_minutes: int
    run_enrichment: bool
    run_time: str = "08:00"
    run_day: str = "friday"
    max_items_per_source: int

# ─── HELPERS CRAWLER OPS ──────────────────────────────────────────────────────

def _parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")

def _parse_int(value: Optional[str], default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default

def _get_config_value(cur, key: str, default: Optional[str] = None) -> Optional[str]:
    cur.execute("SELECT value FROM public.config WHERE key = %s", (key,))
    row = cur.fetchone()
    return row["value"] if row else default

def _set_config_value(cur, key: str, value: Optional[str]):
    safe_value = "" if value is None else str(value)
    cur.execute("""
        INSERT INTO public.config (key, value, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (key) DO UPDATE
        SET value = EXCLUDED.value, updated_at = now()
    """, (key, safe_value))

def _validate_entry_crawler_config(
    frequency_minutes: int,
    hours_back: int,
    max_items_per_source: int,
):
    if frequency_minutes < 15 or frequency_minutes > 10080:
        raise HTTPException(
            status_code=400,
            detail=(
                "frequency_minutes ha d'estar entre 15 i 10080 "
                "(set dies)"
            ),
        )

    if hours_back < 1 or hours_back > 720:
        raise HTTPException(
            status_code=400,
            detail=(
                "hours_back ha d'estar entre 1 i 720 "
                "(trenta dies)"
            ),
        )

    if max_items_per_source < 1 or max_items_per_source > 100:
        raise HTTPException(
            status_code=400,
            detail=(
                "max_items_per_source ha d'estar entre 1 i 100"
            ),
        )

# ─── CRAWLER STATUS ────────────────────────────────────────────────────────────

@protected_router.get("/crawler/sources/status")
def get_source_crawler_status():
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        config_map = get_config_map()

        cur.execute("""
            SELECT *
            FROM public.crawler_log
            ORDER BY executed_at DESC
            LIMIT 1
        """)
        last_log = cur.fetchone()

        enabled = _parse_bool(config_map.get("crawler_enabled"), default=False)
        frequency_minutes = _parse_int(
            config_map.get("crawler_frequency_minutes"),
            default=1440
        )

        return {
            "status": "ok",
            "crawler": {
                "enabled": enabled,
                "frequency_minutes": frequency_minutes,
                "prompt_key": config_map.get(
                    "crawler_prompt_key",
                    "Source candidates discovery"
                ),
                "default_brief": config_map.get("source_discovery_default_brief", ""),
                "last_status": config_map.get("crawler_last_status"),
                "last_run_at": config_map.get("crawler_last_run_at"),
                "last_duration_seconds": config_map.get("crawler_last_duration_seconds"),
                "last_error": config_map.get("crawler_last_error"),
            },
            "last_log": last_log
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

# ─── CRAWLER RUN NOW ───────────────────────────────────────────────────────────

@protected_router.post("/crawler/sources/run", status_code=status.HTTP_201_CREATED)
def run_source_crawler_now(body: SourceCrawlerRunRequest):
    from app.source_candidates import discover_source_candidates, SourceCandidateDiscoverRequest

    started_at = utc_now()
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        effective_brief = (body.brief or "").strip()
        if not effective_brief:
            effective_brief = _get_config_value(cur, "source_discovery_default_brief", "") or ""

        if not effective_brief:
            raise HTTPException(
                status_code=400,
                detail="Falta brief i no hi ha source_discovery_default_brief configurat"
            )

        prompt_key = (body.prompt_key or "").strip() or "Source candidates discovery"

        _set_config_value(cur, "crawler_last_status", "RUNNING")
        _set_config_value(cur, "crawler_last_run_at", started_at.isoformat())
        _set_config_value(cur, "crawler_last_error", None)
        conn.commit()

        discovery_payload = SourceCandidateDiscoverRequest(
            prompt_key=prompt_key,
            input_text=effective_brief,
            proposed_by=body.proposed_by or "admin-run-now",
            dry_run=body.dry_run
        )

        result = discover_source_candidates(discovery_payload)

        finished_at = utc_now()
        duration_seconds = round((finished_at - started_at).total_seconds(), 3)

        cur.execute("""
            INSERT INTO public.crawler_log (
                sources_checked,
                items_found,
                items_relevant,
                items_enriched,
                items_failed,
                duration_seconds,
                notes
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (
            1,
            result.get("detected", 0),
            result.get("inserted", 0),
            0,
            0,
            duration_seconds,
            f"run_now prompt={prompt_key} dry_run={body.dry_run} proposed_by={body.proposed_by or 'admin-run-now'}"
        ))
        log_row = cur.fetchone()

        _set_config_value(cur, "crawler_last_status", "OK")
        _set_config_value(cur, "crawler_last_run_at", started_at.isoformat())
        _set_config_value(cur, "crawler_last_duration_seconds", str(duration_seconds))
        _set_config_value(cur, "crawler_last_error", None)

        conn.commit()

        return {
            "status": "created",
            "message": "Crawler run completat",
            "run": {
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "duration_seconds": duration_seconds,
                "prompt_key": prompt_key,
                "brief_used": effective_brief,
                "dry_run": body.dry_run,
                "proposed_by": body.proposed_by or "admin-run-now"
            },
            "discovery": result,
            "log": log_row
        }

    except HTTPException as e:
        try:
            _set_config_value(cur, "crawler_last_status", "ERROR")
            _set_config_value(cur, "crawler_last_run_at", started_at.isoformat())
            _set_config_value(cur, "crawler_last_error", str(e.detail))
            conn.commit()
        except Exception:
            conn.rollback()
        raise

    except Exception as e:
        conn.rollback()
        try:
            conn = get_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            _set_config_value(cur, "crawler_last_status", "ERROR")
            _set_config_value(cur, "crawler_last_run_at", started_at.isoformat())
            _set_config_value(cur, "crawler_last_error", str(e))
            conn.commit()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass

# ─── LEGACY SOURCE CRAWLER ROUTES ─────────────────────────────────────────────

@protected_router.get(
    "/crawler/status",
    deprecated=True,
    include_in_schema=False
)
def get_legacy_crawler_status():
    return get_source_crawler_status()


@protected_router.post(
    "/crawler/run",
    status_code=status.HTTP_201_CREATED,
    deprecated=True,
    include_in_schema=False
)
def run_legacy_crawler_now(body: SourceCrawlerRunRequest):
    return run_source_crawler_now(body)

# ─── ENTRY CRAWLER OPS ─────────────────────────────────────────────────────────

@protected_router.get("/crawler/entries/status")
def get_entry_crawler_status():
    config_map = get_config_map()

    return {
        "status": "ok",
        "crawler": {
            "enabled": _parse_bool(config_map.get("entry_crawler_enabled"), default=False),
            "frequency_minutes": _parse_int(config_map.get("entry_crawler_frequency_minutes"), default=1440),
            "run_enrichment": _parse_bool(config_map.get("entry_crawler_run_enrichment"), default=True),
            "run_time": config_map.get("entry_crawler_run_time", "08:00"),
            "run_day": config_map.get("entry_crawler_run_day", "friday"),
            "max_items_per_source": _parse_int(config_map.get("entry_crawler_max_items_per_source"), default=20),
            "last_status": config_map.get("entry_crawler_last_status"),
            "last_run_at": config_map.get("entry_crawler_last_run_at"),
            "last_duration_seconds": config_map.get("entry_crawler_last_duration_seconds"),
            "last_error": config_map.get("entry_crawler_last_error"),
        },
    }

@protected_router.put("/crawler/entries/config")
def update_entry_crawler_config(body: EntryCrawlerConfigUpdate):
    _validate_entry_crawler_config(
        frequency_minutes=body.frequency_minutes,
        hours_back=24,
        max_items_per_source=body.max_items_per_source,
    )

    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        _set_config_value(
            cur,
            "entry_crawler_enabled",
            str(body.enabled).lower(),
        )
        _set_config_value(
            cur,
            "entry_crawler_frequency_minutes",
            str(body.frequency_minutes),
        )
        _set_config_value(
            cur,
            "entry_crawler_run_enrichment",
            str(body.run_enrichment).lower(),
        )
        _set_config_value(cur, "entry_crawler_run_time", body.run_time)
        _set_config_value(cur, "entry_crawler_run_day", body.run_day)
        _set_config_value(
            cur,
            "entry_crawler_max_items_per_source",
            str(body.max_items_per_source),
        )

        conn.commit()

        return {
            "status": "ok",
            "crawler": {
                "enabled": body.enabled,
                "frequency_minutes": body.frequency_minutes,
                "run_enrichment": body.run_enrichment,
                "run_time": body.run_time,
                "run_day": body.run_day,
                "max_items_per_source": body.max_items_per_source,
            },
        }

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"No s'ha pogut desar la configuració del crawler d'entries: {str(e)}",
        )
    finally:
        cur.close()
        conn.close()

@protected_router.post("/crawler/entries/run", status_code=status.HTTP_201_CREATED)
def run_entry_crawler_now(body: EntryCrawlerRunRequest):
    """
    Executa el crawler RSS/Atom d'entries.

    No descobreix fonts ni les promociona. Llegeix només fonts actives
    amb ingest_method='rss' i feed_url. Les entries creades queden en
    estat NEW i RAW perquè requereixen revisió humana posterior.
    """
    safe_limit = min(max(body.max_items_per_source, 1), 50)
    source_ids = body.source_ids or [None]

    started_at = utc_now()
    aggregate = {
        "sources_checked": 0,
        "items_found": 0,
        "items_created": 0,
        "items_duplicates": 0,
        "items_skipped": 0,
        "items_failed": 0,
    }
    runs = []

    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        _set_config_value(cur, "entry_crawler_last_status", "RUNNING")
        _set_config_value(cur, "entry_crawler_last_run_at", started_at.isoformat())
        _set_config_value(cur, "entry_crawler_last_error", None)
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        cur.close()
        conn.close()

    try:
        for source_id in source_ids:
            result = run_entries_crawler(
                dry_run=body.dry_run,
                source_id=source_id,
                limit_per_source=safe_limit,
            )
            for key in aggregate:
                aggregate[key] += int(result.get(key, 0) or 0)
            runs.append({"source_id": source_id, "result": result})

        finished_at = utc_now()
        duration_seconds = round((finished_at - started_at).total_seconds(), 3)

        conn = get_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            _set_config_value(cur, "entry_crawler_last_status", "OK")
            _set_config_value(cur, "entry_crawler_last_run_at", started_at.isoformat())
            _set_config_value(cur, "entry_crawler_last_duration_seconds", str(duration_seconds))
            _set_config_value(cur, "entry_crawler_last_error", None)
            conn.commit()
        except Exception:
            conn.rollback()
        finally:
            cur.close()
            conn.close()

        return {
            "status": "ok",
            "crawler_type": "entries_rss",
            "run": {
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "duration_seconds": duration_seconds,
                "source_ids": body.source_ids,
                "max_items_per_source": safe_limit,
                "dry_run": body.dry_run,
                "requested_by": body.requested_by,
                "hours_back_ignored": body.hours_back,
                "run_enrichment_ignored": body.run_enrichment,
                "retry_errors_ignored": body.retry_errors,
                "force_ignored": body.force,
            },
            "result": aggregate,
            "runs": runs,
        }

    except Exception as exc:
        try:
            conn = get_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            _set_config_value(cur, "entry_crawler_last_status", "ERROR")
            _set_config_value(cur, "entry_crawler_last_run_at", started_at.isoformat())
            _set_config_value(cur, "entry_crawler_last_error", str(exc))
            conn.commit()
        except Exception:
            pass
        finally:
            try:
                cur.close()
                conn.close()
            except Exception:
                pass

        raise HTTPException(
            status_code=500,
            detail=f"Error executant crawler d'entries: {str(exc)}",
        )

@protected_router.post(
    "/crawler/entries/search",
    status_code=status.HTTP_201_CREATED,
)
def run_thematic_search_now(body: ThematicEntrySearchRequest):
    """
    Executa una cerca temàtica web utilitzant el prompt "Thematic entry discovery".
    
    El brief defineix la cerca. Les entries creades queden en estat NEW i RAW.
    """
    from app.crawler import run_thematic_search
    
    safe_limit = min(max(body.max_results, 1), 50)
    
    started_at = utc_now()
    
    try:
        result = run_thematic_search(
            brief=body.brief,
            date_range=body.date_range,
            source_scope=body.source_scope,
            source_types=body.source_types,
            max_results=safe_limit,
            run_enrichment=body.run_enrichment,
            dry_run=body.dry_run,
            requested_by=body.requested_by,
            prompt_key=body.prompt_key,
        )
        
        finished_at = utc_now()
        duration_seconds = round(
            (finished_at - started_at).total_seconds(),
            3,
        )
        
        return {
            "status": "ok",
            "crawler_type": "thematic_search",
            "run": {
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "duration_seconds": duration_seconds,
                "brief": body.brief,
                "date_range": body.date_range,
                "source_scope": body.source_scope,
                "max_results": safe_limit,
                "run_enrichment": body.run_enrichment,
                "dry_run": body.dry_run,
                "requested_by": body.requested_by,
                "prompt_key": body.prompt_key,
            },
            "result": result,  # ← Retorna el dict sencer de run_thematic_search()
        }
    
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Error executant cerca temàtica: {str(exc)}",
        )

# ─── BATCH ENDPOINTS ──────────────────────────────────────────────────────────

@protected_router.post("/api/batch/process", status_code=202)
def start_batch_process(body: BatchProcessRequest):
    validate_batch_request(body)

    batch_id = generate_batch_id()
    entry_ids = list(body.entry_ids)
    options = body.options.dict()

    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO public.batch_jobs (
                batch_id,
                mode,
                entry_ids,
                status,
                total,
                options
            )
            VALUES (%s, %s, %s, 'QUEUED', %s, %s)
            """,
            (
                batch_id,
                body.mode,
                entry_ids,
                len(entry_ids),
                Json(options),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise HTTPException(
            status_code=500,
            detail="No s'ha pogut crear el batch",
        )
    finally:
        cur.close()
        conn.close()

    worker = threading.Thread(
        target=process_batch_job,
        args=(batch_id, entry_ids, body.mode, options),
        daemon=True,
        name=f"asimovwatch-{batch_id}",
    )
    worker.start()

    return {
        "batch_id": batch_id,
        "status": "QUEUED",
        "mode": body.mode,
        "total": len(entry_ids),
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
    }


@protected_router.get("/api/batch/status/{batch_id}")
def get_batch_status(batch_id: str):
    job = get_batch_job(batch_id)

    if not job:
        raise HTTPException(
            status_code=404,
            detail="Batch no trobat",
        )

    return {
        "batch_id": job["batch_id"],
        "mode": job["mode"],
        "status": job["status"],
        "total": job["total"],
        "processed": job["processed"],
        "succeeded": job["succeeded"],
        "failed": job["failed"],
        "skipped": job["skipped"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "updated_at": job["updated_at"],
        "error_message": job["error_message"],
        "items": job["items"],
    }

# ─── STATS ────────────────────────────────────────────────────────────────────

@protected_router.get("/stats")
def get_stats():
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cur.execute("""
        SELECT
            COUNT(*) AS total_entries,

            COUNT(*) FILTER (
                WHERE processing_status IN ('RAW', 'ERROR')
            ) AS pending_enrichment,

            COUNT(*) FILTER (
                WHERE processing_status = 'RAW'
            ) AS raw_pending_enrichment,

            COUNT(*) FILTER (
                WHERE processing_status = 'ERROR'
            ) AS enrichment_errors,

            COUNT(*) FILTER (
                WHERE processing_status = 'ERROR'
                  AND COALESCE(processing_retries, 0) < 3
            ) AS retryable_enrichment_errors,

            COUNT(*) FILTER (
                WHERE processing_status = 'ERROR'
                  AND COALESCE(processing_retries, 0) >= 3
            ) AS blocked_enrichment_errors,

            COUNT(*) FILTER (
                WHERE processing_status = 'ENRICHED'
            ) AS enriched,

            COUNT(*) FILTER (
                WHERE processing_status = 'DISCARDED'
            ) AS discarded_by_input,

            COUNT(*) FILTER (
                WHERE processing_status = 'ENRICHED'
                  AND review_status = 'NEW'
            ) AS pending_review,

            COUNT(*) FILTER (
                WHERE processing_status = 'ENRICHED'
                  AND review_status = 'APPROVED'
            ) AS approved,

            COUNT(*) FILTER (
                WHERE processing_status = 'ENRICHED'
                  AND review_status = 'REJECTED'
            ) AS rejected,

            COUNT(*) FILTER (
                WHERE LOWER(BTRIM(COALESCE(input_relevance, ''))) = 'high'
            ) AS high_input_relevance,

            COUNT(*) FILTER (
                WHERE LOWER(BTRIM(COALESCE(input_relevance, ''))) = 'medium'
            ) AS medium_input_relevance,

            COUNT(*) FILTER (
                WHERE LOWER(BTRIM(COALESCE(input_relevance, ''))) = 'low'
            ) AS low_input_relevance,

            COUNT(*) FILTER (
                WHERE LOWER(BTRIM(COALESCE(input_relevance, ''))) NOT IN (
                    'high',
                    'medium',
                    'low'
                )
            ) AS unknown_input_relevance,

            COUNT(*) FILTER (
                WHERE processing_status = 'ENRICHED'
                AND risk_level = 'high'
            ) AS high_risk,

            COUNT(*) FILTER (
                WHERE processing_status = 'ENRICHED'
                AND risk_level = 'medium'
            ) AS medium_risk,

            COUNT(*) FILTER (
                WHERE processing_status = 'ENRICHED'
                AND risk_level = 'low'
            ) AS low_risk,

            COUNT(*) FILTER (
                WHERE processing_status = 'ENRICHED'
                AND (
                    risk_level = 'unknown'
                    OR risk_level IS NULL
                )
            ) AS unknown_risk

        FROM public.entries
        """)

        return cur.fetchone()

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        cur.close()
        conn.close()



# ─── REGISTRE DE ROUTERS ──────────────────────────────────────────────────────

app.include_router(protected_router)
