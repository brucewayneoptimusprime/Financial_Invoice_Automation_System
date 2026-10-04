"""Checks that the OAuth redirect URI registered with Google reaches THIS server process.

Deployed, the redirect URI is the frontend's https address (Vercel proxies /api/gmail/* to this server; DEPLOY_GMAIL_FIX.md), so
for an https URI only the path is checked: the host and port are the proxy's, not this process's.

The Google OAuth client allows exactly http://localhost:8000/api/gmail/oauth/callback. `serve` binds 127.0.0.1:8000 by default;
a browser sends "localhost" to the loopback interface, which works as long as "localhost" resolves to the address the server is
bound to (or the server listens on every interface). `serve` prints these problems at start-up and does not refuse to start,
because Gmail import is optional.
"""
import ipaddress
import socket
from urllib.parse import urlsplit

CALLBACK_PATH = "/api/gmail/oauth/callback"
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}


def localhost_addresses() -> set[str]:
    try:
        return {info[4][0] for info in socket.getaddrinfo("localhost", None)}
    except OSError:
        return set()


def callback_problems(redirect_uri: str, bind_host: str, bind_port: int, resolve=localhost_addresses) -> list[str]:
    parts = urlsplit(redirect_uri)
    if parts.scheme == "https" and parts.hostname:
        return [] if parts.path == CALLBACK_PATH else [f"GMAIL_REDIRECT_URI must end with {CALLBACK_PATH} (got {parts.path or '/'})."]
    if parts.scheme != "http" or (parts.hostname or "") not in _LOOPBACK_NAMES:
        return [f"GMAIL_REDIRECT_URI {redirect_uri} is neither an http://localhost address nor an https address."]
    problems: list[str] = []
    if parts.path != CALLBACK_PATH:
        problems.append(f"GMAIL_REDIRECT_URI must end with {CALLBACK_PATH} (got {parts.path or '/'}).")
    port = parts.port or 80
    if port != bind_port:
        problems.append(f"GMAIL_REDIRECT_URI uses port {port} but the server listens on {bind_port}, so Google would send the browser "
                        f"to a port nobody answers. Start with --port {port}.")
    if bind_host in ("0.0.0.0", "::", "localhost"):
        return problems
    if parts.hostname == "localhost":
        addresses = resolve()
        try:
            bound = str(ipaddress.ip_address(bind_host))
        except ValueError:
            bound = bind_host
        if bound not in addresses:
            fix = sorted(addresses)[0] if addresses else "127.0.0.1"
            problems.append(f"'localhost' resolves to {', '.join(sorted(addresses)) or 'nothing'} on this machine, but the server is "
                            f"bound to {bind_host}, so the OAuth callback would not reach it. Start with --host {fix}.")
    return problems
