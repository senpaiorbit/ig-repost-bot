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

    @staticmethod
    def _is_ad(item) -> bool:
        for key in ("is_ad", "ad_action", "ad_id", "sponsored_info", "is_paid_partnership"):
            try:
                v = item.get(key) if isinstance(item, dict) else getattr(item, key, None)
            except Exception:
                continue
            if v:
                return True
        return False

    @staticmethod
    def _video_url(item) -> Optional[str]:
        def _field(obj, name):
            try:
                if isinstance(obj, dict):
                    return obj.get(name)
                return getattr(obj, name, None)
            except Exception:
                return None
        def _pick(cands):
            best = None
            best_w = -1
            try:
                for c in (cands or []):
                    if isinstance(c, dict):
                        u = c.get("url")
                        w = c.get("width", 0) or 0
                    else:
                        u = getattr(c, "url", None)
                        try:
                            w = getattr(c, "width", 0) or 0
                        except Exception:
                            w = 0
                    if not u or not str(u).startswith("http"):
                        continue
                    try:
                        w = int(w)
                    except Exception:
                        w = 0
                    if w >= best_w:
                        best, best_w = str(u), w
            except Exception:
                pass
            return best
        try:
            direct = _field(item, "video_url")
            if direct and str(direct).startswith("http"):
                return str(direct)
            for key in ("video_versions", "candidates"):
                u = _pick(_field(item, key))
                if u:
                    return u
            car = _field(item, "carousel_media")
            if isinstance(car, list) and car:
                for sub in car:
                    u = IGClient._video_url(sub)
                    if u:
                        return u
        except Exception:
            pass
        return None

    @staticmethod
    def _video_url_all(item) -> list:
        def _field(obj, name):
            try:
                if isinstance(obj, dict):
                    return obj.get(name)
                return getattr(obj, name, None)
            except Exception:
                return None
        def _all(cands):
            found = []
            try:
                for c in (cands or []):
                    if isinstance(c, dict):
                        u = c.get("url")
                        w = c.get("width", 0) or 0
                    else:
                        u = getattr(c, "url", None)
                        try:
                            w = getattr(c, "width", 0) or 0
                        except Exception:
                            w = 0
                    if not u or not str(u).startswith("http"):
                        continue
                    try:
                        w = int(w)
                    except Exception:
                        w = 0
                    found.append((w, str(u)))
            except Exception:
                pass
            return found
        try:
            rest = []
            for key in ("video_versions", "candidates"):
                rest.extend(_all(_field(item, key)))
            car = _field(item, "carousel_media")
            if isinstance(car, list) and car:
                for sub in car:
                    try:
                        for u in IGClient._video_url_all(sub):
                            rest.append((0, u))
                    except Exception:
                        continue
            rest.sort(key=lambda t: t[0], reverse=True)
            urls = []
            direct = _field(item, "video_url")
            if direct and str(direct).startswith("http"):
                urls.append(str(direct))
            for _, u in rest:
                if u not in urls:
                    urls.append(u)
            return urls
        except Exception:
            return []

    @staticmethod
    def _feed_items(payload) -> list:
        if payload is None:
            return []
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("feed_items", "items", "tray", "reels", "results"):
                val = payload.get(key)
                if isinstance(val, list) and val:
                    return val
            return []
        for attr in ("feed_items", "items", "tray", "reels"):
            try:
                val = getattr(payload, attr, None)
            except Exception:
                continue
            if callable(val):
                continue
            if isinstance(val, list) and val:
                return val
        return []

    async def fetch_feed_candidates(self, limit: int = 20) -> list[str]:
        try:
            lim = max(1, int(limit or 20))
        except Exception:
            lim = 20
        out: list[str] = []
        seen = set()
        try:
            self._video_urls = {}
        except Exception:
            pass
        for name in ("get_timeline_feed", "get_reels_tray_feed", "timeline_feed", "reels_feed", "explore_feed", "get_explore_feed"):
            fn = getattr(self.cl, name, None)
            if fn is None:
                continue
            try:
                payload = await _maybe_thread(fn)
            except Exception as e:
                print(f"[ig] {name} failed: {e}")
                continue
            try:
                items = self._feed_items(payload)
            except Exception:
                continue
            for item in items:
                try:
                    url = self._cand_url(item)
                except Exception:
                    continue
                if not url or url in seen:
                    continue
                try:
                    vurl = self._video_url(item)
                except Exception:
                    vurl = None
                if vurl:
                    try:
                        self._video_urls[url] = vurl
                    except Exception:
                        pass
                seen.add(url)
                out.append(url)
                if len(out) >= lim:
                    try:
                        print(f"[ig] feed: {len(out)} candidates, {len(self._video_urls)} with direct url, sample={[str(s)[:60] for s in out[:3]]}")
                    except Exception:
                        pass
                    return out
            if out:
                break
        try:
            print(f"[ig] feed: {len(out)} candidates, {len(self._video_urls)} with direct url, sample={[str(s)[:60] for s in out[:3]]}")
        except Exception:
            pass
        return out[:lim]

    async def download_cover(self, url: Optional[str] = None) -> Path:
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

    async def post_actions(self, media_id, comment_text: Optional[str] = None):
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
