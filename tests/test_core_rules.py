import re

import pytest

from conftest import PATH_RX
from safety_mcp.config import ConfigError
from safety_mcp.core import ToolError, build_command, command_regex, matches_write_rule, select_hosts
from safety_mcp.rules import gen_rules


def names(hosts):
    return [h.name for h in hosts]


def test_pick_first_selects_manager_of_same_stand(cfg):
    cmd = cfg.command("dev", "swarm_nodes_list")
    assert names(select_hosts(cfg, cfg.stands["dev"], cmd, None)) == ["dev-m1"]
    assert names(select_hosts(cfg, cfg.stands["test"], cfg.command("test", "swarm_nodes_list"), None)) == ["test-m1"]


def test_pick_all_selects_all_matching(cfg):
    assert names(select_hosts(cfg, cfg.stands["dev"], cfg.command("dev", "disk_free_all"), None)) == \
        ["dev-m1", "dev-w1", "dev-w2"]


def test_pick_host_requires_host(cfg):
    cmd = cfg.command("dev", "disk_free_worker")
    with pytest.raises(ToolError, match="нужен параметр host"):
        select_hosts(cfg, cfg.stands["dev"], cmd, None)
    assert names(select_hosts(cfg, cfg.stands["dev"], cmd, "dev-w1")) == ["dev-w1"]


def test_host_from_other_stand_rejected(cfg):
    cmd = cfg.command("dev", "disk_free_worker")
    with pytest.raises(ToolError, match="не найден"):
        select_hosts(cfg, cfg.stands["dev"], cmd, "prod-w1")


def test_host_without_tags_rejected(cfg):
    cmd = cfg.command("dev", "disk_free_worker")
    with pytest.raises(ToolError, match="нет тегов"):
        select_hosts(cfg, cfg.stands["dev"], cmd, "dev-m1")


def test_max_hosts(make_cfg):
    from conftest import INVENTORY
    import copy
    inv = copy.deepcopy(INVENTORY)
    inv["settings"]["max_hosts"] = 2
    cfg = make_cfg(inv)
    with pytest.raises(ToolError, match="слишком много"):
        select_hosts(cfg, cfg.stands["dev"], cfg.command("dev", "disk_free_all"), None)


def test_no_matching_host(cfg):
    with pytest.raises(ToolError, match="нет хоста"):
        select_hosts(cfg, cfg.stands["prod"], cfg.command("prod", "swarm_nodes_list"), None)


def test_build_command_ok(cfg):
    cmd = cfg.command("dev", "logs")
    assert build_command(cmd, {"unit": "ssh", "n": "50"}) == "journalctl --no-pager -u ssh -n 50"
    assert build_command(cfg.command("dev", "file_create"), {"path": "safety-sandbox/a/b.txt"}) == \
        "touch -- safety-sandbox/a/b.txt"


@pytest.mark.parametrize("value", ["ssh; id", "$(id)", "-f", "", "a b", "`id`", "ssh\nid", "../x"])
def test_build_command_rejects_injection(cfg, value):
    with pytest.raises(ToolError):
        build_command(cfg.command("dev", "logs"), {"unit": value, "n": "5"})


@pytest.mark.parametrize("path", ["safety-sandbox", "safety-sandbox/../etc/passwd", "/etc/passwd", "safety-sandbox/.ssh",
                                  "safety-sandbox/a/b/c/d/e", "safety-sandbox//a", "x/safety-sandbox/a", "safety-sandbox/-rf"])
def test_path_param_rejects_escape(cfg, path):
    with pytest.raises(ToolError):
        build_command(cfg.command("dev", "file_delete"), {"path": path})


def test_missing_and_unexpected_params(cfg):
    with pytest.raises(ToolError, match="не хватает"):
        build_command(cfg.command("dev", "logs"), {"unit": "ssh"})
    with pytest.raises(ToolError, match="неожиданные"):
        build_command(cfg.command("dev", "disk_free_all"), {"x": "1"})


def test_command_regex_escapes_literals(cfg):
    rx = command_regex(cfg.command("dev", "file_create"))
    assert rx == f"touch -- ({PATH_RX})"
    assert re.fullmatch(command_regex(cfg.command("dev", "disk_free_opt")), "df -h /opt")
    from safety_mcp.config import Command
    c = Command("x", "read", "ssh", "", (), "first", {}, "a.b+c", "common")
    assert command_regex(c) == r"a\.b\+c"
    assert not re.fullmatch(command_regex(c), "aXb+c")


def test_gen_rules_by_tags_and_role(cfg):
    ro_m = gen_rules(cfg, "dev", "dev-m1", "ro").splitlines()
    ro_w = gen_rules(cfg, "dev", "dev-w1", "ro").splitlines()
    assert ro_m[0].startswith("# generated sha256=") and "stand=dev host=dev-m1 role=ro" in ro_m[0]
    assert r"sudo -n docker node ls" in ro_m and r"sudo -n docker node ls" not in ro_w
    assert "df -h /" in ro_w and "df -h /" not in ro_m
    assert "uptime" in ro_m  # dev_only: команда стенда dev
    rw = [r for r in gen_rules(cfg, "dev", "dev-w1", "rw").splitlines() if not r.startswith("#")]
    assert any(r.startswith("touch -- ") for r in rw) and not any("df" in r for r in rw)


def test_gen_rules_other_stand_excludes_stand_commands(cfg):
    assert "uptime" not in gen_rules(cfg, "test", "test-m1", "ro").splitlines()


def test_gen_rules_rw_without_key(cfg):
    with pytest.raises(ConfigError, match="нет ключа rw"):
        gen_rules(cfg, "prod", "prod-w1", "rw")


def test_matches_write_rule(cfg):
    st, h = cfg.stands["dev"], cfg.stands["dev"].hosts["dev-w1"]
    assert matches_write_rule(cfg, st, h, "touch -- safety-sandbox/x").name == "file_create"
    assert matches_write_rule(cfg, st, h, "rm -rf -- safety-sandbox/x") is None
    assert matches_write_rule(cfg, st, h, "df -h") is None
