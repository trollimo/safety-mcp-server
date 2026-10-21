import threading
import time

import pytest

from safety_mcp import totp
from safety_mcp.approver import Approver, enroll
from safety_mcp.queue import Queue
from safety_mcp.sshexec import Audit
from safety_mcp.tools import about_text, build_tools
from test_approver import Clock, Runner, do_enroll


def test_about_tool_and_text(cfg, tmp_path):
    t = {s.name: s for s in build_tools(cfg, Queue(str(tmp_path / "q")), Audit(None), "http://x")}
    assert list(t)[0] == "about" and "ОБЯЗАТЕЛЬНО" in t["about"].description
    text = t["about"].fn()
    assert text == about_text(cfg)
    for must in ("<стенд>_ro_<команда>", "<стенд>_rw_<команда>", "FreeOTP", "rw_approve", "ТОЛЬКО от человека",
                 "- dev: хостов 3; запись: да, код можно передать через агента",
                 "- test: хостов 1; запись: да, только через страницу", "- prod: хостов 1; запись: нет"):
        assert must in text, must


def test_tool_naming_format(cfg, tmp_path):
    names = [s.name for s in build_tools(cfg, Queue(str(tmp_path / "q")), Audit(None), "http://x")]
    general = {"about", "stands_list", "rw_result", "rw_approve"}
    for n in names:
        if n in general:
            continue
        stand, role, cmd = n.split("_", 2)
        assert stand in cfg.stands and role in ("ro", "rw") and cmd, n


@pytest.fixture
def stack(cfg, tmp_path):
    """MCP-инструменты и approver на одной очереди; approver обрабатывает коды в фоне."""
    clock = Clock(time.time())
    q = Queue(str(tmp_path / "queue"))
    runner = Runner()
    ap = Approver(cfg, q, str(tmp_path / "data"), runner=runner, clock=clock)
    key, _ = do_enroll(ap, clock)
    clock.t += 30
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            ap.process_submissions()
            time.sleep(0.05)
    th = threading.Thread(target=loop, daemon=True)
    th.start()
    tools = {s.name: s for s in build_tools(cfg, q, Audit(None), "http://x", approve_wait=5)}
    yield tools, q, ap, runner, clock, key
    stop.set()
    th.join(2)


def rid_of(out):
    return out.split("id: ", 1)[1].split()[0]


def test_rw_approve_via_agent(stack):
    tools, q, ap, runner, clock, key = stack
    out = tools["dev_rw_file_create"].fn(host="dev-w1", path="mcp-sandbox/a.txt")
    assert "rw_approve" in out
    rid = rid_of(out)
    res = tools["rw_approve"].fn(id=rid, code="000000" if totp.totp(key, clock()) != "000000" else "111111")
    assert res.startswith("НЕ ПОДТВЕРЖДЕНО") and "осталось попыток: 2" in res and not runner.calls
    res = tools["rw_approve"].fn(id=rid, code=totp.totp(key, clock()))
    assert "status=approved-ok rc=0" in res and runner.calls == [("dev", "dev-w1", "rw", "touch -- mcp-sandbox/a.txt")]
    assert "уже обработана" in tools["rw_approve"].fn(id=rid, code=totp.totp(key, clock()))
    assert "approved-ok" in tools["rw_result"].fn(id=rid)
    assert len(runner.calls) == 1


def test_rw_approve_forbidden_without_agent_otp(stack):
    tools, q, ap, runner, clock, key = stack
    rid = rid_of(tools["test_rw_file_create"].fn(host="test-m1", path="mcp-sandbox/a"))
    assert "rw_approve" not in tools["test_rw_file_create"].fn(host="test-m1", path="mcp-sandbox/b")
    res = tools["rw_approve"].fn(id=rid, code=totp.totp(key, clock()))
    assert res.startswith("ОШИБКА") and not runner.calls
    # даже если файл с кодом подложить напрямую, approver откажет
    name = q.submit_code(rid, totp.totp(key, clock()))
    for _ in range(50):
        if q.submission_reply(name):
            break
        time.sleep(0.05)
    assert q.submission_reply(name)["ok"] is False and "agent_otp=false" in q.submission_reply(name)["message"]
    assert not runner.calls


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", "12 456"])
def test_rw_approve_bad_code_format(stack, code):
    tools, q, *_ = stack
    rid = rid_of(tools["dev_rw_file_create"].fn(host="dev-w1", path="mcp-sandbox/a"))
    assert tools["rw_approve"].fn(id=rid, code=code).startswith("ОШИБКА")
    assert not q.code_submissions()


def test_rw_approve_unknown_id(stack):
    tools, *_ = stack
    assert tools["rw_approve"].fn(id="0" * 32, code="123456").startswith("ОШИБКА")
    assert tools["rw_approve"].fn(id="../x", code="123456").startswith("ОШИБКА")


def test_rw_approve_timeout_without_approver(cfg, tmp_path):
    q = Queue(str(tmp_path / "q"))
    q.write_status(True)
    tools = {s.name: s for s in build_tools(cfg, q, Audit(None), "http://x", approve_wait=0.6)}
    rid = rid_of(tools["dev_rw_file_create"].fn(host="dev-w1", path="mcp-sandbox/a"))
    assert "ещё не ответил" in tools["rw_approve"].fn(id=rid, code="123456")


def test_submission_processed_once(stack):
    tools, q, ap, runner, clock, key = stack
    rid = rid_of(tools["dev_rw_file_create"].fn(host="dev-w1", path="mcp-sandbox/a"))
    tools["rw_approve"].fn(id=rid, code=totp.totp(key, clock()))
    assert ap.process_submissions() == 0 and len(runner.calls) == 1
