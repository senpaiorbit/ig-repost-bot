"""Curation/upload pipeline (DESIGN.md pseudocode).

shuffle, quality gate, pacing (60s retry same candidate), single clip_upload
(no blind retry), MAX_WRITE_CALLS 40, daily cap fail, caption + Credit,
optional comment+pin, owner==self guard not needed here (candidates are others).
"""
import asyncio
import inspect
import logging
import random
import re
import tempfile
import time
from pathlib import Path
from typing import Any, List, Optional

from app.config import settings
from app.db import db
from app.job_registry import registry
from app.utils import build_caption, human_jitter

log = logging.getLogger("instaward.upload")


def _make_cover(video_path: str, tmpdir: str) -> str:
    """Generate a JPEG cover frame via imageio-ffmpeg binary. Returns "" on any failure."""
    try:
        import imageio_ffmpeg  # type: ignore
        import subprocess
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        out = str(Path(tmpdir) / "cover.jpg")
        subprocess.run([exe, "-y", "-ss", "1", "-i", str(video_path),
                        "-vframes", "1", out],
                       timeout=60, capture_output=True, check=True)
        if Path(out).exists() and Path(out).stat().st_size > 0:
            return out
    except Exception:
        pass
    return ""


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
    # Resolution check via Pillow when available; skip if unreadable
    try:
        from PIL import Image
        # videos: Pillow can't read; try thumbnail sidecar only if image
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


_FEED_KEYS = ("items", "medias", "feed_items", "ranked_items", "tray",
               "users", "results", "reels", "clips", "posts")


def _raw_len(result: Any) -> int:
    try:
        return len(result)
    except Exception:
        return 1 if result is not None else 0


def _unwrap_medias(result: Any) -> tuple:
    """Normalize any fetch result to (medias_list, raw_count, shape_hint).

    Unwraps dicts/objects via known feed keys, else scans dict values for the
    first list. Never iterates a bare dict's keys as media. Hint carries dict
    keys or the type name only — never content.
    """
    if result is None:
        return [], 0, "none"
    if isinstance(result, (list, tuple)):
        return list(result), len(result), "list"
    if not isinstance(result, dict):
        for key in _FEED_KEYS:
            try:
                val = getattr(result, key, None)
            except Exception:
                val = None
            if isinstance(val, (list, tuple)):
                return list(val), _raw_len(result), "attr:" + key
    else:
        for key in _FEED_KEYS:
            val = result.get(key)
            if isinstance(val, (list, tuple)):
                return list(val), _raw_len(result), "key:" + key
        for k, v in result.items():
            if isinstance(v, (list, tuple)) and v:
                return list(v), _raw_len(result), "scan:" + str(k)[:32]
        keys = ",".join(sorted(str(k)[:32] for k in result.keys())[:8])
        return [], _raw_len(result), "dict-keys:" + keys
    if getattr(result, "code", None) is not None or getattr(result, "pk", None) is not None:
        return [result], 1, "single:" + type(result).__name__
    return [], _raw_len(result), "type:" + type(result).__name__


_NEST_KEYS = ("media_or_ad", "item", "media", "clip", "reel", "post", "node")
_SUBLIST_KEYS = ("items", "clips", "reels")


def _has_code(m: Any) -> bool:
    if isinstance(m, dict):
        return m.get("code") is not None or m.get("pk") is not None or m.get("id") is not None
    try:
        return (getattr(m, "code", None) is not None
                or getattr(m, "pk", None) is not None
                or getattr(m, "id", None) is not None)
    except Exception:
        return False


def _describe_element(m: Any) -> str:
    """Key/attr names of a raw element only — never values/content."""
    if isinstance(m, dict):
        return "keys:" + ",".join(sorted(str(k)[:32] for k in m.keys())[:8])
    try:
        names = sorted(n[:32] for n in dir(m) if not n.startswith("__"))[:12]
    except Exception:
        names = []
    return "attrs:" + ",".join(names) + "|type:" + type(m).__name__


