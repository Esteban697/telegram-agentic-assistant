"""The one-time Google OAuth helper: PKCE and client-file parsing only.

The interactive loopback flow is exercised by hand; these tests pin the pieces that are easy to
get wrong and that must not print or store a malformed credential.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from scripts.google_auth import _client_from_file, _pkce_pair


def test_pkce_pair_is_valid_and_s256() -> None:
    verifier, challenge = _pkce_pair()

    assert 43 <= len(verifier) <= 128
    assert all(char.isalnum() or char in "-._~" for char in verifier)
    assert "=" not in challenge
    expected = base64.urlsafe_b64encode(
        __import__("hashlib").sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_client_file_must_be_a_desktop_client(tmp_path: Path) -> None:
    client = {
        "installed": {
            "client_id": "id.apps.googleusercontent.com",
            "client_secret": "secret",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    path = tmp_path / "client.json"
    path.write_text(json.dumps(client), encoding="utf-8")

    parsed = _client_from_file(path)
    assert parsed["client_id"] == "id.apps.googleusercontent.com"
    assert parsed["token_uri"] == "https://oauth2.googleapis.com/token"


def test_client_file_rejects_a_web_client_or_incomplete_payload(tmp_path: Path) -> None:
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {"client_id": "id", "client_secret": "s"}}), encoding="utf-8")
    with pytest.raises(ValueError):
        _client_from_file(web)

    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps({"installed": {"client_id": "id"}}), encoding="utf-8")
    with pytest.raises(ValueError):
        _client_from_file(incomplete)
