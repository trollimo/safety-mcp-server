import asyncio
import os
import subprocess
import threading
import time

import pytest

from safety_mcp import sshexec
from safety_mcp.queue import Queue
from safety_mcp.sshexec import Audit, run_on_hosts, ssh_argv
from safety_mcp.tools import build_tools


class FakeRun:
    """Подмена subprocess.run: считает одновременные вызовы."""

    def __init__(self, delay=0.05, fail_hosts=(), rc=0):
        self.delay, self.fail_hosts, self.rc = delay, set(fail_hosts), rc
        self.calls, self.active, self.max_active = [], 0, 0
        self.lock = threading.Lock()

    def __call__(self, argv, **kw):
        with self.lock:
            self.calls.append((argv, kw))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            target = argv[-2]
            if any(target.endswith("@" + a) for a in self.fail_hosts):
                raise RuntimeError("boom")
            return subprocess.CompletedProcess(argv, self.rc, stdout=f"out from {target}\n", stderr="")
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def fake(monkeypatch):
    f = FakeRun()
    monkeypatch.setattr(sshexec.subprocess, "run", f)
    return f


def test_ssh_argv_safe(cfg):
    st = cfg.stands["dev"]
    argv = ssh_argv(st, st.hosts["dev-m1"], "ro", "df -h")
    s = " ".join(argv)
    for opt in ("StrictHostKeyChecking=yes", "BatchMode=yes", "IdentitiesOnly=yes", "ForwardAgent=no",
                "ClearAllForwardings=yes", f"UserKnownHostsFile={st.known_hosts}"):
        assert opt in s
    assert argv[argv.index("-i") + 1] == "/keys/dev_ro"
    assert argv[-2:] == ["opsuser@10.0.0.1", "df -h"] and argv[-3] == "--"


def test_run_uses_no_shell_and_no_stdin(cfg, fake, tmp_path):
    st = cfg.stands["dev"]
    run_on_hosts(st, [st.hosts["dev-m1"]], "ro", "df -h", Audit(None), 3)
    _, kw = fake.calls[0]
    assert kw["shell"] is False and kw["stdin"] is subprocess.DEVNULL and kw["timeout"] == 60


def test_parallel_limit_order_and_summary(make_cfg, monkeypatch, tmp_path):
    import copy
    from conftest import INVENTORY
    inv = copy.deepcopy(INVENTORY)
    inv["stands"]["dev"]["hosts"] = {f"w{i}": {"address": f"10.9.0.{i}", "tags": []} for i in range(7)}
    cfg = make_cfg(inv)
    f = FakeRun(delay=0.1, fail_hosts=["10.9.0.3"])
    monkeypatch.setattr(sshexec.subprocess, "run", f)
    st = cfg.stands["dev"]
    hosts = sorted(st.hosts.values(), key=lambda h: h.name)
    audit = Audit(str(tmp_path / "audit.log"))
    out = run_on_hosts(st, hosts, "ro", "df -h", audit, 3)
    assert f.max_active == 3
    order = [line.split()[1] for line in out.splitlines() if line.startswith("[stand=")]
    assert order == [f"host=w{i}" for i in range(7)]
    assert "ERROR RuntimeError" in out and "SUMMARY: ok=6 failed=1" in out
    assert len(open(tmp_path / "audit.log").read().splitlines()) == 7


def test_truncation(cfg, monkeypatch):
    monkeypatch.setattr(sshexec.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="x" * 30000, stderr=""))
    st = cfg.stands["dev"]
    out = run_on_hosts(st, [st.hosts["dev-m1"]], "ro", "df -h", Audit(None), 3)
    assert "[обрезано]" in out and len(out) < 20200


def test_timeout_and_denied(cfg, monkeypatch):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 60)
    monkeypatch.setattr(sshexec.subprocess, "run", boom)
    st = cfg.stands["dev"]
    assert "ERROR timeout" in run_on_hosts(st, [st.hosts["dev-m1"]], "ro", "df -h", Audit(None), 3)
    monkeypatch.setattr(sshexec.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 126, stdout="", stderr="denied\n"))
    assert "отклонена обёрткой" in run_on_hosts(st, [st.hosts["dev-m1"]], "ro", "x", Audit(None), 3)


# ---------- инструменты MCP ----------

@pytest.fixture
def queue(tmp_path):
    return Queue(str(tmp_path / "queue"))


def tools(cfg, queue):
    return {t.name: t for t in build_tools(cfg, queue, Audit(None), "http://127.0.0.1:8765")}


