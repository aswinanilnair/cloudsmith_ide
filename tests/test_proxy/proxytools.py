"""Helpers for the proxy tests: hosts, tokens and cookies as the API and the proxy make them."""
import proxy

DOMAIN = "worker.test"


# The host name of a workspace, or of one of its ports.
def host(ws_id: str, port: int = None) -> str:
    return f"{port}-ws-{ws_id}.{DOMAIN}" if port is not None else f"ws-{ws_id}.{DOMAIN}"


# Signs a link token like the one in an Open link.
def link_token(ws_id: str, ttl: int = 300) -> str:
    return proxy.sign({"typ": "link", "sub": "some-user", "ws": ws_id}, ttl)


# Builds a session cookie for a workspace, or one of its ports.
def session_cookie(ws_id: str, port: int = None) -> str:
    claims = {"typ": "workspace", "ws": ws_id}
    if port is not None:
        claims["port"] = port
    return f"{proxy.COOKIE}={proxy.sign(claims, 3600)}"
