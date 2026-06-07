import os
import psycopg2
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def read_root():
    return {"message": "Asimovwatch API v1 is alive"}

@app.get("/db-check")
def db_check():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return {"database": "error", "detail": "DATABASE_URL not set"}

    try:
        conn = psycopg2.connect(database_url)
        cur = conn.cursor()
        cur.execute("SELECT 1;")
        result = cur.fetchone()
        cur.close()
        conn.close()
        return {"database": "connected", "result": result[0]}
    except Exception as e:
        return {"database": "error", "detail": str(e)}
