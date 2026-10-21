"""MCP по HTTP (streamable-http) с обязательным Bearer-токеном.

Токен берётся из файла MCP_HTTP_TOKEN_FILE (или переменной MCP_HTTP_TOKEN, для тестов). Без токена
сервер не запускается. Проверка Host/Origin (защита от DNS-rebinding) делается средствами MCP.
"""
from __future__ import annotations

import hmac
import os

from mcp.server.transport_security import TransportSecuritySettings

MIN_TOKEN_LEN = 32
DEFAULT_PORT = 8766
PATH = "/mcp"


class TokenError(Exception):
    pass


def load_token(env=os.environ) -> str:
    path = env.get("MCP_HTTP_TOKEN_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                token = f.read().strip()
        except OSError as e:
            raise TokenError(f"не читается файл токена {path}: {e.strerror}") from None
    else:
        token = env.get("MCP_HTTP_TOKEN", "").strip()
    if len(token) < MIN_TOKEN_LEN:
        raise TokenError(f"нет токена или он короче {MIN_TOKEN_LEN} символов "
                         "(создайте его: ./init-local.sh, файл local/mcp-token)")
    return token


def security_settings(env=os.environ) -> TransportSecuritySettings:
    """Разрешённые Host — как клиент видит сервер снаружи контейнера (127.0.0.1:<порт на хосте>)."""
    hosts = [h.strip() for h in env.get("MCP_HTTP_ALLOWED_HOSTS", f"127.0.0.1:{DEFAULT_PORT},"
                                        f"localhost:{DEFAULT_PORT}").split(",") if h.strip()]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                     allowed_origins=[f"http://{h}" for h in hosts])


class BearerAuth:
    """ASGI-обёртка: каждый HTTP-запрос должен нести Authorization: Bearer <токен>."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                body = b'{"error":"unauthorized"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b'Bearer realm="safety-mcp-server"'),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def build_app(mcp, token: str):
    return BearerAuth(mcp.streamable_http_app(), token)


def run(mcp, token: str, host: str, port: int) -> None:
    import uvicorn
    uvicorn.run(build_app(mcp, token), host=host, port=port, log_level="warning", server_header=False)
