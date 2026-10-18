"""Register (or inspect) the Telegram webhook — PLAN.md §11.6, spec §13.

    python scripts/set_webhook.py --print --token-file ~/.taa/secrets/telegram-bot-token \
        --url https://<servicio>/telegram/webhook --secret-file ~/.taa/secrets/telegram-webhook-secret
    python scripts/set_webhook.py --set   --token-file ... --url ... --secret-file ...
    python scripts/set_webhook.py --info  --token-file ...

**This script is the one place in the deployment that genuinely holds the bot token in memory**,
because Telegram's API puts it in the URL path: `https://api.telegram.org/bot<TOKEN>/setWebhook`.
That is exactly the shape of the Phase 1 defect — `httpx` echoes the request URL in its exception
messages, so a single `repr(exc)` was enough to put a bot token in a log line, and no unit test
caught it. Therefore:

* the token is read from a **file** (`--token-file`) or the environment, never from an argv
  argument, so it is not in the shell history and not in `ps`;
* nothing here ever prints or logs a URL that contains it — `--print` shows
  `.../bot<redacted>/setWebhook`;
* every failure message goes through `redact()`, which removes the token by value **and** anything
  credential-shaped by pattern (`app.logging_config.scrub_text`).

The transport is `app.bot.telegram_client.TelegramClient` rather than a fresh `httpx` call, because
that client already converts transport failures into `TelegramError("<method> transport failure")`
— a message with no URL in it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bot.telegram_client import TelegramClient  # noqa: E402
from app.errors import AssistantError  # noqa: E402
from app.logging_config import scrub_text  # noqa: E402

PLACEHOLDER = "<redacted>"
# The same shape `app/config.py` enforces in production (Telegram's own rule for setWebhook).
_WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


def redact(text: str, *secrets: str) -> str:
    """Make a message safe to print. Value-based first, then credential-shaped."""

    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, PLACEHOLDER)
    return scrub_text(redacted, limit=600)


def url_shape(base_url: str, method: str) -> str:
    """The URL as it may be printed: the token position is a placeholder, never a value.

    Deliberately takes no token: there is no argument to this function that could carry a secret
    into a print statement, which is a stronger guarantee than remembering to redact one.
    """

    return f"{redact(base_url)}/bot{PLACEHOLDER}/{method}"


def read_secret(path: str | None, *, env_var: str = "", env_file: Path | None = None) -> str:
    """Read a secret from a file first, then the environment, then `.env`. Never from argv."""

    if path:
        candidate = Path(path)
        if not candidate.is_file():
            return ""
        return candidate.read_text(encoding="utf-8").strip()
    value = os.environ.get(env_var, "").strip()
    if value:
        return value
    candidate = env_file or Path(".env")
    if candidate.is_file():
        for line in candidate.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{env_var}="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def _validate(*, token: str, url: str, secret: str, base_url: str) -> None:
    """Fail before any I/O, with messages that never contain the token."""

    if not token:
        raise ValueError(
            "no bot token: pass --token-file, set TELEGRAM_BOT_TOKEN, or put it in .env"
        )
    if not url:
        raise ValueError("no webhook URL: pass --url")
    if not url.startswith("https://"):
        # Telegram refuses anything else, so failing here saves a round trip — and the URL is not
        # a secret, so it may be echoed.
        raise ValueError(f"the webhook URL must be https:// (got {url!r})")
    if not _WEBHOOK_SECRET_RE.match(secret or ""):
        raise ValueError(
            "the webhook secret must be 1-256 characters of [A-Za-z0-9_-] "
            "(Telegram's own rule; see TELEGRAM_WEBHOOK_SECRET)"
        )
    if not base_url.startswith(("https://", "http://")):
        raise ValueError(f"--api-base-url must be an absolute http(s) URL (got {base_url!r})")


async def _run(args: argparse.Namespace) -> int:
    token = read_secret(args.token_file, env_var="TELEGRAM_BOT_TOKEN")
    secret = read_secret(args.secret_file, env_var="TELEGRAM_WEBHOOK_SECRET")

    if args.info:
        if not token:
            raise ValueError("no bot token: pass --token-file or set TELEGRAM_BOT_TOKEN")
        client = TelegramClient(token=token, base_url=args.api_base_url)
        try:
            info = await client.get_webhook_info()
        finally:
            await client.aclose()
        # The token never appears in this payload; `redact` is belt-and-braces for the URL field.
        print(redact(str(info), token))
        return 0

    if args.delete:
        if not token:
            raise ValueError("no bot token: pass --token-file or set TELEGRAM_BOT_TOKEN")
        client = TelegramClient(token=token, base_url=args.api_base_url)
        try:
            gone = await client.delete_webhook(drop_pending_updates=args.drop_pending)
        finally:
            await client.aclose()
        print(f"deleteWebhook -> {gone}  ({url_shape(args.api_base_url, 'deleteWebhook')})")
        return 0 if gone else 1

    if args.print_only:
        # Nothing is sent and nothing is validated beyond what is needed to describe the call.
        print(f"POST {url_shape(args.api_base_url, 'setWebhook')}")
        print(f"  url          : {args.url or '<sin --url>'}")
        print("  secret_token : " + (PLACEHOLDER if secret else "<vacío>"))
        print("  drop_pending : False")
        return 0

    _validate(token=token, url=args.url, secret=secret, base_url=args.api_base_url)

    client = TelegramClient(token=token, base_url=args.api_base_url)
    try:
        ok = await client.set_webhook(
            url=args.url, secret_token=secret, drop_pending_updates=args.drop_pending
        )
    finally:
        await client.aclose()
    print(f"setWebhook -> {ok}  ({url_shape(args.api_base_url, 'setWebhook')})")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Registra o consulta el webhook de Telegram (nunca imprime el token)."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--print", dest="print_only", action="store_true", help="solo imprime")
    mode.add_argument("--set", dest="set_only", action="store_true", help="registra el webhook")
    mode.add_argument("--info", action="store_true", help="consulta getWebhookInfo")
    mode.add_argument(
        "--delete",
        action="store_true",
        help="da de baja el webhook (la reversión documentada en docs/despliegue.md §8)",
    )
    parser.add_argument("--token-file", default="", help="fichero con el token (modo 600)")
    parser.add_argument("--secret-file", default="", help="fichero con el secreto del webhook")
    parser.add_argument("--url", default="", help="URL pública del webhook")
    parser.add_argument("--api-base-url", default="https://api.telegram.org")
    parser.add_argument("--drop-pending", action="store_true")
    args = parser.parse_args(argv)

    token_for_redaction = read_secret(args.token_file, env_var="TELEGRAM_BOT_TOKEN")
    try:
        return asyncio.run(_run(args))
    except (AssistantError, ValueError) as exc:
        # `redact` runs on *every* failure message, which is the whole point: the failure path is
        # where a value that was never meant to be printed usually escapes.
        print(f"error: {redact(str(exc), token_for_redaction)}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - a script must exit non-zero, quietly and safely
        print(f"error: {redact(f'{type(exc).__name__}: {exc}', token_for_redaction)}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
