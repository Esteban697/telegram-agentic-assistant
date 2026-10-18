"""Phase 5 deployment artefacts: `scripts/deploy.sh`, `scripts/lib/redact.sh`, the image.

Credential-free, database-free and network-free by construction: the deploy script is exercised in
its **print** mode (which touches nothing) and in a shimmed execute mode where `gcloud` and `docker`
are shell scripts in a temporary directory that fail immediately. Nothing here can reach the real
project — that is the point: the deploy is the user's decision, not the suite's.

Three properties are enforced mechanically rather than by remembering them (PLAN.md §10 Phase 5's
hard constraint, §12's cost gate, and the *printing* rule):

1. every `gcloud` invocation carries `--project telegram-assistant-esteban`;
2. secret versions are pinned numerically and never `latest`;
3. **no secret value can be printed** — not on the success path, not on the failure path.
"""

from __future__ import annotations

import os
import pathlib
from functools import lru_cache
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DEPLOY = REPO / "scripts" / "deploy.sh"
REDACT = REPO / "scripts" / "lib" / "redact.sh"
DOCKERFILE = REPO / "Dockerfile"
DOCKERIGNORE = REPO / ".dockerignore"

PROJECT = "telegram-assistant-esteban"

# A `gcloud` invocation: the token starts a command — at the beginning of a logical line or right
# after `(` (an array assignment like `version_query=(gcloud …)`).
GCLOUD_CALL = re.compile(r"(?:^|\()\s*gcloud\b")

# The only `gcloud` commands that **cannot** carry `--project`, so the rule does not apply to them
# and they are exempt by name rather than by accident.
#   * `gcloud auth login` is interactive and creates nothing;
#   * `gcloud config set project …` is the command that *is* the project, and exists so that a
#     stray hand-typed command cannot land in the wrong one.
PROJECT_EXEMPT_PREFIXES = ("gcloud auth login", "gcloud config set project")


