import http.client
import io
import json
import os
import re
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import pytest

from safety_mcp import totp
from safety_mcp.approver import Approver, enroll, make_handler
from safety_mcp.queue import Queue


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Runner:
    def __init__(self, rc=0):
        self.calls, self.rc = [], rc

    def __call__(self, stand, host, role, cmd, audit, *a):
        self.calls.append((stand.name, host.name, role, cmd))
        return self.rc, "done\n"


@pytest.fixture
def env(cfg, tmp_path):
    clock = Clock()
    q = Queue(str(tmp_path / "queue"))
    runner = Runner()
    ap = Approver(cfg, q, str(tmp_path / "data"), runner=runner, clock=clock)
    return ap, q, runner, clock


def do_enroll(ap, clock):
    holder = {}

    def fake_input(prompt):
        uri = holder["out"].getvalue()
        secret = re.search(r"secret=([A-Z2-7]+)", uri).group(1)
        holder["secret"] = secret
        return totp.totp(totp.decode_secret(secret), clock())
    holder["out"] = io.StringIO()
    rc = enroll(ap, "tester", False, input_fn=fake_input, out=holder["out"])
    assert rc == 0
    return totp.decode_secret(holder["secret"]), holder["out"].getvalue()


def new_req(q, clock, cmd="touch -- safety-sandbox/a.txt", host="dev-w1", stand="dev"):
    return q.new_request(stand, host, "file_create", cmd, now=clock())["id"]


def test_enroll_prints_qr_and_saves(env):
    ap, q, _, clock = env
    key, out = do_enroll(ap, clock)
    assert "otpauth://totp/" in out and any(ch in out for ch in "█▀▄")
    assert q.enrolled() and ap.enrolled()
    assert oct(os.stat(os.path.join(ap.data_dir, "otp.json")).st_mode & 0o777) == "0o600"
    assert enroll(ap, "x", False, input_fn=lambda p: "000000", out=io.StringIO()) == 3


def test_enroll_reset_requires_valid_code(env):
    ap, q, _, clock = env
    key, _ = do_enroll(ap, clock)
    assert enroll(ap, "x", True, input_fn=lambda p: "000000", out=io.StringIO()) == 4
    old = ap.otp()["secret"]
    clock.t += 30
    seq = iter([totp.totp(key, clock())])

    def inp(prompt):
        try:
            return next(seq)
        except StopIteration:
            return "000000"
    assert enroll(ap, "x", True, input_fn=inp, out=io.StringIO()) == 5  # новый код не введён
    assert ap.otp()["secret"] == old


def test_enroll_wrong_codes_not_saved(env):
    ap, q, _, _ = env
    assert enroll(ap, "x", False, input_fn=lambda p: "000000", out=io.StringIO()) == 5
    assert not ap.enrolled() and not q.enrolled()


def test_approve_flow_executes_once(env):
    ap, q, runner, clock = env
    key, _ = do_enroll(ap, clock)
    clock.t += 30
    rid = new_req(q, clock)
    v = ap.view(rid)
    ok, msg = ap.approve(rid, totp.totp(key, clock()), v["csrf"])
    assert ok, msg
    assert runner.calls == [("dev", "dev-w1", "rw", "touch -- safety-sandbox/a.txt")]
    assert q.result(rid)["status"] == "approved-ok"
    ok, msg = ap.approve(rid, totp.totp(key, clock()), v["csrf"])
    assert not ok and "уже обработана" in msg and len(runner.calls) == 1


def test_code_is_one_time_across_requests(env):
    ap, q, runner, clock = env
    key, _ = do_enroll(ap, clock)
    clock.t += 30
    code = totp.totp(key, clock())
    r1, r2 = new_req(q, clock), new_req(q, clock, "rm -f -- safety-sandbox/a.txt")
    assert ap.approve(r1, code, ap.view(r1)["csrf"])[0]
    ok, msg = ap.approve(r2, code, ap.view(r2)["csrf"])
    assert not ok and len(runner.calls) == 1


def test_three_wrong_codes_deny(env):
    ap, q, runner, clock = env
    key, _ = do_enroll(ap, clock)
    clock.t += 30
    rid = new_req(q, clock)
    csrf = ap.view(rid)["csrf"]
    for i in range(3):
        ok, msg = ap.approve(rid, "000000" if totp.totp(key, clock()) != "000000" else "111111", csrf)
        assert not ok
    assert q.result(rid)["status"] == "denied"
    assert not ap.approve(rid, totp.totp(key, clock()), csrf)[0]
    assert not runner.calls


