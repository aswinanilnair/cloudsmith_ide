"""CloudSmith proxy: http://ws-<id>.<domain>/  ->  http://ws-<id>:8080 (code-server).

code-server runs with --auth none, so this proxy is what keeps a workspace private:

  1. The dashboard's Open button links to the workspace with ?token=<5-minute link token>.
  2. The proxy checks the token, sets a session cookie for that one subdomain, and
     redirects to the same URL without the token.
  3. Every later request (HTTP and WebSocket) must carry that cookie.

The proxy reaches the container by name over the workspace's own Docker network
(the agent attaches the proxy to it), so it needs no database.

Ports: http://<port>-ws-<id>.<domain>/ reaches an app the user runs on that port inside
the workspace (e.g. Flask on 5000), through code-server's /proxy/<port>/. The app sits at
the root of its own host, so its links and static files work without changes. Each port
host gets its own cookie: a visitor without one is sent to the workspace host's
/_cloudsmith/port-login (which has the workspace cookie), and comes back with a
one-minute token that the port host swaps for its cookie, as in steps 2 and 3 above.

It also records when each workspace was last used, for idle shutdown: a workspace is in
use while a browser has it open (the editor keeps a WebSocket connected) and whenever it
makes a request. The agent reads this from GET /_cloudsmith/activity, which needs the
internal token.
"""
import asyncio
import hmac
import html
import json
import os
import re
import time
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import aiohttp
import jwt
from aiohttp import WSMsgType, web
from multidict import CIMultiDict

TOKEN_SECRET = os.getenv("TOKEN_SECRET", "dev-token-secret")
INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "dev-internal-token")
PORT = int(os.getenv("PORT", "8080"))
WORKER_ID = os.getenv("WORKER_ID", "")
COOKIE = "cs_session"
SESSION_TTL_S = 12 * 3600
PORT_LINK_TTL_S = 60
PORT_LOGIN_PATH = "/_cloudsmith/port-login"
PORT_TOKEN_PARAM = "_cloudsmith_token"  # not "token", which the user's app may use itself
# ws-<id>.<domain> for the editor, <port>-ws-<id>.<domain> for an app on that port.
HOST_RE = re.compile(r"^(?:([0-9]{1,5})-)?ws-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.")
CODE_SERVER_PORT_PATH = re.compile(r"^/proxy/([0-9]{1,5})(/.*)?$")  # code-server's /proxy/<port>/...

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "trailers", "transfer-encoding", "upgrade", "proxy-connection",
}
WS_SKIP = HOP_BY_HOP | {
    "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "sec-websocket-protocol", "sec-websocket-accept", "content-length",
}

last_used: dict = {}     # workspace id -> unix time of its last request
open_sockets: dict = {}  # workspace id -> number of WebSockets open right now


# Writes one structured JSON log line.
def log(message: str, severity: str = "INFO", **fields) -> None:
    """One structured log line, in the same format as the API's and the agent's. Only
    per-workspace events are logged (sign-ins, refusals, browsers connecting), not every
    request."""
    print(json.dumps({"severity": severity, "message": message, "component": "proxy", "worker": WORKER_ID,
                      **fields}, default=str), flush=True)


# Internal: reports when each workspace was last used.
def activity(request: web.Request) -> web.Response:
    if not hmac.compare_digest(request.headers.get("X-Internal-Token", ""), INTERNAL_TOKEN):
        return web.Response(status=403, text="Forbidden")
    now = time.time()
    return web.json_response({ws_id: now if open_sockets.get(ws_id) else t for ws_id, t in last_used.items()})


# Builds a simple HTML status page.
def page(status: int, title: str, text: str, refresh: bool = False) -> web.Response:
    meta = '<meta http-equiv="refresh" content="2">' if refresh else ""
    doc = f"""<!doctype html><html><head><meta charset="utf-8">{meta}
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font:15px system-ui,sans-serif;display:grid;place-items:center;min-height:90vh;margin:0;background:#f7f7f8;color:#1d1d1f}}
@media (prefers-color-scheme:dark){{body{{background:#111214;color:#ececf1}}}}
div{{text-align:center;max-width:460px;padding:16px}}p{{color:#888}}</style></head>
<body><div><h2>{html.escape(title)}</h2><p>{html.escape(text)}</p></div></body></html>"""
    return web.Response(status=status, text=doc, content_type="text/html", headers={"Cache-Control": "no-store"})


# Signs a token with the given claims and lifetime.
def sign(claims: dict, ttl_s: int) -> str:
    now = int(time.time())
    return jwt.encode({**claims, "iat": now, "exp": now + ttl_s}, TOKEN_SECRET, algorithm="HS256")


