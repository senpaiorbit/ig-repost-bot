"""aiograpi wrapper: login flow, session cache, uploads, archive, comments.

Login order:
  1. INSTAGRAM_SESSIONID cookies first (bypasses 2FA)
  2. Turso-cached session via set_settings BEFORE login
  3. Fresh user/pass login, passing verification_code UP FRONT (one attempt only)
     2FA resolution: one-shot code -> TOTP provider -> local seed.
429 during login -> 45min (RATE_LOGIN_BLOCK_MIN) breaker persisted in Turso.
Verifies user_id after every reconnect.
"""
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from app.config import settings

log = logging.getLogger("instaward.ig")

try:
    from aiograpi import Client  # type: ignore
    from aiograpi.exceptions import (  # type: ignore
        ClientThrottledError,
        PleaseWaitFewMinutes,
        RateLimitError,
    )
    _AIOGRAPI = True
except ImportError:  # pragma: no cover - allows import without deps installed
    Client = None  # type: ignore

    class ClientThrottledError(Exception):
        pass

    class PleaseWaitFewMinutes(Exception):
        pass

    class RateLimitError(Exception):
        pass

    _AIOGRAPI = False

RATE_LIMIT_EXCS = (ClientThrottledError, PleaseWaitFewMinutes, RateLimitError)


class ThrottledError(Exception):
    def __init__(self, message: str = "login throttled", retry_after_sec: int = 0):
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


_client: Any = None
_client_lock = asyncio.Lock()


def _delay_range():
    lo, hi = getattr(settings, "IG_CALL_JITTER_SEC", (1.0, 3.0))
    try:
        return [float(lo), float(hi)]
    except (ValueError, TypeError):
        return [1, 3]


def _new_client() -> Any:
    if not _AIOGRAPI or Client is None:
        raise RuntimeError("aiograpi is not installed")
    cl = Client(delay_range=_delay_range())  # type: ignore[operator]
    # Keep delays jittered on every call
    try:
        cl.delay_range = _delay_range()
    except Exception:
        pass
    return cl


def _totp_now(seed: str) -> Optional[str]:
    seed = (seed or "").strip().replace(" ", "")
    if not seed:
        return None
    try:
        import pyotp
        return pyotp.TOTP(seed).now()
    except Exception:
        return None


async def _provider_code() -> Optional[str]:
    """TOTP Worker code (step-3 source b). None when unconfigured/failing."""
    try:
        from app import totp_client
        return await totp_client.fetch_code()
    except Exception as e:
        log.info("totp provider unavailable: %s", type(e).__name__)
        return None


