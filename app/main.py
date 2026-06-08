import os
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, Query

app = FastAPI()

def get_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL not set")
    return psycopg2.connect(database_url)

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
                editor_notes
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
