"""ig-repost FastAPI app."""
import asyncio
import contextlib
import random
import tempfile
import time
import uuid
from pathlib import Path

import pyotp
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import (
    check_connection,
    count_today_uploads,
    get_client,
    get_job_row,
    get_old_media,
    init_schema,
    insert_job,
    insert_media,
    update_job,
    update_status,
)
from app.ig import IGClient


def verify_api_key(key: str = Query(..., description="API key")) -> str:
    """Validate API key query param. Dev mode: if settings.API_KEY empty, accept any non-empty."""
    if not key:
        raise HTTPException(status_code=401, detail="missing api key")
    expected = (settings.API_KEY or "").strip()
    if not expected:
        # dev mode — accept any non-empty key
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
    yield


app = FastAPI(title="ig-repost", docs_url="/docs", redoc_url=None, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _find_duplicate(original_url: str):
    con = get_client()
    try:
        cur = con.execute(
            "SELECT id FROM uploaded_media WHERE original_url = ? AND status = 'active'",
            (original_url,),
        )
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


@app.post("/upload")
async def upload(
    key: str = Query(...),
    post_type: str = Query("reel"),
    target_url: str = Body(..., embed=True),
    comment: str = Query(default=""),
    cover: str = Query(default=""),
    sync: int = Query(default=1, description="1=upload inline (default for POST, back-compat), 0=queue background job"),
):
    verify_api_key(key)
    if int(sync or 0):
        return await _do_upload(post_type, str(target_url), comment, cover)
    return await _queue_upload(str(target_url), post_type, comment, cover)


@app.get("/upload")
async def upload_get(
    key: str = Query(...),
    target_url: str = Query(...),
    post_type: str = Query("reel"),
    comment: str = Query(default=""),
    cover: str = Query(default=""),
    sync: int = Query(default=0, description="0=queue background job (default for GET/cron), 1=upload inline"),
):
    verify_api_key(key)
    if int(sync or 0):
        return await _do_upload(post_type, str(target_url), comment, cover)
    return await _queue_upload(str(target_url), post_type, comment, cover)


# ---- Fire-and-forget job registry -------------------------------------
JOBS: dict = {}

UPLOAD_LOCK = asyncio.Lock()


async def _check_daily_cap() -> bool:
    """True if daily upload limit reached. Fail-open (False) on DB error."""
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


def _new_job_id() -> str:
    return uuid.uuid4().hex[:10]


def _job_to_response(job_id: str, job: dict) -> dict:
    return {
        "status": job.get("status", "queued"),
        "job_id": job_id,
        "media_id": job.get("media_id"),
        "error": job.get("error"),
        "target_url": job.get("target_url"),
        "check": f"/job?id={job_id}",
    }


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
            cover_url = ((cover or "").strip() or (getattr(settings, "THUMBNAIL_URL", "") or "").strip() or (settings.COVER_IMAGE_URL or "").strip())
            try:
                cover_path = await ig.download_cover(cover_url) if cover_url else None
            except Exception as e:
                print(f"[job {job_id}] cover download failed: {e}")
                cover_path = None
            media = await ig.upload_reel(str(video_path), caption="", thumbnail_path=str(cover_path) if cover_path else None)
            media_id = _extract_media_id(media)
            await asyncio.sleep(random.uniform(20, 60))
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
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["updated_at"] = time.time()
        try:
            await asyncio.to_thread(update_job, job_id, "error", None, str(e))
        except Exception as ue:
            print(f"[job {job_id}] update_job error failed (non-fatal): {ue}")
        print(f"[job {job_id}] failed: {e}")
        msg = str(e)
        if any(s in msg for s in ("challenge", "Challenge", "429", "throttl")):
            print(f"[job {job_id}] challenge/rate-limit - manual check needed, backing off")
    finally:
        for p in (video_path, cover_path):
            try:
                if p and Path(str(p)).exists() and Path(str(p)).is_file():
                    Path(str(p)).unlink()
            except Exception:
                pass


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
    post_type = (post_type or "reel").lower()
    if post_type not in ("reel", "post"):
        raise HTTPException(status_code=400, detail="post_type must be reel|post")
    if not target_url or not str(target_url).startswith("http"):
        raise HTTPException(status_code=400, detail="invalid target_url")
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
        cover_url = ((cover or "").strip() or (getattr(settings, "THUMBNAIL_URL", "") or "").strip() or (settings.COVER_IMAGE_URL or "").strip())
        try:
            cover_path = await ig.download_cover(cover_url) if cover_url else None
        except Exception as e:
            print(f"[upload] cover download failed: {e}")
            cover_path = None
        media = await ig.upload_reel(str(video_path), caption="", thumbnail_path=str(cover_path) if cover_path else None)
        media_id = _extract_media_id(media)
        await asyncio.sleep(random.uniform(20, 60))
        comment_text = (comment or "").strip() or settings.COMMENT_TEXT
        await ig.post_actions(media_id, comment_text)
        await asyncio.to_thread(insert_media, str(target_url), media_id, post_type)
        return {"status": "success", "media_id": media_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"upload failed: {e}")
    finally:
        for p in (video_path, cover_path):
            try:
                if p and Path(str(p)).exists() and Path(str(p)).is_file():
                    Path(str(p)).unlink()
            except Exception:
                pass


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