def posix(path: pathlib.Path | str) -> str:
    """A path in the form the Git-Bash `PATH` accepts on Windows, POSIX form elsewhere."""

    if os.name != "nt":
        return str(path)
    result = subprocess.run(
        ["cygpath", "-u", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    return result.stdout.strip() or str(path)


def logical_lines(source: str) -> list[str]:
    """Shell lines with `\\\\`-continuations joined, comments dropped.

    Joining first is what makes "every gcloud invocation carries `--project`" checkable at all: the
    flag is routinely on the fifth physical line of a command.
    """

    joined = source.replace("\\\n", " ")
    return [line for line in joined.splitlines() if not line.lstrip().startswith("#")]


def gcloud_invocations(source: str) -> list[str]:
    """Every `gcloud` command in the script, as one string per command.

    A command here is a physical line whose stripped text **starts** with `gcloud` (or with
    `(gcloud`, for the array assignment that captures a version number), plus every following
    `\\`-continued line. Joining the continuations is what makes "each invocation carries
    `--project`" checkable: the flag is routinely on the fourth physical line.

    A `gcloud` mentioned inside a `note "…"` string is not matched, because the line starts with
    `note` — the token has to begin the command.
    """

    invocations: list[str] = []
    current: list[str] | None = None
    for line in source.splitlines():
        stripped = line.strip()
        if current is not None:
            current.append(stripped)
            if not line.rstrip().endswith("\\"):
                invocations.append(" ".join(current))
                current = None
            continue
        if stripped.startswith("#") or not GCLOUD_CALL.search(stripped):
            continue
        if line.rstrip().endswith("\\"):
            current = [stripped]
        else:
            invocations.append(stripped)
    return invocations


BASH_PATH = shutil.which("bash") or "/bin/bash"


def write_executable(path: pathlib.Path, body: str) -> None:
    """Write a shell script *and make it executable the way MSYS understands*.

    `os.chmod(0o755)` only clears the read-only bit on Windows; Git Bash decides executability from
    the shebang **plus** the executable bit, so the chmod has to happen inside bash.

    The shebang is the **absolute** path of the running bash, not `#!/usr/bin/env bash`: the
    execute-mode `PATH` is the shim directory alone, so `env` is not there, and a shim whose
    shebang cannot be resolved does not fail cleanly — it hangs or falls back to the Windows shell.
    That detail is the reason the first version of this harness hung for ten minutes.
    """

    newline = chr(10)
    if body.startswith("#!"):
        body = body.split(newline, 1)[1]
    body = f"#!{posix(BASH_PATH)}{newline}{body}"
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["bash", "-c", f"printf %s {shlex.quote(body)} > {shlex.quote(posix(path))}"
                       f" && chmod +x {shlex.quote(posix(path))}"],
        check=True,
    )


def make_shims(directory: pathlib.Path, *, exit_code: int = 7, marker: pathlib.Path | None = None):
    """`gcloud`, `docker`, `python` and `curl` that echo their argv and fail — the loudest fake.

    The same set as `existing_shims`, so an execute-mode `PATH` of this directory alone is enough
    for the script to run: the tools it calls are all here, and the utilities it needs are
    delegated by `make_passthrough_shims`.
    """

    body = "#!/usr/bin/env bash\n"
    body += 'printf "SHIM %s argv: %s\\n" "$0" "$*"\n'
    if marker is not None:
        body += f'printf "%s\\n" "$*" >> "{posix(marker)}"\n'
    body += f"exit {exit_code}\n"
    for name in ("gcloud", "docker", "python", "python3", "curl"):
        write_executable(directory / name, body)


# A port nothing listens on. Pointing the proxy variables at it makes every outbound HTTP(S) call
# fail immediately instead of succeeding, which is what turns "an accidental call" into "a failed
# test" — the same class of accident has now happened three passes running by three different
# routes, so it is closed structurally rather than by being careful.
DEAD_PROXY = "http://127.0.0.1:9"
PROXY_ENV = {
    # Uppercase only. Windows environment blocks are case-insensitive: passing both spellings
    # makes one of them win arbitrarily (observed: the lowercase one, leaving `$HTTPS_PROXY`
    # empty in the shim). `httpx` and `urllib` both honour the uppercase form.
    "HTTP_PROXY": DEAD_PROXY,
    "HTTPS_PROXY": DEAD_PROXY,
    "ALL_PROXY": DEAD_PROXY,
    "NO_PROXY": "",
}

# POSIX utilities the script itself uses. They are **not** the tools under test, so the shims
# delegate to the real ones by absolute path captured at creation time — that is what lets the
# execute-mode PATH be the shim directory *alone*.
PASSTHROUGH_UTILITIES = (
    # `bash` itself: the harness runs the script with `exec bash …`, and with a shim-only PATH
    # there would otherwise be no shell to exec.
    "bash",
    "sh",
    "grep",
    "sed",
    "date",
    "seq",
    "cat",
    "tr",
    "head",
    "tail",
    "wc",
    "echo",
    # The script resolves its own location with `dirname`, and a shim dir without it makes
    # `HERE="$(cd "$(dirname ...)" && pwd)"` fail with "cd: null directory".
    "dirname",
    "basename",
    "env",
    "uname",
    "sort",
    "cut",
    "mkdir",
)


@lru_cache(maxsize=1)
def passthrough_dir() -> pathlib.Path:
    """One session-wide directory of delegating shims for the POSIX utilities the script uses.

    Created once: a per-test copy cost three seconds of `printf && chmod` every time, and the
    contents never vary. It deliberately contains **no** `gcloud`, `docker`, `python` or `curl`, so
    putting it on the execute-mode `PATH` alongside the shim directory keeps every one of those
    tools unreachable by name.
    """

    directory = pathlib.Path(tempfile.mkdtemp(prefix="taa-utilities-"))
    for name in PASSTHROUGH_UTILITIES:
        real = shutil.which(name)
        if real is None:  # pragma: no cover - a POSIX environment has these
            continue
        write_executable(directory / name, f'exec "{real}" "$@"')
    return directory


def isolated_home(secrets_dir: pathlib.Path | None = None) -> pathlib.Path:
    """A throwaway `$HOME`, so `~/.taa/secrets` cannot resolve to the developer's real files.

    Those files now exist on this machine with live values. A test that picked them up would send
    a real credential to a real API — the one version of this incident that would not be harmless.
    """

    if secrets_dir is not None:
        home = secrets_dir.parent / "home"
    else:
        home = pathlib.Path(tempfile.mkdtemp(prefix="taa-home-"))
    home.mkdir(parents=True, exist_ok=True)
    return home


def run_deploy(
    args: list[str] | None = None,
    *,
    shim_dir: pathlib.Path | None = None,
    secrets_dir: pathlib.Path | None = None,
    extra_env: dict[str, str] | None = None,
    script: pathlib.Path | None = None,
    home: pathlib.Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {
            "TAA_PROJECT",
            "TAA_ENV",
            "TAA_SECRETS_DIR",
            "TAA_REDACT",
            "TAA_IMAGE_TAG",
            "TAA_PYTHON",
            "HOME",
            "USERPROFILE",
            *PROXY_ENV,
        }
    }
    # A fixed image tag so the printed plan is comparable between runs.
    env["TAA_IMAGE_TAG"] = "test-tag"
    # Every run — printing or executing — gets a `$HOME` of its own (Windows reads USERPROFILE).
    resolved_home = home or isolated_home(secrets_dir)
    env["HOME"] = posix(resolved_home)
    env["USERPROFILE"] = str(resolved_home)
    if secrets_dir is not None:
        env["TAA_SECRETS_DIR"] = posix(secrets_dir)
    if extra_env:
        env.update(extra_env)

    target = script or DEPLOY
    command = f"exec bash {shlex.quote(posix(target))}"
    for argument in args or []:
        command += f" {shlex.quote(argument)}"
    if shim_dir is not None:
        # **The shim directory alone.** Not "first on PATH": with the real directories still
        # present, a `python` lookup that misses the shim silently finds a working interpreter. The
        # utilities the script needs are shimmed too (`make_passthrough_shims`), so there is nothing
        # left to fall back to. An *absolute* path can still bypass this — which is why the harness
        # also makes the network unroutable and why a test proves a bypass is detected.
        command = (
            f'PATH="{posix(shim_dir)}:{posix(passthrough_dir())}"; export PATH; ' + command
        )
        # The script resolves the project interpreter itself; a PATH shim cannot intercept an
        # absolute path, so the override is set explicitly and a bypass is a test failure.
        env.setdefault("TAA_PYTHON", posix(shim_dir / "python"))
        # And nothing can leave the machine even if it tries.
        env.update(PROXY_ENV)
    return subprocess.run(
        ["bash", "-c", command], cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180
    )


@pytest.fixture
def secrets_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """A stand-in for `~/.taa/secrets`, with a sentinel value the script must never print."""

    directory = tmp_path / "secrets"
    directory.mkdir()
    for name in (
        "telegram-bot-token",
        "telegram-webhook-secret",
        "ollama-api-key",
        "database-url",
        "checkpoint-url",
    ):
        (directory / name).write_text("SENTINEL-DO-NOT-PRINT-" + name + "\n", encoding="utf-8")
    return directory


# --------------------------------------------------------------------------- #
# 1. The --project rule (PLAN.md §10 Phase 5, the hard constraint)
# --------------------------------------------------------------------------- #
def test_every_gcloud_invocation_carries_the_project_explicitly() -> None:
    """`gcloud config get-value project` on this machine is a *different* project.

    An unqualified command would create billable resources in `presupuesto-esteban`, which is
    somebody else's work — so the rule is not "remember the flag", it is this test.
    """

    invocations = gcloud_invocations(DEPLOY.read_text(encoding="utf-8"))

    assert len(invocations) >= 15, (
        f"only {len(invocations)} gcloud invocations found: the scan is probably broken"
    )
    missing = [line for line in invocations if '--project "$PROJECT"' not in line]
    assert missing == [], f"these gcloud invocations have no --project: {missing}"


def test_the_project_is_the_right_one_and_is_exported_too() -> None:
    """A forgotten flag must not be able to retarget either: `CLOUDSDK_CORE_PROJECT` is set."""

    source = DEPLOY.read_text(encoding="utf-8")

    assert f'PROJECT="{PROJECT}"' in source, (
        "the project must be a literal, not something the environment can override"
    )
    assert 'export CLOUDSDK_CORE_PROJECT="$PROJECT"' in source


def test_the_project_cannot_be_retargeted_by_an_environment_variable() -> None:
    """**The effective** project, not the spelling of the flag.

    The first version of this guard checked that every invocation carried `--project "$PROJECT"`
    and that `PROJECT` defaulted correctly — which is satisfied just as well by
    `PROJECT="${TAA_PROJECT:-telegram-assistant-esteban}"`, i.e. by a script whose entire
    deployment a single exported variable could redirect. The override is gone: the project is a
    literal, so there is no variable left to set.
    """

    source = DEPLOY.read_text(encoding="utf-8")

    assert "TAA_PROJECT" not in source, (
        "an environment override for the project means one exported variable can retarget the "
        "whole deployment, which is the failure this rule exists to prevent"
    )
    assert 'PROJECT="telegram-assistant-esteban"' in source


def test_the_project_scan_can_fail() -> None:
    """Proof the scan above is not vacuous: an unqualified invocation is detected."""

    leaky = (
        "#!/usr/bin/env bash\n"
        f'PROJECT="{PROJECT}"\n'
        'gcloud run deploy "$SERVICE" --region=europe-west1\n'
        '  gcloud secrets list\n'
        '  gcloud sql instances create "$SQL" --project "$PROJECT"\n'
    )
    invocations = gcloud_invocations(leaky)

    assert len(invocations) == 3, invocations
    assert [line for line in invocations if '--project "$PROJECT"' not in line] == [
        'gcloud run deploy "$SERVICE" --region=europe-west1',
        "gcloud secrets list",
    ]
    # ...and the continuation case, which is where every real invocation actually lives.
    continued = (
        "run \"x\" \"gratis\" \\\n"
        "  gcloud services enable \\\n"
        "    run.googleapis.com \\\n"
        "    --project \"$PROJECT\"\n"
    )
    assert len(gcloud_invocations(continued)) == 1
    assert '--project "$PROJECT"' in gcloud_invocations(continued)[0]


# --------------------------------------------------------------------------- #
# 2. Secret versions are pinned, never `latest` (PLAN.md §11.2, Appendix A.8)
# --------------------------------------------------------------------------- #
def secret_reference_problems(reference: str) -> list[str]:
    """The predicate the guard uses: one problem string per malformed secret reference.

    Extracted rather than inlined so the can-fail companion drives *this* function instead of
    restating its logic — the previous companion asserted `":latest" in payload` on a synthetic
    line, which would have passed with the guard deleted.
    """

    problems: list[str] = []
    if not reference.strip():
        return ["empty secret reference"]
    name, separator, version = reference.partition("=")
    if not separator or not version:
        return [f"malformed secret reference: {reference!r}"]
    _, colon, pinned = version.rpartition(":")
    if not colon:
        return [f"{name} has no version at all: {version!r}"]
    if pinned == "latest":
        return [f"{name} is mounted at :latest"]
    if not (pinned.startswith(("$", "{")) or pinned.isdigit()):
        return [f"the version of {name} is not pinned numerically: {version!r}"]
    return problems


def mounted_secret_references() -> dict[str, str]:
    """What each component really mounts, read from the script's own `--dump-env`.

    The guard used to scrape `--set-secrets=` out of the script text, which stopped working the
    moment the payload became a command substitution — and scraping text is the weaker check
    anyway: the dump is what the deploy actually passes, resolved versions and all.
    """

    from scripts.validate_deploy_env import parse_dump

    components = parse_dump(deploy_env_dump())
    # `parse_dump` splits `NAME=reference`; the predicate wants the whole pair back.
    return {
        name: f"{name}={reference}"
        for name, reference in components["service"]["secret"].items()
    }


def test_no_secret_version_is_ever_latest() -> None:
    """A secret mounted as an environment variable is read **once at instance start**."""

    references = mounted_secret_references()

    assert references, "precondition: the script must mount secrets as secret references"
    problems = [
        problem for reference in references.values() for problem in secret_reference_problems(reference)
    ]
    assert problems == [], f"secret references are not version-pinned: {problems}"


def test_the_latest_scan_can_fail() -> None:
    """Proof the predicate above is real: it flags `:latest`, a missing version and an unpinned one."""

    assert secret_reference_problems("TELEGRAM_BOT_TOKEN=taa-prod-telegram-bot-token:latest") == [
        "TELEGRAM_BOT_TOKEN is mounted at :latest"
    ]
    assert secret_reference_problems("DATABASE_URL=taa-prod-database-url") == [
        "DATABASE_URL has no version at all: 'taa-prod-database-url'"
    ]
    assert secret_reference_problems("OLLAMA_API_KEY=taa-prod-ollama-api-key:3") == []
    assert secret_reference_problems("") == ["empty secret reference"]


# --------------------------------------------------------------------------- #
# Appendix B.5: the Cloud SQL socket DSN, confirmed rather than assumed
# --------------------------------------------------------------------------- #
def test_the_cloud_sql_socket_dsn_reaches_psycopg_as_a_host() -> None:
    """PLAN.md Appendix B.5 listed this as "to be confirmed at implementation".

    What can be confirmed on a development machine is the **parsing**: that this spelling survives
    SQLAlchemy's URL parser and reaches the driver as `host=/cloudsql/...` rather than as something
    silently ignored. What cannot — a real connection through the Auth Proxy socket — needs Cloud
    Run, and is still pending. Confirming the half that is checkable is what the appendix asked for.
    """

    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql

    dsn = (
        "postgresql+psycopg://taa:secret@/taa"
        "?host=/cloudsql/telegram-assistant-esteban:europe-west1:taa-prod-pg"
    )
    url = sa.engine.make_url(dsn)
    args, kwargs = postgresql.psycopg.dialect().create_connect_args(url)

    assert url.drivername == "postgresql+psycopg"
    assert url.database == "taa"
    assert url.host is None, "the socket path must not be mistaken for a TCP host"
    assert kwargs["host"] == "/cloudsql/telegram-assistant-esteban:europe-west1:taa-prod-pg"
    assert kwargs["dbname"] == "taa" and kwargs["user"] == "taa"
    assert args == []


def test_the_dsn_parsing_check_can_fail() -> None:
    """The companion: the same DSN without `?host=` loses the socket path entirely."""

    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql

    url = sa.engine.make_url("postgresql+psycopg://taa:secret@/taa")
    _, kwargs = postgresql.psycopg.dialect().create_connect_args(url)

    assert "host" not in kwargs, "precondition: without ?host= there is no socket path at all"


# --------------------------------------------------------------------------- #
# 3. Printing: no secret value, on any path (the coordinator's addition)
# --------------------------------------------------------------------------- #
def test_the_print_mode_runs_nothing_and_says_so(
    tmp_path: pathlib.Path, secrets_dir: pathlib.Path
) -> None:
    """Printing is the deliverable; executing is the user's decision."""

    shims = tmp_path / "shims"
    marker = tmp_path / "executed.txt"
    make_shims(shims, marker=marker)

    result = run_deploy(shim_dir=shims, secrets_dir=secrets_dir)

    assert result.returncode == 0, result.stderr
    assert "PLAN DE DESPLIEGUE" in result.stdout
    assert "--yes-i-have-approval" in result.stdout
    assert "FIN DEL PLAN" in result.stdout
    assert "-> ejecutando" not in result.stdout, "print mode executed something"
    assert not marker.exists(), "print mode invoked a tool"


def test_no_secret_value_is_printed_on_the_success_path(
    tmp_path: pathlib.Path, secrets_dir: pathlib.Path
) -> None:
    """A sentinel sits in every secret file; the printed plan must not contain it.

    The plan prints *paths* and *secret references*, and it never passes a value as an argument, so
    the sentinel has nowhere to appear. The companion below shows that this assertion is not
    vacuous: when a value **is** passed inline, the same harness catches it.
    """

    result = run_deploy(secrets_dir=secrets_dir)

    assert result.returncode == 0, result.stderr
    assert "--data-file=" in result.stdout, "precondition: the plan shows how values are added"
    assert "SENTINEL-DO-NOT-PRINT" not in result.stdout
    assert "SENTINEL-DO-NOT-PRINT" not in result.stderr


def test_no_secret_value_is_printed_on_the_failure_path(
    tmp_path: pathlib.Path, secrets_dir: pathlib.Path
) -> None:
    """The failure path is where a value usually escapes: it is echoed verbatim.

    The shim echoes its own argv and exits non-zero, so anything the script put on a command line
    appears in the output. The script's own error line prints the **redacted** command.
    """

    shims = tmp_path / "shims"
    make_shims(shims, exit_code=7)

    result = run_deploy(
        ["--yes-i-have-approval"],
        shim_dir=shims,
        secrets_dir=secrets_dir,
        extra_env={"TAA_ALLOWED_USER_IDS": "42"},
    )

    assert result.returncode == 7, result.stdout + result.stderr
    assert "SHIM" in result.stdout, (
        "precondition: the shim echoes argv, so this run really does surface command lines"
    )
    assert "SHIM" in result.stdout and "validate_deploy_env.py" in result.stdout, (
        "precondition: the harness must echo the command lines it intercepts"
    )
    assert "SENTINEL-DO-NOT-PRINT" not in result.stdout
    assert "SENTINEL-DO-NOT-PRINT" not in result.stderr
    assert "FALLÓ" in result.stderr
    assert "último comando" in result.stderr


def test_the_runbook_names_the_project_in_every_command_it_shows() -> None:
    """A human copying a command out of the runbook must not drift into the other project.

    PLAN.md §10 Phase 5 asks for this by name: "the equivalent prose rule in §11's runbook must also
    name the project, so a human following it by hand cannot drift either". Only the **fenced code
    blocks** are checked — a command is something you copy, and the surrounding prose is where the
    rule itself is explained.
    """

    runbook = (REPO / "docs" / "despliegue.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", runbook, re.DOTALL)
    assert len(blocks) >= 8, f"only {len(blocks)} code blocks found: the scan is probably broken"

    offenders: list[str] = []
    checked = 0
    for block in blocks:
        for invocation in gcloud_invocations(block):
            if invocation.startswith(PROJECT_EXEMPT_PREFIXES):
                continue
            checked += 1
            if f"--project {PROJECT}" not in invocation:
                offenders.append(invocation)

    assert checked >= 10, f"only {checked} gcloud commands found in the runbook"
    assert offenders == [], f"runbook commands without the project: {offenders}"


def test_the_runbook_teardown_can_actually_run() -> None:
    """Two ways the documented rollback did not work.

    * it prescribed `gcloud storage buckets delete` for a bucket full of document originals, and
      that command refuses a non-empty bucket (409 `BucketNotEmpty`) — so the one destructive step
      that matters most could not be performed as written;
    * it claimed to be complete while the IAM bindings had no teardown at all, leaving the runtime
      SA holding project-level roles after everything else was gone.

    The database is Supabase now, so the block no longer contains the Cloud SQL deletes it used to;
    the marker is the Artifact Registry deletion, which stays.
    """

    runbook = (REPO / "docs" / "despliegue.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```bash\n(.*?)```", runbook, re.DOTALL)
    teardown = [block for block in blocks if "artifacts repositories delete" in block]
    assert len(teardown) == 1, "the teardown block moved or was duplicated"
    text = teardown[0]

    assert "gcloud storage rm --recursive gs://taa-prod-documents" in text, (
        "the bucket holds originals, so `buckets delete` alone cannot empty it"
    )
    assert "gcloud sql" not in text, "Cloud SQL is no longer part of the deployment"
    for role in ("roles/aiplatform.user", "roles/secretmanager.secretAccessor", "roles/run.invoker"):
        assert "remove-iam-policy-binding" in text and role in text, (
            f"the teardown does not retire {role}"
        )
    assert text.count("remove-iam-policy-binding") >= 4, (
        "the project binding, the four secrets and the three jobs all need a teardown"
    )


def test_the_runbook_documents_the_supabase_reality() -> None:
    """The prerequisites a human must have applied, and the caveats that make the plan choice.

    A fresh Supabase project installs `vector` in `extensions` but does not put it on the app role's
    search path: the deploy then fails at the first embedding with an error that names no cause.
    The two statements are the fix, and they have to be in the runbook, not only in this review.
    """

    runbook = (REPO / "docs" / "despliegue.md").read_text(encoding="utf-8")

    for needle in (
        "grant usage on schema extensions to taa_app;",
        "alter database postgres set search_path = public, extensions;",
        "aws-0-eu-west-1.pooler.supabase.com",
        "sslmode=require",
        "taa_app",
        "14 conexiones",
        "plan Pro",
        "se pausa",
        "no tiene copias",
    ):
        assert needle.lower() in runbook.lower(), f"the runbook does not mention {needle!r}"

    assert "gcloud sql" not in runbook, "the runbook still prescribes Cloud SQL commands"


def test_the_teardown_check_can_fail() -> None:
    """**The companion**: the pre-fix block must be rejected by the same predicate."""

    broken = (
        "gcloud storage buckets delete gs://taa-prod-documents --project telegram-assistant-esteban"
    )
    assert "gcloud storage rm --recursive gs://taa-prod-documents" not in broken
    assert broken.count("remove-iam-policy-binding") < 5


def test_the_runbook_scan_can_fail() -> None:
    """Proof it is not vacuous: an unqualified command in a code block is caught."""

    block = "```bash\ngcloud run services list\n```"
    commands = [
        line.strip()
        for line in re.findall(r"```bash\n(.*?)```", block, re.DOTALL)[0].splitlines()
        if GCLOUD_CALL.search(line.strip())
    ]
    assert commands == ["gcloud run services list"]
    assert [line for line in commands if f"--project {PROJECT}" not in line] == commands


def sabotaged_script_copy(tmp_path: pathlib.Path) -> pathlib.Path:
    """A runnable copy of `scripts/` whose `deploy.sh` passes a secret **inline**.

    A copy is needed rather than an in-place mutation (a crashed test would leave the repository
    sabotaged), and a *complete* copy of `scripts/` because `deploy.sh` resolves its siblings from
    its own location. The shims below intercept `python`, so the copy never has to be a working
    Python project — what is under test is the argument list, not the validation.
    """

    target = tmp_path / "sabotaged" / "scripts"
    shutil.copytree(REPO / "scripts", target, ignore=shutil.ignore_patterns("__pycache__"))
    copy = target / "deploy.sh"
    source = copy.read_text(encoding="utf-8")
    anchored = '      --data-file="${SECRET_FILES[$secret]}" \\\n'
    assert anchored in source, "precondition: the anchor for the sabotage must exist"
    copy.write_text(
        source.replace(
            anchored,
            '      --data-file="${SECRET_FILES[$secret]}" '
            '"$(cat "${SECRET_FILES[$secret]}")" \\\n',
            1,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return copy


def test_no_secret_value_reaches_any_command_line(
    tmp_path: pathlib.Path, secrets_dir: pathlib.Path
) -> None:
    """The real script, run to completion with everything shimmed: no value in any argv.

    The shims record every command line in a file, so this is stronger than reading stdout: even a
    value that a command swallowed silently would show up there.
    """

    shims = tmp_path / "shims"
    marker = tmp_path / "executed.txt"
    existing_shims(shims, marker)

    result = run_deploy(
        ["--yes-i-have-approval", "--from-step", "4"],
        shim_dir=shims,
        secrets_dir=secrets_dir,
        extra_env={"TAA_ALLOWED_USER_IDS": "42"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    recorded = marker.read_text(encoding="utf-8")
    assert recorded.strip(), "precondition: the shims must have recorded the command lines"
    assert "SENTINEL-DO-NOT-PRINT" not in recorded, "a secret value reached a command line"
    assert "SENTINEL-DO-NOT-PRINT" not in result.stdout
    assert "SENTINEL-DO-NOT-PRINT" not in result.stderr


def test_the_sentinel_harness_can_fail(tmp_path: pathlib.Path, secrets_dir: pathlib.Path) -> None:
    """**The negative control.** The same harness, on a script that *does* pass a value inline.

    Without this, "the value never appears" could be true because the harness sees nothing — the
    shape of guard this project has been bitten by repeatedly. Here the sabotaged copy runs the
    full plan and the value must appear in the recorded command lines.
    """

    sabotaged = sabotaged_script_copy(tmp_path)
    shims = tmp_path / "shims"
    marker = tmp_path / "executed.txt"
    existing_shims(shims, marker)

    result = run_deploy(
        ["--yes-i-have-approval", "--from-step", "4"],
        shim_dir=shims,
        secrets_dir=secrets_dir,
        script=sabotaged,
        extra_env={"TAA_ALLOWED_USER_IDS": "42"},
    )

    recorded = marker.read_text(encoding="utf-8") if marker.exists() else ""
    assert "SENTINEL-DO-NOT-PRINT" in recorded, (
        "the harness must be able to see a leaked value, or the assertions above prove nothing. "
        f"exit={result.returncode}, recorded={recorded[:400]!r}"
    )


# --------------------------------------------------------------------------- #
# B1: the environment the script deploys must satisfy the application's own rules
# --------------------------------------------------------------------------- #
# The failure this prevents, verbatim from the review: the service and all three jobs failed
# `Settings.problems()` — no `TELEGRAM_ALLOWED_USER_IDS`, no `DATABASE_SYNC_URL`, and no
# `TELEGRAM_WEBHOOK_SECRET` in the jobs — so the revision would exit on `config_error` before
# serving (every Telegram request a 502) and each scheduled run would exit 1, once a minute, for
# ever. The check that catches it is the one used here: take the **exact** environment the script
# deploys, substitute placeholders for the secret values, and run the application's own validator.
EXPECTED_SECRETS = {
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "OLLAMA_API_KEY",
    # One secret serves both names: the DSN string is the same, and `postgresql+psycopg` works
    # synchronously for Alembic and asynchronously for the app.
    "DATABASE_URL",
    "DATABASE_SYNC_URL",
}


def deploy_env_dump(*, extra_env: dict[str, str] | None = None) -> str:
    result = run_deploy(["--dump-env"], extra_env=extra_env or {"TAA_ALLOWED_USER_IDS": "42,77"})
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_the_deployed_environment_passes_the_application_own_validation() -> None:
    from scripts.validate_deploy_env import parse_dump, validate_components

    dump = deploy_env_dump()
    components = parse_dump(dump)

    assert set(components) == {
        "service",
        "taa-prod-job-reminders",
        "taa-prod-job-documents",
        "taa-prod-job-resume",
    }, sorted(components)

    problems = validate_components(dump)
    assert problems == [], (
        "the application would refuse to start with the environment this script deploys: "
        f"{problems}"
    )


def test_every_component_gets_the_same_environment_and_no_dead_secret() -> None:
    """One authority for the environment, and every mounted secret actually read."""

    from scripts.validate_deploy_env import parse_dump

    components = parse_dump(deploy_env_dump())

    environments = {name: dict(spec["env"]) for name, spec in components.items()}
    service = environments["service"]
    for name, environment in environments.items():
        assert environment == service, f"{name} gets a different environment from the service"

    mounted = set(components["service"]["secret"])
    assert mounted == EXPECTED_SECRETS, (
        "the mounted secrets and the ones the application needs have drifted: "
        f"mounted={sorted(mounted)} expected={sorted(EXPECTED_SECRETS)}"
    )


def test_the_environment_check_can_fail() -> None:
    """**The companion.** Drop one required variable and the same check must go red.

    This is the check the reviewer used, and it is worthless if it cannot fail: the whole point is
    that a missing `TELEGRAM_ALLOWED_USER_IDS` — the exact defect found — is reported.
    """

    from scripts.validate_deploy_env import validate_components

    dump = deploy_env_dump()
    assert validate_components(dump) == []

    def without(variable: str) -> str:
        return "\n".join(
            line
            for line in dump.splitlines()
            if f" {variable}=" not in line and f" {variable} " not in line
        )

    for variable in ("TELEGRAM_ALLOWED_USER_IDS", "DATABASE_SYNC_URL", "TELEGRAM_WEBHOOK_SECRET"):
        problems = validate_components(without(variable))
        assert problems, f"removing {variable} was not detected"


# --------------------------------------------------------------------------- #
# B2: nothing billable is created before everything free is validated
# --------------------------------------------------------------------------- #
FREE_VALIDATIONS = (
    "services enable",
    "validate_deploy_env.py",
    "check_pool_budget.py --pooler-max-connections",
    "check_database.py --database-url-file",
    "docker build",
    "audit_image.py --image",
)


def ordering_problems(plan: str) -> list[str]:
    """Which of the free validations run *after* the first billable `create`.

    Shared with the companion below, so the companion drives this predicate rather than restating
    it.
    """

    marker = "repositories create"
    if marker not in plan:
        return ["the plan never creates the Artifact Registry repository"]
    first_create = plan.index(marker)
    return [
        f"{validation} happens after the first billable create"
        for validation in FREE_VALIDATIONS
        if validation not in plan or plan.index(validation) > first_create
    ]


def test_every_free_validation_runs_before_the_first_billable_create() -> None:
    """The rule: a failure before the first `create` costs nothing.

    The previous order ran the connection-budget gate — which needs a database the operator's
    machine does not have — *after* creating Cloud SQL, so a failure there left the dominant
    billable resource running with nothing using it.
    """

    plan = run_deploy(extra_env={"TAA_ALLOWED_USER_IDS": "42"}).stdout

    assert ordering_problems(plan) == [], ordering_problems(plan)
    assert "PRIMERA CREACIÓN FACTURABLE" in plan


def test_the_ordering_check_can_fail() -> None:
    """**The companion**: a plan that creates first and validates later must be flagged."""

    reversed_plan = "gcloud artifacts repositories create x\n" + "\n".join(FREE_VALIDATIONS)
    problems = ordering_problems(reversed_plan)
    assert len(problems) == len(FREE_VALIDATIONS), problems


def test_the_pre_create_pool_gate_is_pure_arithmetic_against_the_pooler() -> None:
    """The gate is a declared pooler ceiling and the deployed pool sizes — no database needed.

    The previous database was Cloud SQL, and the gate demanded a `/cloudsql/…` socket DSN from the
    operator's machine: it could never pass there, and it ran *after* the instance was created.
    Supabase's binding limit (14, session mode) is a property of the provider plan, so it is
    declared as an input and the arithmetic needs nothing but itself.
    """

    plan = run_deploy(extra_env={"TAA_ALLOWED_USER_IDS": "42"}).stdout
    pre_create = plan[: plan.index("repositories create")]

    assert "check_pool_budget.py --pooler-max-connections" in pre_create
    assert "--max-connections" not in pre_create, "the Cloud SQL ceiling is gone"
    assert "--database-sync-url-file" not in pre_create, (
        "the pre-create gate must not need a database"
    )

    # Behavioural: the deployed values fit, the application defaults do not.
    def pool_gate(*extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(REPO / "scripts" / "check_pool_budget.py"), *extra],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    deployed = pool_gate(
        "--pooler-max-connections", "14",
        "--db-pool-size", "2",
        "--checkpointer-pool-size", "1",
        "--concurrent-processes", "4",
        "--reserve", "2",
    )
    assert deployed.returncode == 0, deployed.stdout + deployed.stderr
    too_big = pool_gate(
        "--pooler-max-connections", "14",
        "--db-pool-size", "4",
        "--checkpointer-pool-size", "3",
        "--concurrent-processes", "4",
        "--reserve", "2",
    )
    assert too_big.returncode == 1, too_big.stdout + too_big.stderr
    assert "EMAXCONNSESSION" in too_big.stdout, too_big.stdout


# --------------------------------------------------------------------------- #
# B3: the webhook is registered, with the URL the service actually has
# --------------------------------------------------------------------------- #
def registers_a_real_url(plan: str) -> bool:
    """The predicate: the URL is resolved, the call is `--set`, and nothing is a placeholder."""

    # `status.url` rather than `value(status.url)`: the plan quotes arguments with `printf %q`,
    # which escapes the parentheses.
    return "status.url" in plan and "/telegram/webhook" in plan and "--print --token" not in plan


def test_the_plan_registers_the_webhook_with_the_resolved_url() -> None:
    plan = run_deploy(extra_env={"TAA_ALLOWED_USER_IDS": "42"}).stdout

    assert registers_a_real_url(plan), plan[-800:]


def test_the_webhook_registration_can_fail() -> None:
    """**The companion**: the placeholder form the first version printed must be rejected."""

    placeholder = (
        'python scripts/set_webhook.py --print --token-file=X --url="https://<URL-DEL-SERVICIO>'
        '/telegram/webhook"'
    )
    assert not registers_a_real_url(placeholder)
    assert registers_a_real_url(
        "gcloud run services describe X --format=value(status.url)\n"
        "python scripts/set_webhook.py --set --url=${SERVICE_URL}/telegram/webhook"
    )


# --------------------------------------------------------------------------- #
# B4 / B5: the roles that are granted, and the one that is not
# --------------------------------------------------------------------------- #
def granted_roles(source: str) -> set[str]:
    """Every `--role=` an `add-iam-policy-binding` command grants. Shared with the companion."""

    roles: set[str] = set()
    for invocation in gcloud_invocations(source):
        if "add-iam-policy-binding" not in invocation:
            continue
        match = re.search(r"--role=(?:'|\")?([A-Za-z0-9._/-]+)", invocation)
        if match:
            roles.add(match.group(1))
    return roles


def test_the_runtime_can_use_vertex_and_cannot_impersonate_itself() -> None:
    """B4 and B5, one assertion each, on the roles the script actually grants."""

    roles = granted_roles(DEPLOY.read_text(encoding="utf-8"))

    assert "roles/aiplatform.user" in roles, (
        "without it every embedding — ingestion and retrieval — gets 403 from Vertex and surfaces "
        "as EmbeddingUnavailable"
    )
    assert "roles/iam.serviceAccountUser" not in roles, (
        "nothing needs it: the jobs already run as the runtime SA, and `actAs` on that identity "
        "hands a second principal the ability to read all the secrets and the database"
    )
    assert "roles/editor" not in roles and "roles/owner" not in roles


def test_the_role_scan_can_fail() -> None:
    """**The companion**: the predicate sees a role when it is *granted* and not when the script
    merely mentions it — the script documents the role it deliberately does not grant."""

    assert granted_roles(
        "  gcloud projects add-iam-policy-binding P --role=roles/aiplatform.user\n"
    ) == {"roles/aiplatform.user"}
    assert granted_roles("# se concede roles/aiplatform.user\n") == set()
    assert granted_roles('note "no se concede iam.serviceAccountUser"') == set()


def test_the_plan_agrees_with_the_script_about_the_roles() -> None:
    """PLAN.md §11.4 was the source of the B4 error, so it is checked too."""

    plan = (REPO / "PLAN.md").read_text(encoding="utf-8")
    section = plan[plan.index("### 11.4 Minimum-privilege IAM") :]
    section = section[: section.index("### 11.5")]
    rows = [line for line in section.splitlines() if line.startswith("|")]

    assert any("aiplatform" in row and "Runtime SA" in row for row in rows), (
        "§11.4's table must grant the runtime the Vertex role, which is what B4 was missing"
    )
    # The prose explains *why* `serviceAccountUser` is not granted to the scheduler, so the check
    # is on the table rows: a row that makes the scheduler principal actAs the runtime SA is the
    # over-grant, and mentioning the role in an explanation is not.
    over_granted = [
        row for row in rows if "Scheduler SA" in row and "serviceAccountUser" in row
    ]
    assert over_granted == [], (
        f"§11.4 still grants the scheduler SA actAs on the runtime SA: {over_granted}"
    )


# --------------------------------------------------------------------------- #
# B6: the script is re-entrant — a second run creates nothing
# --------------------------------------------------------------------------- #
CREATE_VERBS = (
    "repositories create",
    "service-accounts create",
    "secrets create",
    "buckets create",
    "instances create",
    "databases create",
    "users create",
    "jobs create",
    "metrics create",
)


def existing_shims(directory: pathlib.Path, marker: pathlib.Path) -> None:
    """`gcloud`/`docker`/`python`/`curl` doubles where **every resource already exists**.

    They record every invocation and exit 0, so the script's existence checks all succeed and any
    `create` that runs shows up as a recorded line. `gcloud run services describe` prints a URL,
    because step 16 resolves one; `gcloud sql users list` prints the user, because that check greps.
    """

    gcloud_body = (
        "#!/usr/bin/env bash\n"
        'printf \'gcloud %s\\n\' "$*" >> MARKER\n'
        'case "$*" in\n'
        "  *\"value(status.url)\"*) printf 'https://taa-prod-api-abc.a.run.app\\n' ;;\n"
        "  *\"sql users list\"*) printf 'taa\\n' ;;\n"
        "  *\"versions list\"*) printf '7\\n' ;;\n"
        "esac\n"
        "exit 0\n"
    )
    simple_body = "#!/usr/bin/env bash\n" 'printf \'%s %s\\n\' "$0" "$*" >> MARKER\n' "exit 0\n"
    # The `python` shim records the environment it was given and a heartbeat. That is how
    # `test_execute_mode_runs_are_hermetic` sees, from *inside* the executed process, that the
    # network is dead, the PATH is ours and `$HOME` is not the developer's — and the heartbeat is
    # how a bypass is detected: if this shim is never called, a real interpreter was resolved.
    python_body = (
        "#!/usr/bin/env bash\n"
        'printf \'HEARTBEAT python\\n\' >> MARKER\n'
        'printf \'ENV PATH=%s\\n\' "$PATH" >> MARKER\n'
        'printf \'ENV HTTPS_PROXY=%s\\n\' "$HTTPS_PROXY" >> MARKER\n'
        'printf \'ENV HOME=%s\\n\' "$HOME" >> MARKER\n'
        'printf \'ENV TAA_SECRETS_DIR=%s\\n\' "$TAA_SECRETS_DIR" >> MARKER\n'
        + simple_body.split(chr(10), 1)[1]
    )
    for name, body in (
        ("gcloud", gcloud_body),
        ("docker", simple_body),
        ("python", python_body),
        ("python3", python_body),
        ("curl", simple_body),
    ):
        write_executable(directory / name, body.replace("MARKER", posix(marker)))


def executed_commands(marker: pathlib.Path) -> list[str]:
    return [line for line in marker.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_a_second_run_creates_nothing_because_everything_exists(
    tmp_path: pathlib.Path, secrets_dir: pathlib.Path
) -> None:
    """The state the project is actually in: the repo, both SAs, the bucket and the APIs exist.

    `--from-step 4` skips the free steps (including the image build, which a shim would not
    perform), and every existence check reports "already there", so the only thing that could
    appear in the marker is a `create` — which is exactly what fails on a real re-run with
    "already exists".
    """

    shims = tmp_path / "shims"
    marker = tmp_path / "executed.txt"
    existing_shims(shims, marker)

    result = run_deploy(
        ["--yes-i-have-approval", "--from-step", "4"],
        shim_dir=shims,
        secrets_dir=secrets_dir,
        extra_env={"TAA_ALLOWED_USER_IDS": "42"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    commands = executed_commands(marker)
    assert commands, "precondition: the shims must have been called at all"

    creates = [line for line in commands if any(verb in line for verb in CREATE_VERBS)]
    assert creates == [], f"a re-run tried to create resources that already exist: {creates}"

    # Non-vacuity: the existence checks really ran, so "no creates" is not "nothing happened".
    checks = [line for line in commands if "describe" in line or "users list" in line]
    assert len(checks) >= 8, f"only {len(checks)} existence checks ran: {checks}"


def test_from_step_skips_both_the_printing_and_the_execution(
    tmp_path: pathlib.Path, secrets_dir: pathlib.Path
) -> None:
    """Resuming after a failure must not re-run what already succeeded."""

    shims = tmp_path / "shims"
    marker = tmp_path / "executed.txt"
    existing_shims(shims, marker)

    result = run_deploy(
        ["--yes-i-have-approval", "--from-step", "8"],
        shim_dir=shims,
        secrets_dir=secrets_dir,
        extra_env={"TAA_ALLOWED_USER_IDS": "42"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[OMITIDO: --from-step 8]" in result.stdout
    assert "omitido (--from-step 8)" in result.stdout

    commands = executed_commands(marker)
    assert not any("services enable" in line for line in commands), commands
    assert not any("repositories" in line for line in commands), commands
    # Step 6's own commands specifically: `secrets add-iam-policy-binding` belongs to step 10 and
    # must still run, so the check cannot be "any line mentioning secrets".
    assert not any("secrets create" in line for line in commands), commands
    assert not any("secrets versions add" in line for line in commands), commands
    # `secrets versions list` DOES run: it is a free read whose result steps 11-13 need, and
    # skipping it is what used to pin version 1 (see the B-1 tests below).
    # ...and step 8 onwards did run.
    assert any("add-iam-policy-binding" in line for line in commands), commands


def test_the_from_step_flag_rejects_nonsense() -> None:
    result = run_deploy(["--from-step", "later"], extra_env={"TAA_ALLOWED_USER_IDS": "42"})
    assert result.returncode == 2
    assert "mero" in result.stderr


# --------------------------------------------------------------------------- #
# B-1: resuming must not pin version 1, and the free checks must still run
# --------------------------------------------------------------------------- #
def version_resolution_count(plan: str) -> int:
    return plan.count("secrets versions list")


def test_resuming_resolves_the_secret_version_instead_of_pinning_one(
    secrets_dir: pathlib.Path,
) -> None:
    """`--from-step 8` used to fall back to `SECRET_VERSION_DEFAULT=1`.

    Rotate the token (version 2 new, version 1 still enabled), resume a failed deploy after step 6,
    and the service and all three jobs would be mounted on the **old** version while the deploy
    reported success. The lookup is a free read that steps 11-13 need, so it always runs.
    """

    plan = run_deploy(
        ["--from-step", "8"], secrets_dir=secrets_dir, extra_env={"TAA_ALLOWED_USER_IDS": "42"}
    ).stdout

    assert version_resolution_count(plan) == 4, (
        "each of the four secrets must be resolved exactly once: "
        f"{version_resolution_count(plan)} lookups found"
    )
    assert "resolver la versión activa" in plan


def test_the_resumed_run_uses_the_resolved_version_in_the_argv(
    tmp_path: pathlib.Path, secrets_dir: pathlib.Path
) -> None:
    """The behavioural half: what the containers are actually mounted on, with `--from-step 8`."""

    shims = tmp_path / "shims"
    marker = tmp_path / "executed.txt"
    existing_shims(shims, marker)

    result = run_deploy(
        ["--yes-i-have-approval", "--from-step", "8"],
        shim_dir=shims,
        secrets_dir=secrets_dir,
        extra_env={"TAA_ALLOWED_USER_IDS": "42"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    recorded = marker.read_text(encoding="utf-8")

    # The shim answers `secrets versions list` with 7.
    assert "taa-prod-telegram-bot-token:7" in recorded, (
        "the resumed deploy pinned a version that is not the resolved one: "
        f"{[line for line in recorded.splitlines() if 'set-secrets' in line][:1]}"
    )
    assert ":1" not in " ".join(
        line for line in recorded.splitlines() if "--set-secrets" in line
    ).replace(":17", ""), "a secret was mounted at version 1 after a resume"


def test_resuming_still_runs_the_free_checks(
    tmp_path: pathlib.Path, secrets_dir: pathlib.Path
) -> None:
    """`--from-step 8` must not skip the validation and the arithmetic gate.

    Those two are the whole reason the plan promises that a failure before the first `create`
    costs nothing; skipping them in resume mode would break that promise on exactly the path the
    flag exists for — and `--from-step 11` with no allow-list would deploy a revision that 502s
    every request.
    """

    shims = tmp_path / "shims"
    marker = tmp_path / "executed.txt"
    existing_shims(shims, marker)

    result = run_deploy(
        ["--yes-i-have-approval", "--from-step", "8"],
        shim_dir=shims,
        secrets_dir=secrets_dir,
        extra_env={"TAA_ALLOWED_USER_IDS": "42"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[SIEMPRE: las comprobaciones gratis no se omiten]" in result.stdout
    recorded = marker.read_text(encoding="utf-8")
    assert "validate_deploy_env.py" in recorded, "the resumed run skipped the environment validation"
    assert "check_pool_budget.py --pooler-max-connections" in recorded, (
        "the resumed run skipped the connection arithmetic"
    )


def test_a_resumed_deploy_without_the_allow_list_refuses_to_continue(
    tmp_path: pathlib.Path, secrets_dir: pathlib.Path
) -> None:
    """The dangerous combination the reviewer named: `--from-step 11` and no allow-list."""

    shims = tmp_path / "shims"
    marker = tmp_path / "executed.txt"
    existing_shims(shims, marker)

    result = run_deploy(
        ["--yes-i-have-approval", "--from-step", "11"],
        shim_dir=shims,
        secrets_dir=secrets_dir,
    )

    assert result.returncode != 0, result.stdout
    assert "TAA_ALLOWED_USER_IDS" in result.stderr + result.stdout
    recorded = marker.read_text(encoding="utf-8") if marker.exists() else ""
    assert "run deploy" not in recorded, "a service was deployed with an empty allow-list"


def test_a_resumed_deploy_with_an_unresolvable_version_fails_loudly(
    tmp_path: pathlib.Path, secrets_dir: pathlib.Path
) -> None:
    """No silent fallback to 1: if the version cannot be read, the deploy stops."""

    shims = tmp_path / "shims"
    marker = tmp_path / "executed.txt"
    existing_shims(shims, marker)  # the gcloud shim returns nothing for `versions list`
    for name in ("gcloud",):
        body = (shims / name).read_text(encoding="utf-8").replace(
            "*\"versions list\"*) printf '7\\n' ;;", ""
        )
        (shims / name).write_text(body, encoding="utf-8")

    result = run_deploy(
        ["--yes-i-have-approval", "--from-step", "8"],
        shim_dir=shims,
        secrets_dir=secrets_dir,
        extra_env={"TAA_ALLOWED_USER_IDS": "42"},
    )

    assert result.returncode != 0, result.stdout
    assert "TAA_SECRET_VERSION" in result.stdout + result.stderr


# --------------------------------------------------------------------------- #
# B-2: the validator must not depend on which interpreter runs it
# --------------------------------------------------------------------------- #
def validator_interpreters() -> list[str]:
    """The venv interpreter, and whatever bare `python` is — the two the reviewer compared."""

    candidates = [str(REPO / ".venv" / "Scripts" / "python.exe"), str(REPO / ".venv" / "bin" / "python")]
    candidates = [path for path in candidates if pathlib.Path(path).exists()]
    bare = shutil.which("python")
    if bare:
        candidates.append(bare)
    return candidates


def run_validator(interpreter: str, dump_file: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [interpreter, str(REPO / "scripts" / "validate_deploy_env.py"), "--dump-file", str(dump_file)],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


def test_the_environment_validator_works_under_any_interpreter(tmp_path: pathlib.Path) -> None:
    """**Out of process, the way `deploy.sh` runs it — with two different interpreters.**

    The first version built `Settings(**UPPERCASE)` and relied on init kwargs matching field names
    case-insensitively. Under the `pydantic-settings` that this machine's bare `python` resolves to,
    every keyword was silently dropped and the validator reported on the application's *defaults*
    instead of the deployment — a different answer for the same input, depending on the
    interpreter. Lowercase field names plus the canary in `settings_for` make it deterministic.
    """

    good = tmp_path / "good.txt"
    good.write_text(deploy_env_dump(), encoding="utf-8")
    bad = tmp_path / "bad.txt"
    bad.write_text(
        "\n".join(
            line
            for line in deploy_env_dump().splitlines()
            if " TELEGRAM_ALLOWED_USER_IDS=" not in line
        ),
        encoding="utf-8",
    )

    interpreters = validator_interpreters()
    assert interpreters, "precondition: at least one interpreter must be available"

    for interpreter in interpreters:
        with_good = run_validator(interpreter, good)
        assert with_good.returncode == 0, f"{interpreter}: {with_good.stdout} {with_good.stderr}"
        assert "ok:" in with_good.stdout, f"{interpreter}: {with_good.stdout}"
        assert "did not apply" not in with_good.stdout

        with_bad = run_validator(interpreter, bad)
        assert with_bad.returncode == 1, f"{interpreter} accepted a bad environment"
        assert "TELEGRAM_ALLOWED_USER_IDS" in with_bad.stderr, (
            f"{interpreter} failed for the wrong reason: {with_bad.stderr[:300]}"
        )
        assert "did not apply the environment" not in with_bad.stderr, (
            f"{interpreter} could not apply the environment at all: {with_bad.stderr[:300]}"
        )


def test_the_canary_reports_an_environment_that_never_arrived() -> None:
    """The companion: if the settings object is not given the dump, the validator says *that*.

    Driven by handing `settings_for` a field the model does not have — the same effect as an
    interpreter that drops unknown keywords.
    """

    import scripts.validate_deploy_env as module
    from scripts.validate_deploy_env import EnvironmentNotApplied, settings_for

    dump = {"env": {"APP_ENV": "production", "STORE_BACKEND": "postgres"}, "secret": {}}

    class Deaf:
        """Accepts keywords and ignores them — what a dropped-kwargs interpreter behaves like."""

        def __init__(self, **_: str) -> None:
            self.app_env = "development"
            self.store_backend = "memory"

    original = module.Settings
    module.Settings = Deaf  # type: ignore[assignment,misc]
    try:
        with pytest.raises(EnvironmentNotApplied):
            settings_for(dump)
    finally:
        module.Settings = original  # type: ignore[assignment,misc]

    # ...and it stays quiet on a healthy interpreter.
    assert settings_for(dump).app_env == "production"


# --------------------------------------------------------------------------- #
# The project rule must also see commands printed inside notes
# --------------------------------------------------------------------------- #
NOTE_CALL = re.compile(r'^\s*(?:note|step|run)\s+"(.*)"\s*$|^\s*note\s+"(.*)"')


def gcloud_commands_inside_notes(source: str) -> list[str]:
    """Any `gcloud …` a `note`/`step` prints for the reader to copy.

    The project-rule guard only looked at lines where `gcloud` *starts the command*, so the one
    copy-pasteable command in the whole plan — the alerting policy, printed by a `note` — went
    unchecked and would have landed in the other project.
    """

    found: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped.startswith('note "'):
            continue
        content = stripped[len('note "') :].rstrip('"')
        # A *command block* is indented inside the note; prose that happens to mention `gcloud`
        # ("gcloud los lee del fichero") is not something anyone can paste.
        if content[:1].isspace() and content.lstrip().startswith("gcloud "):
            found.append(stripped)
    return found


def test_every_gcloud_command_printed_in_a_note_carries_the_project() -> None:
    notes = gcloud_commands_inside_notes(DEPLOY.read_text(encoding="utf-8"))

    printed = [line for line in notes if "gcloud " in line and "no ejecuta" not in line]
    assert printed, "precondition: the plan does print a copy-pasteable gcloud command"
    missing = [line for line in printed if "--project" not in line]
    assert missing == [], f"a printed command would land in the wrong project: {missing}"


EXECUTING_IN_TEXT = re.compile(r"(?<!\\)\$\(")


def executing_text_problems(source: str) -> list[str]:
    """Lines whose `note`/`step`/`run` **string** would execute something when printed.

    Two real incidents, same class: a backtick around `gcloud run deploy` inside a `note` ran it;
    a `$(cat <secret file>)` in a migrations note printed a secret into the plan. Both were text
    meant for the operator, and both executed. Escaped (`\$(…)`) is fine — that is how a command
    is *documented* without running it.
    """

    problems: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("note ", "step ", "run ")):
            continue
        if '"' not in stripped:
            continue
        text = stripped[stripped.index('"') + 1 : stripped.rindex('"')]
        if "`" in text:
            problems.append(f"backtick in a printed string: {stripped[:90]}")
        if EXECUTING_IN_TEXT.search(text):
            problems.append(f"unescaped $( in a printed string: {stripped[:90]}")
    return problems


def test_no_shell_string_can_execute_a_command_by_accident() -> None:
    """The guard that was lost in a test-file splice, and that the migrations note would have hit."""

    problems = executing_text_problems(DEPLOY.read_text(encoding="utf-8"))

    assert problems == [], problems


def test_the_execution_guard_can_fail() -> None:
    """**The companion**: both shapes are caught, and the escaped form is not."""

    assert executing_text_problems('note "  run `gcloud run deploy`"') != []
    assert executing_text_problems('note "  export X=$(cat secret)"') != []
    assert executing_text_problems('note "  export X=\$(cat secret)"') == []
    assert executing_text_problems('note "usa ${PROJECT} y ${SECRETS_DIR}"') == []


def test_the_note_command_scan_can_fail() -> None:
    assert gcloud_commands_inside_notes(
        'note "  gcloud alpha monitoring policies create --channels=X"\n'
    ) == ['note "  gcloud alpha monitoring policies create --channels=X"']
    assert gcloud_commands_inside_notes(
        'note "  gcloud alpha monitoring policies create --project telegram-assistant-esteban"\n'
    ), "the scan must see a compliant note too, or the guard above is vacuous"


# --------------------------------------------------------------------------- #
# The executed webhook URL, not the printed placeholder
# --------------------------------------------------------------------------- #
def test_the_executed_webhook_call_carries_the_resolved_url(
    tmp_path: pathlib.Path, secrets_dir: pathlib.Path
) -> None:
    """`registers_a_real_url` matched the print-mode placeholder, so it passed vacuously.

    This drives the execute path with shims: the recorded argv must contain `--set` and the URL the
    shim returned, and must not contain a placeholder.
    """

    shims = tmp_path / "shims"
    marker = tmp_path / "executed.txt"
    existing_shims(shims, marker)

    result = run_deploy(
        ["--yes-i-have-approval", "--from-step", "14"],
        shim_dir=shims,
        secrets_dir=secrets_dir,
        extra_env={"TAA_ALLOWED_USER_IDS": "42"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    recorded = marker.read_text(encoding="utf-8")
    registrations = [line for line in recorded.splitlines() if "set_webhook.py" in line]

    assert registrations, "precondition: the webhook registration must have been executed"
    assert any("--set" in line for line in registrations), registrations
    assert any(
        "https://taa-prod-api-abc.a.run.app/telegram/webhook" in line for line in registrations
    ), f"the executed URL is not the resolved one: {registrations}"
    assert not any("<URL-DEL-SERVICIO>" in line for line in registrations), registrations


# --------------------------------------------------------------------------- #
# Every component's secrets, and the pool arithmetic the deployment actually uses
# --------------------------------------------------------------------------- #
def test_every_component_mounts_every_required_secret() -> None:
    """The service having five secrets says nothing about the jobs — a job missing the webhook
    secret is a job that exits 1 on every scheduled run."""

    from scripts.validate_deploy_env import parse_dump

    components = parse_dump(deploy_env_dump())

    for name, spec in components.items():
        mounted = set(spec["secret"])
        assert mounted == EXPECTED_SECRETS, (
            f"{name} mounts {sorted(mounted)}, expected {sorted(EXPECTED_SECRETS)}"
        )


def test_the_pool_gate_uses_the_deployment_defaults_not_the_operators_env() -> None:
    """`check_pool_budget.py` read pool sizes from the local `Settings()`, i.e. from `.env`."""

    plan = run_deploy(extra_env={"TAA_ALLOWED_USER_IDS": "42"}).stdout
    # The deploy passes the pool sizes **explicitly** (they are what the containers get), so there
    # is no `.env` to read in the first place.
    assert "--db-pool-size" in plan and "--checkpointer-pool-size" in plan

    def pool_gate(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "check_pool_budget.py"),
                "--pooler-max-connections", pooler_max_connections(),
                "--db-pool-size", deployed_pool("DB_POOL_SIZE"),
                "--checkpointer-pool-size", deployed_pool("CHECKPOINTER_POOL_SIZE"),
                "--concurrent-processes", "4",
                "--reserve", "2",
            ],
            cwd=REPO,
            env={**os.environ, **extra_env, "PYTHONIOENCODING": "utf-8"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    assert pool_gate({}).returncode == 0
    # A hostile `.env` (or exported variable) must not change the answer: the pools are passed
    # explicitly, so the arithmetic describes the deployment and not this machine.
    hostile = pool_gate({"DB_POOL_SIZE": "99", "CHECKPOINTER_POOL_SIZE": "99"})
    assert hostile.returncode == 0, (
        "the gate picked up the operator's environment instead of the deployment's values: "
        f"{hostile.stdout} {hostile.stderr}"
    )


def pooler_max_connections() -> str:
    """Read from the script, so the test cannot drift from the deployed value."""

    match = re.search(
        r'POOLER_MAX_CONNECTIONS="\$\{TAA_POOLER_MAX_CONNECTIONS:-(\d+)\}"',
        DEPLOY.read_text(encoding="utf-8"),
    )
    assert match, "precondition: the script must declare POOLER_MAX_CONNECTIONS with a default"
    return match.group(1)


def deployed_pool(name: str) -> str:
    match = re.search(
        rf'{name}="\$\{{TAA_{name}:-(\d+)\}}"', DEPLOY.read_text(encoding="utf-8")
    )
    assert match, f"precondition: the script must declare {name} with a default"
    return match.group(1)


def test_the_test_reads_the_pooler_ceiling_from_the_script() -> None:
    """A hardcoded 14 would not notice the script changing; a hardcoded 100 once hid a 4x error."""

    assert pooler_max_connections() == "14"
    assert int(deployed_pool("DB_POOL_SIZE")) + int(deployed_pool("CHECKPOINTER_POOL_SIZE")) == 3, (
        "the deployed pools must total 3 per process for the 4-process arithmetic to fit 14"
    )


# --------------------------------------------------------------------------- #
# The Supabase decision (2026-10-18): Cloud SQL is gone, the pooler is the limit
# --------------------------------------------------------------------------- #
CLOUD_SQL_MARKERS = (
    "gcloud sql",
    "--add-cloudsql-instances",
    "/cloudsql/",
    "roles/cloudsql.client",
    "SHOW max_connections",
)


def cloud_sql_problems(source: str) -> list[str]:
    """Every Cloud SQL remnant in the plan. Shared with the companion, so it drives the predicate."""

    return [marker for marker in CLOUD_SQL_MARKERS if marker in source]


def test_cloud_sql_is_gone_from_the_deployment() -> None:
    """The database is Supabase: no instance, no socket, no Cloud SQL IAM, no server-side probe."""

    plan = run_deploy(extra_env={"TAA_ALLOWED_USER_IDS": "42"}).stdout

    assert cloud_sql_problems(plan) == [], cloud_sql_problems(plan)
    assert cloud_sql_problems(DEPLOY.read_text(encoding="utf-8")) == []


def test_the_cloud_sql_removal_check_can_fail() -> None:
    """**The companion**: the pre-decision plan is rejected by the same predicate."""

    old_plan = (
        "gcloud sql instances create taa-prod-pg --project telegram-assistant-esteban\n"
        "gcloud run deploy taa-prod-api --add-cloudsql-instances=telegram-assistant-esteban:"
        "europe-west1:taa-prod-pg\n"
        "  postgresql+psycopg://taa:pw@/taa?host=/cloudsql/telegram-assistant-esteban:europe-west1:taa-prod-pg\n"
        "gcloud projects add-iam-policy-binding P --role=roles/cloudsql.client\n"
        "python scripts/check_pool_budget.py --measure SHOW max_connections\n"
    )
    assert len(cloud_sql_problems(old_plan)) == len(CLOUD_SQL_MARKERS), cloud_sql_problems(old_plan)


def test_one_instance_because_the_pooler_allows_no_more() -> None:
    """`--max-instances=1` is a consequence of the 14-connection ceiling, not a cost choice."""

    plan = run_deploy(extra_env={"TAA_ALLOWED_USER_IDS": "42"}).stdout

    assert "--max-instances=1" in plan
    assert "--max-instances=3" not in plan, "three instances could not fit the pooler"


def test_the_deployed_pools_are_the_ones_the_gate_checks() -> None:
    """The gate's numbers must be the environment the containers actually get.

    The application defaults (DB_POOL_SIZE=4, CHECKPOINTER_POOL_SIZE=3) are 7 x 4 + 2 = 30 against
    a pooler of 14; if the deploy did not pass the sizes explicitly, the gate would describe a
    deployment that is not the one being created.
    """

    from scripts.validate_deploy_env import parse_dump

    components = parse_dump(deploy_env_dump())
    environment = dict(components["service"]["env"])
    plan = run_deploy(extra_env={"TAA_ALLOWED_USER_IDS": "42"}).stdout

    assert environment["DB_POOL_SIZE"] == deployed_pool("DB_POOL_SIZE")
    assert environment["CHECKPOINTER_POOL_SIZE"] == deployed_pool("CHECKPOINTER_POOL_SIZE")
    assert f"--db-pool-size {environment['DB_POOL_SIZE']}" in plan
    assert f"--checkpointer-pool-size {environment['CHECKPOINTER_POOL_SIZE']}" in plan


def test_the_connection_check_reads_the_dsn_from_a_file() -> None:
    """The Supabase DSN carries the password; it must not travel in argv."""

    plan = run_deploy(extra_env={"TAA_ALLOWED_USER_IDS": "42"}).stdout
    section = plan[: plan.index("repositories create")]

    assert "check_database.py --database-url-file" in section
    assert "--database-url " not in section, "the DSN must not be passed on the command line"


def test_the_database_checks_are_right() -> None:
    """`check_database.py` against a fake connection: every failure mode is a finding."""

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_database", REPO / "scripts" / "check_database.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # A `@dataclass` in a module that is not in `sys.modules` makes dataclasses raise
    # `AttributeError: 'NoneType' object has no attribute '__dict__'` — register it first.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    class Result:
        def __init__(self, scalar=None, row=None) -> None:
            self.scalar = scalar
            self.row = row

        def scalar_one_or_none(self):
            return self.scalar

        def one(self):
            return self.row

    class Fake:
        def __init__(self, responses: dict[str, object]) -> None:
            self.responses = responses

        def execute(self, statement):  # noqa: ANN001, ANN201 - the surface `checks` uses
            sql = str(statement)
            for needle, response in self.responses.items():
                if needle in sql:
                    if isinstance(response, Exception):
                        raise response
                    return response
            return Result()

    healthy = Fake(
        {
            "SELECT 1": Result(scalar=1),
            "extversion": Result(scalar="0.8.2"),
            "n.nspname": Result(scalar="extensions"),
            "'[1,2,3]'::vector": Result(scalar="[1,2,3]"),
            "current_setting": Result(scalar="off"),
            "pg_roles": Result(row=("taa_app", False, False, False)),
        }
    )
    assert [check.name for check in module.checks(healthy) if not check.ok] == []

    old_vector = Fake(
        {
            "SELECT 1": Result(scalar=1),
            "extversion": Result(scalar="0.7.4"),
            "n.nspname": Result(scalar="extensions"),
            "'[1,2,3]'::vector": Result(scalar="[1,2,3]"),
            "current_setting": Result(scalar=None),
            "pg_roles": Result(row=("taa_app", False, False, False)),
        }
    )
    failed = [check.name for check in module.checks(old_vector) if not check.ok]
    assert any("0.8" in name for name in failed), failed
    assert any("iterative" in name for name in failed), failed

    wrong_schema = Fake(
        {
            "SELECT 1": Result(scalar=1),
            "extversion": Result(scalar="0.8.2"),
            "n.nspname": Result(scalar="public"),
            "'[1,2,3]'::vector": Result(scalar="[1,2,3]"),
            "current_setting": Result(scalar="off"),
            "pg_roles": Result(row=("taa_app", False, False, False)),
        }
    )
    assert any(
        check.name == "esquema de vector" and not check.ok for check in module.checks(wrong_schema)
    )

    unresolved = Fake(
        {
            "SELECT 1": Result(scalar=1),
            "extversion": Result(scalar="0.8.2"),
            "n.nspname": Result(scalar="extensions"),
            "'[1,2,3]'::vector": RuntimeError("type vector does not exist"),
            "current_setting": Result(scalar="off"),
            "pg_roles": Result(row=("taa_app", False, False, False)),
        }
    )
    assert any(
        check.name.startswith("vector resuelve") and not check.ok
        for check in module.checks(unresolved)
    )

    superuser = Fake(
        {
            "SELECT 1": Result(scalar=1),
            "extversion": Result(scalar="0.8.2"),
            "n.nspname": Result(scalar="extensions"),
            "'[1,2,3]'::vector": Result(scalar="[1,2,3]"),
            "current_setting": Result(scalar="off"),
            "pg_roles": Result(row=("postgres", True, True, True)),
        }
    )
    assert any("sin privilegios" in check.name and not check.ok for check in module.checks(superuser))

    unreachable = Fake({"SELECT 1": RuntimeError("connection refused")})
    problems = module.checks(unreachable)
    assert len(problems) == 1 and not problems[0].ok, problems

    assert module.parse_version("0.8.2") == (0, 8, 2)
    assert module.version_at_least("0.8.0", (0, 8)) is True
    assert module.version_at_least("0.7.9", (0, 8)) is False


# --------------------------------------------------------------------------- #
# redact.sh: the whole PEM block, and many of them
# --------------------------------------------------------------------------- #
def test_redact_removes_a_hundred_private_key_blocks() -> None:
    """The old rule left the footer, and one shared iteration budget ran out part-way.

    Measured with 100 blocks: 36 bodies and 100 footers survived. Occurrences, not lines: the old
    assertion counted lines containing the placeholder, which cannot see a partial result.
    """

    block = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234567890abcdefghij\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    text = block * 100

    # The text goes in over stdin, so the shell script needs no quoting of its own — an earlier
    # version built it inside the script and the escaping swallowed the newlines, which made the
    # result look like a partial redaction when it was a broken test.
    script = (
        f". {posix(REDACT)}; text=\"$(cat)\"; redact_into \"$text\"; printf '%s' \"$REDACTED_TEXT\""
    )
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=REPO,
        input=text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("MIIEow") == 0, "a key body survived"
    assert result.stdout.count("END RSA PRIVATE KEY") == 0, "a key footer survived"
    assert result.stdout.count("<redacted>") == 100, (
        f"{result.stdout.count('<redacted>')} of 100 blocks were redacted"
    )


# --------------------------------------------------------------------------- #
# Structural hermeticity: an execute-mode test cannot reach the real world
# --------------------------------------------------------------------------- #
# The same accident happened three passes running by three different routes: a backtick in a note
# executed a real `gcloud run deploy`; a test ran execute mode without shims; `resolve_python`
# preferred an absolute `.venv/Scripts/python.exe` that a PATH shim cannot intercept. The vectors
# differ every time, so this is closed by construction: a dead proxy for every execute-mode run, a
# PATH of the shim directory alone (plus a utilities-only directory), an isolated `$HOME`, and a
# heartbeat that proves the interpreter really was the shim — with companions for each.

REAL_HOME = pathlib.Path(os.path.expanduser("~"))
REAL_SECRETS = REAL_HOME / ".taa" / "secrets"


def marker_environment(marker: pathlib.Path) -> dict[str, list[str]]:
    """The environment recorded by the `python` shim, from inside the executed process."""

    values: dict[str, list[str]] = {}
    for line in marker.read_text(encoding="utf-8").splitlines():
        if line == "HEARTBEAT python":
            values.setdefault("HEARTBEAT", []).append("python")
        elif line.startswith("ENV ") and "=" in line:
            key, _, value = line[4:].partition("=")
            values.setdefault(key, []).append(value)
    return values


def hermeticity_problems(marker: pathlib.Path, *, developer_home: pathlib.Path = REAL_HOME) -> list[str]:
    """Everything that shows an execute-mode run was not isolated. Empty means hermetic."""

    environment = marker_environment(marker)
    problems: list[str] = []
    if "HEARTBEAT" not in environment:
        problems.append("the python shim was never called: a real interpreter was resolved")
    if (environment.get("HTTPS_PROXY") or [None])[-1] != DEAD_PROXY:
        problems.append(f"HTTPS_PROXY is not the dead proxy: {environment.get('HTTPS_PROXY')}")
    path_value = (environment.get("PATH") or [""])[-1]
    if not path_value:
        problems.append("PATH was not recorded")
    for home in environment.get("HOME", []):
        # A test-scoped `$HOME` always lives under the pytest tree or a `taa-home-` temp dir. The
        # check is by shape rather than by comparing strings, because MSYS reports the same
        # directory as `/c/Users/...` and as `C:\Users\...`.
        normalised = home.replace("\\", "/")
        if "pytest" not in normalised and "taa-home-" not in normalised:
            problems.append(f"$HOME is not a test-scoped directory: {home}")
    for secrets in environment.get("TAA_SECRETS_DIR", []):
        if "taa/secrets" in secrets.replace("\\", "/") and "pytest" not in secrets:
            problems.append(f"TAA_SECRETS_DIR points at the developer's real secrets: {secrets}")
    return problems


def test_execute_mode_runs_are_hermetic(
    tmp_path: pathlib.Path, secrets_dir: pathlib.Path
) -> None:
    """Observed from *inside* the executed process, not from the harness's intent."""

    shims = tmp_path / "shims"
    marker = tmp_path / "executed.txt"
    existing_shims(shims, marker)

    result = run_deploy(
        ["--yes-i-have-approval", "--from-step", "8"],
        shim_dir=shims,
        secrets_dir=secrets_dir,
        extra_env={"TAA_ALLOWED_USER_IDS": "42"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    environment = marker_environment(marker)

    assert "HEARTBEAT" in environment, "the interpreter did not come from the shim"
    assert (environment.get("HTTPS_PROXY") or [""])[-1] == DEAD_PROXY
    path_value = (environment.get("PATH") or [""])[-1]
    entries = [entry for entry in path_value.split(":") if entry]
    # **Every** entry must be ours: the shim directory and the utilities-only directory. A path
    # with the real system directories on it is a path where a missed shim silently finds a working
    # tool — which is exactly how a real interpreter got through in the previous pass.
    allowed = {posix(shims), posix(passthrough_dir())}
    assert set(entries) == allowed, (
        f"the execute-mode PATH contains directories that are not ours: {entries}"
    )
    assert hermeticity_problems(marker) == [], hermeticity_problems(marker)

    # The developer's real secrets exist on this machine; the run must not have been able to see
    # them by `$HOME`.
    assert REAL_SECRETS != secrets_dir
    assert str(REAL_SECRETS).replace("\\", "/") not in (environment.get("TAA_SECRETS_DIR") or [""])[-1]


def test_the_hermeticity_check_can_fail(tmp_path: pathlib.Path) -> None:
    """**The companion**: a marker from a non-isolated run must be rejected by the same predicate."""

    leaky = tmp_path / "leaky.txt"
    leaky.write_text(
        "\n".join(
            [
                "ENV PATH=/usr/local/bin:/usr/bin",
                "ENV HTTPS_PROXY=http://proxy.internal:3128",
                f"ENV HOME={REAL_HOME}",
                f"ENV TAA_SECRETS_DIR={REAL_SECRETS}",
            ]
        ),
        encoding="utf-8",
    )

    problems = hermeticity_problems(leaky)

    assert len(problems) >= 4, problems
    assert any("real interpreter" in problem for problem in problems)


def test_the_dead_proxy_really_blocks_outbound_http(tmp_path: pathlib.Path) -> None:
    """The mechanism itself: with the harness's proxy env, a local HTTP call cannot complete.

    A listener on loopback that must **never** be reached, and the same request with and without the
    proxy variables — so the test proves the variables are honoured rather than assuming it, and
    proves it without sending one byte to the internet.
    """

    import http.server
    import socketserver
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"reached")

        def log_message(self, *args: object) -> None:
            return

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{port}/probe"

            def fetch(environment: dict[str, str]) -> int:
                return subprocess.run(
                    [sys.executable, "-c", "import urllib.request;urllib.request.urlopen('" + url + "',timeout=5)"],
                    env={**os.environ, **environment, "NO_PROXY": "", "no_proxy": ""},
                    capture_output=True,
                    timeout=30,
                ).returncode

            assert fetch({}) == 0, "precondition: without a proxy the listener is reachable"
            assert fetch(PROXY_ENV) != 0, (
                "the dead proxy did not block an outbound request: an accidental call would reach "
                "the real service"
            )
        finally:
            server.shutdown()

def test_a_real_interpreter_bypass_is_detected(
    tmp_path: pathlib.Path, secrets_dir: pathlib.Path
) -> None:
    """**The canary for point 3.** A copy of the script that resolves a real interpreter is caught.

    The pass-3 vector verbatim: `resolve_python` preferring the absolute `.venv/Scripts/python.exe`.
    A PATH of shims cannot intercept an absolute path, so the detection cannot be "the shim was
    first" — it is the shim's **heartbeat**. Here the sabotaged copy is run and the marker must show
    that the python shim was never called; the intact script, in the companion half, must show that
    it was.
    """

    target = tmp_path / "sabotaged" / "scripts"
    shutil.copytree(REPO / "scripts", target, ignore=shutil.ignore_patterns("__pycache__"))
    copy = target / "deploy.sh"
    source = copy.read_text(encoding="utf-8")
    real_python = REPO / ".venv" / "Scripts" / "python.exe"
    if not real_python.exists():
        real_python = REPO / ".venv" / "bin" / "python"
    assert real_python.exists(), "precondition: the project virtualenv must exist"
    anchored = '    "${TAA_PYTHON:-}" \\\n'
    assert anchored in source, "precondition: the interpreter preference list moved"
    copy.write_text(
        source.replace(
            anchored,
            f'    "{real_python}" \\\n' + anchored,
            1,
        ),
        encoding="utf-8",
        newline="\n",
    )

    shims = tmp_path / "shims"
    marker = tmp_path / "executed.txt"
    existing_shims(shims, marker)

    run_deploy(
        ["--yes-i-have-approval", "--from-step", "8"],
        shim_dir=shims,
        secrets_dir=secrets_dir,
        script=copy,
        extra_env={"TAA_ALLOWED_USER_IDS": "42"},
    )

    # The marker may not exist at all: the real interpreter failed before any shim was reached,
    # which is itself the detection (no heartbeat).
    recorded = marker.read_text(encoding="utf-8") if marker.exists() else ""
    assert "HEARTBEAT python" not in recorded, (
        "a real interpreter ran and the harness did not notice: the heartbeat is the detector"
    )

    # The companion direction: the real script does produce the heartbeat.
    clean_marker = tmp_path / "clean.txt"
    clean_shims = tmp_path / "clean-shims"
    existing_shims(clean_shims, clean_marker)
    run_deploy(
        ["--yes-i-have-approval", "--from-step", "8"],
        shim_dir=clean_shims,
        secrets_dir=secrets_dir,
        extra_env={"TAA_ALLOWED_USER_IDS": "42"},
    )
    assert "HEARTBEAT python" in clean_marker.read_text(encoding="utf-8")
    assert hermeticity_problems(clean_marker) == []


def test_the_developers_real_secrets_are_out_of_reach() -> None:
    """The one version of this incident that would not be harmless: a live credential to a live API.

    Every run gets a `$HOME` of its own, so `~/.taa/secrets` inside the executed process cannot be
    the developer's real directory.
    """

    harness_home = isolated_home()

    assert harness_home != REAL_HOME
    assert not str(harness_home).startswith(str(REAL_HOME / ".taa"))
    assert (REAL_SECRETS / "telegram-bot-token") != (harness_home / ".taa" / "secrets" / "telegram-bot-token")

    # And if it *were* the real path, the predicate used by the hermeticity test says so.
    marker = harness_home / "leaky.txt"
    marker.write_text(
        f"HEARTBEAT python\nENV TAA_SECRETS_DIR={REAL_SECRETS}\nENV HOME={REAL_HOME}\n"
        f"ENV HTTPS_PROXY={DEAD_PROXY}\nENV PATH={posix(harness_home)}\n",
        encoding="utf-8",
    )
    problems = hermeticity_problems(marker)
    assert any("real secrets" in problem for problem in problems), problems
