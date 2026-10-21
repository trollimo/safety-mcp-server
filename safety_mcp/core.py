"""Выбор хостов и сборка итоговой команды. Здесь проходит граница безопасности на стороне MCP."""
from __future__ import annotations

import re
import shlex

from .config import CMD_CHARSET_RE, PLACEHOLDER_RE, Command, Config, Host, Stand


class ToolError(Exception):
    """Ошибка, которую можно показать агенту (без путей к ключам и внутренних деталей)."""


def select_hosts(cfg: Config, stand: Stand, cmd: Command, host: str | None) -> list[Host]:
    need = set(cmd.tags)
    if host:
        names = [n.strip() for n in host.split(",")]
        if any(not n for n in names):
            raise ToolError("пустое имя хоста в списке (формат: h1,h2,h3)")
        if len(set(names)) != len(names):
            raise ToolError("хост указан несколько раз")
        if len(names) > cfg.max_hosts:
            raise ToolError(f"слишком много хостов: {len(names)} > max_hosts={cfg.max_hosts}")
        out = []
        for n in names:
            h = stand.hosts.get(n)
            if h is None:
                raise ToolError(f"хост {n!r} не найден в стенде {stand.name}")
            if not need <= set(h.tags):
                raise ToolError(f"у хоста {n} нет тегов {sorted(need - set(h.tags))}, нужных команде {cmd.name}")
            out.append(h)
        return sorted(out, key=lambda h: h.name)
    if cmd.pick == "host":
        raise ToolError(f"команде {cmd.name} нужен параметр host (хост стенда {stand.name} с тегами {sorted(need)})")
    matching = sorted((h for h in stand.hosts.values() if need <= set(h.tags)), key=lambda h: h.name)
    if not matching:
        raise ToolError(f"в стенде {stand.name} нет хоста с тегами {sorted(need)}")
    if cmd.pick == "first":
        return matching[:1]
    if len(matching) > cfg.max_hosts:
        raise ToolError(f"слишком много хостов: {len(matching)} > max_hosts={cfg.max_hosts}")
    return matching


def build_command(cmd: Command, args: dict) -> str:
    given = {k: v for k, v in args.items() if v is not None}
    unexpected = set(given) - set(cmd.params)
    if unexpected:
        raise ToolError(f"неожиданные параметры: {sorted(unexpected)}")
    missing = set(cmd.params) - set(given)
    if missing:
        raise ToolError(f"не хватает параметров: {sorted(missing)}")
    for name, value in given.items():
        if not isinstance(value, str) or not re.fullmatch(cmd.params[name], value):
            raise ToolError(f"параметр {name}: значение не соответствует шаблону {cmd.params[name]}")
        if shlex.quote(value) != value:
            raise ToolError(f"параметр {name}: значение требует экранирования, это запрещено")
    final = PLACEHOLDER_RE.sub(lambda m: shlex.quote(given[m.group(1)]), cmd.cmd)
    if not CMD_CHARSET_RE.fullmatch(final):
        raise ToolError("итоговая команда содержит запрещённые символы или слишком длинная")
    return final


def command_regex(cmd: Command) -> str:
    """ERE-regex на всю команду: литералы экранированы, {param} -> (regex)."""
    special = set(".^$*+?()[]{}|\\")
    out, pos = [], 0
    for m in PLACEHOLDER_RE.finditer(cmd.cmd):
        out.append("".join("\\" + c if c in special else c for c in cmd.cmd[pos:m.start()]))
        out.append("(" + cmd.params[m.group(1)] + ")")
        pos = m.end()
    out.append("".join("\\" + c if c in special else c for c in cmd.cmd[pos:]))
    return "".join(out)


def matches_write_rule(cfg: Config, stand: Stand, host: Host, cmd_text: str) -> Command | None:
    """Повторная проверка заявки на стороне approver: команда должна совпадать с write-командой стенда."""
    for c in cfg.commands_for(stand.name):
        if c.kind != "write" or c.type != "ssh" or not set(c.tags) <= set(host.tags):
            continue
        if re.fullmatch(command_regex(c), cmd_text):
            return c
    return None
