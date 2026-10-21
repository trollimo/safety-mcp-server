"""Описание MCP-инструментов. Не зависит от FastMCP, чтобы легко тестировать.

Имена: <стенд>_ro_<команда> (чтение) и <стенд>_rw_<команда> (изменение через подтверждение человеком).
"""
from __future__ import annotations

import inspect
import os
import re
import time
from dataclasses import dataclass
from typing import Callable

from .config import Command, Config, Stand
from .core import ToolError, build_command, command_regex, select_hosts
from .httpcheck import dry_run_http, run_http
from .queue import ID_RE, Queue
from .sshexec import Audit, run_on_hosts

APPROVE_WAIT = float(os.environ.get("MCP_APPROVE_WAIT", "90"))
ENROLL_HINT = os.environ.get("MCP_ENROLL_HINT", "docker compose --project-directory local -f docker-compose.yml run --rm approver enroll --account <имя>")


def not_enrolled_message() -> str:
    return ("ОШИБКА: OTP не настроен, команды изменения (rw) недоступны. Команды чтения (ro) работают.\n"
            f"Как настроить (выполняет ЧЕЛОВЕК в своём терминале, не агент): {ENROLL_HINT}\n"
            "Затем отсканировать QR-код в FreeOTP и ввести показанный код.")
CODE_RE = re.compile(r"^[0-9]{6}$")


@dataclass
class ToolSpec:
    name: str
    description: str
    fn: Callable[..., str]


def _with_signature(fn, params: list[tuple[str, bool]]):
    """params: [(имя, обязательный)]; параметры строковые, кроме dry_run (bool, по умолчанию false)."""
    sig_params, ann = [], {}
    for name, required in params:
        if name == "dry_run":
            sig_params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=False, annotation=bool))
            ann[name] = bool
        elif name == "mode":
            sig_params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default="short", annotation=str))
            ann[name] = str
        elif required:
            sig_params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=str))
            ann[name] = str
        else:
            sig_params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=None,
                                                annotation=str | None))
            ann[name] = str | None
    fn.__signature__ = inspect.Signature(sig_params, return_annotation=str)
    ann["return"] = str
    fn.__annotations__ = ann
    return fn


def _prefix(stand: str) -> str:
    return f"[СТЕНД: {stand.upper()}] "


def _safe(fn):
    def wrapped(**kw):
        try:
            return fn(**kw)
        except ToolError as e:
            return f"ОШИБКА: {e}"
    return wrapped


def tool_name(stand: str, role: str, command: str) -> str:
    return f"{stand}_{role}_{command}"


def about_text(cfg: Config) -> str:
    stands = []
    for s in sorted(cfg.stands.values(), key=lambda s: s.name):
        rw = "нет"
        if "rw" in s.keys:
            rw = "да, код можно передать через агента (rw_approve)" if s.agent_otp else "да, только через страницу"
        http = f"; HTTP-эндпоинтов {len(s.endpoints)}" if s.endpoints else ""
        stands.append(f"- {s.name}: хостов {len(s.hosts)}{http}; запись: {rw}")
    return f"""safety-mcp-server: доступ к стендам только через заранее описанные команды.

ИМЕНА ИНСТРУМЕНТОВ
- <стенд>_ro_<команда> — ЧТЕНИЕ. Выполняется сразу на хостах стенда, ничего не меняет.
- <стенд>_rw_<команда> — ИЗМЕНЕНИЕ. НЕ выполняется сразу: создаёт заявку, её подтверждает человек.
- <стенд>_ro_hosts_list — хосты стенда и их теги (параметр tag необязателен).
- <стенд>_ro_endpoints_list, global_ro_endpoints_list — HTTP-эндпоинты стенда / вне стендов.
- global_ro_<команда> — HTTP-проверки, не привязанные к стенду.
- stands_list, about, rw_approve, rw_result — общие инструменты.
Стенд зашит в имя инструмента: выбирайте инструмент нужного стенда и не путайте стенды.
Произвольные команды выполнить нельзя. Если подходящей команды нет, скажите об этом человеку.

HTTP-ПРОВЕРКИ
Это тоже ЧТЕНИЕ (только GET/HEAD к адресам из конфига; свой URL указать нельзя). Параметр mode:
short (по умолчанию) — PASS/FAIL, код ответа и время; all — полный ответ: заголовки и тело (до 20000 символов).
Редиректы не выполняются: при 3xx показывается Location.

ПРОБНЫЙ ЗАПУСК
У каждой команды есть параметр dry_run (по умолчанию false). С dry_run=true инструмент только показывает,
на каких хостах и какая именно команда выполнилась бы, и ничего не отправляет на сервер (для rw — не создаёт
заявку). Используйте его, если не уверены в выборе хоста или параметров, и перед каждой rw-командой.

ВЫБОР ХОСТА
Команда выполняется на хостах с нужными тегами (указаны в описании инструмента): на первом подходящем,
на всех подходящих (параллельно) или на указанном в параметре host (тогда он обязателен).
Параметры команд проверяются по шаблону; значения вне шаблона отклоняются.

ИЗМЕНЕНИЯ (rw) — ПОРЯДОК
1. Вызовите <стенд>_rw_<команда>. Получите id заявки и точную команду.
2. Покажите человеку стенд, хост и точную команду. Попросите подтвердить.
3. Подтверждение делает ЧЕЛОВЕК 6-значным кодом из FreeOTP:
   а) на странице approver (откроется у человека автоматически), или
   б) человек сообщает код вам, и вы вызываете rw_approve(id, code) — только для стендов, где это разрешено.
4. Результат: rw_result(id). rw_approve возвращает результат сразу.
Код можно получить ТОЛЬКО от человека. Не придумывайте, не подбирайте и не используйте повторно коды.
Один код действует один раз; на заявку 3 попытки; заявка живёт 10 минут.
Если OTP не настроен, rw-инструменты возвращают ошибку с командой настройки: передайте её человеку,
сами её не выполняйте. Команды ro при этом работают как обычно.
Не пытайтесь обойти подтверждение (docker, файлы очереди, ключи): это нарушение правил работы.

СТЕНДЫ
""" + "\n".join(stands)


