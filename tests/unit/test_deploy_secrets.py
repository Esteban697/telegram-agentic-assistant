"""Phase 5, the two scripts that touch secrets: `set_webhook.py` and `audit_image.py`.

Credential-free, database-free and network-free: `--print` never connects, the failure path is a
*validation* failure that happens before any I/O, and the image audit's `--self-test` builds
synthetic tars in memory instead of touching docker.

The rule being tested is the one the coordinator added to the deploy brief, and it has a concrete
history here: Telegram puts the bot token in the **URL path**, `httpx` renders the URL in its
exception messages, and Phase 1 shipped a log line that contained a live-shaped token because of
it. So the assertions are about *values reaching output*, not about code style — including on the
failure path, which is where values usually escape.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"

# Shaped like a real Telegram bot token (six-plus digits, colon, 35 characters) so the *pattern*
# redactors have something to catch, but obviously not a real one.
SENTINEL = "123456789:AAHsentinel-not-a-real-token-0123456789"


def load_script(filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_script(
    filename: str,
    args: list[str],
    *,
    cwd: pathlib.Path | None = None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    import os

    environment = dict(os.environ)
    environment.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(SCRIPTS / filename), *args],
        cwd=cwd or REPO,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


@pytest.fixture
def token_file(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "telegram-bot-token"
    path.write_text(SENTINEL + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# set_webhook.py
# --------------------------------------------------------------------------- #
def test_the_webhook_url_shape_never_carries_the_token() -> None:
    """The placeholder is built by a function that has no token argument to leak."""

    set_webhook = load_script("set_webhook.py")

    shape = set_webhook.url_shape("https://api.telegram.org", "setWebhook")

    assert shape == "https://api.telegram.org/bot<redacted>/setWebhook"
    assert "<redacted>" in shape


def test_the_leak_shape_is_real_which_is_why_the_placeholder_matters() -> None:
    """The negative control for the test above: the same URL *with* a token really does carry it.

    Without this, "the shape has no token" could be true of a function that never had one to lose.
    Telegram's API is `…/bot<token>/<method>`, so every rendering of the URL is a rendering of the
    token — exactly what the Phase 1 log line did.
    """

    url_with_token = f"https://api.telegram.org/bot{SENTINEL}/setWebhook"
    assert SENTINEL in url_with_token

    set_webhook = load_script("set_webhook.py")
    assert SENTINEL not in set_webhook.url_shape("https://api.telegram.org", "setWebhook")
    assert SENTINEL not in set_webhook.redact(url_with_token, SENTINEL)


def test_print_mode_shows_the_call_without_the_value(
    token_file: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    result = run_script(
        "set_webhook.py",
        [
            "--print",
            "--token-file",
            str(token_file),
            "--url",
            "https://example.invalid/hook",
            # Passed explicitly so the script never falls back to the developer's `.env`; the
            # hermetic version of this test is worth more than the convenient one.
            "--secret-file",
            str(_write_secret(tmp_path, "a-fine-secret")),
        ],
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert "bot<redacted>/setWebhook" in result.stdout, (
        "precondition: the print mode must describe the call it would make"
    )
    assert "https://example.invalid/hook" in result.stdout
    assert SENTINEL not in result.stdout
    assert SENTINEL not in result.stderr


def test_the_failure_path_does_not_print_the_value(token_file: pathlib.Path) -> None:
    """**The failure path is the one that leaks.** The value is in memory when this error is made.

    The failure is deterministic and offline by construction: an invalid webhook secret is
    rejected *after* the token has been read, before any request is sent. So the assertion is not
    vacuous — the process really is holding the token when it prints its error.
    """

    result = run_script(
        "set_webhook.py",
        [
            "--set",
            "--token-file",
            str(token_file),
            "--url",
            "https://example.invalid/hook",
            "--secret-file",
            str(_write_bad_secret(token_file.parent)),
        ],
    )

    assert result.returncode == 1
    assert SENTINEL not in result.stdout
    assert SENTINEL not in result.stderr
    # Precondition: the run got past reading the token (so it was in memory) and failed on the
    # rule it should have failed on — not on "no token".
    assert "no bot token" not in result.stderr
    assert "secret" in result.stderr.lower()


def _write_secret(directory: pathlib.Path, value: str) -> pathlib.Path:
    path = directory / "telegram-webhook-secret"
    path.write_text(value + "\n", encoding="utf-8")
    return path


def _write_bad_secret(directory: pathlib.Path) -> pathlib.Path:
    return _write_secret(directory, "not a valid secret! spaces and !")


def test_a_missing_token_is_reported_without_inventing_one(tmp_path: pathlib.Path) -> None:
    """Runs in a directory with no `.env`, so the fallback cannot quietly supply a real token."""

    result = run_script(
        "set_webhook.py",
        ["--set", "--token-file", str(tmp_path / "does-not-exist"), "--url", "https://x.invalid/h"],
        cwd=tmp_path,
        # Cleared explicitly: on a machine where the variable *is* exported, the script would
        # correctly find a token and this test would be asserting the wrong thing.
        env_extra={"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_WEBHOOK_SECRET": ""},
    )

    assert result.returncode == 1
    assert "no bot token" in result.stderr


def test_the_token_is_never_taken_from_an_argument() -> None:
    """Not a style rule: an argv value is in the process table and in the shell history."""

    source = (SCRIPTS / "set_webhook.py").read_text(encoding="utf-8")

    assert "--token" not in source.replace("--token-file", "")
    assert '"--token"' not in source
    assert "--token-file" in source


# --------------------------------------------------------------------------- #
# audit_image.py (acceptance (c), without docker)
# --------------------------------------------------------------------------- #
def test_the_image_audit_detector_can_fail() -> None:
    """`--self-test` plants the things an image audit exists to catch and requires them detected.

    This is the credential-free half of acceptance (c): the real audit runs `docker export` and
    walks the union filesystem, and `--self-test` proves the *detector* it uses is not inert.
    """

    result = run_script("audit_image.py", ["--self-test"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert "self-test ok" in result.stdout
    for expected in (".env", "service-account.json", "key.pem", "sentinel"):
        assert expected in result.stdout, f"the self-test did not report detecting {expected}"


def test_the_detector_flags_a_sentinel_even_in_a_harmless_looking_file(tmp_path: pathlib.Path) -> None:
    """The value-based layer: a literal that must not be in the image, wherever it appears."""

    import io
    import tarfile

    audit = load_script("audit_image.py")

    archive_path = tmp_path / "image.tar"
    with tarfile.open(archive_path, "w") as archive:
        for name, body in (
            ("app/notes.txt", f"remember: {SENTINEL}\n".encode()),
            ("app/main.py", b"print('hola')\n"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))

    findings = audit.scan_tar(archive_path, sentinel=SENTINEL)

    assert any("sentinel" in finding for finding in findings), findings
    assert not any("main.py" in finding for finding in findings), findings


def test_the_detector_passes_a_clean_image(tmp_path: pathlib.Path) -> None:
    import io
    import tarfile

    audit = load_script("audit_image.py")

    archive_path = tmp_path / "clean.tar"
    with tarfile.open(archive_path, "w") as archive:
        body = b'TELEGRAM_BOT_TOKEN = "the name of the variable, not a value"\n'
        info = tarfile.TarInfo("app/config.py")
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))

    assert audit.scan_tar(archive_path) == []


# --------------------------------------------------------------------------- #
# B7: the documented rollback has to exist
# --------------------------------------------------------------------------- #
def test_delete_mode_deregisters_the_webhook(token_file: pathlib.Path) -> None:
    """`docs/despliegue.md` §8 told the operator to run `--delete`, which was not implemented.

    Driven offline: the module's `TelegramClient` is replaced, so no request leaves the machine and
    the assertion is on the call the script makes.
    """

    set_webhook = load_script("set_webhook.py")
    calls: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *, token: str, base_url: str) -> None:
            calls["token"] = token
            calls["base_url"] = base_url

        async def delete_webhook(self, *, drop_pending_updates: bool = False) -> bool:
            calls["drop_pending"] = drop_pending_updates
            return True

        async def aclose(self) -> None:
            calls["closed"] = True

    original = set_webhook.TelegramClient
    set_webhook.TelegramClient = FakeClient  # type: ignore[assignment]
    try:
        code = set_webhook.main(["--delete", "--token-file", str(token_file)])
    finally:
        set_webhook.TelegramClient = original  # type: ignore[assignment]

    assert code == 0
    assert calls["drop_pending"] is False
    assert calls["closed"] is True, "the client must be closed even on the happy path"


def test_delete_mode_is_recognised_and_needs_a_token(tmp_path: pathlib.Path) -> None:
    """`unrecognized arguments: --delete`, exit 2, was the old behaviour; a missing token is not."""

    result = run_script(
        "set_webhook.py",
        ["--delete", "--token-file", str(tmp_path / "nope")],
        cwd=tmp_path,
        env_extra={"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_WEBHOOK_SECRET": ""},
    )

    assert result.returncode == 1, result.stderr
    assert "no bot token" in result.stderr
    assert "unrecognized arguments" not in result.stderr


def test_the_runbook_rollback_commands_exist_in_the_script() -> None:
    """The documented flag and the implemented flags must be the same set."""

    runbook = (REPO / "docs" / "despliegue.md").read_text(encoding="utf-8")
    source = (SCRIPTS / "set_webhook.py").read_text(encoding="utf-8")

    for flag in ("--print", "--set", "--info", "--delete"):
        if flag in runbook:
            assert f'"{flag}"' in source or f"'{flag}'" in source, (
                f"{flag} is documented but not implemented"
            )
