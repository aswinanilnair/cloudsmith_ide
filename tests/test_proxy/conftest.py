"""Proxy test setup: the real proxy app in front of a fake code-server that echoes back each
request it receives (or, on a WebSocket, each message)."""
import os
import sys
import uuid
from pathlib import Path

import aiohttp
import pytest
from aiohttp import WSMsgType, web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for proxytools
import helpers  # noqa: E402

os.environ.update({"TOKEN_SECRET": "test-token-secret", "INTERNAL_TOKEN": "test-internal-token"})
sys.path.insert(0, str(helpers.ROOT / "services" / "proxy"))
import proxy  # noqa: E402


# Stands in for code-server: echoes requests and WebSocket messages.
async def fake_code_server(request: web.Request) -> web.StreamResponse:
    if request.headers.get("Upgrade", "").lower() == "websocket":
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await ws.send_str(f"echo: {msg.data}")
        return ws
    if request.path == "/proxy/5000/go-to-login":
        # code-server adds its /proxy/<port> prefix to an app's redirects like this.
        return web.Response(status=302, headers={"Location": "/proxy/5000/login?next=%2F"})
    return web.json_response({
        "method": request.method,
        "path": request.raw_path,  # path and query string, as received
        "headers": dict(request.headers),
        "body": (await request.read()).decode(),
    })


# Runs the fake code-server.
@pytest.fixture
async def upstream(aiohttp_server):
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake_code_server)
    return await aiohttp_server(app)


# A test client of the proxy, routed to the fake code-server.
@pytest.fixture
async def client(aiohttp_client, upstream, monkeypatch):
    """A client of the proxy. It keeps no cookies of its own: each test sends exactly the
    cookie it means to, as a browser would for that host."""
    monkeypatch.setattr(proxy, "upstream_for", lambda ws_id: f"127.0.0.1:{upstream.port}")
    proxy.last_used.clear()
    proxy.open_sockets.clear()
    return await aiohttp_client(proxy.make_app(), cookie_jar=aiohttp.DummyCookieJar())


# A new workspace ID.
@pytest.fixture
def ws_id() -> str:
    return str(uuid.uuid4())
