"""TOTP по RFC 6238 (SHA1, 6 цифр, 30 с) на стандартной библиотеке. Совместим с FreeOTP."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

STEP = 30
DIGITS = 6


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def decode_secret(b32: str) -> bytes:
    s = b32.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def hotp(key: bytes, counter: int, digits: int = DIGITS) -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def totp(key: bytes, at: float | None = None, digits: int = DIGITS) -> str:
    at = time.time() if at is None else at
    return hotp(key, int(at // STEP), digits)


def verify(key: bytes, code: str, now: float | None = None, last_step: int = -1, window: int = 1) -> int | None:
    """Возвращает номер шага, если код верен и ещё не использован (шаг > last_step), иначе None."""
    if not isinstance(code, str) or len(code) != DIGITS or not code.isdigit():
        return None
    now = time.time() if now is None else now
    cur = int(now // STEP)
    found = None
    for step in range(cur - window, cur + window + 1):
        if hmac.compare_digest(hotp(key, step), code):
            found = step
    if found is None or found <= last_step:
        return None
    return found


def otpauth_uri(secret_b32: str, account: str, issuer: str = "safety-mcp-server") -> str:
    label = urllib.parse.quote(f"{issuer}:{account}")
    q = urllib.parse.urlencode({"secret": secret_b32, "issuer": issuer, "algorithm": "SHA1",
                                "digits": DIGITS, "period": STEP})
    return f"otpauth://totp/{label}?{q}"
