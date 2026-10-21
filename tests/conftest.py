import copy
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from safety_mcp.config import load_config  # noqa: E402

PATH_RX = "safety-sandbox(/[A-Za-z0-9_][A-Za-z0-9._-]{0,63}){1,4}"

INVENTORY = {
    "settings": {"max_parallel": 3, "max_hosts": 20},
    "stands": {
        "dev": {
            "ssh_user": "opsuser", "known_hosts": "known_hosts.dev",
            "keys": {"ro": "/keys/dev_ro", "rw": "/keys-rw/dev_rw"}, "agent_otp": True,
            "hosts": {
                "dev-m1": {"address": "10.0.0.1", "tags": ["swarm_manager"]},
                "dev-w1": {"address": "10.0.0.11", "tags": ["swarm_worker"]},
                "dev-w2": {"address": "10.0.0.12", "tags": ["swarm_worker"]},
            },
        },
        "test": {
            "ssh_user": "opsuser", "known_hosts": "known_hosts.test",
            "keys": {"ro": "/keys/test_ro", "rw": "/keys-rw/test_rw"},
            "hosts": {"test-m1": {"address": "10.1.0.1", "tags": ["swarm_manager"]}},
        },
        "prod": {
            "ssh_user": "opsuser", "known_hosts": "known_hosts.prod",
            "keys": {"ro": "/keys/prod_ro"}, "max_parallel": 10,
            "hosts": {"prod-w1": {"address": "10.2.0.11", "tags": ["swarm_worker"]}},
        },
    },
}

COMMANDS = {
    "common": {
        "read": {
            "disk_free_all": {"description": "df всех", "tags": [], "pick": "all", "cmd": "df -h"},
            "disk_free_opt": {"description": "df opt", "tags": [], "pick": "all", "cmd": "df -h /opt"},
            "disk_free_worker": {"description": "df одного воркера", "tags": ["swarm_worker"], "pick": "host",
                                 "cmd": "df -h /"},
            "swarm_nodes_list": {"description": "ноды", "tags": ["swarm_manager"], "pick": "first",
                                 "cmd": "sudo -n docker node ls"},
            "logs": {"description": "журнал", "tags": [], "pick": "host",
                     "params": {"unit": "[a-z0-9][a-z0-9@._-]{0,63}", "n": "[0-9]{1,4}"},
                     "cmd": "journalctl --no-pager -u {unit} -n {n}"},
        },
        "write": {
            "file_create": {"description": "создать", "tags": [], "pick": "host",
                            "params": {"path": PATH_RX}, "cmd": "touch -- {path}"},
            "file_delete": {"description": "удалить", "tags": [], "pick": "host",
                            "params": {"path": PATH_RX}, "cmd": "rm -f -- {path}"},
        },
    },
    "stands": {
        "dev": {"read": {"dev_only": {"description": "только dev", "tags": [], "pick": "first", "cmd": "uptime"}}},
        "prod": {},
    },
}


def write_conf(path, inventory=None, commands=None):
    os.makedirs(path, exist_ok=True)
    inv = copy.deepcopy(INVENTORY if inventory is None else inventory)
    cmds = copy.deepcopy(COMMANDS if commands is None else commands)
    with open(os.path.join(path, "inventory.json"), "w") as f:
        json.dump(inv, f)
    with open(os.path.join(path, "commands.json"), "w") as f:
        json.dump(cmds, f)
    for s in inv.get("stands", {}).values():
        kh = s.get("known_hosts")
        if kh:
            with open(os.path.join(path, kh), "w") as f:
                f.write("10.0.0.1 ssh-ed25519 AAAA\n")
    return path


@pytest.fixture
def conf_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SAFETY_ALLOW_WRITABLE", "1")
    return write_conf(str(tmp_path / "conf"))


@pytest.fixture
def cfg(conf_dir):
    return load_config(conf_dir)


@pytest.fixture
def make_cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SAFETY_ALLOW_WRITABLE", "1")
    counter = {"n": 0}

    def make(inventory=None, commands=None):
        counter["n"] += 1
        d = write_conf(str(tmp_path / f"c{counter['n']}"), inventory, commands)
        return load_config(d)
    return make
