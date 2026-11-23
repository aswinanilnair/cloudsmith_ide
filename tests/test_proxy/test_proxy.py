"""The proxy lets a visitor into a workspace only with a valid link or that workspace's own
cookie, routes app ports through code-server, and records when each workspace was used."""
import asyncio
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
import helpers
import proxy
from helpers import find_log
from proxytools import host, link_token, session_cookie

NO_REDIRECTS = {"allow_redirects": False}


# Sends a request to a workspace host through the proxy.
async def get(client, ws_id, path="/", cookie=None, port=None, method="GET", **kwargs):
    headers = {"Host": host(ws_id, port), **kwargs.pop("headers", {})}
    if cookie:
        headers["Cookie"] = cookie
    return await client.request(method, path, headers=headers, allow_redirects=False, **kwargs)


# ------------------------------------------------------------------ editor sign-in


# Without a cookie, the workspace is closed.
async def test_without_a_cookie_the_workspace_is_closed(client, ws_id):
    resp = await get(client, ws_id)
    assert resp.status == 401
    assert "Sign in required" in await resp.text()


# A valid link sets a cookie for that workspace only.
async def test_a_valid_link_sets_a_cookie_for_that_workspace_only(client, ws_id):
    resp = await get(client, ws_id, f"/?folder=/home/coder&token={link_token(ws_id)}")
    assert resp.status == 302
    assert resp.headers["Location"] == "/?folder=%2Fhome%2Fcoder"  # the token is dropped, the rest kept
    cookie = resp.cookies[proxy.COOKIE]
    assert cookie["httponly"] and cookie["samesite"] == "Lax" and cookie["path"] == "/"
    assert not cookie["domain"]  # host-only: not sent to other workspaces' subdomains

    ok = await get(client, ws_id, "/", cookie=f"{proxy.COOKIE}={cookie.value}")
    assert ok.status == 200


# Invalid links are refused.
@pytest.mark.parametrize("make_token", [
    lambda ws: link_token(ws, ttl=-10),                                           # expired
    lambda ws: link_token(str(uuid.uuid4())),                                     # for another workspace
    lambda ws: proxy.sign({"typ": "user", "sub": "u", "ws": ws}, 300),            # a dashboard session
    lambda ws: proxy.sign({"typ": "port-link", "ws": ws, "port": 5000}, 60),      # a port link
    lambda ws: "not-a-token",
])
async def test_invalid_links_are_refused(client, ws_id, make_token):
    resp = await get(client, ws_id, f"/?token={make_token(ws_id)}")
    assert resp.status == 401
    assert "Link expired" in await resp.text()
    assert proxy.COOKIE not in resp.cookies


# A link signed with another secret is refused.
async def test_a_link_signed_with_another_secret_is_refused(client, ws_id):
    import jwt
    forged = jwt.encode({"typ": "link", "ws": ws_id, "exp": int(time.time()) + 300}, "guessed-secret",
                        algorithm="HS256")
    assert (await get(client, ws_id, f"/?token={forged}")).status == 401


# One workspace's cookie does not open another.
async def test_one_workspaces_cookie_does_not_open_another(client, ws_id):
    other = str(uuid.uuid4())
    resp = await get(client, other, "/", cookie=session_cookie(ws_id))
    assert resp.status == 401


# Requests reach code-server with forwarded headers.
async def test_requests_reach_code_server_with_forwarded_headers(client, ws_id):
    resp = await get(client, ws_id, "/static/app.js?v=1", cookie=session_cookie(ws_id),
                     method="POST", data="hello", headers={"X-Forwarded-Proto": "https"})
    seen = await resp.json()
    assert seen["method"] == "POST" and seen["path"] == "/static/app.js?v=1" and seen["body"] == "hello"
    assert seen["headers"]["X-Forwarded-Host"] == host(ws_id)
    assert seen["headers"]["X-Forwarded-Proto"] == "https"


# The proxy does not ask for compression the browser did not.
async def test_the_proxy_does_not_ask_for_compression_the_browser_did_not(client, ws_id):
    resp = await client.get("/", headers={"Host": host(ws_id), "Cookie": session_cookie(ws_id)},
                            skip_auto_headers=["Accept-Encoding"])
    assert "Accept-Encoding" not in (await resp.json())["headers"]


# A workspace that is not up yet shows a starting page.
async def test_a_stopped_workspace_shows_a_starting_page(client, ws_id, monkeypatch):
    port = helpers.free_port()  # nothing listens there
    monkeypatch.setattr(proxy, "upstream_for", lambda _: f"127.0.0.1:{port}")
    resp = await get(client, ws_id, "/", cookie=session_cookie(ws_id))
    assert resp.status == 502
    assert "Your workspace is starting" in await resp.text()


# WebSockets need the cookie, then pass through.
async def test_websockets_need_the_cookie_and_then_pass_through(client, ws_id):
    with pytest.raises(Exception):
        await client.ws_connect("/socket", headers={"Host": host(ws_id)})
    ws = await client.ws_connect("/socket", headers={"Host": host(ws_id), "Cookie": session_cookie(ws_id)})
    await ws.send_str("hello")
    assert (await ws.receive_str()) == "echo: hello"
    await ws.close()


