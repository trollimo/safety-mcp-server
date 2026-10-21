"""Очередь заявок на запись: MCP пишет requests/, approver пишет results/."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import time

ID_RE = re.compile(r"^[0-9a-f]{32}$")
OTP_FILE_RE = re.compile(r"^([0-9a-f]{32})\.otp-([0-9a-f]{8})\.json$")
REQUEST_TTL = 600
STATUS_FILE = "_status.json"


def atomic_write_json(path: str, data: dict, mode: int = 0o644) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


class Queue:
    def __init__(self, base: str):
        self.requests = os.path.join(base, "requests")
        self.results = os.path.join(base, "results")

    def ensure(self, which: str) -> None:
        os.makedirs(getattr(self, which), exist_ok=True)

    def new_request(self, stand: str, hosts, command: str, cmd: str, now: float | None = None) -> dict:
        """Одна заявка на один или несколько хостов: подтверждается одним кодом OTP."""
        now = time.time() if now is None else now
        rid = secrets.token_hex(16)
        hosts = [hosts] if isinstance(hosts, str) else list(hosts)
        data = {
            "id": rid, "stand": stand, "hosts": hosts, "role": "rw", "command": command, "cmd": cmd,
            "cmd_sha256": hashlib.sha256(cmd.encode()).hexdigest(),
            "created": int(now), "expires": int(now + REQUEST_TTL),
        }
        self.ensure("requests")
        atomic_write_json(os.path.join(self.requests, rid + ".json"), data)
        return data

    def request(self, rid: str) -> dict | None:
        if not ID_RE.match(rid or ""):
            return None
        return read_json(os.path.join(self.requests, rid + ".json"))

    def request_ids(self) -> list[str]:
        try:
            names = os.listdir(self.requests)
        except OSError:
            return []
        return sorted(n[:-5] for n in names if n.endswith(".json") and ID_RE.match(n[:-5]))

    def result(self, rid: str) -> dict | None:
        if not ID_RE.match(rid or ""):
            return None
        return read_json(os.path.join(self.results, rid + ".json"))

    def write_result(self, rid: str, data: dict) -> None:
        if not ID_RE.match(rid):
            raise ValueError("bad id")
        self.ensure("results")
        atomic_write_json(os.path.join(self.results, rid + ".json"), data)

    # ---- OTP-код, переданный через агента: MCP пишет в requests/, approver отвечает в results/ ----
    def submit_code(self, rid: str, code: str) -> str:
        if not ID_RE.match(rid):
            raise ValueError("bad id")
        name = f"{rid}.otp-{secrets.token_hex(4)}.json"
        self.ensure("requests")
        atomic_write_json(os.path.join(self.requests, name), {"code": code, "submitted": int(time.time())})
        return name

    def code_submissions(self) -> list[str]:
        try:
            return sorted(n for n in os.listdir(self.requests) if OTP_FILE_RE.match(n))
        except OSError:
            return []

    def read_submission(self, name: str) -> dict | None:
        return read_json(os.path.join(self.requests, name)) if OTP_FILE_RE.match(name) else None

    def write_submission_reply(self, name: str, data: dict) -> None:
        if not OTP_FILE_RE.match(name):
            raise ValueError("bad name")
        self.ensure("results")
        atomic_write_json(os.path.join(self.results, name), data)

    def submission_reply(self, name: str) -> dict | None:
        return read_json(os.path.join(self.results, name)) if OTP_FILE_RE.match(name) else None

    def enrolled(self) -> bool:
        st = read_json(os.path.join(self.results, STATUS_FILE))
        return bool(st and st.get("enrolled") is True)

    def write_status(self, enrolled: bool) -> None:
        self.ensure("results")
        atomic_write_json(os.path.join(self.results, STATUS_FILE), {"enrolled": enrolled, "updated": int(time.time())})
