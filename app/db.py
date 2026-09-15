"""Turso (libSQL) connection, schema, queries.

- Tolerant creds resolver: tolerates `libsql://URL,JWT` paste mistakes in either var.
- Auth failures -> caller raises HTTP 503 with actionable hint, never leaks URL/token.
- Session cache key is per-username via ig_sessions.username PK.
"""
import asyncio
import time
from typing import Any, Dict, List, Optional

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS processed_posts (
    code TEXT PRIMARY KEY,
    source_pk TEXT NOT NULL,
    source_username TEXT NOT NULL,
    repost_pk TEXT,
    repost_code TEXT,
    posted_at INTEGER NOT NULL,
    archived INTEGER DEFAULT 0,
    archive_scanned INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processed_posted ON processed_posts(posted_at);
CREATE INDEX IF NOT EXISTS idx_processed_archive ON processed_posts(archived, archive_scanned, posted_at);
CREATE TABLE IF NOT EXISTS ig_sessions (
    username TEXT PRIMARY KEY,
    settings_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pacing_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS login_breaker (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    blocked_until INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    last_failure_at INTEGER DEFAULT 0
);
"""

AUTH_HINT = (
    "Turso auth failed. Use the per-DB token (full JWT with 3 parts) for TURSO_AUTH_TOKEN, "
    "not the org API key; regenerate it from the Turso dashboard for this database and update "
    "TURSO_URL/TURSO_AUTH_TOKEN env vars."
)


class TursoAuthError(Exception):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def resolve_turso_creds(url: str = "", token: str = "") -> tuple:
    """Tolerate `libsql://URL,JWT` paste mistakes in either var.

    If either var contains a comma, split into (url, token). If the URL part
    lacks a scheme, prepend libsql://. Strips whitespace/quotes.
    """
    url = (url or "").strip().strip("'\"")
    token = (token or "").strip().strip("'\"")
    combined = ""
    if "," in url:
        combined = url
    elif "," in token and "://" in token:
        combined = token
    if combined:
        parts = [p.strip().strip("'\"") for p in combined.split(",")]
        parts = [p for p in parts if p]
        if len(parts) >= 2:
            maybe_url, maybe_token = parts[0], parts[-1]
            # Heuristic: the part containing :// or libsql/turso is the URL
            if "://" in maybe_token and "://" not in maybe_url:
                maybe_url, maybe_token = maybe_token, maybe_url
            url, token = maybe_url, maybe_token
    if url and "://" not in url:
        url = "libsql://" + url
    return url, token


def _looks_like_auth_error(exc: Exception) -> bool:
    msg = f"{type(exc).__name__}: {exc}".lower()
    keys = ("invalidtoken", "invalid token", "unauthorized", "unauthenticated",
            "forbidden", "hrana", "auth", "jwt", "token")
    return any(k in msg for k in keys)


class DB:
    def __init__(self) -> None:
        self._conn: Any = None
        self._lock = asyncio.Lock()

    # -- connection -----------------------------------------------------
    def _connect_sync(self) -> Any:
        from app.config import settings
        try:
            import libsql_experimental as libsql
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("libsql-experimental is not installed") from e
        url, token = resolve_turso_creds(settings.TURSO_URL, settings.TURSO_AUTH_TOKEN)
        if not url:
            raise RuntimeError("TURSO_URL is not set")
        kwargs: Dict[str, Any] = {}
        if token:
            kwargs["auth_token"] = token
        try:
            conn = libsql.connect(url, **kwargs)
        except Exception as e:
            if _looks_like_auth_error(e):
                raise TursoAuthError(AUTH_HINT) from e
            raise
        return conn

    async def _conn_safe(self) -> Any:
        if self._conn is None:
            async with self._lock:
                if self._conn is None:
                    self._conn = await asyncio.to_thread(self._connect_sync)
        return self._conn

    async def _exec_sync(self, fn, *args):
        conn = await self._conn_safe()
        def _run():
            cur = conn.cursor()
            try:
                return fn(cur, *args)
            finally:
                try:
                    conn.commit()
                except Exception:
                    pass
        try:
            return await asyncio.to_thread(_run)
        except Exception as e:
            if _looks_like_auth_error(e):
                raise TursoAuthError(AUTH_HINT) from e
            raise

    async def ping(self) -> None:
        def _q(cur):
            cur.execute("SELECT 1")
            return cur.fetchone()
        await self._exec_sync(_q)

    async def init_schema(self) -> None:
        conn = await self._conn_safe()
        def _run():
            stmts = [s.strip() for s in SCHEMA_SQL.split(";") if s.strip()]
            cur = conn.cursor()
            for s in stmts:
                cur.execute(s)
            try:
                conn.commit()
            except Exception:
                pass
        try:
            await asyncio.to_thread(_run)
        except Exception as e:
            if _looks_like_auth_error(e):
                raise TursoAuthError(AUTH_HINT) from e
            raise

    # -- sessions (per-username key) ------------------------------------
    def session_key(self, username: str) -> str:
        return f"ig_session:{username}"

    async def get_session(self, username: str) -> Optional[dict]:
        import json
        def _q(cur, u):
            cur.execute("SELECT settings_json FROM ig_sessions WHERE username = ?", (u,))
            return cur.fetchone()
        row = await self._exec_sync(_q, username)
        if not row:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None

    async def save_session(self, username: str, settings_obj: dict) -> None:
        import json
        payload = json.dumps(settings_obj or {})
        def _q(cur, u, p, now):
            cur.execute(
                "INSERT INTO ig_sessions(username, settings_json, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(username) DO UPDATE SET settings_json=excluded.settings_json, "
                "updated_at=excluded.updated_at",
                (u, p, now),
            )
        await self._exec_sync(_q, username, payload, _now_ms())

    # -- processed posts -------------------------------------------------
    async def is_processed(self, code: str) -> bool:
        def _q(cur, c):
            cur.execute("SELECT 1 FROM processed_posts WHERE code = ?", (c,))
            return cur.fetchone()
        return (await self._exec_sync(_q, code)) is not None

    async def mark_processed(self, code: str, source_pk: str, source_user: str,
                             repost_pk: str, repost_code: str) -> None:
        now = _now_ms()
        def _q(cur):
            cur.execute(
                "INSERT INTO processed_posts(code, source_pk, source_username, repost_pk, "
                "repost_code, posted_at, archived, archive_scanned, created_at) "
                "VALUES(?,?,?,?,?,?,0,0,?) "
                "ON CONFLICT(code) DO UPDATE SET repost_pk=excluded.repost_pk, "
                "repost_code=excluded.repost_code, posted_at=excluded.posted_at",
                (code, str(source_pk or ""), str(source_user or ""), str(repost_pk or ""),
                 str(repost_code or ""), now, now),
            )
        await self._exec_sync(_q)

    async def mark_archived(self, code: str) -> None:
        def _q(cur, c):
            cur.execute(
                "UPDATE processed_posts SET archived=1, archive_scanned=1 WHERE code = ?", (c,))
        await self._exec_sync(_q, code)

    async def mark_scanned(self, code: str) -> None:
        def _q(cur, c):
            cur.execute(
                "UPDATE processed_posts SET archive_scanned=1 WHERE code = ?", (c,))
        await self._exec_sync(_q, code)

    async def get_archive_candidates(self, time_ms: int, views: int, limit: int) -> List[dict]:
        cutoff = _now_ms() - int(time_ms)
        def _q(cur, c, lim):
            cur.execute(
                "SELECT code, source_pk, source_username, repost_pk, repost_code, posted_at, "
                "archived, archive_scanned FROM processed_posts "
                "WHERE archived = 0 AND posted_at <= ? AND repost_pk IS NOT NULL "
                "ORDER BY posted_at ASC LIMIT ?",
                (c, lim),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        # views threshold is enforced live via IG media_info; DB narrows by age
        return await self._exec_sync(_q, cutoff, int(limit))

    async def get_recent(self, limit: int = 20) -> List[dict]:
        def _q(cur, lim):
            cur.execute(
                "SELECT code, source_pk, source_username, repost_pk, repost_code, posted_at, "
                "archived FROM processed_posts ORDER BY posted_at DESC LIMIT ?",
                (lim,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        return await self._exec_sync(_q, int(limit))

    async def get_by_code(self, code: str) -> Optional[dict]:
        def _q(cur, c):
            cur.execute(
                "SELECT code, source_pk, source_username, repost_pk, repost_code, posted_at, "
                "archived, archive_scanned FROM processed_posts WHERE code = ? OR repost_code = ?",
                (c, c),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
        return await self._exec_sync(_q, code)

    # -- pacing ----------------------------------------------------------
    async def get_pacing(self, key: str) -> Optional[str]:
        def _q(cur, k):
            cur.execute("SELECT value FROM pacing_state WHERE key = ?", (k,))
            r = cur.fetchone()
            return r[0] if r else None
        return await self._exec_sync(_q, key)

    async def set_pacing(self, key: str, value: str) -> None:
        def _q(cur, k, v, now):
            cur.execute(
                "INSERT INTO pacing_state(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (k, str(value), now),
            )
        await self._exec_sync(_q, key, str(value), _now_ms())

    async def incr_daily_count(self) -> int:
        import datetime
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        def _q(cur, day, now):
            cur.execute("SELECT value FROM pacing_state WHERE key = ?", ("daily_count",))
            r = cur.fetchone()
            count, stored_day = 0, ""
            if r:
                try:
                    stored_day, count = str(r[0]).split(":", 1)
                    count = int(count)
                except Exception:
                    count, stored_day = 0, ""
            if stored_day != day:
                count = 0
            count += 1
            cur.execute(
                "INSERT INTO pacing_state(key, value, updated_at) VALUES('daily_count',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (f"{day}:{count}", now),
            )
            return count
        return await self._exec_sync(_q, today, _now_ms())

    # -- login breaker ---------------------------------------------------
    async def get_login_breaker(self) -> dict:
        def _q(cur):
            cur.execute("SELECT blocked_until, failure_count, last_failure_at FROM login_breaker WHERE id = 1")
            r = cur.fetchone()
            if not r:
                return {"blocked_until": 0, "failure_count": 0, "last_failure_at": 0}
            return {"blocked_until": int(r[0] or 0), "failure_count": int(r[1] or 0),
                    "last_failure_at": int(r[2] or 0)}
        return await self._exec_sync(_q)

    async def set_login_breaker(self, blocked_until: int, failure_count: int) -> None:
        def _q(cur, b, f, now):
            cur.execute(
                "INSERT INTO login_breaker(id, blocked_until, failure_count, last_failure_at) "
                "VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET blocked_until=excluded.blocked_until, "
                "failure_count=excluded.failure_count, last_failure_at=excluded.last_failure_at",
                (int(b), int(f), int(now)),
            )
        await self._exec_sync(_q, blocked_until, failure_count, _now_ms())

    async def clear_login_breaker(self) -> None:
        await self.set_login_breaker(0, 0)


db = DB()
