"""Выполнение команд по SSH: один хост или несколько параллельно."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .config import Host, Stand

OUTPUT_LIMIT = 20000
TIMEOUT = 60


class Audit:
    def __init__(self, path: str | None):
        self.path = path
        self._lock = threading.Lock()

    def write(self, **fields) -> None:
        if not self.path:
            return
        line = time.strftime("%Y-%m-%dT%H:%M:%S%z") + " " + " ".join(f"{k}={v!r}" for k, v in fields.items())
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")


def header(stand: Stand, host: Host, role: str) -> str:
    return f"[stand={stand.name} host={host.name} role={role}]"


def ssh_argv(stand: Stand, host: Host, role: str, remote_cmd: str) -> list[str]:
    return [
        "ssh", "-n",
        "-i", stand.keys[role],
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={stand.known_hosts}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "ForwardAgent=no",
        "-o", "ClearAllForwardings=yes",
        "-o", "LogLevel=ERROR",
        "--",
        f"{stand.ssh_user}@{host.address}",
        remote_cmd,
    ]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[обрезано]"


def run_one(stand: Stand, host: Host, role: str, remote_cmd: str, audit: Audit,
            limit: int = OUTPUT_LIMIT) -> tuple[int, str]:
    """Возвращает (код возврата, текст). Исключения не пробрасываются."""
    try:
        r = subprocess.run(ssh_argv(stand, host, role, remote_cmd), capture_output=True, text=True,
                           timeout=TIMEOUT, stdin=subprocess.DEVNULL, shell=False)
        rc, out = r.returncode, (r.stdout or "") + (r.stderr or "")
        if rc == 126 and "denied" in out:
            out = out.rstrip() + "\n(команда отклонена обёрткой на сервере)"
        elif rc == 255:
            out = "ERROR ssh: не удалось подключиться или аутентифицироваться\n" + out
    except subprocess.TimeoutExpired:
        rc, out = -1, f"ERROR timeout {TIMEOUT}s"
    except OSError as e:
        rc, out = -1, f"ERROR {type(e).__name__}"
    except Exception as e:  # noqa: BLE001 - сбой одного хоста не должен ронять остальные
        rc, out = -1, f"ERROR {type(e).__name__}"
    audit.write(type="ssh", stand=stand.name, host=host.name, role=role, cmd=remote_cmd, rc=rc)
    return rc, _truncate(out, limit)


def run_on_hosts(stand: Stand, hosts: list[Host], role: str, remote_cmd: str, audit: Audit,
                 max_parallel: int) -> str:
    if len(hosts) == 1:
        h = hosts[0]
        rc, out = run_one(stand, h, role, remote_cmd, audit)
        return f"{header(stand, h, role)} rc={rc}\n{out}".rstrip() + "\n"
    limit = max(200, OUTPUT_LIMIT // len(hosts))
    workers = max(1, min(max_parallel, len(hosts)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda h: run_one(stand, h, role, remote_cmd, audit, limit), hosts))
    blocks, ok = [], 0
    for h, (rc, out) in zip(hosts, results):
        ok += rc == 0
        blocks.append(f"{header(stand, h, role)} rc={rc}\n{out}".rstrip())
    blocks.append(f"SUMMARY: ok={ok} failed={len(hosts) - ok}")
    return "\n\n".join(blocks) + "\n"