# Unknown hosts get the health check.
async def test_unknown_hosts_get_the_health_check(client):
    resp = await client.get("/", headers={"Host": "10.0.0.5"})
    assert resp.status == 200 and (await resp.text()) == "CloudSmith proxy\n"


# --------------------------------------------------------------------------- ports


# A port host without a cookie goes through the editor host.
async def test_a_port_host_without_a_cookie_goes_through_the_editor_host(client, ws_id):
    resp = await get(client, ws_id, "/books?page=2", port=5000)
    assert resp.status == 302
    loc = urlsplit(resp.headers["Location"])
    assert loc.netloc == host(ws_id) and loc.path == proxy.PORT_LOGIN_PATH
    assert parse_qs(loc.query) == {"port": ["5000"], "next": ["/books?page=2"]}


# The full sign-in round trip for a port host.
async def test_port_login_round_trip(client, ws_id):
    """Editor host (with its cookie) -> port host with a one-minute token -> port cookie -> the app."""
    resp = await get(client, ws_id, f"{proxy.PORT_LOGIN_PATH}?port=5000&next=%2Fbooks%3Fpage%3D2",
                     cookie=session_cookie(ws_id))
    assert resp.status == 302
    loc = urlsplit(resp.headers["Location"])
    assert loc.netloc == host(ws_id, 5000) and loc.path == "/books"
    token = parse_qs(loc.query)[proxy.PORT_TOKEN_PARAM][0]

    resp = await get(client, ws_id, f"/books?page=2&{proxy.PORT_TOKEN_PARAM}={token}", port=5000)
    assert resp.status == 302 and resp.headers["Location"] == "/books?page=2"
    port_cookie = f"{proxy.COOKIE}={resp.cookies[proxy.COOKIE].value}"

    resp = await get(client, ws_id, "/books?page=2", port=5000, cookie=port_cookie)
    seen = await resp.json()
    assert seen["path"] == "/proxy/5000/books?page=2"  # through code-server's port forwarding


# Port sign-in needs the editor cookie.
async def test_port_login_needs_the_editor_cookie(client, ws_id):
    resp = await get(client, ws_id, f"{proxy.PORT_LOGIN_PATH}?port=5000")
    assert resp.status == 401


# Port sign-in only redirects within the port host.
@pytest.mark.parametrize("next_path", ["//evil.example/x", "https://evil.example/x"])
async def test_port_login_only_redirects_within_the_port_host(client, ws_id, next_path):
    resp = await get(client, ws_id, proxy.PORT_LOGIN_PATH, cookie=session_cookie(ws_id),
                     params={"port": "5000", "next": next_path})
    loc = urlsplit(resp.headers["Location"])
    assert loc.netloc == host(ws_id, 5000) and loc.path == "/"


# Port sign-in rejects invalid ports.
@pytest.mark.parametrize("port", ["0", "70000", "abc"])
async def test_port_login_rejects_bad_ports(client, ws_id, port):
    resp = await get(client, ws_id, f"{proxy.PORT_LOGIN_PATH}?port={port}", cookie=session_cookie(ws_id))
    assert resp.status == 400


# Port hosts out of range are rejected.
@pytest.mark.parametrize("port", [0, 70000])
async def test_port_hosts_out_of_range_are_rejected(client, ws_id, port):
    assert (await get(client, ws_id, "/", port=port)).status == 400


# Cookies are not shared between the editor and its ports.
async def test_cookies_are_not_shared_between_the_editor_and_its_ports(client, ws_id):
    # The editor's cookie does not open a port host (it is sent to sign in there instead)...
    assert (await get(client, ws_id, "/", port=5000, cookie=session_cookie(ws_id))).status == 302
    # ...a port's cookie opens neither the editor nor another port...
    assert (await get(client, ws_id, "/", cookie=session_cookie(ws_id, 5000))).status == 401
    assert (await get(client, ws_id, "/", port=8000, cookie=session_cookie(ws_id, 5000))).status == 302
    # ...and another workspace's port cookie does not open this one's port.
    other = session_cookie(str(uuid.uuid4()), 5000)
    assert (await get(client, ws_id, "/", port=5000, cookie=other)).status == 302


# A port host refuses non-GET requests without a cookie.
async def test_a_port_host_refuses_non_get_requests_without_a_cookie(client, ws_id):
    resp = await get(client, ws_id, "/login", port=5000, method="POST", data="user=a")
    assert resp.status == 401


# An app's redirects lose code-server's /proxy/<port> prefix.
async def test_an_apps_redirects_lose_code_servers_prefix(client, ws_id):
    resp = await get(client, ws_id, "/go-to-login", port=5000, cookie=session_cookie(ws_id, 5000))
    assert resp.status == 302
    assert resp.headers["Location"] == "/login?next=%2F"