def _dry_run(stand: Stand, role: str, cmd: Command, hosts, remote: str) -> str:
    rule_ok = re.fullmatch(command_regex(cmd), remote) is not None
    lines = [f"[stand={stand.name} role={role}] ПРОБНЫЙ ЗАПУСК (dry_run): на сервер ничего не отправлено",
             f"хосты ({len(hosts)}): " + ", ".join(f"{h.name} ({h.address})" for h in hosts),
             f"команда: {remote}",
             f"правило серверной обёртки rules.{role}: {'совпадает' if rule_ok else 'НЕ совпадает'}"]
    if role == "ro" and len(hosts) > 1:
        lines.append(f"параллельно: до {min(stand.max_parallel, len(hosts))} хостов одновременно")
    if role == "rw":
        lines.append("заявка НЕ создана. Для выполнения вызовите этот инструмент без dry_run "
                     "(понадобится код OTP от человека).")
    else:
        lines.append("Для выполнения вызовите этот инструмент без dry_run.")
    return "\n".join(lines)


def _read_tool(cfg: Config, stand: Stand, cmd: Command, audit: Audit) -> ToolSpec:
    def run(**kw):
        host = kw.pop("host", None)
        dry = kw.pop("dry_run", False)
        remote = build_command(cmd, kw)
        hosts = select_hosts(cfg, stand, cmd, host)
        if dry:
            return _dry_run(stand, "ro", cmd, hosts, remote)
        return run_on_hosts(stand, hosts, "ro", remote, audit, stand.max_parallel)

    where = {"first": "на первом подходящем хосте", "all": "на ВСЕХ подходящих хостах параллельно",
             "host": "на указанном хосте (параметр host обязателен)"}[cmd.pick]
    tags = f" Теги хоста: {', '.join(cmd.tags)}." if cmd.tags else ""
    desc = (f"{_prefix(stand.name)}{cmd.description or cmd.name}. ЧТЕНИЕ, выполняется {where}.{tags}"
            f" Команда на сервере: `{cmd.cmd}`. dry_run=true: показать хосты и команду, ничего не выполняя.")
    if cmd.params:
        desc += " Параметры: " + ", ".join(f"{p} (шаблон {rx})" for p, rx in cmd.params.items()) + "."
    fn = _with_signature(_safe(run), [("host", cmd.pick == "host")] + [(p, True) for p in cmd.params]
                         + [("dry_run", False)])
    return ToolSpec(tool_name(stand.name, "ro", cmd.name), desc, fn)


