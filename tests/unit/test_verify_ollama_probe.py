"""The Ollama probe reads the configuration the application reads (PLAN.md §10 A10, spec §3).

Regression guard. The probe used ``os.environ`` directly, so a key stored in ``.env`` — the
documented local location, and the file ``Settings`` itself reads — was invisible to it, and
``--model`` silently fell back to the built-in default instead of the configured model. Both
defeated the purpose of the probe, whose only job is to say whether *the selected* model can
call tools. These tests pin the precedence: flags, then environment, then ``.env``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from verify_ollama import EXIT_PENDING, main, resolve_config  # noqa: E402


def _write_env(directory: Path, **values: str) -> None:
    """Write a minimal ``.env`` in ``directory`` (``load_settings`` reads it from the CWD)."""
    (directory / ".env").write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run in a directory whose only configuration is the ``.env`` a test writes.

    The database variables are cleared too: this file tests *model* resolution, so it must not
    be sensitive to whatever DSN the surrounding environment happens to carry.
    """
    monkeypatch.chdir(tmp_path)
    for name in (
        "OLLAMA_API_KEY",
        "OLLAMA_BASE_URL",
        "LLM_MODEL",
        "DATABASE_URL",
        "DATABASE_SYNC_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_the_key_in_the_env_file_is_used(isolated_env: Path) -> None:
    """This is the whole point of the fix: a key in ``.env`` must reach the probe."""
    _write_env(isolated_env, OLLAMA_API_KEY="key-from-dotenv")

    _, _, api_key, _ = resolve_config([])

    assert api_key == "key-from-dotenv"


def test_the_configured_model_is_probed_not_the_built_in_default(isolated_env: Path) -> None:
    """A10 validates the *configured* model; probing the default would validate nothing."""
    _write_env(
        isolated_env,
        OLLAMA_API_KEY="key-from-dotenv",
        LLM_MODEL="probe-me:7b",
        OLLAMA_BASE_URL="https://ollama.example.test",
    )

    base_url, model, _, _ = resolve_config([])

    assert model == "probe-me:7b"
    assert base_url == "https://ollama.example.test"


def test_a_command_line_flag_overrides_the_env_file(isolated_env: Path) -> None:
    _write_env(isolated_env, LLM_MODEL="from-dotenv")

    _, model, _, _ = resolve_config(["--model", "from-flag"])

    assert model == "from-flag"


def test_the_environment_overrides_the_env_file(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented shell method must keep working alongside ``.env``."""
    _write_env(isolated_env, OLLAMA_API_KEY="from-dotenv")
    monkeypatch.setenv("OLLAMA_API_KEY", "from-environment")

    _, _, api_key, _ = resolve_config([])

    assert api_key == "from-environment"


def test_main_reports_pending_when_no_key_is_configured_anywhere(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_env(isolated_env, LLM_MODEL="probe-me:7b")  # no key

    assert main([]) == EXIT_PENDING
    assert "PENDING" in capsys.readouterr().out


def test_main_does_not_report_pending_when_the_key_comes_from_the_env_file(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the fix this returned 2, because the key in ``.env`` was invisible.

    ``run_probe`` is replaced with an async stub that returns success, so the resolved key is
    exercised end to end without touching the network.
    """

    async def _stub_probe(**_kwargs: object) -> int:
        return 0

    _write_env(isolated_env, OLLAMA_API_KEY="key-from-dotenv")
    monkeypatch.setattr("verify_ollama.run_probe", _stub_probe)

    assert main([]) != EXIT_PENDING


def test_an_invalid_application_rule_does_not_block_the_probe(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A deployment-config fault must not make A10 unrunnable, nor report it as "no key".

    Phase 2 added a rule that a set ``DATABASE_URL`` requires ``DATABASE_SYNC_URL``. A probe
    answering a *model capability* question must not be blocked by that, and must not conflate
    it with a missing key. Before this fix ``resolve_config`` raised ``ConfigError`` here.
    """
    _write_env(isolated_env, OLLAMA_API_KEY="key-from-dotenv")
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:9/none")

    _, _, api_key, _ = resolve_config([])

    assert api_key == "key-from-dotenv"
    assert "not valid" in capsys.readouterr().out
