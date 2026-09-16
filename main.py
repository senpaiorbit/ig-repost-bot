"""ig-repost FastAPI app."""
import asyncio
import contextlib
import logging
import os
import random
import tempfile
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Optional

import pyotp
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

try:
    import libsql as _libsql
except ImportError:
    _libsql = None

from app.config import settings
from app.db import (_local_path, check_connection, count_today_uploads, get_client, get_job_row, get_old_media, init_schema, insert_job, insert_media, update_job, update_status)
from app.ig import IGClient

LOGS = deque(maxlen=300)
_LOGIN_CACHE: dict = {"at": 0.0, "state": ""}
_LOGIN_CACHE_TTL = 300


class _buf_handler(logging.Handler):
    def emit(self, record):
        try:
            LOGS.append(self.format(record))
        except Exception:
            pass

BufferHandler = _buf_handler
log = logging.getLogger("igrep")
try:
    _live_handler = _buf_handler()
    _live_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    _live_handler.setLevel(logging.INFO)
    _root_logger = logging.getLogger()
    _root_logger.addHandler(_live_handler)
    _root_logger.setLevel(logging.INFO)
except Exception as _live_err:
    print(f"[startup] log buffer attach failed: {_live_err}")


def verify_api_key(key: str = Query(..., description="API key")) -> str:
    if not key:
        raise HTTPException(status_code=401, detail="missing api key")
    expected = (settings.API_KEY or "").strip()
    if not expected:
        return key
    if key != expected:
        raise HTTPException(status_code=403, detail="invalid api key")
    return key


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await asyncio.to_thread(init_schema)
    except Exception as e:
        print(f"[startup] init_schema failed: {e}")
    try:
        ig = IGClient()
        await asyncio.wait_for(ig.login(), timeout=25)
        try:
            who = await asyncio.wait_for(ig.check_login(), timeout=15)
        except Exception:
            who = getattr(ig, "username", "") or "?"
        print(f"[startup] IG login ok: {who}")
        log.info(f"[startup] IG login ok: {who}")
    except Exception as e:
        print(f"[startup] IG login skipped: {e}")
    yield


