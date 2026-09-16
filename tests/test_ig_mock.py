"""IG client tests — mocked aiograpi Client, no network."""
from unittest.mock import MagicMock, patch

import pytest

import app.ig as igmod
from app.config import settings


@pytest.mark.asyncio
async def test_login_sessionid_path_calls_login_by_sessionid(monkeypatch):
    monkeypatch.setattr(settings, "IG_SESSIONID", "sess123")
    monkeypatch.setattr(settings, "IG_USERNAME", "u")
    monkeypatch.setattr(settings, "IG_PASSWORD", "p")
    mock_cl = MagicMock()
    mock_cl.login_by_sessionid = MagicMock(return_value=None)
    with patch.object(igmod, "Client", return_value=mock_cl):
        ig = igmod.IGClient()
        ok = await ig.login()
        assert ok is True
        mock_cl.login_by_sessionid.assert_called_once_with("sess123")


@pytest.mark.asyncio
async def test_login_falls_back_to_password(monkeypatch):
    monkeypatch.setattr(settings, "IG_SESSIONID", "")
    monkeypatch.setattr(settings, "IG_USERNAME", "u")
    monkeypatch.setattr(settings, "IG_PASSWORD", "p")
    monkeypatch.setattr(settings, "IG_TOTP_SEED", "")
    mock_cl = MagicMock()
    mock_cl.login = MagicMock(return_value=True)
    with patch.object(igmod, "Client", return_value=mock_cl):
        ig = igmod.IGClient()
        ok = await ig.login()
        assert ok is True
        assert mock_cl.login.called


def test_totp_code_6_digits(monkeypatch):
    monkeypatch.setattr(settings, "IG_TOTP_SEED", "JBSWY3DPEHPK3PXP")
    monkeypatch.setattr(settings, "IG_SESSIONID", "")
    with patch.object(igmod, "Client", return_value=MagicMock()):
        ig = igmod.IGClient()
        code = ig._totp_code()
        assert isinstance(code, str)
        assert len(code) == 6
        assert code.isdigit()


def test_extract_media_id_variants():
    m = MagicMock()
    m.pk = 12345
    assert igmod.IGClient.extract_media_id(m) == "12345"
    assert igmod.IGClient.extract_media_id({"id": "abc"}) == "abc"