# Whether a token is valid for this workspace (and port).
def valid(token: str, typ: str, ws_id: str, port: int = None) -> bool:
    try:
        claims = jwt.decode(token, TOKEN_SECRET, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return False
    return claims.get("typ") == typ and claims.get("ws") == ws_id and claims.get("port") == port


# The scheme the browser used (http or https).
def scheme(request: web.Request) -> str:
    return request.headers.get("X-Forwarded-Proto", request.scheme)


# Swaps a one-time token for a session cookie and redirects without the token.
def login_redirect(request: web.Request, ws_id: str, port: int, param: str, typ: str) -> web.Response:
    """Swap a valid one-time token in ?<param>= for a session cookie for this host (the
    editor's, or one port's), and drop the token from the URL."""
    if not valid(request.query[param], typ, ws_id, port):
        log("link refused", "WARNING", workspace_id=ws_id, port=port)
        where = "the CloudSmith dashboard" if port is None else "the workspace's Ports tab"
        return page(401, "Link expired", f"Open it again from {where}.")
    log("signed in", workspace_id=ws_id, port=port)
    query = {k: v for k, v in request.query.items() if k != param}
    resp = web.Response(status=302, headers={
        "Location": request.path + ("?" + urlencode(query) if query else ""),
        "Cache-Control": "no-store",
    })
    claims = {"typ": "workspace", "ws": ws_id} if port is None else {"typ": "workspace", "ws": ws_id, "port": port}
    resp.set_cookie(COOKIE, sign(claims, SESSION_TTL_S), max_age=SESSION_TTL_S, httponly=True,
                    samesite="Lax", path="/", secure=scheme(request) == "https")
    return resp


# Sends the browser on to a port host, from the editor host.
def port_login(request: web.Request, ws_id: str) -> web.Response:
    try:
        port = int(request.query.get("port", ""))
    except ValueError:
        port = 0
    return to_port_host(request, ws_id, port, request.query.get("next", "/"))


# Redirects to a port host with a one-minute token, so it can set its own cookie.
def to_port_host(request: web.Request, ws_id: str, port: int, target: str) -> web.Response:
    """On the editor's host, which has the workspace cookie: send the browser on to a port
    host with a one-minute token, so that host can set its own cookie."""
    if not 1 <= port <= 65535:
        return page(400, "Bad port", "Open the port again from the workspace's Ports tab.")
    if not target.startswith("/") or target.startswith("//"):  # only paths on the port host
        target = "/"
    token = sign({"typ": "port-link", "ws": ws_id, "port": port}, PORT_LINK_TTL_S)
    location = f"{scheme(request)}://{port}-{request.host}{target}{'&' if '?' in target else '?'}{PORT_TOKEN_PARAM}={token}"
    return web.Response(status=302, headers={"Location": location, "Cache-Control": "no-store"})


# The browser's headers to pass on to code-server.
def upstream_headers(request: web.Request, skip: set) -> CIMultiDict:
    headers = CIMultiDict((k, v) for k, v in request.headers.items() if k.lower() not in skip)
    # code-server checks that a WebSocket's Origin matches the forwarded host.
    headers["X-Forwarded-Host"] = request.headers.get("Host", "")
    headers["X-Forwarded-Proto"] = scheme(request)
    return headers


# Removes code-server's /proxy/<port> prefix from an app's redirect.
def unprefix(location: str, prefix: str) -> str:
    """code-server's /proxy/<port>/ adds its own prefix to an app's redirects. On a port host
    the app is at the root, so take the prefix off again."""
    parts = urlsplit(location)
    if parts.path == prefix or parts.path.startswith(prefix + "/"):
        return urlunsplit(parts._replace(path=parts.path[len(prefix):] or "/"))
    return location


# Passes an HTTP request through to code-server.
async def proxy_http(request: web.Request, upstream: str, path: str, strip: str = None) -> web.StreamResponse:
    try:
        resp = await request.app["client"].request(
            request.method,
            f"http://{upstream}{path}",
            headers=upstream_headers(request, HOP_BY_HOP),
            data=request.content if request.body_exists else None,
            allow_redirects=False,
        )
    except (aiohttp.ClientConnectorError, aiohttp.ServerDisconnectedError, asyncio.TimeoutError) as exc:
        host = HOST_RE.match(request.headers.get("Host", "").lower())
        log("workspace unreachable", "WARNING", workspace_id=host.group(2) if host else None, error=repr(exc))
        return page(502, "Your workspace is starting", "This page refreshes automatically.", refresh=True)
    async with resp:
        out = web.StreamResponse(status=resp.status, reason=resp.reason)
        for key, value in resp.headers.items():
            if key.lower() in HOP_BY_HOP:
                continue
            if strip and key.lower() == "location":
                value = unprefix(value, strip)
            out.headers.add(key, value)
        await out.prepare(request)
        async for chunk in resp.content.iter_any():
            await out.write(chunk)
        await out.write_eof()
    return out


# Passes a WebSocket connection through to code-server.
async def proxy_websocket(request: web.Request, upstream: str, path: str, ws_id: str) -> web.StreamResponse:
    protocols = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
    try:
        server = await request.app["client"].ws_connect(
            f"ws://{upstream}{path}",
            headers=upstream_headers(request, WS_SKIP),
            protocols=protocols,
            max_msg_size=0,
        )
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.Response(status=502, text="workspace not reachable")

    browser = web.WebSocketResponse(protocols=[server.protocol] if server.protocol else (), max_msg_size=0)
    await browser.prepare(request)

    # Copies messages from one WebSocket to the other.
    async def pump(src, dst):
        async for msg in src:
            if msg.type == WSMsgType.TEXT:
                await dst.send_str(msg.data)
            elif msg.type == WSMsgType.BINARY:
                await dst.send_bytes(msg.data)
            else:
                break

    tasks = [asyncio.create_task(pump(browser, server)), asyncio.create_task(pump(server, browser))]
    open_sockets[ws_id] = open_sockets.get(ws_id, 0) + 1
    if open_sockets[ws_id] == 1:
        log("browser connected", workspace_id=ws_id)
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        open_sockets[ws_id] -= 1
        if open_sockets[ws_id] == 0:
            log("last browser disconnected", workspace_id=ws_id)
        last_used[ws_id] = time.time()
        for task in tasks:
            task.cancel()
        await server.close()
        await browser.close()
    return browser


# The address of a workspace's code-server on its Docker network.
def upstream_for(ws_id: str) -> str:
    return f"ws-{ws_id}:8080"


# Routes each request by its host: checks access, then passes it to the workspace.
async def handle(request: web.Request) -> web.StreamResponse:
    match = HOST_RE.match(request.headers.get("Host", "").lower())
    if not match:
        if request.path == "/_cloudsmith/activity":
            return activity(request)
        return web.Response(text="CloudSmith proxy\n")  # also serves as the health check
    port = int(match.group(1)) if match.group(1) else None
    ws_id = match.group(2)

    if port is None:  # the editor
        if "token" in request.query:
            return login_redirect(request, ws_id, None, "token", "link")
        if not valid(request.cookies.get(COOKIE, ""), "workspace", ws_id):
            log("refused: no valid cookie", "WARNING", workspace_id=ws_id, path=request.path)
            return page(401, "Sign in required", "Open this workspace from the CloudSmith dashboard.")
        if request.path == PORT_LOGIN_PATH:
            return port_login(request, ws_id)
        old = CODE_SERVER_PORT_PATH.match(request.rel_url.raw_path)
        if old and request.method == "GET" and not request.headers.get("Upgrade"):
            # code-server's own address for a port (the Ports tab of workspaces created before
            # port hosts existed, or typed by hand) breaks apps that link from the root, so
            # send the page to the port's own host instead.
            target = (old.group(2) or "/") + (f"?{request.query_string}" if request.query_string else "")
            return to_port_host(request, ws_id, int(old.group(1)), target)
        path, strip = str(request.rel_url), None
    else:  # an app on a port inside the workspace
        if not 1 <= port <= 65535:
            return page(400, "Bad port", "Ports go from 1 to 65535.")
        if PORT_TOKEN_PARAM in request.query:
            return login_redirect(request, ws_id, port, PORT_TOKEN_PARAM, "port-link")
        if not valid(request.cookies.get(COOKIE, ""), "workspace", ws_id, port):
            if request.method != "GET" or request.headers.get("Upgrade"):
                return page(401, "Sign in required", "Open this port again from the workspace's Ports tab.")
            editor_host = request.host.split("-", 1)[1]  # 5000-ws-<id>.<domain> -> ws-<id>.<domain>
            return web.Response(status=302, headers={"Cache-Control": "no-store", "Location":
                f"{scheme(request)}://{editor_host}{PORT_LOGIN_PATH}?port={port}&next={quote(str(request.rel_url), safe='')}"})
        strip = f"/proxy/{port}"
        path = f"{strip}{request.rel_url}"  # code-server forwards it to localhost:<port> in the workspace

    last_used[ws_id] = time.time()
    upstream = upstream_for(ws_id)
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await proxy_websocket(request, upstream, path, ws_id)
    return await proxy_http(request, upstream, path, strip)


# Opens the HTTP client used to reach code-server.
async def on_startup(app: web.Application) -> None:
    app["client"] = aiohttp.ClientSession(
        auto_decompress=False,
        skip_auto_headers=["Accept-Encoding"],  # pass on only the browser's own, or compressed bodies reach clients that cannot read them
        cookie_jar=aiohttp.DummyCookieJar(),
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=5),
        connector=aiohttp.TCPConnector(limit=0, ttl_dns_cache=10),
    )


# Closes the HTTP client.
async def on_cleanup(app: web.Application) -> None:
    await app["client"].close()


# Builds the proxy application.
def make_app() -> web.Application:
    app = web.Application(client_max_size=1024**3)
    app.router.add_route("*", "/{tail:.*}", handle)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    app = make_app()
    log("proxy listening", port=PORT)
    web.run_app(app, port=PORT, access_log=None, print=None)
