import os
import stat
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "init-local.sh")


def run(tmp_path, *args):
    env = dict(os.environ, HOME=str(tmp_path / "home"))
    os.makedirs(env["HOME"], exist_ok=True)
    return subprocess.run(["bash", SCRIPT, "--dir", str(tmp_path / "data"), "--no-chown", *args],
                          env=env, capture_output=True, text=True, timeout=60)


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_creates_layout_from_templates(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    d = tmp_path / "data"
    for sub in ("conf", "keys-ro", "keys-rw", "state", "queue/requests", "queue/results", "approver-state"):
        assert (d / sub).is_dir(), sub
    assert sorted(os.listdir(d / "conf")) == sorted(os.listdir(os.path.join(ROOT, "conf.example")))
    assert (d / ".env").read_text() == open(os.path.join(ROOT, ".env.example")).read()
    assert mode(d / "keys-ro") == 0o700 and mode(d / "keys-rw") == 0o700
    assert "Дальше:" in r.stdout and "enroll" in r.stdout


def test_keys_generated_and_rw_private_only_in_keys_rw(tmp_path):
    r = run(tmp_path, "--keys", "dev")
    assert r.returncode == 0, r.stderr
    d, home = tmp_path / "data", tmp_path / "home" / ".ssh"
    assert (d / "keys-ro" / "mcp_dev_ro").exists() and (home / "mcp_dev_ro").exists()
    assert (d / "keys-rw" / "mcp_dev_rw").exists() and (home / "mcp_dev_rw.pub").exists()
    assert not (home / "mcp_dev_rw").exists()
    assert mode(d / "keys-ro" / "mcp_dev_ro") == 0o600 and mode(d / "keys-rw" / "mcp_dev_rw") == 0o600


def test_never_overwrites(tmp_path):
    assert run(tmp_path, "--keys", "dev").returncode == 0
    d = tmp_path / "data"
    (d / "conf" / "inventory.json").write_text("{\"mine\": 1}")
    key_before = (d / "keys-rw" / "mcp_dev_rw").read_bytes()
    r = run(tmp_path, "--keys", "dev", "--keys", "prod")
    assert r.returncode == 0, r.stderr
    assert (d / "conf" / "inventory.json").read_text() == "{\"mine\": 1}"
    assert (d / "keys-rw" / "mcp_dev_rw").read_bytes() == key_before
    assert (d / "keys-rw" / "mcp_prod_rw").exists()
    assert "оставлено: conf/inventory.json" in r.stdout and "создано:   ключ mcp_prod_ro" in r.stdout


@pytest.mark.parametrize("bad", ["Dev", "global", "a-b", "1x", ""])
def test_bad_stand_name(tmp_path, bad):
    r = run(tmp_path, "--keys", bad)
    assert r.returncode == 1 and not (tmp_path / "data").exists()


def test_unknown_option(tmp_path):
    assert run(tmp_path, "--wat").returncode == 1