def test_tool_names_prefixed(cfg, queue):
    t = tools(cfg, queue)
    for name in ("dev_ro_disk_free_all", "test_ro_disk_free_all", "prod_ro_disk_free_opt", "dev_ro_hosts_list",
                 "stands_list", "rw_result", "dev_ro_dev_only", "dev_rw_file_create", "dev_rw_file_delete"):
        assert name in t, name
    assert "test_ro_dev_only" not in t
    assert t["dev_ro_disk_free_all"].description.startswith("[СТЕНД: DEV]")


def test_no_write_tools_without_rw(cfg, queue):
    t = tools(cfg, queue)
    assert "prod_rw_file_create" not in t and "prod_rw_file_delete" not in t
    assert "test_rw_file_delete" in t  # у test есть rw, но без agent_otp


def test_read_tool_runs_on_all_hosts(cfg, queue, fake):
    out = tools(cfg, queue)["dev_ro_disk_free_all"].fn()
    assert out.count("[stand=dev host=") == 3 and "SUMMARY: ok=3 failed=0" in out


def test_read_tool_host_validation(cfg, queue, fake):
    t = tools(cfg, queue)
    assert "ОШИБКА" in t["dev_ro_disk_free_worker"].fn(host=None)
    assert "ОШИБКА" in t["dev_ro_disk_free_worker"].fn(host="prod-w1")
    assert "host=dev-w2" in t["dev_ro_disk_free_worker"].fn(host="dev-w2")
    assert not any("10.2.0." in c[0][-2] for c in fake.calls)


def test_ro_hosts_list_per_stand(cfg, queue):
    t = tools(cfg, queue)
    out = t["dev_ro_hosts_list"].fn(tag="swarm_worker")
    assert "dev-w1" in out and "dev-m1" not in out and "prod" not in out
    assert "test-m1" in t["test_ro_hosts_list"].fn(tag=None)


def test_write_tool_requires_enrollment(cfg, queue, fake):
    t = tools(cfg, queue)
    out = t["dev_rw_file_create"].fn(host="dev-w1", path="mcp-sandbox/a.txt")
    assert out.startswith("ОШИБКА: OTP не настроен") and "approver enroll" in out and "ro) работают" in out
    assert "approver enroll" in t["stands_list"].fn()
    assert t["rw_approve"].fn(id="0" * 32, code="123456").startswith("ОШИБКА")
    assert "SUMMARY" in t["dev_ro_disk_free_all"].fn()  # ro работают без OTP
    fake.calls.clear()
    assert not fake.calls and not os.path.isdir(queue.requests)


def test_write_tool_creates_request_not_ssh(cfg, queue, fake):
    queue.write_status(True)
    t = tools(cfg, queue)
    out = t["dev_rw_file_create"].fn(host="dev-w1", path="mcp-sandbox/a.txt")
    assert "ЗАЯВКА СОЗДАНА" in out and not fake.calls
    rid = queue.request_ids()[0]
    req = queue.request(rid)
    assert req["cmd"] == "touch -- mcp-sandbox/a.txt" and req["host"] == "dev-w1"
    assert t["rw_result"].fn(id=rid).startswith("ещё не выполнена")
    assert "ОШИБКА" in t["rw_result"].fn(id="../../etc/passwd")
    assert "ОШИБКА" in t["dev_rw_file_create"].fn(host="dev-w1", path="/etc/passwd")


def test_no_key_paths_in_outputs(cfg, queue, fake):
    queue.write_status(True)
    t = tools(cfg, queue)
    outs = [t["stands_list"].fn(), t["dev_ro_hosts_list"].fn(tag=None), t["dev_ro_disk_free_all"].fn(),
            t["dev_rw_file_create"].fn(host="dev-w1", path="mcp-sandbox/a")]
    outs += [x.description for x in t.values()]
    for o in outs:
        assert "/keys" not in o


def test_fastmcp_registration(cfg, queue):
    from mcp.server.fastmcp import FastMCP
    m = FastMCP("t")
    for s in build_tools(cfg, queue, Audit(None), "http://x"):
        m.add_tool(s.fn, name=s.name, description=s.description)
    listed = {t.name: t for t in asyncio.run(m.list_tools())}
    assert listed["dev_ro_logs"].inputSchema["required"] == ["host", "unit", "n"] or \
        set(listed["dev_ro_logs"].inputSchema["required"]) == {"host", "unit", "n"}
    assert "required" not in listed["dev_ro_disk_free_all"].inputSchema or \
        not listed["dev_ro_disk_free_all"].inputSchema["required"]


