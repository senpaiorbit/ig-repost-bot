"""FastAPI app, routes, middleware.

All endpoints key-protected via ?key= (hmac.compare_digest).
EVERY endpoint accepts ?cronjob=1 -> returns in <2s via background execution.
Rate limits (token buckets per IP) on /upload /upload_one /archive /archive_one /reconnect.
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.utils import constant_time_compare, parse_duration

log = logging.getLogger("instaward")
logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))

RATE_LIMITED_PATHS = {"/upload", "/upload_one", "/archive", "/archive_one", "/reconnect"}


def _is_cronjob(v) -> bool:
    return str(v or "").strip() in ("1", "true", "yes")


def _authorized(key: str) -> bool:
    if not settings.ENV_KEY:
        return True  # dev mode: no key configured
    return constant_time_compare(key or "", settings.ENV_KEY)


def _deny():
    return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)


def _cron_flag(cronjob) -> int:
    return 1 if _is_cronjob(cronjob) else 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from app.db import db
        await db.init_schema()
    except Exception as e:
        log.warning("schema init failed: %s", type(e).__name__)
    yield


app = FastAPI(title="InstaWard Bot", lifespan=lifespan)


@app.middleware("http")
async def _stale_sweep(request: Request, call_next):
    try:
        from app.job_registry import registry
        registry.sweep_stale()
    except Exception:
        pass
    return await call_next(request)


def _rate_check(request: Request, is_cron: bool):
    if request.url.path not in RATE_LIMITED_PATHS:
        return None
    from app.rate_limit import limiter
    ip = request.client.host if request.client else "unknown"
    allowed, retry = limiter.check_limit(ip, is_cron)
    if not allowed:
        return JSONResponse({"ok": False, "error": "rate limited",
                             "retry_after_sec": retry}, status_code=429)
    return None


# -- GET /health ---------------------------------------------------------
@app.get("/health")
async def health(cronjob: str = Query(default="")):
    return {"ok": True, "cronjob": _cron_flag(cronjob)}


# -- GET /turso_check ----------------------------------------------------
@app.get("/turso_check")
async def turso_check(key: str = Query(default="")):
    if not _authorized(key):
        return _deny()
    from app.db import TursoAuthError, db
    try:
        await db.init_schema()
        await db.ping()
        return {"ok": True}
    except TursoAuthError as e:
        return JSONResponse({"ok": False, "hint": str(e)}, status_code=503)
    except Exception as e:
        return JSONResponse({"ok": False, "detail": type(e).__name__}, status_code=503)


# -- GET /totp_check -----------------------------------------------------
@app.get("/totp_check")
async def totp_check(key: str = Query(default="")):
    """Prove the Render -> Cloudflare TOTP provider path end to end.

    Never returns the code itself (length + round-trip only). A login that
    needs 2FA uses this exact path before falling back to the local seed.
    """
    if not _authorized(key):
        return _deny()
    import os
    from app import totp_client
    url = (os.getenv("TOTP_PROVIDER_URL", "") or "").strip()
    has_key = bool(os.getenv("TOTP_KEY", ""))
    slot = (os.getenv("TOTP_SLOT", "") or "").strip() or "default"
    if not url or not has_key:
        return {"ok": False, "configured": False,
                "hint": "set TOTP_PROVIDER_URL + TOTP_KEY env vars"}
    started = time.time()
    code = await totp_client.fetch_code()
    elapsed_ms = int((time.time() - started) * 1000)
    if not code:
        return {"ok": False, "configured": True, "reachable": False,
                "slot": slot, "roundtrip_ms": elapsed_ms,
                "hint": "provider unreachable — logins fall back to local seed"}
    return {"ok": True, "configured": True, "reachable": True,
            "slot": slot, "code_len": len(code), "roundtrip_ms": elapsed_ms}


# -- GET /login_status ---------------------------------------------------
@app.get("/login_status")
async def login_status(key: str = Query(default=""), cronjob: str = Query(default="")):
    if not _authorized(key):
        return _deny()
    from app.db import db
    try:
        state = await db.get_login_breaker()
    except Exception:
        state = {"blocked_until": 0, "failure_count": 0, "last_failure_at": 0}
    now_ms = int(time.time() * 1000)
    blocked_until = int(state.get("blocked_until", 0) or 0)
    blocked = blocked_until > now_ms
    retry = max(0, (blocked_until - now_ms) // 1000) if blocked else 0
    return {"blocked": blocked, "blocked_until_ms": blocked_until,
            "retry_after_sec": retry,
            "failure_count": int(state.get("failure_count", 0) or 0),
            "cronjob": _cron_flag(cronjob)}


# -- GET /reconnect ------------------------------------------------------
@app.get("/reconnect")
async def reconnect(request: Request, key: str = Query(default=""),
                    cronjob: str = Query(default="")):
    if not _authorized(key):
        return _deny()
    denied = _rate_check(request, _is_cronjob(cronjob))
    if denied is not None:
        return denied
    from app import ig_client
    from app.job_registry import registry

    if _is_cronjob(cronjob):
        async def _bg():
            return {"user_id": await ig_client.reconnect()}
        job = registry.start_job("reconnect", _bg)
        return {"ok": True, "background": True, "job_id": job.id,
                "cronjob": 1, "note": job.note}
    try:
        uid = await asyncio.wait_for(ig_client.reconnect(), timeout=28)
    except asyncio.TimeoutError:
        async def _bg2():
            return {"user_id": await ig_client.reconnect()}
        job = registry.start_job("reconnect", _bg2)
        return {"ok": True, "background": True, "job_id": job.id,
                "note": "moved to background (30s budget)"}
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        retry = getattr(e, "retry_after_sec", 0)
        status = 429 if type(e).__name__ == "ThrottledError" else 500
        return JSONResponse({"ok": False, "error": msg,
                             "retry_after_sec": retry}, status_code=status)
    return {"ok": True, "user_id": uid,
            "username": settings.INSTAGRAM_USERNAME, "background": False}


# -- GET /upload ---------------------------------------------------------
@app.get("/upload")
async def upload(request: Request, key: str = Query(default=""),
                 amount: int = Query(default=1),
                 comment: str = Query(default=""),
                 cronjob: str = Query(default="")):
    if not _authorized(key):
        return _deny()
    denied = _rate_check(request, _is_cronjob(cronjob))
    if denied is not None:
        return denied
    from app.upload_job import run_upload_job
    target = 1 if _is_cronjob(cronjob) else max(1, int(amount or 1))
    job = run_upload_job(target, comment or settings.COMMENT_TEXT)
    if job.note == "already running":
        return {"ok": True, "job_id": job.id, "kind": "upload",
                "amount": target, "cronjob": _cron_flag(cronjob),
                "note": "already running"}
    return {"ok": True, "job_id": job.id, "kind": "upload",
            "amount": target, "cronjob": _cron_flag(cronjob)}


# -- GET /upload_one -----------------------------------------------------
@app.get("/upload_one")
async def upload_one(request: Request, key: str = Query(default=""),
                     url: str = Query(default=""),
                     comment: str = Query(default=""),
                     cover: str = Query(default=""),
                     cronjob: str = Query(default="")):
    if not _authorized(key):
        return _deny()
    denied = _rate_check(request, _is_cronjob(cronjob))
    if denied is not None:
        return denied
    if not (url or "").strip():
        return JSONResponse({"ok": False, "error": "missing url"}, status_code=400)
    from app.upload_job import run_single_job
    # Single uploads ALWAYS run in background (job_id instantly, <2s);
    # ?cronjob= is accepted for uniformity and echoed back.
    job = run_single_job(url, comment or settings.COMMENT_TEXT, cover or "")
    if job.note == "already running":
        return {"ok": True, "job_id": job.id, "kind": "single",
                "cronjob": _cron_flag(cronjob), "note": "already running"}
    return {"ok": True, "job_id": job.id, "kind": "single",
            "cronjob": _cron_flag(cronjob)}


# -- GET /a_job ----------------------------------------------------------
@app.get("/a_job")
async def a_job(key: str = Query(default=""), id: str = Query(default=""),
                cronjob: str = Query(default="")):
    if not _authorized(key):
        return _deny()
    from app.job_registry import registry
    job = registry.get_job(id)
    if not job:
        return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)
    d = registry.to_dict(job)
    d["ok"] = True
    return d


# -- GET /live -----------------------------------------------------------
@app.get("/live")
async def live(key: str = Query(default=""), limit: int = Query(default=20),
               cronjob: str = Query(default="")):
    if not _authorized(key):
        return _deny()
    from app.db import TursoAuthError, db
    try:
        rows = await db.get_recent(max(1, min(int(limit or 20), 100)))
    except TursoAuthError as e:
        return JSONResponse({"ok": False, "hint": str(e)}, status_code=503)
    except Exception as e:
        return JSONResponse({"ok": False, "detail": type(e).__name__}, status_code=503)
    return {"ok": True, "rows": [
        {"code": r.get("code"), "source_username": r.get("source_username"),
         "source_pk": r.get("source_pk"), "repost_code": r.get("repost_code"),
         "repost_pk": r.get("repost_pk"), "posted_at": r.get("posted_at"),
         "archived": r.get("archived", 0)} for r in rows]}


# -- GET /archive --------------------------------------------------------
@app.get("/archive")
async def archive(request: Request, key: str = Query(default=""),
                  time: str = Query(default=""),
                  views: int = Query(default=0),
                  all: str = Query(default=""),
                  wait: str = Query(default=""),
                  cronjob: str = Query(default="")):
    if not _authorized(key):
        return _deny()
    denied = _rate_check(request, _is_cronjob(cronjob))
    if denied is not None:
        return denied
    from app.archive_job import run_archive_job
    # cronjob ignores wait=1 -> always background
    time_sec = parse_duration(time) if time else parse_duration(settings.ARCHIVE_TIME)
    all_flag = str(all or "").strip() in ("1", "true", "yes")
    job = run_archive_job(time_sec, int(views or 0), settings.ARCHIVE_BATCH, all_flag)
    if job.note == "already running":
        return {"ok": True, "job_id": job.id, "kind": "archive",
                "cronjob": _cron_flag(cronjob), "note": "already running"}
    return {"ok": True, "job_id": job.id, "kind": "archive",
            "cronjob": _cron_flag(cronjob)}


# -- GET /archive_one ----------------------------------------------------
@app.get("/archive_one")
async def archive_one(request: Request, key: str = Query(default=""),
                      code: str = Query(default=""),
                      cronjob: str = Query(default="")):
    if not _authorized(key):
        return _deny()
    denied = _rate_check(request, _is_cronjob(cronjob))
    if denied is not None:
        return denied
    if not code:
        return JSONResponse({"ok": False, "error": "missing code"}, status_code=400)
    from app.archive_job import run_archive_job
    job = run_archive_job(0, 0, 1, False, only_code=code)
    if job.note == "already running":
        return {"ok": True, "job_id": job.id, "kind": "archive",
                "cronjob": _cron_flag(cronjob), "note": "already running"}
    return {"ok": True, "job_id": job.id, "kind": "archive",
            "cronjob": _cron_flag(cronjob)}
