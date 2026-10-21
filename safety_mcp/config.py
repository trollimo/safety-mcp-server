"""Загрузка и валидация inventory.json и commands.json.

Любая ошибка конфигурации -> ConfigError, сервер не стартует.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from urllib.parse import urlsplit

STAND_RE = re.compile(r"^[a-z][a-z0-9]{0,15}$")
HOST_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}$")
TAG_RE = re.compile(r"^[a-z0-9_]{1,32}$")
CMD_NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")
PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
ADDRESS_RE = re.compile(r"^[A-Za-z0-9.:-]{1,253}$")
SSH_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
# Те же символы, что разрешает серверная обёртка (wrapper/safety-wrapper).
CMD_CHARSET_RE = re.compile(r"^[A-Za-z0-9 ._@:/=,%+-]{1,300}$")

RESERVED_STANDS = {"global"}
ROLES = ("ro", "rw")
PICKS = ("first", "all", "host")
COMMAND_FIELDS = {"type", "description", "tags", "pick", "params", "cmd"}
HTTP_FIELDS = {"type", "description", "scope", "endpoint", "method", "path", "params", "expect"}
HTTP_RESERVED = {"auth", "headers", "body", "via"}
HTTP_METHODS = ("GET", "HEAD")
EXPECT_FIELDS = {"status", "body_contains", "max_ms"}
ENDPOINT_RE = re.compile(r"^[a-z0-9_]{1,32}$")
HTTP_PATH_RE = re.compile(r"^/[A-Za-z0-9._~:@%/?=&+,-]{0,300}$")
RESERVED_PARAMS = {"host", "dry_run", "mode"}
STAND_FIELDS = {"ssh_user", "known_hosts", "keys", "hosts", "max_parallel", "endpoints", "agent_otp"}
HOST_FIELDS = {"address", "tags"}
ENDPOINT_FIELDS = {"base_url", "ca_file", "allow_http", "tls_verify"}

DEFAULT_MAX_PARALLEL = 5
DEFAULT_MAX_HOSTS = 20


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Host:
    name: str
    address: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Endpoint:
    name: str
    base_url: str            # без завершающего /
    scheme: str
    hostname: str
    port: int
    ca_file: str | None      # абсолютный путь или None
    tls_verify: bool = True  # false — не проверять сертификат (задаётся только в инвентаре)


@dataclass
class Stand:
    name: str
    ssh_user: str | None
    known_hosts: str | None  # абсолютный путь
    keys: dict[str, str]     # role -> путь к приватному ключу
    hosts: dict[str, Host]
    max_parallel: int
    agent_otp: bool = False  # можно ли передавать OTP-код через агента (rw_approve)
    endpoints: dict = field(default_factory=dict)  # имя -> Endpoint


@dataclass(frozen=True)
class Command:
    name: str
    kind: str                # "read" | "write"
    type: str                # "ssh"
    description: str
    tags: tuple[str, ...]
    pick: str
    params: dict             # имя -> ERE-regex
    cmd: str
    origin: str              # "common" или имя стенда
    scope: str = "stand"     # http: "stand" или "global"
    endpoint: str = ""       # http: имя эндпоинта
    method: str = "GET"      # http
    path: str = ""           # http: шаблон пути
    expect: dict = field(default_factory=dict)  # http: status, body_contains, max_ms
    param_desc: dict = field(default_factory=dict)  # имя параметра -> описание для агента


@dataclass
class Config:
    conf_dir: str
    stands: dict[str, Stand]
    max_parallel: int
    max_hosts: int
    common: dict[str, dict[str, Command]]                 # kind -> name -> Command
    per_stand: dict[str, dict[str, dict[str, Command]]] = field(default_factory=dict)
    raw_digest_source: bytes = b""
    global_endpoints: dict = field(default_factory=dict)  # имя -> Endpoint

    def global_commands(self) -> list[Command]:
        return sorted((c for c in self.common.get("read", {}).values() if c.type == "http" and c.scope == "global"),
                      key=lambda c: c.name)

    def commands_for(self, stand: str) -> list[Command]:
        out = []
        for kind in ("read", "write"):
            out.extend(c for c in self.common.get(kind, {}).values() if c.scope != "global")
            out.extend(self.per_stand.get(stand, {}).get(kind, {}).values())
        return sorted(out, key=lambda c: (c.kind, c.name))

    def command(self, stand: str, name: str) -> Command | None:
        for c in self.commands_for(stand):
            if c.name == name:
                return c
        return None


def _err(msg: str) -> ConfigError:
    return ConfigError(msg)


def _int_setting(value, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (lo <= value <= hi):
        raise _err(f"{name}: ожидается целое число {lo}..{hi}, получено {value!r}")
    return value


def check_ere(regex: str, where: str) -> None:
    """Regex параметров должен быть совместим и с Python re, и с POSIX ERE (bash [[ =~ ]])."""
    if not isinstance(regex, str) or not regex:
        raise _err(f"{where}: пустой regex")
    if "(?" in regex:
        raise _err(f"{where}: конструкции (?...) не поддерживаются в ERE")
    if re.search(r"\\[A-Za-z0-9]", regex):
        raise _err(f"{where}: классы вида \\d \\w \\s и обратные ссылки запрещены, используйте [0-9] и т.п.")
    if regex.startswith("^") or regex.endswith("$"):
        raise _err(f"{where}: не используйте ^ и $, сопоставление и так по всей строке")
    try:
        re.compile(regex)
    except re.error as e:
        raise _err(f"{where}: некорректный regex: {e}") from e


def _parse_params(raw: dict, where: str) -> tuple[dict, dict]:
    """params: имя -> "regex" или {"pattern": "regex", "description": "..."}. Возвращает (шаблоны, описания)."""
    params = raw.get("params", {})
    if not isinstance(params, dict):
        raise _err(f"{where}: params должен быть объектом")
    patterns, descs = {}, {}
    for pname, spec in params.items():
        pw = f"{where}.params.{pname}"
        if not PARAM_NAME_RE.match(pname) or pname in RESERVED_PARAMS:
            raise _err(f"{where}: недопустимое имя параметра {pname!r} (зарезервированы: {sorted(RESERVED_PARAMS)})")
        if isinstance(spec, dict):
            if set(spec) - {"pattern", "description"} or "pattern" not in spec:
                raise _err(f"{pw}: ожидается строка-шаблон или объект {{\"pattern\", \"description\"}}")
            desc = spec.get("description", "")
            if not isinstance(desc, str) or len(desc) > 300:
                raise _err(f"{pw}.description: строка до 300 символов")
            rx = spec["pattern"]
            if desc:
                descs[pname] = desc
        else:
            rx = spec
        check_ere(rx, pw)
        patterns[pname] = rx
    return patterns, descs


def _parse_http_command(name: str, raw: dict, kind: str, origin: str, where: str) -> Command:
    reserved = set(raw) & HTTP_RESERVED
    if reserved:
        raise _err(f"{where}: поля {sorted(reserved)} зарезервированы и в этой версии не поддерживаются")
    unknown = set(raw) - HTTP_FIELDS
    if unknown:
        raise _err(f"{where}: неизвестные поля {sorted(unknown)}")
    if kind != "read":
        raise _err(f"{where}: HTTP-команды бывают только в блоке read")
    scope = raw.get("scope", "stand")
    if scope not in ("stand", "global"):
        raise _err(f"{where}: scope должен быть stand или global")
    if scope == "global" and origin != "common":
        raise _err(f"{where}: scope=global допустим только в блоке common")
    endpoint = raw.get("endpoint")
    if not isinstance(endpoint, str) or not ENDPOINT_RE.match(endpoint):
        raise _err(f"{where}: endpoint обязателен ({ENDPOINT_RE.pattern})")
    method = raw.get("method", "GET")
    if method not in HTTP_METHODS:
        raise _err(f"{where}: method {method!r} не поддерживается (только {', '.join(HTTP_METHODS)})")
    path = raw.get("path", "/")
    params, param_desc = _parse_params(raw, where)
    if not isinstance(path, str):
        raise _err(f"{where}: path должен быть строкой")
    placeholders = PLACEHOLDER_RE.findall(path)
    if sorted(set(placeholders)) != sorted(params):
        raise _err(f"{where}: плейсхолдеры в path {sorted(set(placeholders))} не совпадают с params {sorted(params)}")
    literal = PLACEHOLDER_RE.sub("x", path)
    if not HTTP_PATH_RE.match(literal) or literal.startswith("//") or "/../" in literal + "/" or "\\" in literal:
        raise _err(f"{where}: недопустимый path (должен начинаться с /, без //, .. и спецсимволов)")
    expect = raw.get("expect", {}) or {}
    if not isinstance(expect, dict) or set(expect) - EXPECT_FIELDS:
        raise _err(f"{where}: expect допускает только {sorted(EXPECT_FIELDS)}")
    st = expect.get("status")
    if st is not None and (not isinstance(st, list) or not st or not all(
            isinstance(x, int) and not isinstance(x, bool) and 100 <= x <= 599 for x in st)):
        raise _err(f"{where}: expect.status — непустой список кодов 100..599")
    bc = expect.get("body_contains")
    if bc is not None and (not isinstance(bc, str) or not 0 < len(bc) <= 200):
        raise _err(f"{where}: expect.body_contains — строка 1..200 символов")
    if "max_ms" in expect:
        _int_setting(expect["max_ms"], f"{where}: expect.max_ms", 1, 60000)
    desc = raw.get("description", "")
    if not isinstance(desc, str):
        raise _err(f"{where}: description должен быть строкой")
    return Command(name=name, kind=kind, type="http", description=desc, tags=(), pick="first", params=params,
                   cmd="", origin=origin, scope=scope, endpoint=endpoint, method=method, path=path,
                   expect=dict(expect), param_desc=param_desc)


def _parse_command(name: str, raw: dict, kind: str, origin: str) -> Command:
    where = f"commands.json: {origin}.{kind}.{name}"
    if not CMD_NAME_RE.match(name):
        raise _err(f"{where}: недопустимое имя команды")
    if not isinstance(raw, dict):
        raise _err(f"{where}: ожидается объект")
    ctype = raw.get("type", "ssh")
    if ctype == "http":
        return _parse_http_command(name, raw, kind, origin, where)
    if ctype != "ssh":
        raise _err(f"{where}: type={ctype!r} не поддерживается (ssh или http)")
    unknown = set(raw) - COMMAND_FIELDS
    if unknown:
        raise _err(f"{where}: неизвестные поля {sorted(unknown)}")
    cmd = raw.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        raise _err(f"{where}: поле cmd обязательно")
    tags = raw.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) and TAG_RE.match(t) for t in tags):
        raise _err(f"{where}: tags должны быть списком строк {TAG_RE.pattern}")
    pick = raw.get("pick", "first")
    if pick not in PICKS:
        raise _err(f"{where}: pick должен быть одним из {PICKS}")
    params, param_desc = _parse_params(raw, where)
    placeholders = PLACEHOLDER_RE.findall(cmd)
    if sorted(set(placeholders)) != sorted(params):
        raise _err(f"{where}: плейсхолдеры в cmd {sorted(set(placeholders))} не совпадают с params {sorted(params)}")
    literal = PLACEHOLDER_RE.sub("x", cmd)
    if not CMD_CHARSET_RE.match(literal):
        raise _err(f"{where}: cmd содержит запрещённые символы (разрешено: буквы, цифры, пробел и ._@:/=,%+-)")
    if "  " in cmd or cmd != cmd.strip():
        raise _err(f"{where}: в cmd не должно быть двойных и крайних пробелов")
    desc = raw.get("description", "")
    if not isinstance(desc, str):
        raise _err(f"{where}: description должен быть строкой")
    return Command(name=name, kind=kind, type=ctype, description=desc, tags=tuple(tags),
                   pick=pick, params=dict(params), cmd=cmd, origin=origin, param_desc=param_desc)


def _parse_command_block(block, origin: str) -> dict[str, dict[str, Command]]:
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise _err(f"commands.json: {origin}: ожидается объект")
    unknown = set(block) - {"read", "write"}
    if unknown:
        raise _err(f"commands.json: {origin}: неизвестные блоки {sorted(unknown)}")
    out: dict[str, dict[str, Command]] = {}
    for kind in ("read", "write"):
        items = block.get(kind) or {}
        if not isinstance(items, dict):
            raise _err(f"commands.json: {origin}.{kind}: ожидается объект")
        out[kind] = {n: _parse_command(n, r, kind, origin) for n, r in items.items()}
    return out


def _parse_endpoints(raw, where: str, conf_dir: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _err(f"{where}: ожидается объект")
    out = {}
    for name, e in raw.items():
        ew = f"{where}.{name}"
        if not ENDPOINT_RE.match(name):
            raise _err(f"{ew}: недопустимое имя эндпоинта")
        if not isinstance(e, dict) or set(e) - ENDPOINT_FIELDS:
            raise _err(f"{ew}: допустимы только поля {sorted(ENDPOINT_FIELDS)}")
        url = e.get("base_url")
        allow_http = e.get("allow_http", False)
        if not isinstance(allow_http, bool):
            raise _err(f"{ew}.allow_http: ожидается true или false")
        if not isinstance(url, str):
            raise _err(f"{ew}.base_url обязателен")
        try:
            u = urlsplit(url)
            port = u.port
        except ValueError as ex:
            raise _err(f"{ew}.base_url: некорректный URL") from ex
        if u.scheme not in (("https", "http") if allow_http else ("https",)):
            raise _err(f"{ew}.base_url: допускается только https://" + (" (или http:// с allow_http)" if not allow_http else ""))
        if not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
            raise _err(f"{ew}.base_url: нужен только схема://хост[:порт], без пути, query и учётных данных")
        tls_verify = e.get("tls_verify", True)
        if not isinstance(tls_verify, bool):
            raise _err(f"{ew}.tls_verify: ожидается true или false")
        ca = e.get("ca_file")
        ca_path = None
        if ca is not None:
            if not isinstance(ca, str) or "/" in ca or not ca:
                raise _err(f"{ew}.ca_file: имя файла в каталоге конфига")
            ca_path = os.path.join(conf_dir, ca)
            if not os.path.isfile(ca_path):
                raise _err(f"{ew}.ca_file: файл {ca} не найден")
        port = port or (443 if u.scheme == "https" else 80)
        out[name] = Endpoint(name, f"{u.scheme}://{u.netloc}", u.scheme, u.hostname, port, ca_path, tls_verify)
    return out


def _parse_stand(name: str, raw: dict, conf_dir: str, default_parallel: int) -> Stand:
    where = f"inventory.json: stands.{name}"
    if not STAND_RE.match(name):
        raise _err(f"{where}: недопустимое имя стенда")
    if name in RESERVED_STANDS:
        raise _err(f"{where}: имя {name!r} зарезервировано")
    if not isinstance(raw, dict):
        raise _err(f"{where}: ожидается объект")
    unknown = set(raw) - STAND_FIELDS
    if unknown:
        raise _err(f"{where}: неизвестные поля {sorted(unknown)}")
    hosts_raw = raw.get("hosts", {}) or {}
    if not isinstance(hosts_raw, dict):
        raise _err(f"{where}.hosts: ожидается объект")
    hosts: dict[str, Host] = {}
    for hname, h in hosts_raw.items():
        hw = f"{where}.hosts.{hname}"
        if not HOST_RE.match(hname):
            raise _err(f"{hw}: недопустимое имя хоста")
        if not isinstance(h, dict) or set(h) - HOST_FIELDS:
            raise _err(f"{hw}: допустимы только поля {sorted(HOST_FIELDS)}")
        addr = h.get("address")
        if not isinstance(addr, str) or not ADDRESS_RE.match(addr) or addr.startswith("-"):
            raise _err(f"{hw}: недопустимый address")
        tags = h.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(t, str) and TAG_RE.match(t) for t in tags):
            raise _err(f"{hw}: tags должны быть списком строк {TAG_RE.pattern}")
        hosts[hname] = Host(hname, addr, tuple(tags))

    keys_raw = raw.get("keys", {}) or {}
    if not isinstance(keys_raw, dict) or set(keys_raw) - set(ROLES):
        raise _err(f"{where}.keys: допустимы только ключи {ROLES}")
    for role, path in keys_raw.items():
        if not isinstance(path, str) or not os.path.isabs(path):
            raise _err(f"{where}.keys.{role}: нужен абсолютный путь")
    ssh_user = raw.get("ssh_user")
    known_hosts = raw.get("known_hosts")
    if hosts:
        if not isinstance(ssh_user, str) or not SSH_USER_RE.match(ssh_user):
            raise _err(f"{where}.ssh_user: обязателен и должен соответствовать {SSH_USER_RE.pattern}")
        if "ro" not in keys_raw:
            raise _err(f"{where}.keys.ro: обязателен, если у стенда есть hosts")
        if not isinstance(known_hosts, str) or "/" in known_hosts or not known_hosts:
            raise _err(f"{where}.known_hosts: имя файла в каталоге конфига обязательно")
        kh_path = os.path.join(conf_dir, known_hosts)
        if not os.path.isfile(kh_path):
            raise _err(f"{where}.known_hosts: файл {known_hosts} не найден в каталоге конфига")
    else:
        kh_path = os.path.join(conf_dir, known_hosts) if isinstance(known_hosts, str) else None
    mp = raw.get("max_parallel", default_parallel)
    mp = _int_setting(mp, f"{where}.max_parallel", 1, 50)
    agent_otp = raw.get("agent_otp", False)
    if not isinstance(agent_otp, bool):
        raise _err(f"{where}.agent_otp: ожидается true или false")
    endpoints = _parse_endpoints(raw.get("endpoints"), f"{where}.endpoints", conf_dir)
    return Stand(name, ssh_user, kh_path, dict(keys_raw), hosts, mp, agent_otp, endpoints)


def _check_not_writable(paths: list[str]) -> None:
    writable = [p for p in paths if os.path.exists(p) and os.access(p, os.W_OK)]
    if not writable:
        return
    if os.environ.get("SAFETY_ALLOW_WRITABLE") == "1":
        print(f"ПРЕДУПРЕЖДЕНИЕ: конфиг доступен на запись ({', '.join(writable)}); "
              "SAFETY_ALLOW_WRITABLE=1 допустим только для отладки", file=sys.stderr)
        return
    raise _err("конфиг доступен на запись текущему пользователю: " + ", ".join(writable)
               + ". Сделайте файлы root:root 0644 или смонтируйте каталог только для чтения (:ro).")


def load_config(conf_dir: str | None = None, check_writable: bool = True) -> Config:
    conf_dir = os.path.abspath(conf_dir or os.environ.get("SAFETY_CONF", "/conf"))
    inv_path = os.path.join(conf_dir, "inventory.json")
    cmd_path = os.path.join(conf_dir, "commands.json")
    raw_bytes = b""
    data = {}
    for label, path in (("inventory", inv_path), ("commands", cmd_path)):
        try:
            with open(path, "rb") as f:
                b = f.read()
        except OSError as e:
            raise _err(f"не удалось прочитать {path}: {e.strerror}. Проверьте, что каталог конфига смонтирован: "
                       "в Docker это ./conf рядом с docker-compose.yml или в каталоге из --project-directory") from e
        raw_bytes += b
        try:
            data[label] = json.loads(b)
        except json.JSONDecodeError as e:
            raise _err(f"{os.path.basename(path)}: некорректный JSON: {e}") from e

    inv, cmds = data["inventory"], data["commands"]
    if not isinstance(inv, dict) or set(inv) - {"stands", "settings", "global"}:
        raise _err("inventory.json: допустимы только ключи stands, settings, global")
    settings = inv.get("settings", {}) or {}
    if not isinstance(settings, dict) or set(settings) - {"max_parallel", "max_hosts"}:
        raise _err("inventory.json: settings допускает только max_parallel и max_hosts")
    max_parallel = _int_setting(settings.get("max_parallel", DEFAULT_MAX_PARALLEL), "settings.max_parallel", 1, 50)
    max_hosts = _int_setting(settings.get("max_hosts", DEFAULT_MAX_HOSTS), "settings.max_hosts", 1, 200)
    stands_raw = inv.get("stands")
    if not isinstance(stands_raw, dict) or not stands_raw:
        raise _err("inventory.json: нужен непустой блок stands")
    stands = {n: _parse_stand(n, r, conf_dir, max_parallel) for n, r in stands_raw.items()}
    glob = inv.get("global", {}) or {}
    if not isinstance(glob, dict) or set(glob) - {"endpoints"}:
        raise _err("inventory.json: global допускает только endpoints")
    global_endpoints = _parse_endpoints(glob.get("endpoints"), "inventory.json: global.endpoints", conf_dir)

    if not isinstance(cmds, dict) or set(cmds) - {"common", "stands"}:
        raise _err("commands.json: допустимы только ключи common и stands")
    common = _parse_command_block(cmds.get("common"), "common")
    per_stand_raw = cmds.get("stands") or {}
    if not isinstance(per_stand_raw, dict):
        raise _err("commands.json: stands должен быть объектом")
    per_stand = {}
    for sname, block in per_stand_raw.items():
        if sname not in stands:
            raise _err(f"commands.json: stands.{sname}: стенда нет в inventory.json")
        per_stand[sname] = _parse_command_block(block, sname)
        for kind in ("read", "write"):
            for cname in per_stand[sname].get(kind, {}):
                for k2 in ("read", "write"):
                    if cname in common.get(k2, {}):
                        raise _err(f"commands.json: имя {cname!r} есть и в common, и в stands.{sname}")
        names = list(per_stand[sname].get("read", {})) + list(per_stand[sname].get("write", {}))
        if len(names) != len(set(names)):
            raise _err(f"commands.json: stands.{sname}: одинаковое имя в read и write")
    cnames = list(common.get("read", {})) + list(common.get("write", {}))
    if len(cnames) != len(set(cnames)):
        raise _err("commands.json: common: одинаковое имя в read и write")
    # HTTP-команды должны ссылаться на существующие эндпоинты
    for c in common.get("read", {}).values():
        if c.type != "http":
            continue
        if c.scope == "global" and c.endpoint not in global_endpoints:
            raise _err(f"commands.json: common.read.{c.name}: эндпоинта {c.endpoint!r} нет в global.endpoints")
        if c.scope == "stand" and not any(c.endpoint in s.endpoints for s in stands.values()):
            raise _err(f"commands.json: common.read.{c.name}: эндпоинта {c.endpoint!r} нет ни у одного стенда")
    for sname, block in per_stand.items():
        for c in block.get("read", {}).values():
            if c.type == "http" and c.endpoint not in stands[sname].endpoints:
                raise _err(f"commands.json: stands.{sname}.read.{c.name}: у стенда нет эндпоинта {c.endpoint!r}")
    for reserved in ("hosts_list", "approve", "result", "endpoints_list"):
        if reserved in cnames:
            raise _err(f"commands.json: имя {reserved!r} зарезервировано")

    if check_writable:
        paths = [conf_dir, inv_path, cmd_path] + [s.known_hosts for s in stands.values() if s.known_hosts]
        paths += [e.ca_file for s in stands.values() for e in s.endpoints.values() if e.ca_file]
        paths += [e.ca_file for e in global_endpoints.values() if e.ca_file]
        _check_not_writable(paths)

    return Config(conf_dir=conf_dir, stands=stands, max_parallel=max_parallel, max_hosts=max_hosts,
                  common=common, per_stand=per_stand, raw_digest_source=raw_bytes,
                  global_endpoints=global_endpoints)
