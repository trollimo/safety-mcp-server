import copy
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import COMMANDS, INVENTORY
from safety_mcp.config import ConfigError
from safety_mcp.queue import Queue
from safety_mcp.sshexec import Audit
from safety_mcp.tools import build_tools


class Handler(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Handler.seen.append(self.path)
        if self.path == "/login":
            body = b"<html><form action=/session>login</form></html>"
            self.send_response(200)
            self.send_header("Set-Cookie", "session=SECRET123")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/old":
            self.send_response(302)
            self.send_header("Location", "http://evil.example/")
            self.end_headers()
        elif self.path == "/big":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"x" * 300_000)
        elif self.path.startswith("/page/"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(self.path.encode())
        else:
            self.send_response(503)
            self.end_headers()

    do_HEAD = do_GET


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def http_conf(port):
    inv = copy.deepcopy(INVENTORY)
    inv["stands"]["prod"]["endpoints"] = {"app": {"base_url": f"http://127.0.0.1:{port}", "allow_http": True}}
    inv["stands"]["web"] = {"endpoints": {"app": {"base_url": f"http://127.0.0.1:{port}", "allow_http": True}}}
    inv["global"] = {"endpoints": {"sso": {"base_url": f"http://127.0.0.1:{port}", "allow_http": True}}}
    cmds = copy.deepcopy(COMMANDS)
    cmds["common"]["read"].update({
        "login_form_check": {"type": "http", "endpoint": "app", "path": "/login",
                             "expect": {"status": [200], "body_contains": "<form", "max_ms": 5000}},
        "server_is_life": {"type": "http", "endpoint": "app", "path": "/", "expect": {"status": [200]}},
        "old_page": {"type": "http", "endpoint": "app", "path": "/old", "expect": {"status": [200]}},
        "big": {"type": "http", "endpoint": "app", "path": "/big"},
        "page_check": {"type": "http", "endpoint": "app", "path": "/page/{page}",
                       "params": {"page": "[a-z0-9][a-z0-9_-]{0,40}"}, "expect": {"status": [200]}},
        "sso_health": {"type": "http", "scope": "global", "endpoint": "sso", "path": "/login",
                       "expect": {"status": [200]}},
    })
    return inv, cmds


@pytest.fixture
def tools(make_cfg, server, tmp_path):
    inv, cmds = http_conf(server)
    cfg = make_cfg(inv, cmds)
    return {t.name: t for t in build_tools(cfg, Queue(str(tmp_path / "q")), Audit(str(tmp_path / "a.log")), "http://x")}


def test_tool_registration(tools):
    assert "prod_ro_login_form_check" in tools and "web_ro_login_form_check" in tools
    assert "dev_ro_login_form_check" not in tools          # у dev нет эндпоинта app
    assert "global_ro_sso_health" in tools and "prod_ro_sso_health" not in tools
    assert "web_ro_disk_free_all" not in tools and "web_ro_hosts_list" not in tools  # у web нет хостов
    assert "prod_ro_endpoints_list" in tools and "global_ro_endpoints_list" in tools
    assert tools["global_ro_sso_health"].description.startswith("[ВНЕ СТЕНДОВ]")
    assert "HTTP-ПРОВЕРКИ" in tools["about"].fn()


def test_pass_short_and_all(tools, server):
    out = tools["prod_ro_login_form_check"].fn()
    assert out.startswith(f"[stand=prod endpoint=app GET http://127.0.0.1:{server}/login]")
    assert "RESULT: PASS status=200" in out and "- body_contains '<form': ok" in out
    assert "<form action" not in out and "Set-Cookie" not in out          # short: без тела и заголовков
    full = tools["prod_ro_login_form_check"].fn(mode="all")
    assert "HTTP 200 OK" in full and "<form action=/session>" in full
    assert "Set-Cookie: [скрыто]" in full and "SECRET123" not in full


def test_fail_and_code(tools):
    out = tools["prod_ro_server_is_life"].fn()
    assert "RESULT: FAIL status=503" in out and "- status in [200]: FAIL" in out


def test_redirect_not_followed(tools):
    Handler.seen.clear()
    out = tools["prod_ro_old_page"].fn()
    assert "status=302" in out and "Location: http://evil.example/" in out and "RESULT: FAIL" in out
    assert Handler.seen == ["/old"]


def test_body_limits(tools):
    out = tools["prod_ro_big"].fn(mode="all")
    assert "тело обрезано при чтении" in out and "[обрезано]" in out and len(out) < 21000


def test_params_encoded_and_validated(tools, server):
    Handler.seen.clear()
    assert "status=200" in tools["prod_ro_page_check"].fn(page="abc")
    assert Handler.seen == ["/page/abc"]
    for bad in ("../x", "@evil.com", "a/b", "", "a b", "%2e%2e"):
        assert tools["prod_ro_page_check"].fn(page=bad).startswith("ОШИБКА"), bad
    assert Handler.seen == ["/page/abc"]


def test_mode_and_dry_run(tools):
    Handler.seen.clear()
    assert tools["prod_ro_login_form_check"].fn(mode="full").startswith("ОШИБКА")
    out = tools["prod_ro_login_form_check"].fn(dry_run=True)
    assert "ПРОБНЫЙ ЗАПУСК" in out and "GET http://127.0.0.1:" in out and "body_contains" in out
    assert Handler.seen == []


def test_global_command(tools):
    out = tools["global_ro_sso_health"].fn()
    assert out.startswith("[scope=global endpoint=sso GET") and "RESULT: PASS" in out


def test_connection_error(make_cfg, tmp_path):
    inv, cmds = http_conf(1)  # порт 1: соединение отклонено
    cfg = make_cfg(inv, cmds)
    t = {x.name: x for x in build_tools(cfg, Queue(str(tmp_path / "q")), Audit(None), "http://x")}
    out = t["prod_ro_server_is_life"].fn()
    assert "RESULT: ERROR connection" in out and "Traceback" not in out


def test_tls_verification_against_github_like_selfsigned(make_cfg, tmp_path, server):
    # https на обычный http-порт: TLS-рукопожатие не удастся -> ERROR, без отключения проверки
    inv, cmds = http_conf(server)
    inv["stands"]["prod"]["endpoints"]["app"] = {"base_url": f"https://127.0.0.1:{server}"}
    cfg = make_cfg(inv, cmds)
    t = {x.name: x for x in build_tools(cfg, Queue(str(tmp_path / "q")), Audit(None), "http://x")}
    assert "RESULT: ERROR" in t["prod_ro_server_is_life"].fn()


@pytest.mark.parametrize("change,match", [
    (lambda i, c: c["common"]["read"]["server_is_life"].update(method="POST"), "не поддерживается"),
    (lambda i, c: c["common"]["read"]["server_is_life"].update(auth="x"), "зарезервированы"),
    (lambda i, c: c["common"]["read"]["server_is_life"].update(headers={}), "зарезервированы"),
    (lambda i, c: c["common"]["read"]["server_is_life"].update(via="x"), "зарезервированы"),
    (lambda i, c: c["common"]["read"]["server_is_life"].update(path="//evil.com"), "path"),
    (lambda i, c: c["common"]["read"]["server_is_life"].update(path="/a/../b"), "path"),
    (lambda i, c: c["common"]["read"]["server_is_life"].update(path="no-slash"), "path"),
    (lambda i, c: c["common"]["read"]["server_is_life"].update(endpoint="nope"), "ни у одного стенда"),
    (lambda i, c: c["common"]["read"]["server_is_life"].update(expect={"status": []}), "expect.status"),
    (lambda i, c: c["common"]["read"]["server_is_life"].update(expect={"foo": 1}), "expect"),
    (lambda i, c: c["common"].setdefault("write", {}).update(
        http_w={"type": "http", "endpoint": "app", "path": "/"}), "только в блоке read"),
    (lambda i, c: c["stands"]["dev"]["read"].update(
        x={"type": "http", "scope": "global", "endpoint": "sso", "path": "/"}), "только в блоке common"),
    (lambda i, c: c["stands"]["dev"]["read"].update(
        x={"type": "http", "endpoint": "app", "path": "/"}), "нет эндпоинта"),
    (lambda i, c: i["stands"]["prod"]["endpoints"]["app"].update(base_url="http://127.0.0.1:1", allow_http=False), "https"),
    (lambda i, c: i["stands"]["prod"]["endpoints"]["app"].update(base_url="https://u:p@h.example"), "base_url"),
    (lambda i, c: i["stands"]["prod"]["endpoints"]["app"].update(base_url="https://h.example/path"), "base_url"),
    (lambda i, c: i["stands"]["prod"]["endpoints"]["app"].update(base_url="https://h.example/?a=1"), "base_url"),
    (lambda i, c: i["stands"]["prod"]["endpoints"]["app"].update(ca_file="missing.pem"), "ca_file"),
    (lambda i, c: c["common"]["read"]["page_check"]["params"].update(mode="[a-z]+"), "параметр"),
])
def test_http_config_validation(make_cfg, server, change, match):
    inv, cmds = http_conf(server)
    change(inv, cmds)
    with pytest.raises(ConfigError, match=match):
        make_cfg(inv, cmds)


@pytest.fixture(scope="module")
def tls_server(tmp_path_factory):
    import shutil
    import ssl
    import subprocess
    if not shutil.which("openssl"):
        pytest.skip("нет openssl")
    d = tmp_path_factory.mktemp("tls")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=selfsigned",
                    "-keyout", str(d / "k.pem"), "-out", str(d / "c.pem")], check=True, capture_output=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(d / "c.pem"), str(d / "k.pem"))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], d / "c.pem"
    srv.shutdown()


