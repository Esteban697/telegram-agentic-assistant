"""One-time Google OAuth authorization for the Calendar integration.

Run this **locally**, once, after creating a Desktop OAuth client in the Google Cloud project:

    python scripts/google_auth.py --client-file ~/Downloads/client_secret_....json

It opens the browser, receives the loopback redirect, exchanges the code with PKCE, and writes a
single credentials JSON to ``~/.taa/secrets/google-oauth-credentials`` (mode 600, exact bytes, no
CRLF). The deploy script mounts that file as ``GOOGLE_OAUTH_CREDENTIALS``.

It never prints the refresh token or the client secret. The consent screen must be published to
**In production** (unverified is fine for the owner's account): in **Testing** Google expires
refresh tokens after 7 days.

The loopback + PKCE flow is the one Google recommends for installed apps:
https://developers.google.com/identity/protocols/oauth2/native-app
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import pathlib
import secrets
import socketserver
import sys
import threading
import urllib.parse
import webbrowser
from typing import Any

import httpx

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.integrations.google_calendar import CALENDAR_SCOPES, TOKEN_URI  # noqa: E402

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
DEFAULT_OUTPUT = pathlib.Path.home() / ".taa" / "secrets" / "google-oauth-credentials"


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _client_from_file(path: pathlib.Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    installed = payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(installed, dict):
        raise ValueError(
            "the client file is not a Desktop OAuth client (expected an 'installed' object)"
        )
    client_id = str(installed.get("client_id") or "")
    client_secret = str(installed.get("client_secret") or "")
    if not client_id or not client_secret:
        raise ValueError("the client file has no client_id/client_secret")
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "token_uri": str(installed.get("token_uri") or TOKEN_URI),
    }


class _LoopbackServer(http.server.HTTPServer):
    """`HTTPServer.server_bind` calls `socket.getfqdn`, which can block on DNS.

    The loopback listener only needs the bound port, so skip the reverse lookup.
    """

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server_version = "taa-oauth/1.0"

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        state = (params.get("state") or [""])[0]
        error = (params.get("error") or [""])[0]
        code = (params.get("code") or [""])[0]

        result = self.server.result  # type: ignore[attr-defined]
        if error:
            result["error"] = error
        elif state != result["state"]:
            result["error"] = "state_mismatch"
        elif code:
            result["code"] = code
        else:
            result["error"] = "missing_code"

        body = b"<html><body><h1>Authorization received.</h1>You can close this tab.</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return  # keep the terminal quiet and never echo the query string


def _wait_for_callback(
    server: http.server.HTTPServer, *, state: str, timeout: float
) -> tuple[str, int]:
    result: dict[str, str] = {"state": state, "code": "", "error": ""}
    server.result = result  # type: ignore[attr-defined]    server.timeout = timeout
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout + 1)
    if result["error"]:
        raise RuntimeError(f"authorization failed: {result['error']}")
    if not result["code"]:
        raise RuntimeError("timed out waiting for the loopback redirect")
    return result["code"], server.server_address[1]


def _exchange_code(
    *, client: dict[str, str], code: str, verifier: str, redirect_uri: str
) -> dict[str, Any]:
    response = httpx.post(
        client["token_uri"],
        data={
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30.0,
    )
    if response.status_code >= 400:
        # Do not echo the body: it can contain OAuth error details.
        raise RuntimeError(f"token exchange returned status {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("refresh_token"):
        raise RuntimeError(
            "token exchange returned no refresh_token; revoke the app's access and retry"
        )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Authorize the Google Calendar integration and write the OAuth credentials."
    )
    parser.add_argument("--client-file", required=True, type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)

    try:
        client = _client_from_file(args.client_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 1

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)

    # Bind the real listener first so the URL carries the actual ephemeral port. Reusing a
    # probe-then-rebind was racy on Windows.
    server = _LoopbackServer(("127.0.0.1", args.port), _CallbackHandler)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}"

    auth_params = {
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(CALENDAR_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    url = f"{AUTH_URI}?{urllib.parse.urlencode(auth_params)}"
    print("Open this URL and authorize the Calendar access:")
    print(url)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - headless machines
            pass

    try:
        code, actual_port = _wait_for_callback(server, state=state, timeout=args.timeout)
        redirect_uri = f"http://127.0.0.1:{actual_port}"
        payload = _exchange_code(
            client=client, code=code, verifier=verifier, redirect_uri=redirect_uri
        )
    except (RuntimeError, httpx.HTTPError) as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        server.server_close()

    credentials = {
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "refresh_token": payload["refresh_token"],
        "token_uri": client["token_uri"],
        "scopes": list(CALENDAR_SCOPES),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(
        json.dumps(credentials, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    try:
        os.chmod(args.output, 0o600)
    except OSError:  # pragma: no cover - Windows may not support mode bits
        pass
    print(f"OK: credentials written to {args.output}")
    print("Next: run the deploy script; it will create the secret and enable the calendar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
