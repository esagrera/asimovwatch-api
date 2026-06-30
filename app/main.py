from dotenv import load_dotenv
load_dotenv()

import hashlib
import json
import os

import psycopg2
import psycopg2.errors
from psycopg2.extras import RealDictCursor, Json
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, AnyUrl
from typing import Optional
from datetime import datetime, timezone


app = FastAPI(title="Asimovwatch API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://asimovwatch.com",
        "https://api.asimovwatch.com",
        "http://localhost:3000",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── DB ───────────────────────────────────────────────────────────────────────

def get_connection():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", 5432),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
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
    theme_tags: Optional[list[str]] = None
    affected_principles: Optional[list[str]] = None
    risk_level: Optional[str] = None
    debate_questions: Optional[list[str]] = None
    confidence_notes: Optional[str] = None
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
    debate_questions: Optional[list[str]] = None
    theme_tags: Optional[list[str]] = None
    affected_principles: Optional[list[str]] = None
    risk_level: Optional[str] = None


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


# ─── HEALTH ───────────────────────────────────────────────────────────────────

@app.get("/")
def read_root():
    return {"message": "Asimovwatch API v2 is alive"}


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

@app.get("/entries")
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

        if q:
            filters.append("""
                (
                    LOWER(source_title) LIKE LOWER(%s)
                    OR LOWER(raw_snippet) LIKE LOWER(%s)
                    OR LOWER(summary_factual) LIKE LOWER(%s)
                    OR LOWER(translated_summary_ca) LIKE LOWER(%s)
                )
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
                id,
                source_url,
                source_domain,
                source_title,
                source_type,
                source_language,
                country_region,
                risk_level,
                review_status,
                reviewer,
                published_date,
                detected_at,
                ingested_at,
                ingest_status,
                summary_factual,
                theme_tags,
                affected_principles,
                processing_status,
                relevance_score,
                relevance_reason,
                enriched_at,
                enriched_model,
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


# ─── ENTRY DETAIL ─────────────────────────────────────────────────────────────

@app.get("/entries/{entry_id}")
def get_entry(entry_id: int):
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cur.execute("""
            SELECT
                id,
                source_url,
                source_domain,
                source_title,
                source_type,
                source_language,
                ingest_method,
                external_id,
                author_name,
                canonical_url,
                published_date,
                detected_at,
                country_region,
                institution_type,
                raw_snippet,
                raw_snippet_original,
                raw_content,
                raw_content_format,
                raw_payload,
                summary_factual,
                why_it_matters,
                theme_tags,
                affected_principles,
                risk_level,
                debate_questions,
                confidence_notes,
                review_status,
                reviewer,
                reviewed_at,
                editor_notes,
                validation_notes,
                dedup_key,
                ingest_status,
                ingested_at,
                updated_at,
                processing_status,
                processing_error,
                processing_retries,
                relevance_score,
                relevance_reason,
                enriched_at,
                enriched_model,
                translated_summary_ca,
                translated_whyitmatters_ca,
                translated_debatequestions_ca
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

@app.post("/entries", status_code=status.HTTP_201_CREATED,
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
                confidence_notes, review_status, reviewer, reviewed_at, editor_notes,
                validation_notes, dedup_key, ingested_at, updated_at, ingest_status,
                processing_status
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                now(), now(), 'ingested', 'RAW'
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
            entry.confidence_notes, entry.review_status, entry.reviewer,
            reviewed_at, entry.editor_notes, entry.validation_notes, dedup_key
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

@app.put("/entries/{entry_id}/review",
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
                review_status    = %s,
                reviewer         = %s,
                editor_notes     = %s,
                validation_notes = %s,
                reviewed_at      = COALESCE(%s, reviewed_at),
                updated_at       = now()
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


# ─── ENRICH ENTRY (NOU) ───────────────────────────────────────────────────────

@app.put("/entries/{entry_id}/enrich",
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
        }

        for col, val in field_map.items():
            if val is not None:
                fields.append(f"{col} = %s")
                values.append(val)

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


# ─── ENTRIES PENDING ENRICHMENT (NOU) ────────────────────────────────────────

@app.get("/entries/pending/enrichment")
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


# ─── DELETE ENTRY ─────────────────────────────────────────────────────────────

@app.delete("/entries/{entry_id}",
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


# ─── CONFIG (NOU) ─────────────────────────────────────────────────────────────

@app.get("/config")
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


@app.put("/config/{key}")
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


# ─── CRAWLER LOG (NOU) ────────────────────────────────────────────────────────

@app.get("/crawler-log")
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


@app.post("/crawler-log", status_code=status.HTTP_201_CREATED)
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


# ─── STATS (NOU) ──────────────────────────────────────────────────────────────

@app.get("/stats")
def get_stats():
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT
                COUNT(*) AS total_entries,
                COUNT(*) FILTER (WHERE processing_status = 'RAW') AS pending_enrichment,
                COUNT(*) FILTER (WHERE processing_status = 'ENRICHED') AS enriched,
                COUNT(*) FILTER (WHERE processing_status = 'ERROR') AS enrichment_errors,
                COUNT(*) FILTER (WHERE review_status = 'NEW') AS pending_review,
                COUNT(*) FILTER (WHERE review_status = 'APPROVED') AS approved,
                COUNT(*) FILTER (WHERE review_status = 'REJECTED') AS rejected,
                COUNT(*) FILTER (WHERE relevance_score = 'high') AS high_relevance,
                COUNT(*) FILTER (WHERE relevance_score = 'medium') AS medium_relevance,
                COUNT(*) FILTER (WHERE relevance_score = 'low') AS low_relevance
            FROM public.entries
        """)
        stats = cur.fetchone()
        return stats
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()
