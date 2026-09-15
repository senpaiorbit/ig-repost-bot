"""Retirement pipeline (DESIGN.md pseudocode)."""
import logging
from typing import Any

from app.config import settings
from app.db import db
from app.job_registry import registry
from app.utils import human_jitter, parse_duration

log = logging.getLogger("instaward.archive")


async def _inner(time_sec: int, views_thresh: int, batch_limit: int,
                 all_flag: bool, only_code: str, job_id: str) -> dict:
    from app import ig_client
    try:
        my_id = await ig_client.reconnect()
    except Exception as e:
        return {"archived": 0, "scanned": 0, "error": f"{type(e).__name__}: {e}"}

    if only_code:
        row = await db.get_by_code(only_code)
        candidates = [row] if row else []
    else:
        candidates = await db.get_archive_candidates(time_sec * 1000, views_thresh, batch_limit)

    archived = 0
    scanned = 0
    registry.touch(job_id, progress=0, total=len(candidates))

    for row in candidates:
        if not row or not row.get("repost_pk"):
            continue
        if int(row.get("archived", 0) or 0) and not all_flag:
            continue
        repost_pk = str(row["repost_pk"])
        # OWNER == SELF GUARD: verify live owner before touching
        try:
            await human_jitter((1, 2))
            info = await ig_client.fetch_media_info(repost_pk)
            user = getattr(info, "user", None)
            if user is None and isinstance(info, dict):
                user = info.get("user", {})
            owner_pk = ""
            if isinstance(user, dict):
                owner_pk = str(user.get("pk", "") or user.get("id", ""))
            else:
                owner_pk = str(getattr(user, "pk", "") or getattr(user, "id", ""))
            if owner_pk and str(my_id) and owner_pk != str(my_id):
                await db.mark_scanned(row["code"])
                scanned += 1
                continue
            views = 0
            for attr in ("view_count", "play_count", "like_count"):
                v = getattr(info, attr, None)
                if v is None and isinstance(info, dict):
                    v = info.get(attr)
                if v is not None:
                    try:
                        views = max(views, int(v))
                    except (ValueError, TypeError):
                        pass
            if not all_flag and not only_code and views > views_thresh:
                await db.mark_scanned(row["code"])
                scanned += 1
                continue
            media_type = getattr(info, "media_type", None)
            if media_type is None and isinstance(info, dict):
                media_type = info.get("media_type")
            is_clip = str(media_type).lower() in ("2", "clip", "reel", "8")
            product = str(getattr(info, "product_type", "") or (
                info.get("product_type", "") if isinstance(info, dict) else ""))
            if product.lower() in ("clips", "reel", "igtv"):
                is_clip = True
        except Exception as e:
            log.info("archive verify failed %s: %s", row.get("code"), type(e).__name__)
            scanned += 1
            continue

        try:
            await human_jitter()
            if is_clip:
                await ig_client.delete_media(repost_pk)   # clips can't be archived
            else:
                await ig_client.archive_media(repost_pk)  # photos archived
            await db.mark_archived(row["code"])
            archived += 1
        except Exception as e:
            log.info("archive action failed %s: %s", row.get("code"), type(e).__name__)
            continue
        registry.touch(job_id, progress=archived)

    return {"archived": archived, "scanned": scanned}


def run_archive_job(time_sec: int = 0, views_thresh: int = 0,
                    batch_limit: int = 0, all_flag: bool = False,
                    only_code: str = "") -> Any:
    time_sec = time_sec or parse_duration(settings.ARCHIVE_TIME)
    views_thresh = views_thresh if views_thresh else settings.ARCHIVE_VIEWS
    batch_limit = batch_limit if batch_limit else settings.ARCHIVE_BATCH
    jid_holder: dict = {}

    async def _coro():
        return await _inner(time_sec, views_thresh, batch_limit, all_flag,
                            only_code, jid_holder["id"])

    job = registry.start_job("archive", _coro, total=batch_limit)
    jid_holder["id"] = job.id
    return job
