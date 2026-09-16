"""App config via pydantic-settings."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    API_KEY: str = "changeme"
    IG_USERNAME: str = ""
    IG_PASSWORD: str = ""
    IG_TOTP_SEED: str = ""
    IG_SESSIONID: str = ""
    TURSO_DATABASE_URL: str = ""
    TURSO_AUTH_TOKEN: str = ""
    COVER_IMAGE_URL: str = "https://i.ibb.co/sp6WvzJK/1.jpg"
    THUMBNAIL_URL: str = ""
    COMMENT_TEXT: str = "follow me 🔥"
    COMMENT_ENABLED: int = 0
    SESSION_FILE: str = "session.json"
    MAX_UPLOADS_PER_DAY: int = 2
    IG_PROXY: str = ""
    AUTO_FEED_LIMIT: int = 20
    LOG_LEVEL: str = "INFO"
    TOTP_PROVIDER_URL: str = "https://ig-totp.tanbirst2st2.workers.dev"


settings = Settings()
