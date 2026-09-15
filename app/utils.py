"""Helpers: jitter, caption, duration parsing, constant-time compare."""
import asyncio
import hmac
import random
import re

from app.config import settings


async def human_jitter(range_sec=None) -> None:
    lo, hi = range_sec or getattr(settings, "IG_CALL_JITTER_SEC", (1.0, 3.0))
    try:
        lo, hi = float(lo), float(hi)
    except (ValueError, TypeError):
        lo, hi = 1.0, 3.0
    if hi < lo:
        lo, hi = hi, lo
    await asyncio.sleep(random.uniform(lo, hi))


def build_caption(original_caption: str, author: str, limit: int = 2000) -> str:
    original = (original_caption or "").strip()
    credit = f"Credit=@{author}" if author else "Credit=@unknown"
    if not original:
        return credit[:limit]
    full = f"{original}\n{credit}"
    if len(full) <= limit:
        return full
    # Truncate original so credit line always survives
    keep = limit - len(credit) - 1
    if keep <= 0:
        return credit[:limit]
    return original[:keep].rstrip() + "\n" + credit


def parse_duration(raw: str, default_sec: int = 24 * 3600) -> int:
    """Parse '24h', '30m', '7d', '90s' or plain seconds -> seconds."""
    if raw is None:
        return default_sec
    s = str(raw).strip().lower()
    if not s:
        return default_sec
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd]?)", s)
    if not m:
        return default_sec
    val = float(m.group(1))
    unit = m.group(2) or "s"
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return int(val * mult)


def constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(str(a or ""), str(b or ""))