def tls_tools(make_cfg, tmp_path, port, **ep):
    inv, cmds = http_conf(port)
    inv["stands"]["prod"]["endpoints"]["app"] = {"base_url": f"https://127.0.0.1:{port}", **ep}
    cfg = make_cfg(inv, cmds)
    return {x.name: x for x in build_tools(cfg, Queue(str(tmp_path / "q")), Audit(None), "http://x")}


def test_tls_selfsigned_rejected_by_default(make_cfg, tmp_path, tls_server):
    out = tls_tools(make_cfg, tmp_path, tls_server[0])["prod_ro_login_form_check"].fn()
    assert "RESULT: ERROR TLS" in out and "tls_verify=off" not in out


def test_tls_verify_off(make_cfg, tmp_path, tls_server):
    t = tls_tools(make_cfg, tmp_path, tls_server[0], tls_verify=False)
    out = t["prod_ro_login_form_check"].fn()
    assert "tls_verify=off]" in out.splitlines()[0] and "RESULT: PASS status=200" in out
    assert "без проверки TLS-сертификата" in t["prod_ro_login_form_check"].description
    assert "проверка TLS-сертификата отключена" in t["prod_ro_login_form_check"].fn(dry_run=True)


def test_tls_custom_ca_wrong_hostname_still_checked(make_cfg, tmp_path, tls_server):
    import shutil
    port, cert = tls_server
    inv, cmds = http_conf(port)
    inv["stands"]["prod"]["endpoints"]["app"] = {"base_url": f"https://127.0.0.1:{port}", "ca_file": "ca.pem"}
    cfg_dir = tmp_path / "cfgca"
    from conftest import write_conf
    write_conf(str(cfg_dir), inv, cmds)
    shutil.copy(cert, cfg_dir / "ca.pem")
    from safety_mcp.config import load_config
    cfg = load_config(str(cfg_dir))
    t = {x.name: x for x in build_tools(cfg, Queue(str(tmp_path / "q")), Audit(None), "http://x")}
    assert "RESULT: ERROR TLS" in t["prod_ro_login_form_check"].fn()   # CN=selfsigned != 127.0.0.1


def test_tls_verify_must_be_bool(make_cfg, server):
    inv, cmds = http_conf(server)
    inv["stands"]["prod"]["endpoints"]["app"]["tls_verify"] = "no"
    with pytest.raises(ConfigError, match="tls_verify"):
        make_cfg(inv, cmds)