async def _breaker_blocked() -> int:
    """Return remaining block seconds, 0 if not blocked."""
    from app.db import db
    try:
        state = await db.get_login_breaker()
    except Exception:
        return 0
    now_ms = int(time.time() * 1000)
    remaining = int(state.get("blocked_until", 0) or 0) - now_ms
    return max(0, remaining // 1000)


async def _trip_breaker() -> int:
    from app.db import db
    block_sec = int(settings.RATE_LOGIN_BLOCK_MIN) * 60
    now_ms = int(time.time() * 1000)
    try:
        state = await db.get_login_breaker()
        fails = int(state.get("failure_count", 0) or 0) + 1
    except Exception:
        fails = 1
    try:
        await db.set_login_breaker(now_ms + block_sec * 1000, fails)
    except Exception as e:
        log.warning("breaker persist failed: %s", type(e).__name__)
    return block_sec


def _is_rate_limit(exc: BaseException) -> bool:
    if isinstance(exc, RATE_LIMIT_EXCS):
        return True
    msg = f"{type(exc).__name__} {exc}".lower()
    return ("429" in msg or "throttl" in msg or "please wait" in msg
            or "rate limit" in msg or "rate_limit" in msg)


async def login(username: str = "", password: str = "", sessionid: str = "",
                totp_seed: str = "", twofa_code: str = "") -> int:
    """Full login flow. Returns user_id. Raises ThrottledError on breaker/429."""
    from app.db import db
    username = username or settings.INSTAGRAM_USERNAME
    password = password or settings.INSTAGRAM_PASSWORD
    sessionid = sessionid or settings.INSTAGRAM_SESSIONID
    totp_seed = totp_seed or settings.INSTAGRAM_TOTP_SEED
    twofa_code = twofa_code or settings.INSTAGRAM_2FA_CODE

    remaining = await _breaker_blocked()
    if remaining > 0:
        raise ThrottledError("login circuit breaker active", retry_after_sec=remaining)

    global _client
    async with _client_lock:
        cl = _new_client()
        csrftoken = settings.INSTAGRAM_CSRFTOKEN
        ds_user_id = settings.INSTAGRAM_DS_USER_ID

        # 1. Sessionid cookies first (bypasses 2FA)
        if sessionid and csrftoken and ds_user_id:
            try:
                cl.set_settings({
                    "authorization_data": {"sessionid": sessionid},
                    "cookies": {"sessionid": sessionid, "csrftoken": csrftoken,
                                "ds_user_id": ds_user_id},
                })
                await cl.get_timeline_feed()
                uid = int(cl.user_id or ds_user_id or 0)
                try:
                    await db.save_session(username, cl.get_settings())
                except Exception as e:
                    log.warning("session save failed: %s", type(e).__name__)
                try:
                    await db.clear_login_breaker()
                except Exception:
                    pass
                _client = cl
                return uid
            except Exception as e:
                if _is_rate_limit(e):
                    sec = await _trip_breaker()
                    raise ThrottledError("throttled during cookie login",
                                         retry_after_sec=sec) from e
                log.info("cookie login failed, falling through: %s", type(e).__name__)

        # 2. Cached session from Turso (set_settings BEFORE login)
        try:
            cached = await db.get_session(username)
        except Exception:
            cached = None
        if cached:
            try:
                cl.set_settings(cached)
                # Respect TTL: stale cache still tried, failure falls through
                await cl.get_timeline_feed()
                uid = int(cl.user_id or 0)
                if uid:
                    _client = cl
                    return uid
            except Exception as e:
                if _is_rate_limit(e):
                    sec = await _trip_breaker()
                    raise ThrottledError("throttled during cached-session check",
                                         retry_after_sec=sec) from e
                log.info("cached session invalid, fresh login: %s", type(e).__name__)
                cl = _new_client()

        # 3. Fresh login — exactly ONE attempt, 2FA code passed UP FRONT.
        # Resolution order: (a) one-shot code, (b) TOTP provider, (c) local seed.
        twofa = ((twofa_code or "").strip()
                 or await _provider_code()
                 or _totp_now(totp_seed))
        # Optional full session-state restore before login
        if settings.INSTAGRAM_SESSION_STATE:
            try:
                cl.set_settings(json.loads(settings.INSTAGRAM_SESSION_STATE))
            except Exception:
                pass
        try:
            if twofa:
                await cl.login(username, password, verification_code=str(twofa).strip())
            else:
                await cl.login(username, password)
        except Exception as e:
            if _is_rate_limit(e):
                sec = await _trip_breaker()
                raise ThrottledError("throttled during login",
                                     retry_after_sec=sec) from e
            raise
        uid = int(cl.user_id or 0)
        try:
            await db.save_session(username, cl.get_settings())
        except Exception as e:
            log.warning("session save failed: %s", type(e).__name__)
        # Consume one-shot codes after success (env-managed; just log, never print value)
        if twofa_code:
            log.info("one-shot 2FA code consumed")
        try:
            await db.clear_login_breaker()
        except Exception:
            pass
        _client = cl
        return uid


async def reconnect() -> int:
    """Reconnect using cached client if valid, else full login. Verifies user_id."""
    global _client
    if _client is not None:
        try:
            await _client.get_timeline_feed()
            uid = int(_client.user_id or 0)
            if uid:
                return uid
        except Exception as e:
            if _is_rate_limit(e):
                sec = await _trip_breaker()
                raise ThrottledError("throttled during reconnect check",
                                     retry_after_sec=sec) from e
    uid = await login()
    if not uid:
        raise RuntimeError("reconnect failed: no user_id")
    # Verify expected account when DS_USER_ID is configured
    if settings.INSTAGRAM_DS_USER_ID:
        try:
            if int(uid) != int(settings.INSTAGRAM_DS_USER_ID):
                raise RuntimeError(
                    "session user_id mismatch vs INSTAGRAM_DS_USER_ID")
        except ValueError:
            pass
    return uid


async def get_client() -> Any:
    global _client
    if _client is None:
        await reconnect()
    return _client


async def upload_reel(video_path: str, caption: str, thumbnail_path: str = "") -> Any:
    cl = await get_client()
    cl.delay_range = _delay_range()
    # Guarded ffmpeg setup: aiograpi shells out to `ffmpeg` when analyzing
    # clips/generating thumbnails; Render's Python image has none. Point env
    # lookups at imageio-ffmpeg's static binary when available. Never log paths.
    try:
        import imageio_ffmpeg  # type: ignore
        import os as _os
        _exe = imageio_ffmpeg.get_ffmpeg_exe()
        _os.environ["IMAGEIO_FFMPEG_EXE"] = _exe
        _os.environ["FFMPEG_BINARY"] = _exe
        _dir = _os.path.dirname(_exe)
        if _dir and _dir not in _os.environ.get("PATH", "").split(_os.pathsep):
            _os.environ["PATH"] = _dir + _os.pathsep + _os.environ.get("PATH", "")
    except Exception:
        pass
    kwargs: dict = {}
    if thumbnail_path:
        kwargs["thumbnail"] = Path(thumbnail_path)
    media = await cl.clip_upload(Path(video_path), caption, **kwargs)
    return media


async def download_video(pk: str, folder: str) -> Path:
    cl = await get_client()
    Path(folder).mkdir(parents=True, exist_ok=True)
    result = await cl.video_download(int(pk), folder=Path(folder))
    return Path(result) if not isinstance(result, Path) else result


async def fetch_media_info(pk: str) -> Any:
    cl = await get_client()
    return await cl.media_info(int(pk))


async def archive_media(media_pk: str) -> bool:
    cl = await get_client()
    await cl.media_archive(int(media_pk))
    return True


async def delete_media(media_pk: str) -> bool:
    cl = await get_client()
    await cl.media_delete(int(media_pk))
    return True


async def set_hide_like(media_pk: str, hide: bool = True) -> bool:
    """Best-effort hide-like on a media. Never raises.

    aiograpi/instagrapi signatures differ across builds: most accept
    media_hide_likes(media_id) with no flag, some accept a revert flag,
    and some expose it only via media_edit(...). Probe each variant and
    swallow TypeError mismatches so uploads never fail on this step.
    """
    try:
        cl = await get_client()
    except Exception as e:
        log.info("hidelike no client: %s", type(e).__name__)
        return False
    if hide and hasattr(cl, "media_hide_likes"):
        for args in ((int(media_pk),), (int(media_pk), False), (int(media_pk), True)):
            try:
                await cl.media_hide_likes(*args)
                return True
            except TypeError:
                continue
            except Exception as e:
                log.info("hidelike failed: %s", type(e).__name__)
                return False
    if not hide and hasattr(cl, "media_unhide_likes"):
        try:
            await cl.media_unhide_likes(int(media_pk))
            return True
        except Exception as e:
            log.info("hidelike failed: %s", type(e).__name__)
            return False
    if hasattr(cl, "media_edit"):
        for kwargs in ({"hide_like": hide}, {"like_and_view_counts_disabled": hide}):
            try:
                await cl.media_edit(int(media_pk), **kwargs)
                return True
            except TypeError:
                continue
            except Exception as e:
                log.info("hidelike failed: %s", type(e).__name__)
                return False
    log.info("hidelike unsupported on client")
    return False


async def comment_and_pin(media_pk: str, text: str) -> bool:
    cl = await get_client()
    comment = await cl.media_comment(int(media_pk), str(text))
    try:
        pk = getattr(comment, "pk", None) or (comment.get("pk") if isinstance(comment, dict) else None)
        if pk:
            await cl.comment_pin(int(media_pk), int(pk))
    except Exception as e:
        log.info("pin failed (comment kept): %s", type(e).__name__)
    return True