def test_global_lockout(env):
    ap, q, runner, clock = env
    key, _ = do_enroll(ap, clock)
    clock.t += 30
    for _ in range(3):
        rid = new_req(q, clock)
        csrf = ap.view(rid)["csrf"]
        for _ in range(2):
            ap.approve(rid, "000000", csrf)
    rid = new_req(q, clock)
    ok, msg = ap.approve(rid, totp.totp(key, clock()), ap.view(rid)["csrf"])
    assert not ok and "заблокировано" in msg and not runner.calls
    clock.t += 601
    rid2 = new_req(q, clock)
    assert ap.approve(rid2, totp.totp(key, clock()), ap.view(rid2)["csrf"])[0]


def test_bad_csrf(env):
    ap, q, runner, clock = env
    key, _ = do_enroll(ap, clock)
    rid = new_req(q, clock)
    ap.view(rid)
    assert not ap.approve(rid, totp.totp(key, clock() + 30), "nope")[0]
    assert not ap.deny(rid, "nope")[0]


def test_deny(env):
    ap, q, runner, clock = env
    do_enroll(ap, clock)
    rid = new_req(q, clock)
    assert ap.deny(rid, ap.view(rid)["csrf"])[0]
    assert q.result(rid)["status"] == "denied" and not runner.calls


def test_expired(env):
    ap, q, runner, clock = env
    key, _ = do_enroll(ap, clock)
    rid = new_req(q, clock)
    clock.t += 601
    ok, msg = ap.approve(rid, totp.totp(key, clock()), "x")
    assert not ok and "истёк" in msg and q.result(rid)["status"] == "expired"


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(cmd="touch -- /etc/passwd"),                       # хэш не совпадёт
    lambda r: r.update(cmd="df -h", cmd_sha256=__import__("hashlib").sha256(b"df -h").hexdigest()),
    lambda r: r.update(hosts=["prod-w1"]),                                # хост другого стенда
    lambda r: r.update(hosts=["dev-w1", "prod-w1"]),                     # чужой хост в списке
    lambda r: r.update(hosts=["dev-w1", "dev-w1"]),                      # повтор
    lambda r: r.update(hosts=["evil.example"]),                          # хоста нет в инвентаре
    lambda r: r.update(hosts=[]),
    lambda r: r.update(hosts="dev-w1"),                                  # не список
    lambda r: r.update(stand="prod", hosts=["prod-w1"]),                 # у prod нет rw
    lambda r: r.update(created=r["created"] + 3600),                     # дата из будущего
])
def test_tampered_request_rejected(env, mutate):
    ap, q, runner, clock = env
    key, _ = do_enroll(ap, clock)
    clock.t += 30
    rid = new_req(q, clock)
    csrf = ap.view(rid)["csrf"]          # настоящий CSRF: отказ должен быть из-за подмены, а не токена
    path = os.path.join(q.requests, rid + ".json")
    r = json.load(open(path))
    mutate(r)
    json.dump(r, open(path, "w"))
    ok, msg = ap.approve(rid, totp.totp(key, clock()), csrf)
    assert not ok and not runner.calls
    assert "CSRF" not in msg


def test_untampered_control(env):
    """Контроль к тесту выше: без подмены та же последовательность проходит."""
    ap, q, runner, clock = env
    key, _ = do_enroll(ap, clock)
    clock.t += 30
    rid = new_req(q, clock)
    csrf = ap.view(rid)["csrf"]
    assert ap.approve(rid, totp.totp(key, clock()), csrf)[0] and len(runner.calls) == 1


def test_multi_host_one_code(env):
    ap, q, runner, clock = env
    key, _ = do_enroll(ap, clock)
    clock.t += 30
    rid = q.new_request("dev", ["dev-w2", "dev-m1", "dev-w1"], "file_create", "touch -- safety-sandbox/a", now=clock())["id"]
    v = ap.view(rid)
    assert [h.name for h in v["hosts"]] == ["dev-m1", "dev-w1", "dev-w2"]
    ok, msg = ap.approve(rid, totp.totp(key, clock()), v["csrf"])
    assert ok, msg
    assert sorted(c[1] for c in runner.calls) == ["dev-m1", "dev-w1", "dev-w2"]
    res = q.result(rid)
    assert res["status"] == "approved-ok" and "SUMMARY: ok=3 failed=0" in res["output"]
    assert res["hosts"] == ["dev-w2", "dev-m1", "dev-w1"]


