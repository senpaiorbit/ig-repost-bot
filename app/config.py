"""Env loading, validation, defaults (prompt section 4 + DESIGN.md env table)."""
import os


def _get(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return v if v is not None else default


def _get_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip() or str(default))
    except (ValueError, TypeError):
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, str(default))).strip() or str(default))
    except (ValueError, TypeError):
        return default


def _parse_jitter(raw: str) -> tuple:
    try:
        parts = [p.strip() for p in str(raw).split(",") if p.strip() != ""]
        if len(parts) >= 2:
            return (float(parts[0]), float(parts[1]))
        if len(parts) == 1:
            return (float(parts[0]), float(parts[0]))
    except (ValueError, TypeError):
        pass
    return (1.0, 3.0)


class Settings:
    """Loads all env vars with sane defaults. Names only per prompt section 4."""

    def __init__(self) -> None:
        # Secrets (user-supplied, never invented)
        self.ENV_KEY: str = _get("ENV_KEY", "")
        self.INSTAGRAM_USERNAME: str = _get("INSTAGRAM_USERNAME", "")
        self.INSTAGRAM_PASSWORD: str = _get("INSTAGRAM_PASSWORD", "")
        self.INSTAGRAM_SESSIONID: str = _get("INSTAGRAM_SESSIONID", "")
        self.INSTAGRAM_CSRFTOKEN: str = _get("INSTAGRAM_CSRFTOKEN", "")
        self.INSTAGRAM_DS_USER_ID: str = _get("INSTAGRAM_DS_USER_ID", "")
        self.INSTAGRAM_SESSION_STATE: str = _get("INSTAGRAM_SESSION_STATE", "")
        self.INSTAGRAM_TOTP_SEED: str = _get("INSTAGRAM_TOTP_SEED", "")
        self.INSTAGRAM_2FA_CODE: str = _get("INSTAGRAM_2FA_CODE", "")
        self.TOTP_PROVIDER_URL: str = _get("TOTP_PROVIDER_URL", "")
        self.TOTP_KEY: str = _get("TOTP_KEY", "")
        self.TOTP_SLOT: str = _get("TOTP_SLOT", "default")
        self.TURSO_URL: str = _get("TURSO_URL", "")
        self.TURSO_AUTH_TOKEN: str = _get("TURSO_AUTH_TOKEN", "")
        self.TELEGRAM_BOT_TOKEN: str = _get("TELEGRAM_BOT_TOKEN", "")
        self.TELEGRAM_CHAT_ID: str = _get("TELEGRAM_CHAT_ID", "")

        # Non-secrets with defaults
        self.THUMBNAIL_URL: str = _get("THUMBNAIL_URL", "")
        self.LOG_LEVEL: str = _get("LOG_LEVEL", "INFO")
        self.MAX_PER_DAY: int = _get_int("MAX_PER_DAY", 10)
        self.FETCH_COUNT: int = _get_int("FETCH_COUNT", 30)
        self.BOTLOG: int = _get_int("BOTLOG", 1)
        self.HIDELIKE: int = _get_int("HIDELIKE", 1)
        self.IG_CALL_JITTER_SEC: tuple = _parse_jitter(_get("IG_CALL_JITTER_SEC", "1,3"))
        self.SESSION_REUSE_TTL_MIN: int = _get_int("SESSION_REUSE_TTL_MIN", 120)
        self.MIN_POST_INTERVAL_MIN: int = _get_int("MIN_POST_INTERVAL_MIN", 30)
        self.COMMENT_ENABLED: int = _get_int("COMMENT_ENABLED", 0)
        self.COMMENT_TEXT: str = _get("COMMENT_TEXT", "")
        self.ARCHIVE_TIME: str = _get("ARCHIVE_TIME", "24h")
        self.ARCHIVE_VIEWS: int = _get_int("ARCHIVE_VIEWS", 1000)
        self.ARCHIVE_BATCH: int = _get_int("ARCHIVE_BATCH", 50)
        self.QUALITY_MIN_WIDTH: int = _get_int("QUALITY_MIN_WIDTH", 720)
        self.QUALITY_MIN_HEIGHT: int = _get_int("QUALITY_MIN_HEIGHT", 1280)
        # Accept both QUALITY_MIN_SIZE_MB and QUALITY_* SIZE variants
        size_raw = os.getenv("QUALITY_MIN_SIZE_MB", os.getenv("QUALITY_MIN_SIZE", "1"))
        try:
            self.QUALITY_MIN_SIZE_MB: float = float(str(size_raw).strip() or "1")
        except (ValueError, TypeError):
            self.QUALITY_MIN_SIZE_MB = 1.0
        self.RATE_LOGIN_BLOCK_MIN: int = _get_int("RATE_LOGIN_BLOCK_MIN", 45)
        self.RATE_ENDPOINT_CAP: int = _get_int("RATE_ENDPOINT_CAP", 5)
        self.RATE_ENDPOINT_REFILL_SEC: int = _get_int("RATE_ENDPOINT_REFILL_SEC", 60)
        self.RATE_CRON_CAP: int = _get_int("RATE_CRON_CAP", 3)
        self.RATE_CRON_REFILL_SEC: int = _get_int("RATE_CRON_REFILL_SEC", 300)
        self.MAX_WRITE_CALLS: int = _get_int("MAX_WRITE_CALLS", 40)
        self.PORT: int = _get_int("PORT", 8000)


settings = Settings()
