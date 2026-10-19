"""Validate the environment `deploy.sh` gives each component, with the application's own rules.

    bash scripts/deploy.sh --dump-env | python scripts/validate_deploy_env.py --stdin
    python scripts/validate_deploy_env.py                 # asks deploy.sh for the dump itself

**Why this exists.** An adversarial review of the first version of `deploy.sh` found that the
service and all three jobs would fail configuration validation and never start: the deployment set
neither `TELEGRAM_ALLOWED_USER_IDS` nor `DATABASE_SYNC_URL`, and the jobs did not mount
`TELEGRAM_WEBHOOK_SECRET`. The service revision would have exited on `config_error` before serving
(every Telegram request a 502) and every scheduled run would have exited 1, once a minute, for ever.
The artefacts and the application simply disagreed about what production needs.

The check that catches that is the one the reviewer used: take **the exact environment the script
deploys** — not a hand-copied approximation — substitute placeholder values for the secrets, and run
`Settings.problems()` on it. `--dump-env` is what makes "exact" mean exact: the same arrays that
build `--set-env-vars` and `--set-secrets` produce the dump, so the two cannot drift.

Exit codes: 0 = every component validates, 1 = at least one does not, 2 = could not read the dump.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "scripts" / "deploy.sh"

# Placeholder values for the mounted secrets. They must be *plausible* — the point is to exercise
# the application's own validators, so a placeholder that would itself be rejected (an invalid
# webhook secret, say) would produce a false alarm rather than a finding.
#
# They must also not be *shaped* like the real thing: this file ships inside the image, and the
# deploy's image audit scans it for credential shapes. A token-shaped placeholder is a true
# positive by shape and stopped the first deploy, so this one is deliberately a plain word —
# `TELEGRAM_BOT_TOKEN` is validated only for emptiness, never for shape.
PLACEHOLDER_VALUES: dict[str, str] = {
    "TELEGRAM_BOT_TOKEN": "placeholder-bot-token",
    "TELEGRAM_WEBHOOK_SECRET": "placeholder-webhook-secret",
    "OLLAMA_API_KEY": "placeholder-ollama-key",
    # The shape the deploy actually ships: Supabase's pooler over TCP with TLS, not a Cloud SQL
    # socket. Only the shape matters to validation, but it must be the deployed shape.
    "DATABASE_URL": (
        "postgresql+psycopg://taa_app.placeholder:placeholder"
        "@aws-0-eu-west-1.pooler.supabase.com:5432/postgres?sslmode=require"
    ),
    # The calendar is enabled in the deployed environment, so the JSON must parse and carry the
    # three required fields. Plain words, not credential-shaped values: the image audit scans
    # this file too.
    "GOOGLE_OAUTH_CREDENTIALS": json.dumps(
        {
            "client_id": "placeholder-client-id",
            "client_secret": "placeholder-client-secret",
            "refresh_token": "placeholder-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": [
                "https://www.googleapis.com/auth/calendar.events",
                "https://www.googleapis.com/auth/calendar.events.freebusy",
            ],
        }
    ),
}
PLACEHOLDER_VALUES["DATABASE_SYNC_URL"] = PLACEHOLDER_VALUES["DATABASE_URL"]


class EnvironmentNotApplied(Exception):
    """The interpreter built a `Settings` that never received the dump.

    Reported as a *finding*, never as a pass: see `settings_for`.
    """


def parse_dump(text: str) -> dict[str, dict[str, dict[str, str]]]:
    """`DUMP <component> env|secret KEY=VALUE` → ``{component: {"env": {...}, "secret": {...}}}``."""

    components: dict[str, dict[str, dict[str, str]]] = {}
    for line in text.splitlines():
        parts = line.split(" ", 3)
        if len(parts) != 4 or parts[0] != "DUMP":
            continue
        _, component, kind, entry = parts
        key, _, value = entry.partition("=")
        components.setdefault(component, {"env": {}, "secret": {}}).setdefault(kind, {})[key] = value
    return components


def settings_for(component_dump: dict[str, dict[str, str]]) -> Settings:
    """The environment as the container sees it: env vars plus the secrets' placeholder values.

    **Hermetic, and that is the whole point.** `Settings` reads the process environment and `.env`
    alongside the values passed to it, so on a machine where `TELEGRAM_ALLOWED_USER_IDS` happens to
    be exported, a dump *missing* that variable would still validate — the check would pass for a
    reason that has nothing to do with the deployment. Observed exactly that: the first version of
    this function reported no problems for a dump with the allow-list stripped out. So the process
    environment is cleared for the construction (restored afterwards) and `.env` is not read: the
    dump is the only source, and anything it does not carry falls back to the application's own
    default — which is what the container would get.
    """

    # **Field names, not environment-variable names.** `Settings(**kwargs)` matching is only
    # case-insensitive in some versions of pydantic-settings: under the one this machine's bare
    # `python` resolved to, every uppercase keyword was dropped (`extra="ignore"`), so the object
    # was built from defaults and reported on a system that has nothing to do with the deployment.
    environment = {
        key.lower(): value for key, value in component_dump.get("env", {}).items()
    }
    for name, reference in component_dump.get("secret", {}).items():
        # `NAME=secret-name:version` — the value is what the secret *holds*, which is unknown here.
        assert reference, f"{name} is mounted from an empty secret reference"
        version = reference.rpartition(":")[2]
        assert version.isdigit(), f"{name} is mounted at a non-numeric version: {reference!r}"
        environment[name.lower()] = PLACEHOLDER_VALUES.get(name, "placeholder")

    with mock.patch.dict(os.environ, {}, clear=True):
        settings = Settings(_env_file=None, **environment)

    # **The canary.** Case-matching aside, a `Settings` that did not receive the environment is the
    # one failure this validator cannot afford: it would report `ok` for a deployment that cannot
    # start, or a plausible-looking problem for the wrong reason. Compare the two fields the
    # deployment is defined by, and refuse to report at all when they did not arrive.
    for key in ("app_env", "store_backend"):
        if key in environment and getattr(settings, key) != environment[key]:
            raise EnvironmentNotApplied(
                f"Settings did not receive {key}={environment[key]!r} "
                f"(got {getattr(settings, key)!r})"
            )
    return settings


def validate_components(dump: str) -> list[str]:
    """Return one problem line per component that would refuse to start. Empty means all good."""

    problems: list[str] = []
    components = parse_dump(dump)
    if not components:
        return ["the dump contained no components: deploy.sh --dump-env produced nothing usable"]

    for name, component_dump in sorted(components.items()):
        try:
            settings = settings_for(component_dump)
        except EnvironmentNotApplied as exc:
            problems.append(
                f"{name}: the validator did not apply the environment ({exc}). That is a "
                "validator/interpreter problem, not a deployment one: fix the interpreter before "
                "trusting this run (deploy.sh resolves .venv/Scripts/python.exe first for exactly "
                "this reason)"
            )
            continue
        except Exception as exc:  # noqa: BLE001 - a bad value is itself a finding
            problems.append(f"{name}: could not be constructed: {type(exc).__name__}: {exc}")
            continue
        for problem in settings.problems():
            problems.append(f"{name}: {problem}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Valida el entorno que deploy.sh da a cada componente."
    )
    parser.add_argument("--stdin", action="store_true", help="lee el volcado de stdin")
    parser.add_argument("--dump-file", default="", help="lee el volcado de un fichero")
    args = parser.parse_args(argv)

    if args.dump_file:
        dump = pathlib.Path(args.dump_file).read_text(encoding="utf-8")
    elif args.stdin:
        dump = sys.stdin.read()
    else:
        result = subprocess.run(
            ["bash", str(DEPLOY), "--dump-env"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            print(f"error: no se pudo obtener el volcado: {result.stderr.strip()[:200]}", file=sys.stderr)
            return 2
        dump = result.stdout

    components = parse_dump(dump)
    problems = validate_components(dump)

    print(f"intérprete: {sys.executable}")
    for name in sorted(components):
        env_count = len(components[name].get("env", {}))
        secret_count = len(components[name].get("secret", {}))
        print(f"{name}: {env_count} variables y {secret_count} secretos montados")

    if problems:
        print(f"\nFALLAN {len({line.split(':')[0] for line in problems})} componente(s):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print(
            "\nLa aplicación no arrancaría con este entorno. Corrige COMMON_ENV / common_secrets "
            "en scripts/deploy.sh (los dos sitios que producen este volcado).",
            file=sys.stderr,
        )
        return 1

    print("\nok: los cuatro componentes pasarían la validación de arranque de la aplicación")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