def _write_tool(cfg: Config, stand: Stand, cmd: Command, queue: Queue, approver_url: str) -> ToolSpec:
    def run(**kw):
        host = kw.pop("host", None)
        dry = kw.pop("dry_run", False)
        remote = build_command(cmd, kw)
        h = select_hosts(cfg, stand, cmd, host)[0]
        if dry:
            return _dry_run(stand, "rw", cmd, [h], remote)
        if not queue.enrolled():
            return not_enrolled_message()
        req = queue.new_request(stand.name, h.name, cmd.name, remote)
        how = f"на странице {approver_url.rstrip('/')}/r/{req['id']} (откроется автоматически)"
        if stand.agent_otp:
            how += f", либо сообщает код вам — тогда вызовите rw_approve(id=\"{req['id']}\", code=\"<6 цифр>\")"
        return (f"[stand={stand.name} host={h.name} role=rw] ЗАЯВКА СОЗДАНА, НЕ ВЫПОЛНЕНА\n"
                f"id: {req['id']}\nкоманда: {remote}\n"
                f"Покажите человеку стенд, хост и команду. Человек подтверждает кодом из FreeOTP {how}.\n"
                f"Заявка действует 10 минут. Результат: rw_result(id=\"{req['id']}\").")

    desc = (f"{_prefix(stand.name)}{cmd.description or cmd.name}. ИЗМЕНЕНИЕ: не выполняется сразу, а создаёт заявку; "
            f"нужен 6-значный код OTP от человека (см. about). Команда на сервере: `{cmd.cmd}`. "
            f"dry_run=true: показать хост и команду, заявку не создавать.")
    if cmd.params:
        desc += " Параметры: " + ", ".join(f"{p} (шаблон {rx})" for p, rx in cmd.params.items()) + "."
    fn = _with_signature(_safe(run), [("host", cmd.pick == "host")] + [(p, True) for p in cmd.params]
                         + [("dry_run", False)])
    return ToolSpec(tool_name(stand.name, "rw", cmd.name), desc, fn)


def _http_tool(scope_name: str, ep, cmd: Command, audit: Audit) -> ToolSpec:
    scope = "scope=global" if scope_name == "global" else f"stand={scope_name}"

    def run(**kw):
        mode = kw.pop("mode", None) or "short"
        dry = kw.pop("dry_run", False)
        if mode not in ("short", "all"):
            raise ToolError("mode должен быть short (только результат и код) или all (полный ответ)")
        if dry:
            return dry_run_http(scope, ep, cmd, kw, mode)
        return run_http(scope, ep, cmd, kw, mode, audit)

    exp = cmd.expect
    checks = []
    if "status" in exp:
        checks.append(f"код ответа в {exp['status']}")
    if "body_contains" in exp:
        checks.append(f"тело содержит {exp['body_contains']!r}")
    if "max_ms" in exp:
        checks.append(f"время не больше {exp['max_ms']} мс")
    label = "[ВНЕ СТЕНДОВ] " if scope_name == "global" else _prefix(scope_name)
    tls = " (без проверки TLS-сертификата)" if ep.scheme == "https" and not ep.tls_verify else ""
    desc = (f"{label}{cmd.description or cmd.name}. ЧТЕНИЕ, HTTP-проверка: {cmd.method} {ep.base_url}{cmd.path}{tls}. "
            f"Проверки: {', '.join(checks) or 'любой ответ'}. mode=short (по умолчанию): результат, код и время; "
            f"mode=all: полный ответ (заголовки и тело). dry_run=true: показать запрос, не отправляя.")
    if cmd.params:
        desc += " Параметры: " + ", ".join(f"{p} (шаблон {rx})" for p, rx in cmd.params.items()) + "."
    fn = _with_signature(_safe(run), [(p, True) for p in cmd.params] + [("mode", False), ("dry_run", False)])
    return ToolSpec(tool_name(scope_name, "ro", cmd.name), desc, fn)


def _endpoints_tool(scope_name: str, endpoints: dict) -> ToolSpec:
    def endpoints_list():
        head = "[scope=global]" if scope_name == "global" else f"[stand={scope_name}]"
        return head + "\n" + "\n".join(f"{e.name}\t{e.base_url}" for e in sorted(endpoints.values(), key=lambda e: e.name))
    label = "[ВНЕ СТЕНДОВ] " if scope_name == "global" else _prefix(scope_name)
    return ToolSpec(tool_name(scope_name, "ro", "endpoints_list"), f"{label}HTTP-эндпоинты (имя и адрес). ЧТЕНИЕ.",
                    _with_signature(endpoints_list, []))


def _hosts_tool(stand: Stand) -> ToolSpec:
    def hosts_list(**kw):
        tag = kw.get("tag")
        rows = [h for h in sorted(stand.hosts.values(), key=lambda h: h.name) if not tag or tag in h.tags]
        if not rows:
            return f"[stand={stand.name}] нет хостов" + (f" с тегом {tag}" if tag else "")
        return f"[stand={stand.name}]\n" + "\n".join(
            f"{h.name}\t{h.address}\ttags={','.join(h.tags)}" for h in rows)
    return ToolSpec(tool_name(stand.name, "ro", "hosts_list"),
                    f"{_prefix(stand.name)}Хосты стенда с тегами ролей. ЧТЕНИЕ. Параметр tag необязателен.",
                    _with_signature(hosts_list, [("tag", False)]))