app = FastAPI(title="ig-repost", docs_url="/docs", redoc_url=None, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def _find_duplicate(original_url: str):
    con = get_client()
    try:
        cur = con.execute("SELECT id FROM uploaded_media WHERE original_url = ? AND status = 'active'", (original_url,))
        return cur.fetchone()
    finally:
        try:
            con.close()
        except Exception:
            pass


def _extract_media_id(media) -> str:
    try:
        return IGClient.extract_media_id(media)
    except Exception:
        pass
    mid = getattr(media, "pk", None) or getattr(media, "id", None)
    if mid is not None:
        return str(mid)
    if isinstance(media, dict):
        for k in ("pk", "id", "media_id"):
            if k in media:
                return str(media[k])
    return str(media)


@app.get("/")
async def root():
    return {"service": "ig-repost", "status": "ok", "docs": "/docs"}


@app.get("/health")
async def health():
    try:
        try:
            ok = await asyncio.to_thread(check_connection)
            db = "ok" if ok else "failed: check returned false"
        except Exception as e:
            db = f"failed: {e}"
        try:
            has_creds = bool(((settings.IG_USERNAME or "").strip()) or ((settings.IG_PASSWORD or "").strip()) or ((settings.IG_SESSIONID or "").strip()))
        except Exception:
            has_creds = False
        if not has_creds:
            login_state = "skipped: no credentials configured"
        else:
            try:
                now = time.time()
                cached_at = float(_LOGIN_CACHE.get("at", 0) or 0)
                cached_state = _LOGIN_CACHE.get("state", "") or ""
                if cached_state and (now - cached_at) < _LOGIN_CACHE_TTL:
                    login_state = cached_state
                else:
                    probe_ig = IGClient()
                    await asyncio.wait_for(probe_ig.login(), timeout=25)
                    try:
                        who = await asyncio.wait_for(probe_ig.check_login(), timeout=15)
                    except Exception:
                        who = getattr(probe_ig, "username", "") or "ok"
                    login_state = f"ok:{who}"
                    _LOGIN_CACHE["at"] = now
                    _LOGIN_CACHE["state"] = login_state
            except Exception as e:
                login_state = f"failed:{e}"
                try:
                    _LOGIN_CACHE["at"] = time.time()
                    _LOGIN_CACHE["state"] = login_state
                except Exception:
                    pass
        return {"status": "ok", "db": db, "login": login_state}
    except Exception as e:
        return {"status": "ok", "db": f"failed: {e}", "login": "failed: health probe error"}


@app.post("/upload")
async def upload(key: str = Query(...), post_type: str = Query("reel"), target_url: str = Body(..., embed=True), comment: str = Query(default=""), cover: str = Query(default=""), sync: int = Query(default=1)):
    verify_api_key(key)
    if int(sync or 0):
        return await _do_upload(post_type, str(target_url), comment, cover)
    return await _queue_upload(str(target_url), post_type, comment, cover)


@app.get("/upload")
async def upload_get(key: str = Query(...), target_url: Optional[str] = Query(default=None), post_type: str = Query("reel"), comment: str = Query(default=""), cover: str = Query(default=""), sync: int = Query(default=0), amount: int = Query(default=1), cronjob: int = Query(default=0)):
    verify_api_key(key)
    if target_url and str(target_url).startswith("http"):
        if int(sync or 0):
            return await _do_upload(post_type, str(target_url), comment, cover)
        return await _queue_upload(str(target_url), post_type, comment, cover)
    if target_url and str(target_url).strip():
        raise HTTPException(status_code=400, detail="invalid target_url")
    return await _queue_auto_upload(amount=amount, comment=comment, cover=cover)


JOBS: dict = {}
UPLOAD_LOCK = asyncio.Lock()


async def _check_daily_cap() -> bool:
    try:
        today = await asyncio.to_thread(count_today_uploads)
    except Exception as e:
        print(f"[upload] count_today failed (non-fatal): {e}")
        return False
    try:
        cap = int(getattr(settings, "MAX_UPLOADS_PER_DAY", 2) or 0)
    except Exception:
        cap = 2
    return bool(cap and today >= cap)


def _validate_upload_params(post_type: str, target_url: str) -> tuple[str, str]:
    pt = (post_type or "reel").lower()
    if pt not in ("reel", "post"):
        raise HTTPException(status_code=400, detail="post_type must be reel|post")
    if not target_url or not str(target_url).startswith("http"):
        raise HTTPException(status_code=400, detail="invalid target_url")
    return pt, str(target_url)


def _cover_url(cover: str = "") -> str:
    explicit = (cover or "").strip()
    if explicit:
        return explicit
    thumb = (getattr(settings, "THUMBNAIL_URL", "") or "").strip()
    if thumb:
        return thumb
    return IGClient.default_cover_url()


def _cleanup_paths(*paths) -> None:
    for p in paths:
        try:
            if p and Path(str(p)).exists() and Path(str(p)).is_file():
                Path(str(p)).unlink()
        except Exception:
            pass


def _new_job_id() -> str:
    return uuid.uuid4().hex[:10]


def _job_to_response(job_id: str, job: dict) -> dict:
    try:
        jlogs = [ln for ln in list(LOGS)[-300:] if job_id in ln][-20:]
    except Exception:
        jlogs = []
    return {"status": job.get("status", "queued"), "job_id": job_id, "media_id": job.get("media_id"), "error": job.get("error"), "target_url": job.get("target_url"), "check": f"/job?id={job_id}", "logs": jlogs}


async def _queue_upload(target_url: str, post_type: str = "reel", comment: str = "", cover: str = "") -> JSONResponse:
    post_type, target_url = _validate_upload_params(post_type, target_url)
    dup = await asyncio.to_thread(_find_duplicate, target_url)
    if dup:
        return JSONResponse(status_code=409, content={"status": "error", "message": "duplicate"})
    if await _check_daily_cap():
        return JSONResponse(status_code=429, content={"status": "error", "message": "daily limit reached"})
    job_id = _new_job_id()
    now = time.time()
    JOBS[job_id] = {"status": "queued", "media_id": None, "error": None, "target_url": target_url, "post_type": post_type, "comment": comment or "", "cover": cover or "", "created_at": now, "updated_at": now}
    try:
        await asyncio.to_thread(insert_job, job_id, target_url, post_type, "queued")
    except Exception as e:
        print(f"[job {job_id}] insert_job failed (non-fatal): {e}")
    asyncio.create_task(_run_job(job_id, post_type, target_url, comment, cover))
    return JSONResponse(status_code=202, content={"status": "queued", "job_id": job_id, "check": f"/job?id={job_id}"})


async def _queue_auto_upload(amount: int = 1, comment: str = "", cover: str = "") -> JSONResponse:
    try:
        amount = max(1, min(3, int(amount or 1)))
    except Exception:
        amount = 1
    if await _check_daily_cap():
        return JSONResponse(status_code=429, content={"status": "error", "message": "daily limit reached"})
    job_id = _new_job_id()
    now = time.time()
    JOBS[job_id] = {"status": "queued", "media_id": None, "error": None, "target_url": "auto:feed", "post_type": "reel", "mode": "auto", "amount": amount, "comment": comment or "", "cover": cover or "", "created_at": now, "updated_at": now}
    try:
        await asyncio.to_thread(insert_job, job_id, "auto:feed", "reel", "queued")
    except Exception as e:
        print(f"[job {job_id}] insert_job failed (non-fatal): {e}")
    asyncio.create_task(_run_auto_job(job_id, amount, comment, cover))
    log.info(f"[job {job_id}] queued auto amount={amount}")
    return JSONResponse(status_code=202, content={"status": "queued", "job_id": job_id, "mode": "auto", "check": f"/job?id={job_id}"})


async def _run_job(job_id: str, post_type: str, target_url: str, comment: str = "", cover: str = ""):
    job = JOBS.get(job_id)
    if job is None:
        return
    job["status"] = "running"
    job["updated_at"] = time.time()
    try:
        await asyncio.to_thread(update_job, job_id, "running")
    except Exception as e:
        print(f"[job {job_id}] update_job running failed (non-fatal): {e}")
    video_path = None
    cover_path = None
    tmpdir = tempfile.mkdtemp(prefix="igdl_")
    try:
        dup = await asyncio.to_thread(_find_duplicate, target_url)
        if dup:
            raise RuntimeError("duplicate")
        async with UPLOAD_LOCK:
            ig = IGClient()
            await ig.login()
            video_path = await ig.download_video(target_url, tmpdir)
            cover_url = _cover_url(cover)
            try:
                cover_path = await ig.download_cover(cover_url) if cover_url else None
            except Exception as e:
                print(f"[job {job_id}] cover download failed: {e}")
                cover_path = None
            media = await ig.upload_reel(str(video_path), caption="", thumbnail_path=str(cover_path) if cover_path else None)
            media_id = _extract_media_id(media)
            await asyncio.sleep(random.uniform(15, 90))
            comment_text = (comment or "").strip() or settings.COMMENT_TEXT
            await ig.post_actions(media_id, comment_text)
            await asyncio.to_thread(insert_media, target_url, media_id, post_type)
        job["status"] = "success"
        job["media_id"] = media_id
        job["error"] = None
        job["updated_at"] = time.time()
        try:
            await asyncio.to_thread(update_job, job_id, "success", media_id, None)
        except Exception as e:
            print(f"[job {job_id}] update_job success failed (non-fatal): {e}")
        log.info(f"[job {job_id}] success media_id={media_id}")
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["updated_at"] = time.time()
        try:
            await asyncio.to_thread(update_job, job_id, "error", None, str(e))
        except Exception as ue:
            print(f"[job {job_id}] update_job error failed (non-fatal): {ue}")
        print(f"[job {job_id}] failed: {e}")
        log.info(f"[job {job_id}] error: {str(e)[:200]}")
    finally:
        _cleanup_paths(video_path, cover_path)


async def _run_auto_job(job_id: str, amount: int = 1, comment: str = "", cover: str = ""):
    job = JOBS.get(job_id)
    if job is None:
        return
    try:
        amount = max(1, min(3, int(amount or 1)))
    except Exception:
        amount = 1
    job["status"] = "running"
    job["updated_at"] = time.time()
    try:
        await asyncio.to_thread(update_job, job_id, "running")
    except Exception as e:
        print(f"[job {job_id}] update_job running failed (non-fatal): {e}")
    try:
        try:
            feed_limit = int(getattr(settings, "AUTO_FEED_LIMIT", 20) or 20)
        except Exception:
            feed_limit = 20
        uploaded: list[str] = []
        last_media_id = None
        tried = 0
        last_err = ""
        async with UPLOAD_LOCK:
            ig = IGClient()
            await ig.login()
            cands = await ig.fetch_feed_candidates(limit=feed_limit)
            log.info(f"[job {job_id}] feed fetched: {len(cands)} candidates")
            if not cands:
                raise RuntimeError("empty feed — no candidates")
            for cand in cands:
                if len(uploaded) >= amount:
                    break
                if await _check_daily_cap():
                    if uploaded:
                        break
                    raise RuntimeError("daily limit reached")
                if cand.startswith("http"):
                    try:
                        dup = await asyncio.to_thread(_find_duplicate, cand)
                    except Exception:
                        dup = None
                    if dup:
                        last_err = f"duplicate skipped: {cand}"
                        continue
                tried += 1
                tmpdir = tempfile.mkdtemp(prefix="igdl_")
                video_path = None
                cover_path = None
                try:
                    video_path = await ig.download_candidate(cand, tmpdir)
                    cover_url = _cover_url(cover)
                    try:
                        cover_path = await ig.download_cover(cover_url) if cover_url else None
                    except Exception as e:
                        print(f"[job {job_id}] cover download failed: {e}")
                        cover_path = None
                    media = await ig.upload_reel(str(video_path), caption="", thumbnail_path=str(cover_path) if cover_path else None)
                    media_id = _extract_media_id(media)
                    await asyncio.sleep(random.uniform(15, 60))
                    comment_text = (comment or "").strip() or settings.COMMENT_TEXT
                    await ig.post_actions(media_id, comment_text)
                    await asyncio.to_thread(insert_media, cand, media_id, "reel")
                    uploaded.append(media_id)
                    last_media_id = media_id
                except Exception as e:
                    last_err = str(e)
                    if "duplicate" in str(e).lower():
                        continue
                    print(f"[job {job_id}] candidate failed, trying next: {e}")
                    log.info(f"[job {job_id}] candidate failed: {str(cand)[:60]} :: {str(e)[:160]}")
                    continue
                finally:
                    _cleanup_paths(video_path, cover_path)
            if not uploaded:
                raise RuntimeError(f"0/{len(cands)} candidates ok (tried {tried}), last error: {last_err or 'unknown'}")
        job["status"] = "success"
        job["media_id"] = last_media_id
        job["error"] = None
        job["updated_at"] = time.time()
        try:
            await asyncio.to_thread(update_job, job_id, "success", last_media_id, None)
        except Exception as e:
            print(f"[job {job_id}] update_job success failed (non-fatal): {e}")
        log.info(f"[job {job_id}] success media_id={last_media_id}")
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["updated_at"] = time.time()
        try:
            await asyncio.to_thread(update_job, job_id, "error", None, str(e))
        except Exception as ue:
            print(f"[job {job_id}] update_job error failed (non-fatal): {ue}")
        print(f"[job {job_id}] failed: {e}")
        log.info(f"[job {job_id}] error: {str(e)[:200]}")


def _lookup_job(job_id: str):
    job = JOBS.get(job_id)
    if job is not None:
        return job_id, job
    try:
        row = get_job_row(job_id)
    except Exception as e:
        print(f"[job] get_job_row failed: {e}")
        row = None
    if row:
        return job_id, {"status": row.get("status", "queued"), "media_id": row.get("media_id"), "error": row.get("error"), "target_url": row.get("target_url"), "post_type": row.get("post_type", "reel"), "created_at": row.get("created_at"), "updated_at": row.get("updated_at")}
    return None, None


@app.get("/job")
async def get_job(id: str = Query(..., description="job id")):
    job_id, job = _lookup_job(id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_to_response(id, job)


@app.get("/a_job")
async def get_a_job(id: str = Query(..., description="job id (alias of /job)")):
    _job_id, job = _lookup_job(id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_to_response(id, job)


async def _do_upload(post_type: str, target_url: str, comment: str = "", cover: str = ""):
    post_type, target_url = _validate_upload_params(post_type, target_url)
    dup = await asyncio.to_thread(_find_duplicate, str(target_url))
    if dup:
        return JSONResponse(status_code=409, content={"status": "error", "message": "duplicate"})
    if await _check_daily_cap():
        raise HTTPException(status_code=429, detail="daily limit reached")
    video_path = None
    cover_path = None
    tmpdir = tempfile.mkdtemp(prefix="igdl_")
    try:
        ig = IGClient()
        await ig.login()
        video_path = await ig.download_video(str(target_url), tmpdir)
        cover_url = _cover_url(cover)
        try:
            cover_path = await ig.download_cover(cover_url) if cover_url else None
        except Exception as e:
            print(f"[upload] cover download failed: {e}")
            cover_path = None
        media = await ig.upload_reel(str(video_path), caption="", thumbnail_path=str(cover_path) if cover_path else None)
        media_id = _extract_media_id(media)
        await asyncio.sleep(random.uniform(15, 90))
        comment_text = (comment or "").strip() or settings.COMMENT_TEXT
        await ig.post_actions(media_id, comment_text)
        await asyncio.to_thread(insert_media, str(target_url), media_id, post_type)
        return {"status": "success", "media_id": media_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"upload failed: {e}")
    finally:
        _cleanup_paths(video_path, cover_path)


@app.post("/archive")
async def archive(key: str = Query(...), max_age_hours: int = Query(24), min_views: int = Query(1000), force: bool = Query(False)):
    verify_api_key(key)
    rows = await asyncio.to_thread(get_old_media, max_age_hours)
    ig = IGClient()
    try:
        await ig.login()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"login failed: {e}")
    archived = 0
    skipped = 0
    for row in rows:
        mid = row.get("instagram_media_id") if isinstance(row, dict) else row[2]
        try:
            views = await ig.get_views(str(mid))
            if views > min_views and not force:
                skipped += 1
                continue
            status = await ig.archive_or_delete(str(mid))
            await asyncio.to_thread(update_status, str(mid), status)
            archived += 1
        except Exception as e:
            print(f"[archive] {mid} failed: {e}")
            skipped += 1
            continue
    return {"status": "success", "archived": archived, "skipped": skipped, "total": len(rows)}


@app.get("/check_db")
async def check_db():
    try:
        ok = await asyncio.to_thread(check_connection)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"db error: {e}")
    if ok:
        return {"status": "ok", "db": "connected"}
    raise HTTPException(status_code=500, detail="db check failed")


@app.get("/check_login")
async def check_login():
    try:
        ig = IGClient()
        await ig.login()
        username = await ig.check_login()
        return {"status": "ok", "username": username}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"login check failed: {e}")


