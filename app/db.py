"""SQLite DB (stdlib) with optional Turso/libsql when available."""
import os
import sqlite3
from typing import Optional

try:
    import libsql as _libsql
except ImportError:
    _libsql = None

from app.config import settings

SCHEMA_SQL = "CREATE TABLE IF NOT EXISTS uploaded_media (id INTEGER PRIMARY KEY AUTOINCREMENT, original_url TEXT NOT NULL UNIQUE, instagram_media_id TEXT NOT NULL, media_type TEXT CHECK(media_type IN ('reel','post')), status TEXT DEFAULT 'active', views INTEGER DEFAULT 0, uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP, archived_at DATETIME);"

JOBS_SCHEMA_SQL = "CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, target_url TEXT NOT NULL, post_type TEXT DEFAULT 'reel', status TEXT DEFAULT 'queued', media_id TEXT, error TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);"


def _local_path() -> str:
    db_url = settings.TURSO_DATABASE_URL or "file:local.db"
    if db_url.startswith("file:"):
        return db_url[5:] or "local.db"
    return "local.db"


def get_client():
    db_url = settings.TURSO_DATABASE_URL or ""
    token = settings.TURSO_AUTH_TOKEN or ""
    if _libsql is not None and db_url and not db_url.startswith("file:"):
        return _libsql.connect(database=db_url, auth_token=token)
    return sqlite3.connect(_local_path(), check_same_thread=False)


def init_schema():
    con = get_client()
    con.execute(SCHEMA_SQL)
    con.execute(JOBS_SCHEMA_SQL)
    con.commit()
    con.close()


def init_jobs_schema():
    con = get_client()
    con.execute(JOBS_SCHEMA_SQL)
    con.commit()
    con.close()


def insert_media(original_url: str, instagram_media_id: str, media_type: str = "reel"):
    con = get_client()
    con.execute(SCHEMA_SQL)
    con.execute("INSERT OR IGNORE INTO uploaded_media (original_url, instagram_media_id, media_type) VALUES (?, ?, ?)", (original_url, instagram_media_id, media_type))
    con.commit()
    cur = con.execute("SELECT id FROM uploaded_media WHERE original_url = ?", (original_url,))
    row = cur.fetchone()
    con.close()
    return row[0] if row else None


def get_old_media(max_age_hours: int = 24):
    con = get_client()
    cur = con.execute("SELECT * FROM uploaded_media WHERE uploaded_at <= datetime('now', '-' || ? || ' hours') AND status = 'active'", (max_age_hours,))
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description] if cur.description else []
    con.close()
    return [dict(zip(cols, r)) for r in rows]


def update_status(instagram_media_id: str, status: str):
    con = get_client()
    if status in ("archived", "deleted"):
        con.execute("UPDATE uploaded_media SET status = ?, archived_at = CURRENT_TIMESTAMP WHERE instagram_media_id = ?", (status, instagram_media_id))
    else:
        con.execute("UPDATE uploaded_media SET status = ? WHERE instagram_media_id = ?", (status, instagram_media_id))
    con.commit()
    con.close()


def check_connection():
    con = get_client()
    cur = con.execute("SELECT 1")
    row = cur.fetchone()
    con.close()
    return row[0] == 1 if row else False


def insert_job(job_id: str, target_url: str, post_type: str = "reel", status: str = "queued"):
    con = get_client()
    con.execute(JOBS_SCHEMA_SQL)
    con.execute("INSERT OR IGNORE INTO jobs (job_id, target_url, post_type, status) VALUES (?, ?, ?, ?)", (job_id, target_url, post_type, status))
    con.commit()
    con.close()


def update_job(job_id: str, status: str, media_id: Optional[str] = None, error: Optional[str] = None):
    con = get_client()
    con.execute(JOBS_SCHEMA_SQL)
    con.execute("UPDATE jobs SET status = ?, media_id = COALESCE(?, media_id), error = COALESCE(?, error), updated_at = CURRENT_TIMESTAMP WHERE job_id = ?", (status, media_id, error, job_id))
    con.commit()
    con.close()


def get_job_row(job_id: str):
    con = get_client()
    try:
        try:
            con.execute(JOBS_SCHEMA_SQL)
        except Exception:
            pass
        cur = con.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        row = cur.fetchone()
        cols = [d[0] for d in cur.description] if cur.description else []
        return dict(zip(cols, row)) if row else None
    finally:
        try:
            con.close()
        except Exception:
            pass


def count_today_uploads() -> int:
    con = get_client()
    try:
        cur = con.execute("SELECT COUNT(*) FROM uploaded_media WHERE date(uploaded_at) = date('now')")
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        try:
            con.close()
        except Exception:
            pass
