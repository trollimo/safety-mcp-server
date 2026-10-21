"""HTTP-проверки эндпоинтов из инвентаря. Только стандартная библиотека.

Агент не задаёт URL, хост, схему, заголовки и метод: всё берётся из конфига. Редиректы не выполняются,
TLS проверяется всегда, тело ответа ограничено по размеру.
"""
from __future__ import annotations

import http.client
import re
import socket
import ssl
import time
from urllib.parse import quote, urlsplit

from .config import PLACEHOLDER_RE, Command, Endpoint
from .core import ToolError

READ_LIMIT = 200 * 1024
OUTPUT_LIMIT = 20000
TIMEOUT = 10.0
USER_AGENT = "safety-mcp-server/1"
REDACT_HEADERS = {"set-cookie", "authorization", "proxy-authorization", "www-authenticate", "cookie"}


def build_url(ep: Endpoint, cmd: Command, args: dict) -> tuple[str, str]:
    """Возвращает (полный URL, путь с query). Проверяет, что хост, порт и схема совпадают с эндпоинтом."""
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
    path = PLACEHOLDER_RE.sub(lambda m: quote(given[m.group(1)], safe=""), cmd.path)
    url = ep.base_url + path
    u = urlsplit(url)
    if (u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80)) != (ep.scheme, ep.hostname, ep.port) \
            or u.username or u.password:
        raise ToolError("итоговый URL указывает не на эндпоинт из инвентаря")
    return url, path


def header_line(scope: str, ep: Endpoint, method: str, url: str) -> str:
    tls = " tls_verify=off" if ep.scheme == "https" and not ep.tls_verify else ""
    return f"[{scope} endpoint={ep.name} {method} {url}{tls}]"


def _checks(expect: dict, status: int, body: bytes, ms: int) -> list[tuple[str, bool]]:
    out = []
    if "status" in expect:
        out.append((f"status in {expect['status']}", status in expect["status"]))
    if "body_contains" in expect:
        out.append((f"body_contains {expect['body_contains']!r}", expect["body_contains"].encode() in body))
    if "max_ms" in expect:
        out.append((f"max_ms {expect['max_ms']}", ms <= expect["max_ms"]))
    return out


def _context(ep: Endpoint) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=ep.ca_file) if ep.ca_file else ssl.create_default_context()
    if ep.tls_verify:
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
    else:  # явно разрешено в инвентаре для этого эндпоинта
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def fetch(ep: Endpoint, method: str, path: str, timeout: float = TIMEOUT):
    """(status, reason, headers[list], body[bytes], ms, truncated). Без редиректов и прокси."""
    if ep.scheme == "https":
        conn = http.client.HTTPSConnection(ep.hostname, ep.port, timeout=timeout, context=_context(ep))
    else:
        conn = http.client.HTTPConnection(ep.hostname, ep.port, timeout=timeout)
    t0 = time.monotonic()
    try:
        conn.request(method, path, headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "close"})
        r = conn.getresponse()
        body = b"" if method == "HEAD" else r.read(READ_LIMIT + 1)
        ms = int((time.monotonic() - t0) * 1000)
        truncated = len(body) > READ_LIMIT
        return r.status, r.reason, r.getheaders(), body[:READ_LIMIT], ms, truncated
    finally:
        conn.close()


def run_http(scope: str, ep: Endpoint, cmd: Command, args: dict, mode: str, audit) -> str:
    url, path = build_url(ep, cmd, args)
    head = header_line(scope, ep, cmd.method, url)
    try:
        status, reason, headers, body, ms, truncated = fetch(ep, cmd.method, path)
    except ssl.SSLError:
        result = "ERROR TLS (сертификат не прошёл проверку или ошибка TLS)"
    except (socket.timeout, TimeoutError):
        result = f"ERROR timeout {int(TIMEOUT)}s"
    except socket.gaierror:
        result = "ERROR DNS (имя не разрешается)"
    except (ConnectionError, OSError):
        result = "ERROR connection (не удалось подключиться)"
    except http.client.HTTPException:
        result = "ERROR protocol (некорректный HTTP-ответ)"
    else:
        checks = _checks(cmd.expect, status, body, ms)
        ok = all(c[1] for c in checks)
        audit.write(type="http", scope=scope, endpoint=ep.name, method=cmd.method, url=url, status=status, ms=ms,
                    tls_verify=ep.tls_verify)
        lines = [head, f"RESULT: {'PASS' if ok else 'FAIL'} status={status} time={ms}ms size={len(body)}B"
                       + (" (тело обрезано при чтении)" if truncated else "")]
        lines += [f"- {name}: {'ok' if good else 'FAIL'}" for name, good in checks]
        loc = next((v for k, v in headers if k.lower() == "location"), None)
        if loc and mode == "short":
            lines.append(f"Location: {loc}")
        if mode == "all":
            lines.append("")
            lines.append(f"HTTP {status} {reason}")
            for k, v in headers:
                lines.append(f"{k}: {'[скрыто]' if k.lower() in REDACT_HEADERS else v}")
            lines.append("")
            text = body.decode("utf-8", "replace")
            if len(text) > OUTPUT_LIMIT:
                text = text[:OUTPUT_LIMIT] + "\n[обрезано]"
            lines.append(text)
        return "\n".join(lines).rstrip() + "\n"
    audit.write(type="http", scope=scope, endpoint=ep.name, method=cmd.method, url=url, status=result.split()[1])
    return f"{head}\nRESULT: {result}\n"


def dry_run_http(scope: str, ep: Endpoint, cmd: Command, args: dict, mode: str) -> str:
    url, _ = build_url(ep, cmd, args)
    lines = [f"[{scope} endpoint={ep.name}] ПРОБНЫЙ ЗАПУСК (dry_run): запрос не отправлен",
             f"запрос: {cmd.method} {url}" + (" (проверка TLS-сертификата отключена в инвентаре)"
                                               if ep.scheme == "https" and not ep.tls_verify else ""),
             "проверки: " + (", ".join(n for n, _ in _checks(cmd.expect, 0, b"", 0)) or "нет (достаточно любого ответа)"),
             f"режим вывода: {mode}", "Для выполнения вызовите этот инструмент без dry_run."]
    return "\n".join(lines)
