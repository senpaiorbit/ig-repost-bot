"""Instagram client built on aiograpi (async API, instagrapi-compatible)."""
import asyncio
import inspect
import random
import re
import tempfile
from pathlib import Path
from typing import Optional

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
        self._video_urls: dict = {}
        self._pub = None

    def _pub_client(self):
        try:
            if self._pub is None:
                pub = Client()
                try:
                    pub.delay_range = [2, 5]
                except Exception:
                    pass
                self._pub = pub
            return self._pub
        except Exception as e:
            print(f"[ig] public client init failed: {e}")
            return None

    async def _pub_download(self, pk, dest_dir) -> Path:
        pub = self._pub_client()
        if pub is None:
            raise RuntimeError("no public client")
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        last_err = None
        for name in ("video_download", "clip_download"):
            fn = getattr(pub, name, None)
            if fn is None:
                continue
            try:
                res = await _maybe_thread(fn, pk, folder=dest)
                if res is not None:
                    print("[ig] public fallback ok")
                    return Path(res)
                vids = sorted(dest.glob("*.mp4"), key=lambda p: p.stat().st_mtime if p.exists() else 0)
                if vids:
                    print("[ig] public fallback ok")
                    return vids[-1]
                print("[ig] public fallback ok")
                return dest
            except Exception as e:
                print(f"[ig] public {name} failed: {e}")
                last_err = e
                continue
        raise RuntimeError(f"public download failed: {last_err}")

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
        if not self.totp_seed:
            return None
        try:
            base = (getattr(settings, "TOTP_PROVIDER_URL", "") or "").strip().rstrip("/")
        except Exception:
            base = ""
        if base:
            try:
                import re
                with httpx.Client(timeout=10) as client:
                    r = client.get(f"{base}/", params={"seed": self.totp_seed, "json": "1"})
                    r.raise_for_status()
                    data = r.json()
                if isinstance(data, dict) and isinstance(data.get("data"), dict):
                    data = data["data"]
                raw = ""
                if isinstance(data, dict):
                    for k in ("code", "totp", "otp", "token", "pin"):
                        if data.get(k) not in (None, ""):
                            raw = str(data[k])
                            break
                digits = "".join(re.findall(r"\d", raw))
                if len(digits) >= 6:
                    print("[ig] TOTP via provider")
                    return digits[:6]
                print(f"[ig] TOTP provider bad payload, falling back: {raw!r}")
            except Exception as e:
                print(f"[ig] TOTP provider failed, falling back: {e}")
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
                if inspect.iscoroutinefunction(fn):
                    await fn(self.sessionid)
                else:
                    await asyncio.to_thread(fn, self.sessionid)
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
        except TypeError:
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
        try:
            code_info = self._code_from_url(target_url)
        except Exception:
            code_info = None
        if code_info:
            try:
                return await self.download_via_embed(code_info[1], dest, kind=code_info[0])
            except Exception as e:
                print(f"[ig] embed download failed, falling back: {e}")
        pk = await self.resolve_media_pk(target_url)
        try:
            return await self.download_via_media_info(pk, dest)
        except Exception as e:
            print(f"[ig] media_info download failed: {e}")
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
        try:
            return await self._pub_download(pk, dest)
        except Exception as e:
            print(f"[ig] public fallback failed: {e}")
            if last_err is None:
                last_err = e
        raise RuntimeError(f"download_video failed: {last_err}")

    async def download_via_media_info(self, pk: int, dest_dir) -> Path:
        info = await _maybe_thread(self.cl.media_info, pk)
        try:
            urls = self._video_url_all(info)
        except Exception:
            urls = []
        if not urls:
            try:
                one = self._video_url(info)
            except Exception:
                one = None
            if one:
                urls = [one]
        if not urls:
            raise RuntimeError("media_info has no video url")
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"mi_{pk}.mp4"
        last_err = None
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for url in urls:
                try:
                    r = await client.get(url)
                    r.raise_for_status()
                    data = r.content
                except Exception as e:
                    last_err = e
                    continue
                ctype = (r.headers.get("content-type", "") or "").lower()
                if ("video" in ctype) or (len(data) > 100 * 1024):
                    out.write_bytes(data)
                    return out
                last_err = RuntimeError(f"media_info url rejected (ctype={ctype!r}, bytes={len(data)})")
        raise RuntimeError(f"media_info download failed: {last_err}")

    @staticmethod
    def _code_from_url(url) -> Optional[tuple]:
        try:
            m = re.search(r"/(reel|reels|p)/([^/?#]+)", str(url or ""))
        except Exception:
            return None
        if not m:
            return None
        kind = "reel" if m.group(1) in ("reel", "reels") else "p"
        return kind, m.group(2)

    async def download_via_embed(self, code: str, dest_dir, kind: str = "reel") -> Path:
        page_url = f"https://www.instagram.com/{kind}/{code}/embed/"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
            r = await client.get(page_url)
            if r.status_code != 200:
                raise RuntimeError(f"embed page HTTP {r.status_code}")
            m = re.search(r'"video_url":"(https://[^"]+\.mp4[^"]*)"', r.text)
            if not m:
                raise RuntimeError("embed page has no video_url")
            vurl = m.group(1).replace("\\/", "/").replace("\\u0026", "&")
            vr = await client.get(vurl)
            vr.raise_for_status()
            data = vr.content
            ctype = (vr.headers.get("content-type", "") or "").lower()
            if ("video" in ctype) or (len(data) > 100 * 1024):
                dest = Path(dest_dir)
                dest.mkdir(parents=True, exist_ok=True)
                out = dest / f"embed_{code}.mp4"
                out.write_bytes(data)
                return out
            raise RuntimeError(f"embed video rejected (ctype={ctype!r}, bytes={len(data)})")

    async def resolve_candidate(self, cand: str) -> str:
        return (cand or "").strip() if isinstance(cand, str) else cand

    async def download_candidate(self, cand: str, dest_dir) -> Path:
        if isinstance(cand, str) and cand.startswith("http"):
            try:
                code_info = self._code_from_url(cand)
            except Exception:
                code_info = None
            if code_info:
                try:
                    return await self.download_via_embed(code_info[1], dest_dir, kind=code_info[0])
                except Exception as e:
                    print(f"[ig] embed download failed, falling back: {e}")
        try:
            direct = (self._video_urls or {}).get(cand)
        except Exception:
            direct = None
        if direct:
            try:
                dest = Path(dest_dir)
                dest.mkdir(parents=True, exist_ok=True)
                slug = "".join(ch for ch in str(cand) if ch.isalnum())[-12:] or "vid"
                out = dest / f"cand_{slug}.mp4"
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                    r = await client.get(direct)
                    r.raise_for_status()
                    data = r.content
                ctype = (r.headers.get("content-type", "") or "").lower()
                if ("video" in ctype) or (len(data) > 100 * 1024):
                    out.write_bytes(data)
                    return out
                print(f"[ig] direct video url rejected (ctype={ctype!r}, bytes={len(data)})")
            except Exception as e:
                print(f"[ig] direct video download failed, falling back: {e}")
        if isinstance(cand, str) and cand.startswith("pk:"):
            dest = Path(dest_dir)
            dest.mkdir(parents=True, exist_ok=True)
            try:
                pk = int(cand[3:])
            except ValueError:
                raise RuntimeError(f"bad pk candidate: {cand}")
            try:
                to_code = getattr(self.cl, "media_pk_to_code", None) or getattr(self.cl, "media_id_to_code", None)
                if callable(to_code):
                    code = await _maybe_thread(to_code, pk)
                    if code and str(code).strip():
                        return await self.download_via_embed(str(code).strip(), dest)
            except Exception as e:
                print(f"[ig] pk->code/embed failed: {e}")
            try:
                return await self.download_via_media_info(pk, dest)
            except Exception as e:
                print(f"[ig] media_info download failed: {e}")
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
            try:
                return await self._pub_download(pk, dest)
            except Exception as e:
                print(f"[ig] public fallback failed: {e}")
                if last_err is None:
                    last_err = e
            raise RuntimeError(f"download_candidate failed: {last_err}")
        return await self.download_video(str(cand), dest_dir)

    @staticmethod
    def _cand_url(item) -> Optional[str]:
        try:
            if IGClient._is_ad(item):
                return None
        except Exception:
            pass
        code = None
        mtype = ""
        ptype = ""
        if isinstance(item, dict):
            code = item.get("code")
            mtype = str(item.get("media_type", "") or "")
            ptype = str(item.get("product_type", "") or "")
        else:
            code = getattr(item, "code", None)
            try:
                mtype = str(getattr(item, "media_type", "") or "")
            except Exception:
                mtype = ""
            try:
                ptype = str(getattr(item, "product_type", "") or "")
            except Exception:
                ptype = ""
        if code:
            blob = f"{mtype} {ptype}".lower()
            is_video = ("2" in blob) or any(w in blob for w in ("clip", "video", "reel", "igtv"))
            kind = "reel" if is_video else "p"
            return f"https://www.instagram.com/{kind}/{code}/"
        return None