@app.get("/turso_check")
async def turso_check():
    try:
        db_url = (settings.TURSO_DATABASE_URL or "").strip()
        turso_configured = bool(db_url and not db_url.startswith("file:"))
        backend = "turso" if (_libsql is not None and turso_configured) else "sqlite"
        def _probe():
            con = get_client()
            try:
                tables = {}
                for t in ("uploaded_media", "jobs"):
                    try:
                        cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,))
                        tables[t] = cur.fetchone() is not None
                    except Exception:
                        tables[t] = False
                def _count(sql, params=()):
                    try:
                        cur = con.execute(sql, params)
                        row = cur.fetchone()
                        return int(row[0]) if row and row[0] is not None else 0
                    except Exception:
                        return 0
                counts = {"active_media": 0, "jobs_queued": 0, "jobs_running": 0, "jobs_success": 0, "jobs_error": 0}
                if tables.get("uploaded_media"):
                    counts["active_media"] = _count("SELECT COUNT(*) FROM uploaded_media WHERE status='active'")
                if tables.get("jobs"):
                    for st in ("queued", "running", "success", "error"):
                        counts[f"jobs_{st}"] = _count("SELECT COUNT(*) FROM jobs WHERE status=?", (st,))
                return tables, counts
            finally:
                try:
                    con.close()
                except Exception:
                    pass
        tables, counts = await asyncio.to_thread(_probe)
        if backend == "sqlite":
            try:
                path = _local_path()
            except Exception:
                path = "local.db"
            if os.path.exists(path):
                writable = bool(os.access(path, os.W_OK))
            else:
                parent = os.path.dirname(os.path.abspath(path)) or "."
                try:
                    writable = bool(os.access(parent, os.W_OK))
                except Exception:
                    writable = False
        else:
            path = "remote"
            writable = True
        return {"status": "ok", "backend": backend, "path": path, "writable": writable, "tables": tables, "counts": counts, "turso_configured": turso_configured}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/reconnect")
