"""Instagram client built on aiograpi (async API, instagrapi-compatible)."""
import asyncio
import inspect
import random
import tempfile
from pathlib import Path

import httpx
import pyotp
from aiograpi import Client

from app.config import settings


async def _call(fn, *args, **kwargs):
    res = fn(*args, **kwargs)
    if inspect.isawaitable(res):
        return await res
    return res


async def _maybe_thread(fn, *args, **kwargs):
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)


class IGClient:
    def __init__(self):
        self.cl = Client()
        try:
            self.cl.delay_range = [2, 5]
        except Exception:
            pass
        proxy = ""
        try:
            proxy = (getattr(settings, "IG_PROXY", "") or "").strip()
        except Exception:
            proxy = ""
        if proxy:
            try:
                self.cl.set_proxy(proxy)
            except Exception as e:
                print(f"[ig] set_proxy failed: {e}")
        self.username = settings.IG_USERNAME
        self.password = settings.IG_PASSWORD
        self.sessionid = settings.IG_SESSIONID
        self.totp_seed = settings.IG_TOTP_SEED

    def _session_path(self) -> str:
        try:
            return (getattr(settings, "SESSION_FILE", "") or "").strip() or "session.json"
        except Exception:
            return "session.json"

    async def _load_session(self):
        try:
            load_fn = getattr(self.cl, "load_settings", None)
            if load_fn is None:
                return
            path = self._session_path()
            if inspect.iscoroutinefunction(load_fn):
                await load_fn(path)
            else:
                await asyncio.to_thread(load_fn, path)
            print("[ig] session loaded")
        except Exception as e:
            print(f"[ig] load_settings skipped: {e}")

    async def _dump_session(self):
        try:
            dump_fn = getattr(self.cl, "dump_settings", None)
            if dump_fn is None:
                return
            path = self._session_path()
            if inspect.iscoroutinefunction(dump_fn):
                await dump_fn(path)
            else:
                await asyncio.to_thread(dump_fn, path)
            print("[ig] session saved")
        except Exception as e:
            print(f"[ig] dump_settings skipped: {e}")

    def _totp_code(self):
        if self.totp_seed:
            try:
                return pyotp.TOTP(self.totp_seed).now()
            except Exception as e:
                print(f"[ig] TOTP failed: {e}")
        return None

    async def login(self):
        await self._load_session()
        if self.sessionid:
            try:
                fn = getattr(self.cl, "login_by_sessionid", None)
                if fn is None:
                    raise AttributeError("login_by_sessionid missing")
                try:
                    if inspect.iscoroutinefunction(fn):
                        await fn(self.sessionid)
                    else:
                        await asyncio.to_thread(fn, self.sessionid)
                except TypeError:
                    await fn(self.sessionid)
                print("[ig] logged in via sessionid")
                await self._dump_session()
                return True
            except Exception as e:
                print(f"[ig] sessionid login failed, falling back: {e}")
        code = self._totp_code()
        kwargs = {}
        if code:
            kwargs["verification_code"] = code
        login_fn = self.cl.login
        try:
            if inspect.iscoroutinefunction(login_fn):
                await login_fn(self.username, self.password, **kwargs)
            else:
                await asyncio.to_thread(login_fn, self.username, self.password, **kwargs)
        except TypeError as e:
            print(f"[ig] login TypeError, retrying without code: {e}")
            if inspect.iscoroutinefunction(login_fn):
                await login_fn(self.username, self.password)
            else:
                await asyncio.to_thread(login_fn, self.username, self.password)
        print("[ig] logged in via username/password")
        await self._dump_session()
        return True

    async def resolve_media_pk(self, target_url: str):
        return await _maybe_thread(self.cl.media_pk_from_url, target_url)

    async def download_video(self, target_url: str, dest_dir) -> Path:
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        pk = await self.resolve_media_pk(target_url)
        last_err = None
        for name in ("video_download", "clip_download"):
            fn = getattr(self.cl, name, None)
            if fn is None:
                continue
            try:
                if inspect.iscoroutinefunction(fn):
                    res = await fn(pk, folder=dest)
                else:
                    res = await asyncio.to_thread(fn, pk, folder=dest)
                if res is not None:
                    return Path(res)
                vids = sorted(dest.glob("*.mp4"), key=lambda p: p.stat().st_mtime if p.exists() else 0)
                if vids:
                    return vids[-1]
                return dest
            except Exception as e:
                print(f"[ig] {name} failed: {e}")
                last_err = e
                continue
        raise RuntimeError(f"download_video failed: {last_err}")

    async def download_cover(self, url: str | None = None) -> Path:
        src = url or settings.COVER_IMAGE_URL
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.get(src)
            r.raise_for_status()
            data = r.content
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(data)
        tmp.close()
        return Path(tmp.name)

    @staticmethod
    def extract_media_id(media) -> str:
        for attr in ("pk", "id"):
            v = getattr(media, attr, None)
            if v is not None:
                return str(v)
        if isinstance(media, dict):
            for k in ("pk", "id", "media_id"):
                if k in media:
                    return str(media[k])
            return str(media)
        return str(media)

    async def upload_reel(self, video_path, caption: str, thumbnail_path=None):
        fn = self.cl.clip_upload
        if inspect.iscoroutinefunction(fn):
            return await fn(str(video_path), caption, thumbnail=thumbnail_path)
        return await asyncio.to_thread(fn, str(video_path), caption, thumbnail=thumbnail_path)

    async def post_actions(self, media_id, comment_text: str | None = None):
        hide_fn = getattr(self.cl, "media_hide_like", None)
        if hide_fn is not None:
            try:
                if inspect.iscoroutinefunction(hide_fn):
                    await hide_fn(media_id)
                else:
                    await asyncio.to_thread(hide_fn, media_id)
            except Exception as e:
                print(f"[ig] media_hide_like failed: {e}")
        explicit = comment_text is not None and str(comment_text).strip() != ""
        enabled = bool(getattr(settings, "COMMENT_ENABLED", 1))
        if not (enabled or explicit):
            return
        effective = (str(comment_text).strip() if explicit else (settings.COMMENT_TEXT or "").strip())
        if not effective:
            return
        await asyncio.sleep(random.uniform(2, 6))
        comment_fn = getattr(self.cl, "media_comment", None)
        if comment_fn is not None:
            try:
                if inspect.iscoroutinefunction(comment_fn):
                    await comment_fn(media_id, effective)
                else:
                    await asyncio.to_thread(comment_fn, media_id, effective)
            except Exception as e:
                print(f"[ig] media_comment failed: {e}")

    async def get_views(self, media_id) -> int:
        try:
            info_fn = getattr(self.cl, "media_info", None)
            if info_fn is None:
                return 0
            if inspect.iscoroutinefunction(info_fn):
                info = await info_fn(media_id)
            else:
                info = await asyncio.to_thread(info_fn, media_id)
            for attr in ("view_count", "play_count", "like_count"):
                v = getattr(info, attr, None)
                if isinstance(v, (int, float)) and v:
                    return int(v)
            if isinstance(info, dict):
                for k in ("view_count", "play_count", "like_count"):
                    v = info.get(k)
                    if isinstance(v, (int, float)) and v:
                        return int(v)
            return 0
        except Exception as e:
            print(f"[ig] get_views failed: {e}")
            return 0

    async def archive_or_delete(self, media_id) -> str:
        arch_fn = getattr(self.cl, "media_archive", None)
        if arch_fn is not None:
            try:
                if inspect.iscoroutinefunction(arch_fn):
                    await arch_fn(media_id)
                else:
                    await asyncio.to_thread(arch_fn, media_id)
                return "archived"
            except Exception as e:
                print(f"[ig] media_archive failed: {e}")
        del_fn = getattr(self.cl, "media_delete", None)
        if del_fn is not None:
            if inspect.iscoroutinefunction(del_fn):
                await del_fn(media_id)
            else:
                await asyncio.to_thread(del_fn, media_id)
            return "deleted"
        raise RuntimeError("neither media_archive nor media_delete available")

    async def check_login(self):
        for name in ("account_info", "user_info", "get_timeline_feed"):
            fn = getattr(self.cl, name, None)
            if fn is None:
                continue
            try:
                if inspect.iscoroutinefunction(fn):
                    info = await fn()
                else:
                    await asyncio.to_thread(fn)
                    info = None
                if info is not None:
                    uname = getattr(info, "username", None)
                    if uname:
                        return uname
                    if isinstance(info, dict) and info.get("username"):
                        return str(info["username"])
                if self.username:
                    return self.username
                return "ok"
            except Exception as e:
                print(f"[ig] {name} failed: {e}")
                continue
        return self.username or "ok"