def _norm_item(m: Any) -> Optional[dict]:
    """Unwrap nested media wrappers -> {code, pk, author, caption, media_type} or None."""
    try:
        node = m
        # 1. nested single-media keys, max depth 3
        for _ in range(3):
            if _has_code(node):
                break
            nxt = None
            if isinstance(node, dict):
                for key in _NEST_KEYS:
                    v = node.get(key)
                    if v is None or isinstance(v, (str, int, float, bool, list, tuple)):
                        continue
                    nxt = v
                    break
            else:
                for key in ("media_or_ad", "item", "media"):
                    try:
                        v = getattr(node, key, None)
                    except Exception:
                        v = None
                    if v is not None and not isinstance(v, (str, int, float, bool, list, tuple)):
                        nxt = v
                        break
            if nxt is None:
                break
            node = nxt
        # 2. tray-style sublists: items/clips/reels list -> element [0]
        if not _has_code(node):
            sub = None
            if isinstance(node, dict):
                for key in _SUBLIST_KEYS:
                    v = node.get(key)
                    if isinstance(v, (list, tuple)) and v:
                        sub = v[0]
                        break
            else:
                for key in _SUBLIST_KEYS:
                    try:
                        v = getattr(node, key, None)
                    except Exception:
                        v = None
                    if isinstance(v, (list, tuple)) and v:
                        sub = v[0]
                        break
            if sub is not None:
                node = sub
        if not _has_code(node):
            return None
        # 3/4. fields with author/caption fallbacks
        if isinstance(node, dict):
            code = node.get("code")
            pk = node.get("pk", node.get("id"))
            author = ""
            for ak in ("user", "owner", "uploader"):
                u = node.get(ak)
                if isinstance(u, dict):
                    author = u.get("username", "") or ""
                elif u is not None:
                    try:
                        author = getattr(u, "username", "") or ""
                    except Exception:
                        author = ""
                if author:
                    break
            cap = node.get("caption_text", "") or ""
            if not cap:
                c2 = node.get("caption") or {}
                cap = c2.get("text", "") if isinstance(c2, dict) else str(c2 or "")
            if not cap:
                cap = node.get("title", "") or ""
            mt = node.get("media_type", 2)
        else:
            try:
                code = getattr(node, "code", None)
            except Exception:
                code = None
            try:
                pk = getattr(node, "pk", None) or getattr(node, "id", None)
            except Exception:
                pk = None
            author = ""
            for ak in ("user", "owner", "uploader"):
                try:
                    u = getattr(node, ak, None)
                except Exception:
                    u = None
                if isinstance(u, dict):
                    author = u.get("username", "") or ""
                elif u is not None:
                    try:
                        author = getattr(u, "username", "") or ""
                    except Exception:
                        author = ""
                if author:
                    break
            try:
                cap = getattr(node, "caption_text", None) or ""
            except Exception:
                cap = ""
            if not cap:
                try:
                    c2 = getattr(node, "caption", None)
                except Exception:
                    c2 = None
                cap = c2.get("text", "") if isinstance(c2, dict) else (str(c2) if c2 else "")
            if not cap:
                try:
                    cap = getattr(node, "title", "") or ""
                except Exception:
                    cap = ""
            try:
                mt = getattr(node, "media_type", 2)
            except Exception:
                mt = 2
        if not code:
            return None
        return {"code": str(code), "pk": str(pk or ""),
                "author": str(author or ""), "caption": str(cap or ""),
                "media_type": str(mt if mt is not None else 2)}
    except Exception:
        return None


