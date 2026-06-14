import os
import json
import hashlib
from datetime import datetime, timezone
from typing import Any, Literal

import psycopg2
from psycopg2.extras import RealDictCursor, Json
from fastapi import FastAPI, Query, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl

app = FastAPI()


def get_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL not set")
    return psycopg2.connect(database_url)


class EntryIngest(BaseModel):
    source_url: HttpUrl
    source_domain: str
    source_title: str

    source_type: Literal["rss", "web", "api", "manual", "newsletter"]
    ingest_method: Literal["manual", "crawler", "feed", "api_push"]

    source_language: str | None = None
    external_id: str | None = None
    author_name: str | None = None
    canonical_url: HttpUrl | None = None

    published_date: datetime | None = None
    detected_at: datetime | None = None

    country_region: str | None = None
    institution_type: str | None = None

    raw_snippet: str | None = None
    raw_content: str | None = None
    raw_content_format: Literal["html", "markdown", "plain", "json"] | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    summary_factual: str | None = None
    why_it_matters: str | None = None

    theme_tags: list[str] = Field(default_factory=list)
    affected_principles: list[str] = Field(default_factory=list)
    debate_questions: list[str] = Field(default_factory=list)

    risk_level: Literal["low", "medium", "high"] | None = None
    confidence_notes: str | None = None

    review_status: str = "NEW"
    reviewer: str | None = None
    reviewed_at: datetime | None = None
    editor_notes: str | None = None
    validation_notes: str | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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


@app.get("/")
def read_root():
    return {"message": "Asimovwatch API v1 is alive"}


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


@app.get("/entries")
def get_entries(limit: int = Query(20, gt=0, le=100)):
    try:
        conn = get_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT
                id,
                source_url,
                source_domain,
                source_title,
                source_type,
                source_language,
                ingest_method,
                published_date,
                detected_at,
                country_region,
                institution_type,
                raw_snippet,
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
                ingest_status,
                ingested_at
            FROM public.entries
            ORDER BY detected_at DESC NULLS LAST, id DESC
            LIMIT %s;
            """,
            (limit,)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {"count": len(rows), "items": rows}
    except Exception as e:
        return {"error": str(e)}


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
                updated_at
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


@app.post(
    "/entries",
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {
            "description": "Duplicate entry"
        }
    }
)
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
            SELECT id, dedup_key
            FROM public.entries
            WHERE dedup_key = %s
            LIMIT 1
        """, (dedup_key,))
        existing = cur.fetchone()

        if existing:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "duplicate",
                    "id": existing["id"],
                    "dedup_key": existing["dedup_key"]
                }
            )

        cur.execute("""
            INSERT INTO public.entries (
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
                ingested_at,
                updated_at,
                ingest_status
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                now(), now(), 'ingested'
            )
            RETURNING
                id,
                source_url,
                source_domain,
                source_title,
                source_type,
                ingest_method,
                review_status,
                ingest_status,
                dedup_key,
                ingested_at
        """, (
            source_url,
            entry.source_domain.strip().lower(),
            entry.source_title.strip(),
            entry.source_type,
            entry.source_language,
            entry.ingest_method,
            entry.external_id.strip() if entry.external_id else None,
            entry.author_name,
            canonical_url,
            published_date,
            detected_at,
            entry.country_region,
            entry.institution_type,
            entry.raw_snippet,
            entry.raw_content,
            entry.raw_content_format,
            Json(entry.raw_payload),
            entry.summary_factual,
            entry.why_it_matters,
            entry.theme_tags,
            entry.affected_principles,
            entry.risk_level,
            entry.debate_questions,
            entry.confidence_notes,
            entry.review_status,
            entry.reviewer,
            reviewed_at,
            entry.editor_notes,
            entry.validation_notes,
            dedup_key
        ))

        created = cur.fetchone()
        conn.commit()

        return {
            "status": "created",
            "item": created
        }

    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise HTTPException(status_code=409, detail="Duplicate entry")

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create entry: {str(e)}")

    finally:
        cur.close()
        conn.close()