# unprefix strips only this port's prefix from a redirect.
@pytest.mark.parametrize("location, expected", [
    ("/proxy/5000/login", "/login"),
    ("/proxy/5000", "/"),
    ("/proxy/5000?x=1", "/?x=1"),
    ("http://5000-ws-x.test/proxy/5000/a", "http://5000-ws-x.test/a"),
    ("/proxy/50001/a", "/proxy/50001/a"),  # another port's prefix is left alone
    ("/elsewhere", "/elsewhere"),
])
def test_unprefix(location, expected):
    assert proxy.unprefix(location, "/proxy/5000") == expected


# code-server's own /proxy/<port>/ address redirects to the port host.
async def test_code_servers_own_port_address_redirects_to_the_port_host(client, ws_id):
    """The Ports tab of older workspaces opens /proxy/<port>/ on the editor host."""
    resp = await get(client, ws_id, "/proxy/5000/static/style.css?v=2", cookie=session_cookie(ws_id))
    assert resp.status == 302
    loc = urlsplit(resp.headers["Location"])
    assert loc.netloc == host(ws_id, 5000) and loc.path == "/static/style.css"
    assert parse_qs(loc.query)["v"] == ["2"]


# Non-GET requests to code-server's port address pass through.
async def test_non_get_requests_to_code_servers_port_address_pass_through(client, ws_id):
    resp = await get(client, ws_id, "/proxy/5000/api", cookie=session_cookie(ws_id), method="POST", data="{}")
    assert (await resp.json())["path"] == "/proxy/5000/api"


# ------------------------------------------------------------------------ activity


# Asks the proxy for its activity report.
async def activity(client, token="test-internal-token"):
    return await client.get("/_cloudsmith/activity", headers={"Host": "localhost:8080", "X-Internal-Token": token})


# The activity report needs the internal token.
async def test_activity_needs_the_internal_token(client):
    assert (await activity(client, token="")).status == 403
    assert (await activity(client, token="wrong")).status == 403


# Requests are recorded as activity.
async def test_requests_are_recorded_as_activity(client, ws_id):
    before = time.time()
    await get(client, ws_id, "/", cookie=session_cookie(ws_id))
    seen = await (await activity(client)).json()
    assert seen[ws_id] >= before


# Refused requests are not activity.
async def test_refused_requests_are_not_activity(client, ws_id):
    await get(client, ws_id, "/")  # no cookie
    assert ws_id not in await (await activity(client)).json()


# An open editor tab counts as activity.
async def test_an_open_editor_tab_counts_as_activity(client, ws_id):
    ws = await client.ws_connect("/socket", headers={"Host": host(ws_id), "Cookie": session_cookie(ws_id)})
    await ws.send_str("ping")
    await ws.receive_str()
    proxy.last_used[ws_id] = 0  # long ago
    seen = await (await activity(client)).json()
    assert seen[ws_id] >= time.time() - 5  # in use now, because the socket is open

    await ws.close()
    await asyncio.sleep(0.2)
    assert proxy.open_sockets[ws_id] == 0
    assert proxy.last_used[ws_id] >= time.time() - 5  # closing the tab counts as its last use


# ------------------------------------------------------------------------- logging


# Sign-ins and refusals are logged.
async def test_sign_ins_and_refusals_are_logged(client, ws_id, capsys):
    await get(client, ws_id, f"/?token={link_token(ws_id)}")
    await get(client, ws_id, f"/?token={link_token(ws_id, ttl=-10)}")
    await get(client, ws_id, "/")
    out = capsys.readouterr().out
    [signed_in] = find_log(out, "signed in")
    assert signed_in["severity"] == "INFO" and signed_in["workspace_id"] == ws_id and signed_in["component"] == "proxy"
    assert find_log(out, "link refused")[0]["severity"] == "WARNING"
    assert find_log(out, "refused: no valid cookie")[0]["workspace_id"] == ws_id


# The first and last browser connections are logged.
async def test_the_first_and_last_browser_connections_are_logged(client, ws_id, capsys):
    headers = {"Host": host(ws_id), "Cookie": session_cookie(ws_id)}
    first = await client.ws_connect("/socket", headers=headers)
    second = await client.ws_connect("/socket", headers=headers)  # e.g. a second editor tab
    for ws in (first, second):
        await ws.send_str("ping")
        await ws.receive_str()
    await first.close()
    await second.close()
    await asyncio.sleep(0.2)
    out = capsys.readouterr().out
    assert len(find_log(out, "browser connected")) == 1
    assert len(find_log(out, "last browser disconnected")) == 1


# An unreachable workspace is logged.
async def test_an_unreachable_workspace_is_logged(client, ws_id, monkeypatch, capsys):
    port = helpers.free_port()
    monkeypatch.setattr(proxy, "upstream_for", lambda _: f"127.0.0.1:{port}")
    await get(client, ws_id, "/", cookie=session_cookie(ws_id))
    [unreachable] = find_log(capsys.readouterr().out, "workspace unreachable")
    assert unreachable["severity"] == "WARNING" and unreachable["workspace_id"] == ws_id
