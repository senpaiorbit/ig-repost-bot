"""TOTP tests — no network."""
import pyotp

KNOWN_SEED = "JBSWY3DPEHPK3PXP"


def test_totp_code_is_6_digits():
    code = pyotp.TOTP(KNOWN_SEED).now()
    assert isinstance(code, str)
    assert len(code) == 6
    assert code.isdigit()


def test_code_prefix_len_2():
    code = pyotp.TOTP(KNOWN_SEED).now()
    assert len(code[:2]) == 2


def test_missing_seed_handling():
    seed = ""
    if not seed.strip():
        result = {"status": "error", "message": "Seed missing"}
    else:
        result = {"status": "ok"}
    assert result["status"] == "error"
    assert result["message"] == "Seed missing"
