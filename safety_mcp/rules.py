"""Генерация правил для серверной обёртки mcp-wrapper из commands.json."""
from __future__ import annotations

import hashlib

from .config import Config, ConfigError
from .core import command_regex

ROLE_KIND = {"ro": "read", "rw": "write"}


def gen_rules(cfg: Config, stand_name: str, host_name: str, role: str) -> str:
    if role not in ROLE_KIND:
        raise ConfigError(f"роль должна быть ro или rw, получено {role!r}")
    stand = cfg.stands.get(stand_name)
    if stand is None:
        raise ConfigError(f"стенд {stand_name!r} не найден")
    host = stand.hosts.get(host_name)
    if host is None:
        raise ConfigError(f"хост {host_name!r} не найден в стенде {stand_name}")
    if role not in stand.keys:
        raise ConfigError(f"у стенда {stand_name} нет ключа {role}")
    digest = hashlib.sha256(cfg.raw_digest_source).hexdigest()
    lines = [f"# generated sha256={digest} stand={stand_name} host={host_name} role={role}"]
    for c in cfg.commands_for(stand_name):
        if c.kind == ROLE_KIND[role] and c.type == "ssh" and set(c.tags) <= set(host.tags):
            lines.append(command_regex(c))
    return "\n".join(lines) + "\n"
