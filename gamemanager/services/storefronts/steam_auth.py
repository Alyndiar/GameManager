from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
import re
import threading
from urllib import parse, request
import webbrowser


_STEAM_OPENID_ENDPOINT = "https://steamcommunity.com/openid/login"
_STEAM_ID_RE = re.compile(r"/openid/id/(?P<id>\d+)$")


@dataclass(slots=True)
class SteamOpenIdResult:
    success: bool
    steam_id: str = ""
    error: str = ""


class _SteamOpenIdServer(HTTPServer):
    allow_reuse_address = True

    def __init__(self, addr):
        super().__init__(addr, _SteamOpenIdHandler)
        self.payload: dict[str, str] = {}
        self.event = threading.Event()


class _SteamOpenIdHandler(BaseHTTPRequestHandler):
    server: _SteamOpenIdServer

    def log_message(self, format, *args):  # noqa: A003
        _ = (format, args)

    def do_GET(self):  # noqa: N802
        parsed = parse.urlparse(self.path)
        if parsed.path != "/steam-openid/callback":
            self.send_response(404)
            self.end_headers()
            return
        query = parse.parse_qs(parsed.query, keep_blank_values=True)
        payload = {
            str(key): str(values[0]) if values else ""
            for key, values in query.items()
        }
        self.server.payload = payload
        self.server.event.set()
        content = (
            "<html><body><h3>Steam sign-in completed.</h3>"
            "<p>You can close this tab and return to GameManager.</p></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def _openid_verify(payload: dict[str, str]) -> bool:
    required = (
        "openid.assoc_handle",
        "openid.signed",
        "openid.sig",
    )
    for key in required:
        if not str(payload.get(key, "")).strip():
            return False
    verify_payload = dict(payload)
    verify_payload["openid.mode"] = "check_authentication"
    body = parse.urlencode(verify_payload).encode("utf-8")
    req = request.Request(
        _STEAM_OPENID_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with request.urlopen(req, timeout=20) as response:
        text = response.read().decode("utf-8", errors="ignore")
    return "is_valid:true" in text


def _steam_id_from_payload(payload: dict[str, str]) -> str:
    claimed = str(payload.get("openid.claimed_id", "")).strip()
    match = _STEAM_ID_RE.search(claimed)
    if not match:
        return ""
    return str(match.group("id") or "").strip()


def authenticate_steam_openid(timeout_s: int = 300) -> SteamOpenIdResult:
    server = _SteamOpenIdServer(("127.0.0.1", 0))
    port = int(server.server_port)
    callback = f"http://127.0.0.1:{port}/steam-openid/callback"
    realm = f"http://127.0.0.1:{port}/"
    auth_params = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": callback,
        "openid.realm": realm,
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }
    auth_url = f"{_STEAM_OPENID_ENDPOINT}?{parse.urlencode(auth_params)}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not webbrowser.open(auth_url, new=2):
            return SteamOpenIdResult(
                success=False,
                error="Could not open browser for Steam sign-in.",
            )
        if not server.event.wait(timeout=float(max(15, int(timeout_s)))):
            return SteamOpenIdResult(
                success=False,
                error="Timed out waiting for Steam sign-in callback.",
            )
        payload = dict(server.payload or {})
        if not _openid_verify(payload):
            return SteamOpenIdResult(
                success=False,
                error="Steam OpenID verification failed.",
            )
        steam_id = _steam_id_from_payload(payload)
        if not steam_id:
            return SteamOpenIdResult(
                success=False,
                error="Steam sign-in completed but no SteamID was returned.",
            )
        return SteamOpenIdResult(success=True, steam_id=steam_id)
    except Exception as exc:
        return SteamOpenIdResult(success=False, error=str(exc))
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
