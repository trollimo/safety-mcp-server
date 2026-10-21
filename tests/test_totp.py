from safety_mcp import totp

RFC_KEY = b"12345678901234567890"


def test_rfc6238_vectors():
    # RFC 6238, приложение B (SHA1), последние 6 цифр 8-значных значений
    vectors = {59: "94287082", 1111111109: "07081804", 1111111111: "14050471",
               1234567890: "89005924", 2000000000: "69279037", 20000000000: "65353130"}
    for t, code in vectors.items():
        assert totp.totp(RFC_KEY, t, digits=8) == code
        assert totp.totp(RFC_KEY, t) == code[-6:]


def test_verify_window_and_replay():
    now = 1_000_000
    code = totp.totp(RFC_KEY, now)
    step = totp.verify(RFC_KEY, code, now)
    assert step == now // 30
    assert totp.verify(RFC_KEY, code, now, last_step=step) is None          # повтор запрещён
    assert totp.verify(RFC_KEY, totp.totp(RFC_KEY, now - 30), now) is not None  # -1 шаг допустим
    assert totp.verify(RFC_KEY, totp.totp(RFC_KEY, now - 90), now) is None      # -3 шага нет
    for bad in ("", "12345", "1234567", "abcdef", None):
        assert totp.verify(RFC_KEY, bad, now) is None


def test_secret_roundtrip_and_uri():
    s = totp.new_secret()
    assert len(totp.decode_secret(s)) == 20
    uri = totp.otpauth_uri(s, "operator")
    assert uri.startswith("otpauth://totp/safety-mcp-server%3Aoperator?")
    assert f"secret={s}" in uri and "digits=6" in uri and "period=30" in uri and "algorithm=SHA1" in uri
