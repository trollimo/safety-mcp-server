import copy
import os

import pytest

from conftest import COMMANDS, INVENTORY, write_conf
from safety_mcp.config import ConfigError, load_config


def inv(**changes):
    d = copy.deepcopy(INVENTORY)
    for path, value in changes.items():
        node = d
        keys = path.split("__")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value
    return d


def test_valid_config_loads(cfg):
    assert set(cfg.stands) == {"dev", "test", "prod"}
    assert cfg.max_parallel == 3
    assert cfg.stands["prod"].max_parallel == 10
    assert cfg.stands["dev"].max_parallel == 3


def test_common_and_stand_commands(cfg):
    dev = {c.name for c in cfg.commands_for("dev")}
    test = {c.name for c in cfg.commands_for("test")}
    assert "dev_only" in dev and "dev_only" not in test
    assert {"disk_free_all", "disk_free_opt", "file_create"} <= test
    assert {c.name for c in cfg.commands_for("prod")} == {c.name for c in cfg.commands_for("test")}


@pytest.mark.parametrize("bad", ["Dev", "global", "1dev", "dev-1", "a" * 17])
def test_bad_stand_names(make_cfg, bad):
    d = copy.deepcopy(INVENTORY)
    d["stands"][bad] = d["stands"].pop("test")
    with pytest.raises(ConfigError):
        make_cfg(d)


@pytest.mark.parametrize("value", [0, 51, "a", 3.5, True])
def test_max_parallel_bounds(make_cfg, value):
    with pytest.raises(ConfigError):
        make_cfg(inv(settings__max_parallel=value))
    with pytest.raises(ConfigError):
        make_cfg(inv(stands__dev__max_parallel=value))


def test_max_hosts_bounds(make_cfg):
    with pytest.raises(ConfigError):
        make_cfg(inv(settings__max_hosts=0))


def test_ro_key_required_with_hosts(make_cfg):
    with pytest.raises(ConfigError, match="keys.ro"):
        make_cfg(inv(stands__test__keys={}))


def test_relative_key_path_rejected(make_cfg):
    with pytest.raises(ConfigError, match="абсолютный"):
        make_cfg(inv(stands__test__keys={"ro": "keys/x"}))


@pytest.mark.parametrize("addr", ["-oProxyCommand=x", "a b", "", "10.0.0.1;id"])
def test_bad_address(make_cfg, addr):
    with pytest.raises(ConfigError):
        make_cfg(inv(stands__test__hosts={"t1": {"address": addr, "tags": []}}))


def test_unknown_stand_in_commands(make_cfg):
    c = copy.deepcopy(COMMANDS)
    c["stands"]["nope"] = {}
    with pytest.raises(ConfigError, match="нет в inventory"):
        make_cfg(commands=c)


def test_duplicate_name_common_and_stand(make_cfg):
    c = copy.deepcopy(COMMANDS)
    c["stands"]["dev"]["read"]["disk_free_all"] = {"cmd": "df -h"}
    with pytest.raises(ConfigError, match="и в common"):
        make_cfg(commands=c)


def test_empty_or_missing_stand_block_ok(make_cfg):
    c = copy.deepcopy(COMMANDS)
    del c["stands"]
    cfg = make_cfg(commands=c)
    assert {x.name for x in cfg.commands_for("dev")} == {x.name for x in cfg.commands_for("test")}


@pytest.mark.parametrize("rx", [r"\d+", r"(?i)abc", r"\w+", "^abc", "abc$", "(", ""])
def test_non_ere_param_regex_rejected(make_cfg, rx):
    c = copy.deepcopy(COMMANDS)
    c["common"]["read"]["logs"]["params"]["n"] = rx
    with pytest.raises(ConfigError):
        make_cfg(commands=c)


@pytest.mark.parametrize("cmd", ["df -h; id", "df -h | head", "echo $HOME", "ls *", "cat 'a'", "a  b", " df"])
def test_forbidden_cmd(make_cfg, cmd):
    c = copy.deepcopy(COMMANDS)
    c["common"]["read"]["disk_free_all"]["cmd"] = cmd
    with pytest.raises(ConfigError):
        make_cfg(commands=c)


def test_placeholder_param_mismatch(make_cfg):
    c = copy.deepcopy(COMMANDS)
    c["common"]["read"]["logs"]["cmd"] = "journalctl -u {unit}"
    with pytest.raises(ConfigError, match="плейсхолдеры"):
        make_cfg(commands=c)


def test_write_pick_all_rejected(make_cfg):
    c = copy.deepcopy(COMMANDS)
    c["common"]["write"]["file_create"]["pick"] = "all"
    with pytest.raises(ConfigError, match="pick: all"):
        make_cfg(commands=c)


@pytest.mark.parametrize("field", ["auth", "headers", "body", "via", "scope", "method"])
def test_reserved_fields_rejected(make_cfg, field):
    c = copy.deepcopy(COMMANDS)
    c["common"]["read"]["disk_free_all"][field] = "x"
    with pytest.raises(ConfigError):
        make_cfg(commands=c)


def test_http_type_not_supported_yet(make_cfg):
    c = copy.deepcopy(COMMANDS)
    c["common"]["read"]["disk_free_all"]["type"] = "http"
    with pytest.raises(ConfigError, match="не поддерживается"):
        make_cfg(commands=c)


def test_writable_config_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_SSH_ALLOW_WRITABLE", raising=False)
    d = write_conf(str(tmp_path / "c"))
    with pytest.raises(ConfigError, match="на запись"):
        load_config(d)
    assert load_config(d, check_writable=False)


@pytest.mark.skipif(os.geteuid() == 0, reason="root пишет в любые файлы")
def test_readonly_config_accepted(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_SSH_ALLOW_WRITABLE", raising=False)
    d = write_conf(str(tmp_path / "c"))
    for n in os.listdir(d):
        os.chmod(os.path.join(d, n), 0o444)
    os.chmod(d, 0o555)
    try:
        assert load_config(d)
    finally:
        os.chmod(d, 0o755)


def test_missing_known_hosts_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_SSH_ALLOW_WRITABLE", "1")
    d = write_conf(str(tmp_path / "c"))
    os.unlink(os.path.join(d, "known_hosts.dev"))
    with pytest.raises(ConfigError, match="known_hosts"):
        load_config(d)
