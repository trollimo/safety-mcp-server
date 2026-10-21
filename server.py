#!/usr/bin/env python3
"""MCP-сервер (без аргументов) и CLI для администратора.

  python server.py                         запустить MCP-сервер (stdio)
  python server.py check                   проверить конфиг
  python server.py list-hosts <stand>      "имя адрес" по строкам
  python server.py ssh-user <stand>
  python server.py key-path <stand> <ro|rw>
  python server.py gen-rules <stand> <host> <ro|rw>
CLI не импортирует пакет mcp и работает на чистом python3.
"""
import os
import sys

from safety_mcp import SERVER_NAME, __version__, queue_dir, state_dir
from safety_mcp.config import ConfigError, load_config
from safety_mcp.rules import gen_rules


def cli(argv: list[str]) -> int:
    cmd, args = argv[0], argv[1:]
    # deploy.sh запускается человеком на хосте, где конфиг лежит в рабочем каталоге
    try:
        cfg = load_config(check_writable=(cmd in ("check", "serve")))
        if cmd == "check":
            n = sum(len(cfg.commands_for(s)) for s in cfg.stands)
            print(f"ok: стендов {len(cfg.stands)}, команд (с учётом стендов) {n}")
        elif cmd == "list-hosts" and len(args) == 1:
            for h in sorted(cfg.stands[args[0]].hosts.values(), key=lambda h: h.name):
                print(h.name, h.address)
        elif cmd == "ssh-user" and len(args) == 1:
            print(cfg.stands[args[0]].ssh_user)
        elif cmd == "key-path" and len(args) == 2:
            print(cfg.stands[args[0]].keys[args[1]])
        elif cmd == "gen-rules" and len(args) == 3:
            sys.stdout.write(gen_rules(cfg, *args))
        else:
            print(__doc__, file=sys.stderr)
            return 1
    except ConfigError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 2
    except KeyError as e:
        print(f"ошибка: не найдено {e}", file=sys.stderr)
        return 2
    return 0


def serve() -> int:
    from safety_mcp.mcpserver import StrictFastMCP
    from safety_mcp.queue import Queue
    from safety_mcp.sshexec import Audit
    from safety_mcp.tools import about_text, build_tools

    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"{SERVER_NAME}: ошибка конфигурации: {e}", file=sys.stderr)
        return 2
    queue = Queue(queue_dir())
    audit = Audit(os.path.join(state_dir(), "audit.log"))
    # instructions клиент передаёт агенту при подключении: правила работы видны без вызова about
    mcp = StrictFastMCP(SERVER_NAME, instructions=about_text(cfg))
    for spec in build_tools(cfg, queue, audit, os.environ.get("APPROVER_URL", "http://127.0.0.1:8765")):
        mcp.add_tool(spec.fn, name=spec.name, description=spec.description)
    print(f"{SERVER_NAME} {__version__}: готов", file=sys.stderr)
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]) if len(sys.argv) > 1 else serve())
