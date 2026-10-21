import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from safety_mcp import httpserver
from safety_mcp.mcpserver import StrictFastMCP
from safety_mcp.queue import Queue
from safety_mcp.sshexec import Audit
from safety_mcp.tools import about_text, build_tools

TOKEN = "t" * 40
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def test_load_token_file_env_and_errors(tmp_path):
    f = tmp_path / "tok"
    f.write_text(TOKEN + "\n")
    assert httpserver.load_token({"MCP_HTTP_TOKEN_FILE": str(f)}) == TOKEN
    assert httpserver.load_token({"MCP_HTTP_TOKEN": TOKEN}) == TOKEN
    for env in ({}, {"MCP_HTTP_TOKEN": "short"}, {"MCP_HTTP_TOKEN_FILE": str(tmp_path / "nope")}):
        with pytest.raises(httpserver.TokenError):
            httpserver.load_token(env)


@pytest.fixture
def server(cfg, tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {"MCP_HTTP_ALLOWED_HOSTS": f"127.0.0.1:{port}"}
    mcp = StrictFastMCP("t", instructions=about_text(cfg), transport_security=httpserver.security_settings(env))
    for spec in build_tools(cfg, Queue(str(tmp_path / "q")), Audit(None), "http://x"):
        mcp.add_tool(spec.fn, name=spec.name, description=spec.description)
    srv = uvicorn.Server(uvicorn.Config(httpserver.build_app(mcp, TOKEN), host="127.0.0.1", port=port,
                                        log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}/mcp"
    srv.should_exit = True
    th.join(5)


@pytest.mark.parametrize("auth", [None, "Bearer wrong", f"bearer {TOKEN}", TOKEN, f"Bearer {TOKEN}x"])
def test_rejects_without_valid_token(server, auth):
    h = dict(HDR, **({"Authorization": auth} if auth else {}))
    r = httpx.post(server, json=INIT, headers=h)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")


def test_rejects_foreign_host_and_origin(server):
    h = dict(HDR, Authorization=f"Bearer {TOKEN}")
    assert httpx.post(server, json=INIT, headers=dict(h, Host="evil.example:80")).status_code in (400, 421)
    assert httpx.post(server, json=INIT, headers=dict(h, Origin="http://evil.example")).status_code == 403


def test_tools_list_with_token(server):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def go():
        client = httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"})
        async with client, streamable_http_client(server, http_client=client) as (r, w, _):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                tools = await s.list_tools()
                about = await s.call_tool("about", {})
                return init, [t.name for t in tools.tools], about
    init, names, about = asyncio.run(go())
    assert init.instructions and "rw_approve" in init.instructions
    assert names[0] == "about" and "dev_ro_hosts_list" in names
    assert "FreeOTP" in about.content[0].text