def test_description_optional_and_cmd_shown(make_cfg, queue):
    import copy
    from conftest import COMMANDS
    c = copy.deepcopy(COMMANDS)
    del c["common"]["read"]["disk_free_opt"]["description"]
    cfg = make_cfg(commands=c)
    t = tools(cfg, queue)
    assert "disk_free_opt" in t["dev_ro_disk_free_opt"].description
    assert "`df -h /opt`" in t["dev_ro_disk_free_opt"].description
    assert "`touch -- {path}`" in t["dev_rw_file_create"].description
    assert t["dev_ro_disk_free_all"].description.startswith("[СТЕНД: DEV] df всех.")


# ---------- dry_run ----------

def test_dry_run_ro_sends_nothing(cfg, queue, fake):
    t = tools(cfg, queue)
    out = t["dev_ro_disk_free_all"].fn(dry_run=True)
    assert not fake.calls
    assert out.startswith("[stand=dev role=ro] ПРОБНЫЙ ЗАПУСК")
    assert "хосты (3): dev-m1 (10.0.0.1), dev-w1 (10.0.0.11), dev-w2 (10.0.0.12)" in out
    assert "команда: df -h\n" in out and "rules.ro: совпадает" in out and "до 3 хостов одновременно" in out
    out = t["dev_ro_logs"].fn(host="dev-w1", unit="ssh", n="20", dry_run=True)
    assert "команда: journalctl --no-pager -u ssh -n 20" in out and "dev-w1 (10.0.0.11)" in out and not fake.calls


def test_dry_run_still_validates(cfg, queue, fake):
    t = tools(cfg, queue)
    assert t["dev_ro_logs"].fn(host="dev-w1", unit="ssh; id", n="1", dry_run=True).startswith("ОШИБКА")
    assert t["dev_ro_disk_free_worker"].fn(host="dev-m1", dry_run=True).startswith("ОШИБКА")
    assert t["dev_ro_disk_free_worker"].fn(host=None, dry_run=True).startswith("ОШИБКА")
    assert not fake.calls


def test_dry_run_false_executes(cfg, queue, fake):
    assert "SUMMARY" in tools(cfg, queue)["dev_ro_disk_free_all"].fn(dry_run=False) and len(fake.calls) == 3


def test_dry_run_rw_creates_no_request(cfg, queue, fake):
    t = tools(cfg, queue)
    out = t["dev_rw_file_create"].fn(host="dev-w1", path="mcp-sandbox/a.txt", dry_run=True)  # OTP не настроен
    assert "ПРОБНЫЙ ЗАПУСК" in out and "команда: touch -- mcp-sandbox/a.txt" in out and "заявка НЕ создана" in out
    queue.write_status(True)
    t["dev_rw_file_create"].fn(host="dev-w1", path="mcp-sandbox/a.txt", dry_run=True)
    assert queue.request_ids() == [] and not fake.calls


def test_dry_run_in_schema_and_about(cfg, queue):
    from mcp.server.fastmcp import FastMCP
    m = FastMCP("t")
    for s in build_tools(cfg, queue, Audit(None), "http://x"):
        m.add_tool(s.fn, name=s.name, description=s.description)
    listed = {x.name: x for x in asyncio.run(m.list_tools())}
    for n in ("dev_ro_disk_free_all", "dev_rw_file_create"):
        prop = listed[n].inputSchema["properties"]["dry_run"]
        assert prop["type"] == "boolean" and prop["default"] is False
        assert "dry_run" not in listed[n].inputSchema.get("required", [])
    assert "dry_run" not in listed["about"].inputSchema.get("properties", {})
    assert "ПРОБНЫЙ ЗАПУСК" in tools(cfg, queue)["about"].fn()


def test_unknown_params_rejected_not_ignored(cfg, queue, fake):
    from mcp.server.fastmcp.exceptions import ToolError
    from safety_mcp.mcpserver import StrictFastMCP
    m = StrictFastMCP("t")
    for s in build_tools(cfg, queue, Audit(None), "http://x"):
        m.add_tool(s.fn, name=s.name, description=s.description)
    with pytest.raises(ToolError, match="неизвестные параметры \\['path'\\]"):
        asyncio.run(m.call_tool("dev_ro_disk_free_opt", {"path": "; ls -la"}))
    with pytest.raises(ToolError, match="неизвестные параметры"):
        asyncio.run(m.call_tool("about", {"x": "1"}))
    assert not fake.calls
    res = asyncio.run(m.call_tool("dev_ro_disk_free_opt", {"dry_run": True}))
    assert "ПРОБНЫЙ ЗАПУСК" in str(res)
