"""API tests — no network/creds (mocked duplicates + settings)."""
from fastapi.testclient import TestClient

import main as mainmod
from app.config import settings

KEY = "testkey"


def _client():
    mainmod.init_schema = lambda: None  # noqa: E731
    return TestClient(mainmod.app, raise_server_exceptions=False)


def test_root_ok():
    c = _client()
    r = c.get("/")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_check_totp_ok_with_seed(monkeypatch):
    monkeypatch.setattr(settings, "IG_TOTP_SEED", "JBSWY3DPEHPK3PXP")
    c = _client()
    r = c.get("/check_totp")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert len(body["code_prefix"]) == 2


def test_check_totp_seed_missing(monkeypatch):
    monkeypatch.setattr(settings, "IG_TOTP_SEED", "")
    c = _client()
    r = c.get("/check_totp")
    assert r.status_code == 200
    assert r.json()["status"] == "error"


def test_upload_requires_key():
    c = _client()
    r = c.post("/upload", json={"target_url": "https://instagram.com/reel/x"})
    assert r.status_code == 422


def test_upload_wrong_key(monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", KEY)
    c = _client()
    r = c.post(f"/upload?key=wrong&post_type=reel", json={"target_url": "https://instagram.com/reel/x"})
    assert r.status_code == 403


def test_upload_duplicate_guard_mocked(monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", KEY)
    monkeypatch.setattr(mainmod, "_find_duplicate", lambda url: (1,))
    c = _client()
    r = c.post(f"/upload?key={KEY}&post_type=reel", json={"target_url": "https://instagram.com/reel/dup"})
    assert r.status_code == 409
    assert r.json() == {"status": "error", "message": "duplicate"}


def test_upload_bad_post_type(monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", KEY)
    monkeypatch.setattr(mainmod, "_find_duplicate", lambda url: None)
    c = _client()
    r = c.post(f"/upload?key={KEY}&post_type=story", json={"target_url": "https://instagram.com/reel/x"})
    assert r.status_code == 400


def test_upload_bad_url(monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", KEY)
    monkeypatch.setattr(mainmod, "_find_duplicate", lambda url: None)
    c = _client()
    r = c.post(f"/upload?key={KEY}&post_type=reel", json={"target_url": "notaurl"})
    assert r.status_code == 400