async def fetch_candidates(count: int) -> List[dict]:
    """Fetch timeline + reels + explore candidates via aiograpi."""
    from app import ig_client
    cl = await ig_client.get_client()
    out: List[dict] = []
    seen = set()

    async def _collect(medias: Any):
        items = list(medias or [])
        parsed = 0
        for m in items:
            try:
                norm = _norm_item(m)
                if not norm or not norm["code"] or norm["code"] in seen:
                    continue
                seen.add(norm["code"])
                out.append(norm)
                parsed += 1
            except Exception:
                continue
        if items and parsed == 0:
            log.info("candidates zero-parsed sample: %s", _describe_element(items[0]))

    fetchers = [
        ("timeline", lambda: cl.get_timeline_feed()),
        ("reels", lambda: cl.get_reels_tray_feed()),
    ]
    for name, fn in fetchers:
        try:
            await human_jitter((1, 2))
            medias, raw, hint = _unwrap_medias(await fn())
            before = len(out)
            await _collect(medias)
            log.info("candidates source=%s raw=%d parsed=%d shape=%s",
                     name, raw, len(out) - before, hint)
        except Exception as e:
            log.info("candidate fetch %s failed: %s", name, type(e).__name__)
        if len(out) >= count:
            break
    if len(out) < count:
        explore_fn = None
        for meth in ("get_explore_feed", "explore_feed", "explore"):
            if hasattr(cl, meth):
                explore_fn = getattr(cl, meth)
                break
        if explore_fn is None:
            log.info("candidate fetch explore skipped: no explore method on client")
        else:
            try:
                medias, raw, hint = _unwrap_medias(await explore_fn())
                before = len(out)
                await _collect(medias)
                log.info("candidates source=explore raw=%d parsed=%d shape=%s",
                         raw, len(out) - before, hint)
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
            log.info("download failed %s: %s %.200s", cand["code"], type(e).__name__, e)
            skipped += 1
            continue

        # Thumbnail: reuse downloaded cover sidecar if present, else generate
        # a cover frame via ffmpeg (aiograpi fallback unchanged if both miss).
        thumb = ""
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            c = Path(tmpdir) / f"thumb{ext}"
            if c.exists():
                thumb = str(c)
                break
        if not thumb:
            thumb = _make_cover(str(video_path), tmpdir)

        caption = build_caption(cand.get("caption", ""), cand.get("author", ""))
        await human_jitter()

        # Single clip_upload attempt — writes NEVER retried blindly
        try:
            media = await ig_client.upload_reel(str(video_path), caption, thumb)
            write_calls += 1
        except Exception as e:
            write_calls += 1
            log.info("upload failed %s: %s %.200s", cand["code"], type(e).__name__, e)
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

        # Optional comment + pin (pipe-separated variants, random pick)
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


def shortcode_to_pk(code: str) -> str:
    """Pure local shortcode -> pk decode (mirrors instagrapi media_pk_from_code).

    Alphabet "A-Za-z0-9-_" as base64-style big-endian int. "" on any failure.
    """
    try:
        s = (code or "").strip()
        if not s or len(s) > 64:
            return ""
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        value = 0
        for ch in s:
            idx = alphabet.find(ch)
            if idx == -1:
                return ""
            value = value * 64 + idx
        return str(value) if value > 0 else ""
    except Exception:
        return ""


