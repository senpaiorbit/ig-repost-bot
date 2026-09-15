"""In-memory job registry with stale detection + hard timeouts."""
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

STALE_NO_PROGRESS_SEC = 8 * 60
UPLOAD_TIMEOUT_SEC = 7 * 60
ARCHIVE_TIMEOUT_SEC = 5 * 60

TIMEOUTS = {"upload": UPLOAD_TIMEOUT_SEC, "archive": ARCHIVE_TIMEOUT_SEC,
            "single": 7 * 60}


@dataclass
class Job:
    id: str
    kind: str
    status: str = "running"  # running | done | error | expired
    progress: int = 0
    total: int = 0
    result: Any = None
    error: Optional[str] = None
    note: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)
    task: Optional[asyncio.Task] = field(default=None, repr=False)


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._running_by_kind: Dict[str, str] = {}

    def running_job_of_kind(self, kind: str) -> Optional[Job]:
        jid = self._running_by_kind.get(kind)
        if not jid:
            return None
        job = self._jobs.get(jid)
        if job and job.status == "running":
            return job
        self._running_by_kind.pop(kind, None)
        return None

    def start_job(self, kind: str, coro_factory: Callable[[], Any], total: int = 0,
                  note: Optional[str] = None) -> Job:
        existing = self.running_job_of_kind(kind)
        if existing:
            # Concurrent same-kind jobs return already-running marker
            dup = Job(id=existing.id, kind=kind, status="running",
                      progress=existing.progress, total=existing.total,
                      note="already running")
            dup.task = existing.task
            return dup
        jid = uuid.uuid4().hex[:12]
        job = Job(id=jid, kind=kind, total=total, note=note)
        self._jobs[jid] = job
        self._running_by_kind[kind] = jid
        timeout = TIMEOUTS.get(kind)

        async def _runner():
            try:
                if timeout:
                    res = await asyncio.wait_for(coro_factory(), timeout=timeout)
                else:
                    res = await coro_factory()
                job.result = res
                job.status = "done"
            except asyncio.TimeoutError:
                job.status = "error"
                job.error = f"{kind} job timed out after {timeout}s"
            except asyncio.CancelledError:
                job.status = "error"
                job.error = f"{kind} job cancelled"
                raise
            except Exception as e:
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"
            finally:
                job.updated_at = time.monotonic()
                if self._running_by_kind.get(kind) == jid:
                    self._running_by_kind.pop(kind, None)

        try:
            loop = asyncio.get_running_loop()
            job.task = loop.create_task(_runner())
        except RuntimeError:
            job.task = asyncio.ensure_future(_runner())
        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        self.sweep_stale()
        return self._jobs.get(job_id)

    def touch(self, job_id: str, progress: Optional[int] = None, total: Optional[int] = None) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        job.updated_at = time.monotonic()
        if progress is not None:
            job.progress = progress
        if total is not None:
            job.total = total

    def sweep_stale(self) -> None:
        now = time.monotonic()
        for jid, job in list(self._jobs.items()):
            if job.status == "running" and (now - job.updated_at) > STALE_NO_PROGRESS_SEC:
                job.status = "expired"
                job.error = "expired: no progress for >8min"
                try:
                    if job.task and not job.task.done():
                        job.task.cancel()
                except Exception:
                    pass
                if self._running_by_kind.get(job.kind) == jid:
                    self._running_by_kind.pop(job.kind, None)

    def to_dict(self, job: Job) -> dict:
        return {
            "id": job.id, "kind": job.kind, "status": job.status,
            "progress": job.progress, "total": job.total,
            "result": job.result, "error": job.error, "note": job.note,
        }


registry = JobRegistry()
