"""DB tests — hermetic, no network (mocked get_client)."""
from unittest.mock import MagicMock, patch

import app.db as dbmod
from app.db import SCHEMA_SQL, check_connection


def test_schema_has_unique_original_url():
    assert "uploaded_media" in SCHEMA_SQL
    assert "UNIQUE" in SCHEMA_SQL
    assert "original_url" in SCHEMA_SQL


def test_check_connection_ok_mocked():
    cur = MagicMock()
    cur.fetchone.return_value = (1,)
    con = MagicMock()
    con.execute.return_value = cur
    with patch.object(dbmod, "get_client", return_value=con):
        assert check_connection() is True


def test_check_connection_false_on_empty():
    cur = MagicMock()
    cur.fetchone.return_value = None
    con = MagicMock()
    con.execute.return_value = cur
    with patch.object(dbmod, "get_client", return_value=con):
        assert check_connection() is False


def test_insert_media_returns_row_id_mocked():
    cur = MagicMock()
    cur.fetchone.return_value = (7,)
    con = MagicMock()
    con.execute.return_value = cur
    with patch.object(dbmod, "get_client", return_value=con):
        rid = dbmod.insert_media("https://x.test/r/1", "mid123", "reel")
        assert rid == 7
        assert con.execute.called
        con.commit.assert_called()


def test_get_old_media_returns_dicts_mocked():
    cur = MagicMock()
    cur.fetchall.return_value = [(1, "https://x.test/r/1", "mid1", "reel", "active", 0, "2024-01-01", None)]
    cur.description = [("id",), ("original_url",), ("instagram_media_id",), ("media_type",), ("status",), ("views",), ("uploaded_at",), ("archived_at",)]
    con = MagicMock()
    con.execute.return_value = cur
    with patch.object(dbmod, "get_client", return_value=con):
        rows = dbmod.get_old_media(24)
        assert len(rows) == 1
        assert rows[0]["instagram_media_id"] == "mid1"
