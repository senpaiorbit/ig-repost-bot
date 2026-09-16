import base64

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import Response

app = FastAPI()


@app.get("/")
def root():
    return {"service": "ig-repost-cache", "status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/image")
async def image_proxy(request: Request, url: str = Query(...)):
    key = "img:" + url
    env = request.scope.get("env")
    cache = getattr(env, "CACHE", None) if env is not None else None
    if cache is not None:
        try:
            cached = await cache.get(key)
            if cached:
                if isinstance(cached, (bytes, bytearray)):
                    data = bytes(cached)
                elif isinstance(cached, str):
                    try:
                        data = base64.b64decode(cached)
                    except Exception:
                        data = cached.encode()
                else:
                    data = bytes(cached)
                return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
        except Exception:
            pass
    async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.content
        media_type = (r.headers.get("content-type", "image/jpeg").split(";")[0].strip() or "image/jpeg")
    if cache is not None:
        try:
            await cache.put(key, base64.b64encode(data).decode())
        except Exception:
            pass
    return Response(content=data, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})