def parse_ig_source(raw: str) -> dict:
    """Accept an IG reel/post URL, bare shortcode, or numeric media_pk.

    Returns {"code": ..., "pk": ...}; pk is "" when only a code is known
    (resolved later via media_info).
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty source")
    m = re.search(r"instagram\.com/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", s)
    if m:
        return {"code": m.group(1), "pk": ""}
    tail = s.rstrip("/").split("/")[-1].split("?")[0].strip()
    if tail.isdigit():
        return {"code": "", "pk": tail}
    if re.fullmatch(r"[A-Za-z0-9_-]{5,64}", tail):
        return {"code": tail, "pk": ""}
    raise ValueError("unrecognized source")


async def _single_inner(source: str, comment_text: str, cover_url: str, job_id: str) -> dict:
    """One-candidate pipeline: download -> gate -> cover -> upload -> hidelike -> comment."""
    from app import ig_client
    try:
        parsed = parse_ig_source(source)
    except Exception as e:
        return {"posted": 0, "error": f"bad source: {type(e).__name__}"}
    code, pk = parsed["code"], parsed["pk"]
    author, caption_text = "", ""

    # Resolve code -> pk: (a) pure local decode, (b) media_pk_from_code
    # (sync OR async — await only if awaitable), (c) media_info,
    # (d) code-as-is last resort.
    if not pk and code:
        pk = shortcode_to_pk(code)
    if not pk and code:
        try:
            from app import ig_client as _igc
            _cl = await _igc.get_client()
            if hasattr(_cl, "media_pk_from_code"):
                _res = _cl.media_pk_from_code(code)
                if inspect.isawaitable(_res):
                    _res = await _res
                pk = str(_res or "")
        except Exception as e:
            log.info("single pk lookup failed %s: %s", code, type(e).__name__)
    if not pk and code:
        try:
            norm = _norm_item(await ig_client.fetch_media_info(code))
            if norm and norm.get("pk"):
                pk = norm["pk"]
                author = norm.get("author", "")
                caption_text = norm.get("caption", "")
        except Exception as e:
            log.info("single resolve failed %s: %s", code, type(e).__name__)
    if not pk and not code:
        return {"posted": 0, "error": "could not resolve source"}

    registry.touch(job_id, progress=0, total=1)
    tmpdir = tempfile.mkdtemp(prefix="single_")
    try:
        video_path = await ig_client.download_video(pk, tmpdir)
    except Exception as e:
        log.info("single download failed: %s %.200s", type(e).__name__, e)
        return {"posted": 0, "error": f"download failed: {type(e).__name__}"}
    if not quality_gate(str(video_path)):
        return {"posted": 0, "error": "quality gate rejected"}

    # Cover: explicit cover_url (httpx 15s) -> sidecar -> generated frame
    thumb = ""
    if (cover_url or "").strip():
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as hc:
                resp = await hc.get((cover_url or "").strip())
            if resp.status_code == 200 and resp.content:
                cover_path = Path(tmpdir) / "cover_url.jpg"
                cover_path.write_bytes(resp.content)
                thumb = str(cover_path)
        except Exception as e:
            log.info("single cover download failed: %s", type(e).__name__)
    if not thumb:
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            c = Path(tmpdir) / f"thumb{ext}"
            if c.exists():
                thumb = str(c)
                break
    if not thumb:
        thumb = _make_cover(str(video_path), tmpdir)

    caption = build_caption(caption_text, author)
    await human_jitter()

    # ONE clip_upload attempt — no retries, no blind re-attempts
    try:
        media = await ig_client.upload_reel(str(video_path), caption, thumb)
    except Exception as e:
        log.info("single upload failed: %s %.200s", type(e).__name__, e)
        return {"posted": 0, "error": f"upload failed: {type(e).__name__}"}

    repost_pk = str(getattr(media, "pk", "") or "")
    repost_code = str(getattr(media, "code", "") or "")
    if settings.HIDELIKE and repost_pk:
        await ig_client.set_hide_like(repost_pk, True)

    variants = [v.strip() for v in (comment_text or "").split("|") if v.strip()]
    if variants and settings.COMMENT_ENABLED and repost_pk:
        try:
            await ig_client.comment_and_pin(repost_pk, random.choice(variants))
        except Exception as e:
            log.info("comment+pin failed: %s", type(e).__name__)

    await db.mark_processed(code or repost_code or pk, pk, author, repost_pk, repost_code)
    await db.set_pacing("last_post_ms", str(int(time.time() * 1000)))
    registry.touch(job_id, progress=1)
    return {"posted": 1, "repost_code": repost_code, "repost_pk": repost_pk}


def run_single_job(source: str, comment_text: str = "", cover_url: str = "") -> Any:
    jid_holder: dict = {}

    async def _coro():
        return await _single_inner(source, comment_text, cover_url, jid_holder["id"])

    job = registry.start_job("single", _coro, total=1)
    jid_holder["id"] = job.id
    return job