async def reconnect(key: str = Query(...), reset: int = Query(default=0)):
    verify_api_key(key)
    fresh = bool(int(reset or 0))
    if fresh:
        try:
            sess = (getattr(settings, "SESSION_FILE", "") or "").strip() or "session.json"
            p = Path(sess)
            if p.exists():
                p.unlink()
        except Exception as e:
            print(f"[reconnect] session reset failed: {e}")
    try:
        ig = IGClient()
        await ig.login()
        username = await ig.check_login()
        try:
            sess = (getattr(settings, "SESSION_FILE", "") or "").strip() or "session.json"
            saved = Path(sess).exists()
        except Exception:
            saved = False
        return {"status": "ok", "username": username, "session_saved": saved, "fresh": fresh}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"reconnect failed: {e}")


@app.get("/check_totp")
async def check_totp():
    seed = (settings.IG_TOTP_SEED or "").strip()
    if not seed:
        return {"status": "error", "message": "Seed missing"}
    try:
        code = pyotp.TOTP(seed).now()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"totp failed: {e}")
    return {"status": "ok", "code_prefix": code[:2]}


@app.get("/live")
async def live(key: str = Query(...), n: int = Query(default=100), clear: int = Query(default=0)):
    verify_api_key(key)
    if int(clear or 0):
        try:
            LOGS.clear()
        except Exception:
            pass
    try:
        nn = max(1, int(n or 100))
    except Exception:
        nn = 100
    jobs = {}
    try:
        for jid, j in list(JOBS.items()):
            try:
                err = j.get("error")
                jobs[jid] = {"status": j.get("status"), "media_id": j.get("media_id"), "error": (str(err)[:200] if err is not None else None)}
            except Exception:
                continue
    except Exception:
        pass
    try:
        logs = list(LOGS)[-nn:]
    except Exception:
        logs = []
    return {"service": "ig-repost", "logs": logs, "jobs": jobs}
