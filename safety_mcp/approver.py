"""Исполнитель операций записи: веб-страница подтверждения + TOTP + выполнение ssh ключом rw.

Работает отдельно от MCP-сервера (свой пользователь/контейнер, свои тома).
"""
from __future__ import annotations

import hashlib
import hmac
import html
import os
import re
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from . import totp
from .config import CMD_CHARSET_RE, Config
from .core import matches_write_rule
from .queue import ID_RE, REQUEST_TTL, Queue, atomic_write_json, read_json
from .sshexec import Audit, run_one

MAX_ATTEMPTS = 3
# Допустимое расхождение часов телефона и этой машины, секунды (0..120)
TOTP_SKEW = int(os.environ.get("APPROVER_TOTP_SKEW", "50"))
if not 0 <= TOTP_SKEW <= 120:
    raise SystemExit("APPROVER_TOTP_SKEW должен быть от 0 до 120 секунд")
GLOBAL_FAILS = 5
GLOBAL_WINDOW = 600
FINAL = {"approved-ok", "approved-failed", "denied", "expired", "invalid"}


class Approver:
    def __init__(self, cfg: Config, queue: Queue, data_dir: str, audit: Audit | None = None,
                 runner=run_one, clock=time.time):
        self.cfg, self.queue, self.data_dir = cfg, queue, data_dir
        self.audit = audit or Audit(os.path.join(data_dir, "audit.log"))
        self.runner, self.clock = runner, clock
        self.lock = threading.RLock()
        os.makedirs(os.path.join(data_dir, "req"), exist_ok=True)

    # ---------- OTP ----------
    def _otp_path(self) -> str:
        return os.path.join(self.data_dir, "otp.json")

    def otp(self) -> dict | None:
        d = read_json(self._otp_path())
        return d if d and isinstance(d.get("secret"), str) else None

    def enrolled(self) -> bool:
        return self.otp() is not None

    def save_otp(self, secret: str, account: str, last_step: int) -> None:
        atomic_write_json(self._otp_path(), {"secret": secret, "account": account, "created": int(self.clock()),
                                             "last_step": last_step}, mode=0o600)
        self.queue.write_status(True)

    def check_current_code(self, code: str) -> bool:
        """Проверка кода текущего секрета (для enroll --reset). Код считается использованным."""
        with self.lock:
            o = self.otp()
            if not o:
                return False
            step = totp.verify(totp.decode_secret(o["secret"]), code, self.clock(), o.get("last_step", -1),
                               skew=TOTP_SKEW)
            if step is None:
                return False
            o["last_step"] = step
            atomic_write_json(self._otp_path(), o, mode=0o600)
            return True

    # ---------- глобальная блокировка ----------
    def _fails(self) -> list[float]:
        d = read_json(os.path.join(self.data_dir, "failures.json")) or {}
        now = self.clock()
        return [t for t in d.get("times", []) if isinstance(t, (int, float)) and now - t < GLOBAL_WINDOW]

    def _add_fail(self) -> None:
        times = self._fails() + [self.clock()]
        atomic_write_json(os.path.join(self.data_dir, "failures.json"), {"times": times}, mode=0o600)

    def locked_until(self) -> float | None:
        times = self._fails()
        if len(times) >= GLOBAL_FAILS:
            return min(times[-GLOBAL_FAILS:]) + GLOBAL_WINDOW
        return None

    # ---------- заявки ----------
    def _state_path(self, rid: str) -> str:
        return os.path.join(self.data_dir, "req", rid + ".json")

    def _state(self, rid: str) -> dict:
        st = read_json(self._state_path(rid))
        if st is None:
            st = {"attempts": 0, "csrf": secrets.token_urlsafe(24)}
            atomic_write_json(self._state_path(rid), st, mode=0o600)
        return st

    def _finish(self, req: dict, status: str, rc=None, output: str = "", reason: str = "") -> dict:
        res = {"id": req["id"], "stand": req.get("stand"), "hosts": req.get("hosts"), "status": status,
               "finished": int(self.clock())}
        if rc is not None:
            res["rc"] = rc
        if output:
            res["output"] = output
        if reason:
            res["reason"] = reason
        self.queue.write_result(req["id"], res)
        self.audit.write(type="approval", id=req["id"], stand=req.get("stand"), hosts=req.get("hosts"),
                         cmd=req.get("cmd"), status=status, rc=rc, reason=reason)
        return res

    def load(self, rid: str):
        """Возвращает (req, stand, hosts, problem). problem — текст, если заявку выполнять нельзя."""
        if not ID_RE.match(rid or ""):
            return None, None, None, "некорректный id"
        req = self.queue.request(rid)
        if req is None or req.get("id") != rid:
            return None, None, None, "заявка не найдена"
        res = self.queue.result(rid)
        if res and res.get("status") in FINAL:
            return req, None, None, f"заявка уже обработана: {res['status']}"
        stand = self.cfg.stands.get(req.get("stand"))
        names = req.get("hosts")
        hosts = None
        if stand is not None and isinstance(names, list) and names and len(names) <= self.cfg.max_hosts \
                and all(isinstance(n, str) for n in names) and len(set(names)) == len(names):
            found = [stand.hosts.get(n) for n in names]
            hosts = None if None in found else sorted(found, key=lambda h: h.name)
        cmd = req.get("cmd")
        problem = None
        if stand is None or hosts is None or "rw" not in stand.keys:
            problem = "стенд или хосты не найдены в инвентаре, либо у стенда нет ключа rw"
        elif not isinstance(cmd, str) or not CMD_CHARSET_RE.fullmatch(cmd):
            problem = "команда содержит запрещённые символы"
        elif hashlib.sha256(cmd.encode()).hexdigest() != req.get("cmd_sha256"):
            problem = "хэш команды не совпадает"
        elif any(matches_write_rule(self.cfg, stand, h, cmd) is None for h in hosts):
            problem = "команда не соответствует write-команде стенда для одного из хостов"
        if problem:
            self._finish(req, "invalid", reason=problem)
            return req, stand, hosts, problem
        created = req.get("created")
        now = self.clock()
        if not isinstance(created, int) or created > now + 60 or now > created + REQUEST_TTL:
            self._finish(req, "expired", reason="срок заявки истёк")
            return req, stand, hosts, "срок заявки истёк"
        return req, stand, hosts, None

    def pending(self) -> list[dict]:
        out = []
        for rid in self.queue.request_ids():
            res = self.queue.result(rid)
            if res and res.get("status") in FINAL:
                continue
            req, _, _, problem = self.load(rid)
            if req and not problem:
                out.append(req)
        return out

    def view(self, rid: str) -> dict:
        with self.lock:
            req, stand, hosts, problem = self.load(rid)
            if problem:
                return {"req": req, "problem": problem}
            st = self._state(rid)
            return {"req": req, "hosts": hosts, "attempts_left": MAX_ATTEMPTS - st["attempts"], "csrf": st["csrf"],
                    "enrolled": self.enrolled(), "locked_until": self.locked_until()}

    def deny(self, rid: str, csrf: str) -> tuple[bool, str]:
        with self.lock:
            req, _, _, problem = self.load(rid)
            if problem:
                return False, problem
            st = self._state(rid)
            if not hmac.compare_digest(str(csrf or ""), st["csrf"]):
                return False, "некорректный CSRF-токен, обновите страницу"
            self._finish(req, "denied", reason="отклонено человеком")
            return True, "заявка отклонена"

    def approve(self, rid: str, code: str, csrf: str | None, via_agent: bool = False) -> tuple[bool, str]:
        with self.lock:
            req, stand, hosts, problem = self.load(rid)
            if problem:
                return False, problem
            st = self._state(rid)
            if via_agent:
                if not stand.agent_otp:
                    return False, (f"для стенда {stand.name} передача кода через агента запрещена (agent_otp=false); "
                                   "подтвердите на странице approver")
            elif not hmac.compare_digest(str(csrf or ""), st["csrf"]):
                return False, "некорректный CSRF-токен, обновите страницу"
            o = self.otp()
            if o is None:
                return False, "OTP не настроен: выполните enroll"
            lu = self.locked_until()
            if lu:
                return False, f"слишком много неверных кодов, подтверждение заблокировано до {time.strftime('%H:%M:%S', time.localtime(lu))}"
            key = totp.decode_secret(o["secret"])
            code = str(code or "").strip()
            step = totp.verify(key, code, self.clock(), o.get("last_step", -1), skew=TOTP_SKEW)
            if step is None:
                st["attempts"] += 1
                atomic_write_json(self._state_path(rid), st, mode=0o600)
                self._add_fail()
                left = MAX_ATTEMPTS - st["attempts"]
                d = totp.drift(key, code, self.clock())
                hint = (" " + totp.drift_hint(d)) if d is not None and abs(d) > TOTP_SKEW else ""
                if left <= 0:
                    self._finish(req, "denied", reason="3 неверных кода")
                    return False, "неверный код; попытки исчерпаны, заявка отклонена." + hint
                return False, f"неверный или уже использованный код; осталось попыток: {left}." + hint
            o["last_step"] = step
            atomic_write_json(self._otp_path(), o, mode=0o600)
            self.audit.write(type="approval", id=rid, stand=stand.name, hosts=[h.name for h in hosts],
                             status="approved", channel="agent" if via_agent else "page")
            rc, out = self._execute(stand, hosts, req["cmd"])
            res = self._finish(req, "approved-ok" if rc == 0 else "approved-failed", rc=rc, output=out)
            return rc == 0, f"выполнено: {res['status']} (rc={rc})"

    def _execute(self, stand, hosts, cmd: str) -> tuple[int, str]:
        """Выполняет одобренную команду на всех хостах заявки параллельно. rc=0, только если везде успешно."""
        if len(hosts) == 1:
            return self.runner(stand, hosts[0], "rw", cmd, self.audit)
        workers = max(1, min(stand.max_parallel, len(hosts)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(lambda h: self.runner(stand, h, "rw", cmd, self.audit), hosts))
        blocks, failed = [], 0
        for h, (rc, out) in zip(hosts, results):
            failed += rc != 0
            blocks.append(f"[stand={stand.name} host={h.name} role=rw] rc={rc}\n{out}".rstrip())
        blocks.append(f"SUMMARY: ok={len(hosts) - failed} failed={failed}")
        return (0 if failed == 0 else 1), "\n\n".join(blocks) + "\n"


    def process_submissions(self) -> int:
        """Обрабатывает коды, переданные через агента (файлы <id>.otp-<n>.json). Каждый файл один раз."""
        done = 0
        for name in self.queue.code_submissions():
            if self.queue.submission_reply(name) is not None:
                continue
            rid = name.split(".", 1)[0]
            sub = self.queue.read_submission(name) or {}
            code = sub.get("code") if isinstance(sub.get("code"), str) else ""
            ok, msg = self.approve(rid, code, None, via_agent=True)
            res = self.queue.result(rid) or {}
            self.queue.write_submission_reply(name, {"ok": ok, "message": msg, "status": res.get("status"),
                                                     "processed": int(self.clock())})
            done += 1
        return done


def _poll_submissions(approver: "Approver", interval: float = 1.0) -> None:
    while True:
        try:
            approver.process_submissions()
        except Exception as e:  # noqa: BLE001 - фоновый цикл не должен падать
            print(f"approver: ошибка обработки кодов: {type(e).__name__}", file=sys.stderr)
        time.sleep(interval)


# ---------------- HTTP ----------------

PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
body{{font:16px/1.45 system-ui,sans-serif;margin:0;background:#f5f5f4;color:#1c1917}}
main{{max-width:720px;margin:0 auto;padding:16px}}
.stand{{font-size:28px;font-weight:700;color:#fff;padding:12px 16px;border-radius:8px;background:{color}}}
.card{{background:#fff;border-radius:8px;padding:16px;margin-top:12px;border:1px solid #e7e5e4}}
pre{{white-space:pre-wrap;word-break:break-all;background:#1c1917;color:#fafaf9;padding:12px;border-radius:6px}}
input[type=text]{{font-size:28px;letter-spacing:6px;width:9ch;padding:6px 10px}}
button{{font-size:18px;padding:10px 18px;margin:8px 8px 0 0;border-radius:6px;border:0;cursor:pointer}}
.ok{{background:#15803d;color:#fff}} .no{{background:#e7e5e4}} .msg{{font-weight:600}}
.muted{{color:#57534e;font-size:14px}}
@media (prefers-color-scheme:dark){{body{{background:#1c1917;color:#fafaf9}}.card{{background:#292524;border-color:#44403c}}
.no{{background:#44403c;color:#fafaf9}}.muted{{color:#a8a29e}}}}
</style></head><body><main>{body}</main></body></html>"""


def _color(stand: str) -> str:
    return "#b91c1c" if "prod" in stand else "#1d4ed8"


def render_request(v: dict, message: str = "") -> str:
    e = html.escape
    req = v.get("req")
    if v.get("problem"):
        body = f"<div class='card'><p class='msg'>{e(v['problem'])}</p>"
        if req:
            body += f"<pre>{e(str(req.get('cmd', '')))}</pre>"
        body += "<p><a href='/'>к списку заявок</a></p></div>"
        return PAGE.format(title="Заявка", color="#57534e", body=body)
    hosts = v["hosts"]
    left = max(0, int(req["created"] + REQUEST_TTL - time.time()))
    host_list = "".join(f"<li><b>{e(h.name)}</b> ({e(h.address)})</li>" for h in hosts)
    body = (f"<div class='stand'>СТЕНД: {e(req['stand'].upper())}</div><div class='card'>"
            f"<p>Роль <b>rw</b> · команда <b>{e(req['command'])}</b> · хостов: <b>{len(hosts)}</b>"
            f"{' (один код подтверждает все)' if len(hosts) > 1 else ''}</p><ul>{host_list}</ul>"
            f"<pre>{e(req['cmd'])}</pre>"
            f"<p class='muted'>Заявка {e(req['id'])} · осталось {left // 60} мин {left % 60} с · "
            f"время сервера {e(time.strftime('%H:%M:%S'))}</p>")
    if message:
        body += f"<p class='msg'>{e(message)}</p>"
    if not v["enrolled"]:
        body += "<p class='msg'>OTP не настроен. Выполните: docker compose run --rm approver enroll</p>"
    elif v.get("locked_until"):
        body += "<p class='msg'>Слишком много неверных кодов. Подтверждение временно заблокировано.</p>"
    form_tail = (f"<input type='hidden' name='csrf' value='{e(v['csrf'])}'>")
    body += (f"<form method='post'>{form_tail}<p>Код из FreeOTP (попыток осталось: {v['attempts_left']}):</p>"
             "<input type='text' name='code' inputmode='numeric' autocomplete='one-time-code' maxlength='6' "
             "pattern='[0-9]{6}' autofocus>"
             "<div><button class='ok' name='action' value='approve'>Разрешить</button>"
             "<button class='no' name='action' value='deny' formnovalidate>Отклонить</button></div></form></div>")
    return PAGE.format(title=f"Подтверждение: {req['stand']}", color=_color(req["stand"]), body=body)


def render_done(message: str, res: dict | None) -> str:
    e = html.escape
    body = f"<div class='card'><p class='msg'>{e(message)}</p>"
    if res and res.get("output"):
        body += f"<pre>{e(res['output'])}</pre>"
    body += "<p><a href='/'>к списку заявок</a></p></div>"
    return PAGE.format(title="Готово", color="#57534e", body=body)


def render_index(pending: list[dict]) -> str:
    e = html.escape
    if not pending:
        rows = "<p>Нет заявок, ожидающих подтверждения.</p>"
    else:
        rows = "".join(f"<p><a href='/r/{e(r['id'])}'><b>{e(r['stand'].upper())}</b> · {e(', '.join(r['hosts']))} · "
                       f"{e(r['cmd'])}</a></p>" for r in pending)
    return PAGE.format(title="Заявки", color="#57534e", body=f"<div class='card'><h2>Заявки</h2>{rows}</div>")


def make_handler(approver: Approver, allowed_hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "approver"
        sys_version = ""

        def log_message(self, fmt, *args):
            sys.stderr.write("approver: %s %s\n" % (self.command, self.path.split("?")[0]))

        def _send(self, code: int, body: str):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                self._send(421, "<p>Недопустимый заголовок Host</p>")
                return False
            return True

        def _rid(self) -> str | None:
            m = re.fullmatch(r"/r/([0-9a-f]{32})", self.path.split("?")[0])
            return m.group(1) if m else None

        def do_GET(self):
            if not self._host_ok():
                return
            if self.path in ("/", ""):
                return self._send(200, render_index(approver.pending()))
            rid = self._rid()
            if not rid:
                return self._send(404, "<p>Не найдено</p>")
            self._send(200, render_request(approver.view(rid)))

        def do_POST(self):
            if not self._host_ok():
                return
            origin = self.headers.get("Origin")
            if origin and origin.removeprefix("http://") not in allowed_hosts:
                return self._send(403, "<p>Недопустимый Origin</p>")
            rid = self._rid()
            if not rid:
                return self._send(404, "<p>Не найдено</p>")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not 0 < length <= 4096:
                return self._send(400, "<p>Некорректный запрос</p>")
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            get = lambda k: (form.get(k) or [""])[0]
            if get("action") == "deny":
                ok, msg = approver.deny(rid, get("csrf"))
            elif get("action") == "approve":
                ok, msg = approver.approve(rid, get("code"), get("csrf"))
            else:
                return self._send(400, "<p>Некорректное действие</p>")
            res = approver.queue.result(rid)
            if res and res.get("status") in FINAL:
                return self._send(200, render_done(msg, res))
            self._send(200, render_request(approver.view(rid), msg))

    return Handler


def serve(approver: Approver, bind: str, port: int, allowed_hosts: set[str]) -> None:
    approver.queue.write_status(approver.enrolled())
    threading.Thread(target=_poll_submissions, args=(approver,), daemon=True).start()
    httpd = ThreadingHTTPServer((bind, port), make_handler(approver, allowed_hosts))
    print(f"approver: слушаю {bind}:{port}, OTP настроен: {'да' if approver.enrolled() else 'нет'}", file=sys.stderr)
    httpd.serve_forever()


def enroll(approver: Approver, account: str, reset: bool, input_fn=input, out=sys.stdout) -> int:
    if approver.enrolled() and not reset:
        print("OTP уже настроен. Для замены: enroll --reset (нужен действующий код).", file=out)
        return 3
    if reset:
        if not approver.enrolled():
            print("OTP ещё не настроен, сброс не нужен.", file=out)
            return 3
        if not approver.check_current_code(input_fn("Текущий код из FreeOTP: ").strip()):
            print("Неверный код. Сброс отменён.", file=out)
            return 4
    secret = totp.new_secret()
    uri = totp.otpauth_uri(secret, account)
    print("Отсканируйте QR-код в FreeOTP (кнопка «+», затем сканирование):\n", file=out)
    try:
        import segno
        segno.make(uri, error="m").terminal(out=out, compact=True)
    except ImportError:
        print("(segno не установлен, используйте ссылку ниже)", file=out)
    print(f"\nЕсли QR не читается, добавьте вручную: {uri}\n", file=out)
    key = totp.decode_secret(secret)
    print(f"Время этой машины (UTC): {time.strftime('%H:%M:%S', time.gmtime(approver.clock()))}. "
          f"Часы телефона и машины должны совпадать с точностью до {TOTP_SKEW} с.\n", file=out)
    for _ in range(MAX_ATTEMPTS):
        code = input_fn("Введите код из FreeOTP для проверки: ").strip()
        step = totp.verify(key, code, approver.clock(), -1, skew=TOTP_SKEW)
        if step is not None:
            approver.save_otp(secret, account, step)
            print("Готово: OTP настроен.", file=out)
            return 0
        d = totp.drift(key, code, approver.clock())
        if d is not None:
            print("Неверный код. " + totp.drift_hint(d), file=out)
        else:
            print("Неверный код.", file=out)
    print("Настройка не сохранена.", file=out)
    return 5


def main(argv: list[str]) -> int:
    import argparse

    from . import queue_dir
    from .config import ConfigError, load_config

    p = argparse.ArgumentParser(prog="approver")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--bind", default=os.environ.get("APPROVER_BIND", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("APPROVER_PORT", "8765")))
    en = sub.add_parser("enroll")
    en.add_argument("--account", default=os.environ.get("APPROVER_ACCOUNT", "operator"))
    en.add_argument("--reset", action="store_true")
    a = p.parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"approver: ошибка конфигурации: {e}", file=sys.stderr)
        return 2
    data_dir = os.environ.get("APPROVER_DATA", "/data")
    approver = Approver(cfg, Queue(queue_dir()), data_dir)
    if a.cmd == "enroll":
        return enroll(approver, a.account, a.reset)
    allowed = set(filter(None, os.environ.get("APPROVER_ALLOWED_HOSTS",
                                              f"127.0.0.1:{a.port},localhost:{a.port}").split(",")))
    serve(approver, a.bind, a.port, allowed)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
