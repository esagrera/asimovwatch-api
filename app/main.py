from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List
import sqlite3, os, json
from datetime import datetime

app = FastAPI(title="AsimovWatch API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "asimovwatch.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_title TEXT NOT NULL,
            source_url TEXT,
            source_domain TEXT,
            source_type TEXT DEFAULT 'web',
            published_date TEXT,
            country_region TEXT,
            summary_factual TEXT,
            why_it_matters TEXT,
            theme_tags TEXT DEFAULT '[]',
            affected_principles TEXT DEFAULT '[]',
            risk_level TEXT DEFAULT 'medium',
            debate_questions TEXT DEFAULT '[]',
            status TEXT DEFAULT 'DRAFT',
            reviewer TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

@app.get("/")
def root():
    return {"message": "AsimovWatch API v1 is alive"}

@app.get("/entries")
def list_entries(
    status: Optional[str] = "PUBLISHED",
    q: Optional[str] = None,
    risk_level: Optional[str] = None,
    source_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0
):
    conn = get_db()
    sql = "SELECT * FROM entries WHERE 1=1"
    params = []
    if status:
        sql += " AND status=?"; params.append(status)
    if q:
        sql += " AND (source_title LIKE ? OR summary_factual LIKE ?)"; params += [f"%{q}%", f"%{q}%"]
    if risk_level:
        sql += " AND risk_level=?"; params.append(risk_level)
    if source_type:
        sql += " AND source_type=?"; params.append(source_type)
    total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
    sql += " ORDER BY published_date DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    items = []
    for r in rows:
        d = dict(r)
        d["theme_tags"] = json.loads(d.get("theme_tags") or "[]")
        d["affected_principles"] = json.loads(d.get("affected_principles") or "[]")
        d["debate_questions"] = json.loads(d.get("debate_questions") or "[]")
        items.append(d)
    return {"total": total, "items": items}

@app.get("/entries/{entry_id}")
def get_entry(entry_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM entries WHERE id=?", [entry_id]).fetchone()
    conn.close()
    if not row:
        return {"error": "Not found"}
    d = dict(row)
    d["theme_tags"] = json.loads(d.get("theme_tags") or "[]")
    d["affected_principles"] = json.loads(d.get("affected_principles") or "[]")
    d["debate_questions"] = json.loads(d.get("debate_questions") or "[]")
    return d

@app.post("/entries")
def create_entry(entry: dict):
    conn = get_db()
    conn.execute("""
        INSERT INTO entries (source_title, source_url, source_domain, source_type,
        published_date, country_region, summary_factual, why_it_matters,
        theme_tags, affected_principles, risk_level, debate_questions, status, reviewer)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, [
        entry.get("source_title"), entry.get("source_url"), entry.get("source_domain"),
        entry.get("source_type","web"), entry.get("published_date"), entry.get("country_region"),
        entry.get("summary_factual"), entry.get("why_it_matters"),
        json.dumps(entry.get("theme_tags",[])), json.dumps(entry.get("affected_principles",[])),
        entry.get("risk_level","medium"), json.dumps(entry.get("debate_questions",[])),
        entry.get("status","DRAFT"), entry.get("reviewer")
    ])
    conn.commit()
    conn.close()
    return {"ok": True}