"""TOTP provider client (bot side of the ig-totp Worker).

Env-driven — read at CALL time (os.getenv inside functions, never at import)
so tests and late env changes work:
  TOTP_PROVIDER_URL  e.g. https://ig-totp.<sub>.workers.dev ("" = disabled)
  TOTP_KEY           per-slot tenant key
  TOTP_SLOT          slot label, default "default"

Single-flight + cache: a code is reused while its remaining validity is >5s
(module-level cache keyed by slot). Concurrent callers for the same slot
share one in-flight request.

Secrecy: NEVER log code/key/URL-with-key — log lengths/booleans only.
All failures -> None (silent fallback to local pyotp seed in ig_client).
"""
import asyncio
import logging
import os
import time
from typing import Dict, Optional, Tuple

log = logging.getLogger("instaward.totp")

TIMEOUT_SEC = 10.0
REUSE_MIN_SEC = 5  # reuse cached code while it stays valid longer than this

_CACHE: Dict[str, Tuple[str, float]] = {}  # slot -> (code, valid_until_epoch)
_INFLIGHT: Dict[str, asyncio.Event] = {}
_lock = asyncio.Lock()


def _creds() -> Tuple[str, str, str]:
    url = (os.getenv("TOTP_PROVIDER_URL", "") or "").strip().rstrip("/")
    key = os.getenv("TOTP_KEY", "") or ""
    slot = (os.getenv("TOTP_SLOT", "") or "").strip() or "default"
    return url, key, slot


async def _fetch_remote(url: str, key: str, slot: str) -> Tuple[str, int]:
    import httpx  # lazy: totp provider is optional
    async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
        resp = await client.get(url + "/code", params={"key": key, "slot": slot})
    if resp.status_code != 200:
        raise RuntimeError(f"provider status {resp.status_code}")
    data = resp.json()
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError("provider not ok")
    code = str(data.get("code", "") or "").strip()
    try:
        expires = int(data.get("expires_in_sec", 0) or 0)
    except (ValueError, TypeError):
        expires = 0
    if not code:
        raise RuntimeError("provider empty code")
    return code, expires


def clear_cache() -> None:
    _CACHE.clear()


async def fetch_code(slot: str = "") -> Optional[str]:
    """Return a current TOTP code from the provider, or None on any failure."""
    url, key, default_slot = _creds()
    slot = (slot or "").strip() or default_slot
    if not url or not key:
        return None

    async with _lock:
        hit = _CACHE.get(slot)
        if hit and hit[1] - time.time() > REUSE_MIN_SEC:
            log.info("totp cache hit (slot_len=%d)", len(slot))
            return hit[0]
        existing = _INFLIGHT.get(slot)
        if existing is not None:
            event = existing
            is_waiter = True
        else:
            event = asyncio.Event()
            _INFLIGHT[slot] = event
            is_waiter = False

    if is_waiter:
        try:
            await asyncio.wait_for(event.wait(), timeout=TIMEOUT_SEC + 5)
        except Exception:
            return None
        async with _lock:
            hit = _CACHE.get(slot)
            if hit and hit[1] - time.time() > 0:
                return hit[0]
            return None

    try:
        code, expires = await _fetch_remote(url, key, slot)
    except Exception as e:
        log.info("totp provider failed (has_url=%s): %s", bool(url), type(e).__name__)
        return None
    finally:
        async with _lock:
            _INFLIGHT.pop(slot, None)
            event.set()

    async with _lock:
        _CACHE[slot] = (code, time.time() + max(0, expires))
    log.info("totp provider ok (slot_len=%d, expires_in=%ds)", len(slot), int(expires))
    return code
