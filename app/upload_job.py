"""Curation/upload pipeline (DESIGN.md pseudocode)."""
import asyncio
import logging
import random
import tempfile
import time
from pathlib import Path
from typing import Any, List

from app.config import settings
from app.db import db
from app.job_registry import registry
from app.utils import build_caption, human_jitter

log = logging.getLogger("instaward.upload")


def quality_gate(video_path: str) -> bool:
    p = Path(video_path)
    try:
        if not p.exists():
            return False
        size_mb = p.stat().st_size / (1024 * 1024)
        if size_mb < float(settings.QUALITY_MIN_SIZE_MB):
            return False
    except Exception:
        return False
    try:
        from PIL import Image
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            with Image.open(p) as im:
                w, h = im.size
                if w < settings.QUALITY_MIN_WIDTH or h < settings.QUALITY_MIN_HEIGHT:
                    return False
    except ImportError:
        pass
    except Exception:
        pass
    return True


async def fetch_candidates(count: int) -> List[dict]:
    """Fetch timeline + reels + explore candidates via aiograpi."""
    from app import ig_client
    cl = await ig_client.get_client()
    out: List[dict] = []
    seen = set()

    async def _collect(medias: Any):
        for m in medias or []:
            try:
                code = getattr(m, "code", None) or (m.get("code") if isinstance(m, dict) else None)
                pk = getattr(m, "pk", None) or getattr(m, "id", None) or (
                    m.get("pk") if isinstance(m, dict) else None)
                user = getattr(m, "user", None) or (m.get("user") if isinstance(m, dict) else {})
                if isinstance(user, dict):
                    uname = user.get("username", "")
                else:
                    uname = getattr(user, "username", "")
                caption_text = ""
                cap = getattr(m, "caption_text", None)
                if cap:
                    caption_text = str(cap)
                elif isinstance(m, dict):
                    cap2 = m.get("caption_text") or m.get("caption") or {}
                    caption_text = cap2.get("text", "") if isinstance(cap2, dict) else str(cap2 or "")
                if not code or code in seen:
                    continue
                seen.add(code)
                out.append({"code": str(code), "pk": str(pk or ""),
                            "author": str(uname or ""), "caption": caption_text,
                            "media_type": str(getattr(m, "media_type", 2))})
            except Exception:
                continue

    fetchers = [
        ("timeline", lambda: cl.get_timeline_feed()),
        ("reels", lambda: cl.get_reels_tray_feed()),
    ]
    for name, fn in fetchers:
        try:
            await human_jitter((1, 2))
            medias = await fn()
            if isinstance(medias, dict):
                medias = medias.get("items") or medias.get("medias") or []
            await _collect(medias)
        except Exception as e:
            log.info("candidate fetch %s failed: %s", name, type(e).__name__)
        if len(out) >= count:
            break
    try:
        if len(out) < count:
            explore = await cl.get_explore_feed()
            items = explore.get("items") if isinstance(explore, dict) else explore
            await _collect(items)
    except Exception as e:
        log.info("candidate fetch explore failed: %s", type(e).__name__)
    return out[:count]


async def _inner(target_count: int, comment_text: str, job_id: str) -> dict:
    daily = await db.incr_daily_count()
    if daily > settings.MAX_PER_DAY:
        return {"posted": 0, "skipped": 0, "error": "daily cap reached", "daily": daily}

    write_calls = 0
    posted = 0
    skipped = 0
    last_raw = await db.get_pacing("last_post_ms")
    try:
        last_post_ms = int(last_raw) if last_raw else 0
    except (ValueError, TypeError):
        last_post_ms = 0

    candidates = await fetch_candidates(settings.FETCH_COUNT)
    random.shuffle(candidates)
    registry.touch(job_id, progress=0, total=min(target_count, len(candidates)))

    for cand in candidates:
        if posted >= target_count:
            break
        if write_calls >= settings.MAX_WRITE_CALLS:
            log.info("write budget exhausted (%s)", write_calls)
            break
        if await db.is_processed(cand["code"]):
            skipped += 1
            continue

        # Pacing: min interval — sleep 60s, retry SAME candidate, no attempt counted
        interval_ms = settings.MIN_POST_INTERVAL_MIN * 60 * 1000
        if last_post_ms and (int(time.time() * 1000) - last_post_ms) < interval_ms:
            await asyncio.sleep(60)
            registry.touch(job_id, progress=posted)
            last_raw = await db.get_pacing("last_post_ms")
            try:
                last_post_ms = int(last_raw) if last_raw else 0
            except (ValueError, TypeError):
                last_post_ms = 0
            if last_post_ms and (int(time.time() * 1000) - last_post_ms) < interval_ms:
                continue

        # Download + quality gate
        from app import ig_client
        tmpdir = tempfile.mkdtemp(prefix="insta_")
        try:
            video_path = await ig_client.download_video(cand["pk"], tmpdir)
        except Exception as e:
            log.info("download failed %s: %s", cand["code"], type(e).__name__)
            skipped += 1
            continue
        if not quality_gate(str(video_path)):
            skipped += 1
            continue

        thumb = ""
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            c = Path(tmpdir) / f"thumb{ext}"
            if c.exists():
                thumb = str(c)
                break

        caption = build_caption(cand.get("caption", ""), cand.get("author", ""))
        await human_jitter()

        # Single clip_upload attempt — writes NEVER retried blindly
        try:
            media = await ig_client.upload_reel(str(video_path), caption, thumb)
            write_calls += 1
        except Exception as e:
            write_calls += 1
            log.info("upload failed %s: %s", cand["code"], type(e).__name__)
            skipped += 1
            continue

        repost_pk = str(getattr(media, "pk", "") or "")
        repost_code = str(getattr(media, "code", "") or "")
        await db.mark_processed(cand["code"], cand["pk"], cand.get("author", ""),
                                repost_pk, repost_code)
        now_ms = int(time.time() * 1000)
        await db.set_pacing("last_post_ms", str(now_ms))
        last_post_ms = now_ms
        posted += 1
        registry.touch(job_id, progress=posted)

        variants = [v.strip() for v in (comment_text or "").split("|") if v.strip()]
        if variants and settings.COMMENT_ENABLED and repost_pk:
            try:
                await ig_client.comment_and_pin(repost_pk, random.choice(variants))
            except Exception as e:
                log.info("comment+pin failed: %s", type(e).__name__)

    return {"posted": posted, "skipped": skipped, "write_calls": write_calls, "daily": daily}


def run_upload_job(target_count: int = 1, comment_text: str = "") -> Any:
    jid_holder: dict = {}

    async def _coro():
        return await _inner(target_count, comment_text, jid_holder["id"])

    job = registry.start_job("upload", _coro, total=target_count)
    jid_holder["id"] = job.id
    return job
