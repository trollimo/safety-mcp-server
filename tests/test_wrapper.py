import os
import subprocess

import pytest

from safety_mcp.rules import gen_rules

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAPPER = os.path.join(ROOT, "wrapper", "mcp-wrapper")


@pytest.fixture
def home(cfg, tmp_path):
    d = tmp_path / "mcp"
    d.mkdir()
    (d / "rules.ro").write_text(gen_rules(cfg, "dev", "dev-w1", "ro") + "echo ok-[0-9]+\n")
    (d / "rules.rw").write_text(gen_rules(cfg, "dev", "dev-w1", "rw"))
    return str(d)


def run(home, role, cmd):
    env = {"PATH": os.environ["PATH"], "HOME": home, "MCP_SSH_HOME": home, "SSH_ORIGINAL_COMMAND": cmd}
    r = subprocess.run(["bash", WRAPPER, role], env=env, capture_output=True, text=True, timeout=10)
    return r.returncode, r.stdout, r.stderr


def test_allowed_commands_execute(home):
    rc, out, _ = run(home, "ro", "echo ok-42")
    assert rc == 0 and out.strip() == "ok-42"
    rc, out, _ = run(home, "ro", "df -h")
    assert rc == 0 and len(out.splitlines()) >= 2


@pytest.mark.parametrize("cmd", ["echo ok-1; id", "echo ok-1 && id", "echo $(id)", "echo ok-1 | cat", "id",
                                 "", "echo ok-1\nid", "echo ok-x", "df -h /etc", "df -h; id", "touch -- mcp-sandbox/a"])
def test_denied_ro(home, cmd):
    rc, out, err = run(home, "ro", cmd)
    assert rc == 126 and "denied" in err


def test_rw_rules_separate(home, tmp_path):
    os.makedirs(os.path.join(home, "mcp-sandbox"))
    cwd = os.getcwd()
    os.chdir(home)
    try:
        assert run(home, "rw", "touch -- mcp-sandbox/f.txt")[0] == 0
        assert os.path.exists(os.path.join(home, "mcp-sandbox", "f.txt"))
        assert run(home, "rw", "rm -f -- mcp-sandbox/f.txt")[0] == 0
        assert not os.path.exists(os.path.join(home, "mcp-sandbox", "f.txt"))
        assert run(home, "rw", "rm -rf -- mcp-sandbox")[0] == 126
        assert run(home, "rw", "rm -f -- mcp-sandbox/../x")[0] == 126
        assert run(home, "rw", "df -h")[0] == 126
    finally:
        os.chdir(cwd)


def test_bad_role_and_missing_rules(home, tmp_path):
    assert run(home, "root", "echo ok-1")[0] == 126
    assert run(str(tmp_path / "empty"), "ro", "echo ok-1")[0] == 126


def test_version():
    r = subprocess.run(["bash", WRAPPER, "--version"], capture_output=True, text=True)
    assert r.stdout.startswith("mcp-wrapper ")
