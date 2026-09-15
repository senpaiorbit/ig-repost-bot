"""Pydantic models for requests/responses."""
from typing import Any, List, Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    ok: bool = True
    cronjob: int = 0


class TursoCheckResponse(BaseModel):
    ok: bool
    hint: Optional[str] = None
    detail: Optional[str] = None


class ReconnectResponse(BaseModel):
    ok: bool = True
    user_id: Optional[int] = None
    username: Optional[str] = None
    background: bool = False
    job_id: Optional[str] = None
    cronjob: int = 0


class LoginStatusResponse(BaseModel):
    blocked: bool = False
    blocked_until_ms: int = 0
    retry_after_sec: int = 0
    failure_count: int = 0


class UploadResponse(BaseModel):
    ok: bool = True
    job_id: str
    kind: str = "upload"
    amount: int = 1
    cronjob: int = 0
    note: Optional[str] = None


class ArchiveResponse(BaseModel):
    ok: bool = True
    job_id: str
    kind: str = "archive"
    cronjob: int = 0
    note: Optional[str] = None


class JobResponse(BaseModel):
    id: str
    kind: str
    status: str
    progress: int = 0
    total: int = 0
    result: Optional[Any] = None
    error: Optional[str] = None
    note: Optional[str] = None


class LiveRow(BaseModel):
    code: str
    source_username: str = ""
    source_pk: str = ""
    repost_code: Optional[str] = None
    repost_pk: Optional[str] = None
    posted_at: int = 0
    archived: int = 0


class LiveResponse(BaseModel):
    ok: bool = True
    rows: List[LiveRow] = Field(default_factory=list)
