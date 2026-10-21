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


def verify(key: bytes, code: str, now: float | None = None, last_step: int = -1, window: int = 1,
           skew: float | None = None) -> int | None:
    """Возвращает номер шага, если код верен и ещё не использован (шаг > last_step), иначе None.

    skew (секунды): принимать коды шагов, пересекающихся с интервалом [now-skew, now+skew].
    Если skew не задан, используется window (в шагах)."""
    if not isinstance(code, str) or len(code) != DIGITS or not code.isdigit():
        return None
    now = time.time() if now is None else now
    if skew is not None:
        lo, hi = int((now - skew) // STEP), int((now + skew) // STEP)
    else:
        cur = int(now // STEP)
        lo, hi = cur - window, cur + window
    found = None
    for step in range(lo, hi + 1):
        if hmac.compare_digest(hotp(key, step), code):
            found = step
    if found is None or found <= last_step:
        return None
    return found


def drift(key: bytes, code: str, now: float | None = None, search: int = 20) -> int | None:
    """Только для диагностики: если код верен для времени в пределах ±search шагов, вернуть сдвиг в секундах
    (положительный — телефон спешит относительно этой машины). Код при этом НЕ принимается."""
    if not isinstance(code, str) or len(code) != DIGITS or not code.isdigit():
        return None
    now = time.time() if now is None else now
    cur = int(now // STEP)
    for d in sorted(range(-search, search + 1), key=abs):
        if hmac.compare_digest(hotp(key, cur + d), code):
            return d * STEP
    return None


def drift_hint(seconds: int) -> str:
    side = "спешит" if seconds > 0 else "отстаёт"
    return (f"Код похож на верный, но телефон {side} относительно этой машины примерно на {abs(seconds)} с. "
            "Синхронизируйте часы (NTP) на машине с approver.")


def otpauth_uri(secret_b32: str, account: str, issuer: str = "safety-mcp-server") -> str:
    label = urllib.parse.quote(f"{issuer}:{account}")
    q = urllib.parse.urlencode({"secret": secret_b32, "issuer": issuer, "algorithm": "SHA1",
                                "digits": DIGITS, "period": STEP})
    return f"otpauth://totp/{label}?{q}"