def test_multi_host_partial_failure(cfg, tmp_path):
    clock = Clock()
    q = Queue(str(tmp_path / "queue"))

    def runner(stand, host, role, cmd, audit, *a):
        return (1, "fail\n") if host.name == "dev-w2" else (0, "ok\n")
    ap = Approver(cfg, q, str(tmp_path / "data"), runner=runner, clock=clock)
    key, _ = do_enroll(ap, clock)
    clock.t += 30
    rid = q.new_request("dev", ["dev-w1", "dev-w2"], "file_create", "touch -- safety-sandbox/a", now=clock())["id"]
    ok, _ = ap.approve(rid, totp.totp(key, clock()), ap.view(rid)["csrf"])
    res = q.result(rid)
    assert not ok and res["status"] == "approved-failed" and "SUMMARY: ok=1 failed=1" in res["output"]


def test_not_enrolled(env):
    ap, q, runner, clock = env
    rid = new_req(q, clock)
    ok, msg = ap.approve(rid, "123456", ap.view(rid)["csrf"])
    assert not ok and "не настроен" in msg


# ---------- HTTP ----------

@pytest.fixture
def web(env):
    ap = env[0]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ap, {"approver.test"}))
    port = srv.server_address[1]
    srv.allowed_port = port
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield env, port
    srv.shutdown()


def req(port, method, path, host="approver.test", body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Host": host}
    if body is not None:
        body = urlencode(body)
        h["Content-Type"] = "application/x-www-form-urlencoded"
    h.update(headers or {})
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read().decode()


def test_http_host_header_checked(web):
    (ap, q, runner, clock), port = web
    assert req(port, "GET", "/", host="evil.example")[0] == 421


def test_http_page_and_approve(web):
    (ap, q, runner, clock), port = web
    key, _ = do_enroll(ap, clock)
    clock.t += 30
    rid = new_req(q, clock)
    st, hdr, body = req(port, "GET", f"/r/{rid}")
    assert st == 200 and "СТЕНД: DEV" in body and "touch -- safety-sandbox/a.txt" in body
    assert hdr["Cache-Control"] == "no-store" and hdr["X-Frame-Options"] == "DENY"
    assert "default-src 'none'" in hdr["Content-Security-Policy"]
    csrf = re.search(r"name='csrf' value='([^']+)'", body).group(1)
    st, _, body = req(port, "POST", f"/r/{rid}", body={"action": "approve", "code": "000000", "csrf": csrf})
    assert "осталось попыток: 2" in body
    st, _, body = req(port, "POST", f"/r/{rid}", body={"action": "approve", "code": totp.totp(key, clock()),
                                                        "csrf": csrf}, headers={"Origin": "http://evil"})
    assert st == 403
    st, _, body = req(port, "POST", f"/r/{rid}", body={"action": "approve", "code": totp.totp(key, clock()),
                                                        "csrf": csrf})
    assert "approved-ok" in body and "done" in body and len(runner.calls) == 1


def test_http_index_and_escaping(web):
    (ap, q, runner, clock), port = web
    rid = new_req(q, clock)
    st, _, body = req(port, "GET", "/")
    assert rid in body
    assert req(port, "GET", "/r/../../etc/passwd")[0] == 404


def test_enroll_reports_clock_skew(env):
    ap, q, _, clock = env
    holder = {"out": io.StringIO()}

    def skewed_input(prompt):
        secret = re.search(r"secret=([A-Z2-7]+)", holder["out"].getvalue()).group(1)
        return totp.totp(totp.decode_secret(secret), clock() + 66)
    assert enroll(ap, "x", False, input_fn=skewed_input, out=holder["out"]) == 5
    out = holder["out"].getvalue()
    assert "Время этой машины (UTC)" in out and "спешит" in out and "60 с" in out
    assert not ap.enrolled()


def test_approve_reports_clock_skew(env):
    ap, q, runner, clock = env
    key, _ = do_enroll(ap, clock)
    clock.t += 30
    rid = new_req(q, clock)
    ok, msg = ap.approve(rid, totp.totp(key, clock() - 120), ap.view(rid)["csrf"])
    assert not ok and "отстаёт" in msg and "осталось попыток: 2" in msg and not runner.calls