def _format_result(res: dict) -> str:
    head = f"[stand={res.get('stand')} host={res.get('host')} role=rw] status={res.get('status')}"
    if "rc" in res:
        head += f" rc={res['rc']}"
    if res.get("reason"):
        head += f" ({res['reason']})"
    return head + ("\n" + res["output"] if res.get("output") else "")


def build_tools(cfg: Config, queue: Queue, audit: Audit, approver_url: str,
                approve_wait: float | None = None) -> list[ToolSpec]:
    wait = APPROVE_WAIT if approve_wait is None else approve_wait
    specs: list[ToolSpec] = []
    about = about_text(cfg)

    specs.append(ToolSpec("about", "ОБЯЗАТЕЛЬНО вызовите первым: как работать с этим MCP (имена <стенд>_ro|rw_<команда>, "
                                   "подтверждение изменений кодом OTP от человека).",
                          _with_signature(lambda: about, [])))

    def stands_list():
        lines = []
        for s in sorted(cfg.stands.values(), key=lambda s: s.name):
            lines.append(f"{s.name}: hosts={len(s.hosts)} rw={'да' if 'rw' in s.keys else 'нет'} "
                         f"agent_otp={'да' if s.agent_otp else 'нет'} max_parallel={s.max_parallel}")
        lines.append(f"OTP для записи настроен: {'да' if queue.enrolled() else 'нет (rw недоступны; настройка: ' + ENROLL_HINT + ')'}")
        return "\n".join(lines)
    specs.append(ToolSpec("stands_list", "Список стендов: разрешена ли запись (rw) и передача кода через агента.",
                          _with_signature(stands_list, [])))

    def rw_result(**kw):
        rid = kw.get("id") or ""
        if not ID_RE.match(rid):
            return "ОШИБКА: некорректный id"
        if queue.request(rid) is None:
            return "ОШИБКА: заявка не найдена"
        res = queue.result(rid)
        return _format_result(res) if res else "ещё не выполнена (ожидает подтверждения человеком)"
    specs.append(ToolSpec("rw_result", "Результат заявки на изменение по её id.",
                          _with_signature(rw_result, [("id", True)])))

    def rw_approve(**kw):
        rid, code = kw.get("id") or "", (kw.get("code") or "").strip()
        if not ID_RE.match(rid):
            return "ОШИБКА: некорректный id"
        req = queue.request(rid)
        if req is None:
            return "ОШИБКА: заявка не найдена"
        stand = cfg.stands.get(req.get("stand"))
        if stand is None or not stand.agent_otp:
            return ("ОШИБКА: для этого стенда код через агента передавать нельзя. "
                    "Человек подтверждает заявку на странице approver.")
        if not queue.enrolled():
            return not_enrolled_message()
        if not CODE_RE.match(code):
            return "ОШИБКА: код должен состоять из 6 цифр. Спросите код у человека."
        res = queue.result(rid)
        if res and res.get("status") not in (None, "pending"):
            return "заявка уже обработана\n" + _format_result(res)
        name = queue.submit_code(rid, code)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            reply = queue.submission_reply(name)
            if reply is not None:
                res = queue.result(rid)
                if reply.get("ok") and res:
                    return _format_result(res)
                return f"НЕ ПОДТВЕРЖДЕНО: {reply.get('message')}" + (
                    "\n" + _format_result(res) if res and res.get("status") else "")
            time.sleep(0.5)
        return "код передан, но approver ещё не ответил (он запущен?). Проверьте позже: rw_result(id)."
    specs.append(ToolSpec("rw_approve",
                          "Передать 6-значный код OTP, который СООБЩИЛ ЧЕЛОВЕК, для подтверждения заявки (id). "
                          "Только для стендов с agent_otp. Никогда не придумывайте код. Ждёт выполнения и "
                          "возвращает результат.",
                          _with_signature(rw_approve, [("id", True), ("code", True)])))

    for stand in sorted(cfg.stands.values(), key=lambda s: s.name):
        if stand.hosts:
            specs.append(_hosts_tool(stand))
        if stand.endpoints:
            specs.append(_endpoints_tool(stand.name, stand.endpoints))
        for cmd in cfg.commands_for(stand.name):
            if cmd.type == "http":
                if cmd.endpoint in stand.endpoints:
                    specs.append(_http_tool(stand.name, stand.endpoints[cmd.endpoint], cmd, audit))
            elif not stand.hosts:
                continue
            elif cmd.kind == "read":
                specs.append(_read_tool(cfg, stand, cmd, audit))
            elif "rw" in stand.keys:
                specs.append(_write_tool(cfg, stand, cmd, queue, approver_url))
    if cfg.global_endpoints:
        specs.append(_endpoints_tool("global", cfg.global_endpoints))
        for cmd in cfg.global_commands():
            specs.append(_http_tool("global", cfg.global_endpoints[cmd.endpoint], cmd, audit))
    return specs
