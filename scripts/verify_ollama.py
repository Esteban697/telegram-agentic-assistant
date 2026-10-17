"""Ollama model-compatibility probe (PLAN.md §10 Phase 1 deliverable 12).

Spec §3: "no asumir que un modelo soporta correctamente tool calling, JSON estructurado o
cualquier modalidad solo por su nombre. Implementar una prueba de compatibilidad que valide
llamadas a herramientas, argumentos, errores y respuestas."

Run it with a real key:

    uv run python scripts/verify_ollama.py

Exit codes:

* ``0`` — chat works, tool calling works, and a ``format``-constrained turn returns JSON.
* ``1`` — the probe ran and the model failed a capability. Tool calling being unsupported is
  the case PLAN.md §10 Phase 1 A10 calls out; the phase then closes with this recorded output
  and the fallback decision, never with a claim that tool calling works.
* ``2`` — no ``OLLAMA_API_KEY`` is configured (neither in ``.env`` nor in the environment), or a
  configured value is malformed enough that the settings cannot be read at all, so the probe
  could not run (pending external validation). An application *rule* the configuration violates
  — a database DSN pair, a production-only guard — is warned about and the probe still runs,
  because it answers a question about model capability, not about deployment.

The authoritative model name is printed from ``/api/tags`` rather than trusted from
configuration, because the cloud catalogue and the CLI disagree about the ``:cloud`` suffix
(PLAN.md Risks item 3).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from pydantic import ValidationError

# Allow `python scripts/verify_ollama.py` without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The operator-facing output contains Spanish text; a Windows console defaults to a legacy
# code page and would mojibake it. Reconfigure rather than printing escapes.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import Settings, load_settings  # noqa: E402
from app.errors import ConfigError, OllamaError  # noqa: E402
from app.llm.ollama_client import OllamaChatClient  # noqa: E402
from app.llm.protocols import ChatMessage, ToolSpec  # noqa: E402

EXIT_OK = 0
EXIT_CAPABILITY_FAILURE = 1
EXIT_PENDING = 2

WEATHER_TOOL = ToolSpec(
    name="get_current_weather",
    description="Get the current weather for a location.",
    parameters={
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "City, e.g. Madrid"},
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
        },
        "required": ["location", "unit"],
    },
)

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["answer", "confidence"],
}


def _line(title: str) -> None:
    print(f"\n=== {title} ===")


def _result(label: str, ok: bool, detail: str = "") -> None:
    marker = "PASS" if ok else "FAIL"
    print(f"[{marker}] {label}" + (f" — {detail}" if detail else ""))


async def run_probe(*, base_url: str, model: str, api_key: str, timeout: float) -> int:
    failures: list[str] = []

    async with OllamaChatClient(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout=timeout,
        max_retries=1,
    ) as client:
        # --- 1. Catalogue ------------------------------------------------- #
        _line("1. Catalogue (/api/tags)")
        try:
            names = await client.list_models()
        except OllamaError as exc:
            _result("list models", False, f"{exc.code}: {exc.detail}")
            print("\nRESULT: cannot reach the API; nothing can be concluded about the model.")
            return EXIT_CAPABILITY_FAILURE

        _result("list models", bool(names), f"{len(names)} model(s) advertised")
        configured_present = model in names
        print(f"Configured LLM_MODEL : {model}")
        print(f"Present in catalogue : {configured_present}")
        print("Advertised names (first 40):")
        for name in names[:40]:
            print(f"  - {name}")
        if not configured_present:
            close = [n for n in names if model.split(":")[0] in n]
            print(
                "\nNOTE: the configured name is not in the catalogue verbatim. "
                "Related entries: " + (", ".join(close[:8]) if close else "none")
            )
            print("      Record the catalogue name and set LLM_MODEL to it (Risks item 3).")
            failures.append("configured model name not found in /api/tags")

        # --- 2. Plain chat ------------------------------------------------- #
        _line("2. Plain chat turn")
        started = time.perf_counter()
        try:
            chat = await client.chat(
                [ChatMessage(role="user", content="Responde solo con la palabra: hola")]
            )
            elapsed = (time.perf_counter() - started) * 1000.0
            ok = bool(chat.content.strip())
            _result("chat returns content", ok, f"{elapsed:.0f} ms, {len(chat.content)} chars")
            print(f"model reported by the API: {chat.model}")
            if chat.prompt_tokens is not None:
                print(
                    f"tokens: prompt={chat.prompt_tokens} completion={chat.completion_tokens}"
                )
            if not ok:
                failures.append("chat returned empty content")
        except OllamaError as exc:
            _result("chat returns content", False, f"{exc.code}: {exc.detail}")
            failures.append(f"chat failed: {exc.code}")

        # --- 3. Tool calling ----------------------------------------------- #
        _line("3. Tool calling")
        tool_ok = False
        try:
            tooled = await client.chat(
                [ChatMessage(role="user", content="What is the weather in Madrid, in celsius?")],
                tools=[WEATHER_TOOL],
            )
            if tooled.tool_calls:
                first = tooled.tool_calls[0]
                args_ok = isinstance(first.arguments, dict) and "location" in first.arguments
                tool_ok = first.name == WEATHER_TOOL.name and args_ok
                _result(
                    "tool call returned with parseable arguments",
                    tool_ok,
                    f"tool={first.name!r} args={json.dumps(first.arguments, ensure_ascii=False)}",
                )
            else:
                _result("tool call returned", False, "the model answered without calling the tool")
                print(f"content was: {tooled.content[:200]!r}")
            if not tool_ok:
                failures.append("tool calling did not produce a parseable tool call")
        except OllamaError as exc:
            _result("tool call returned", False, f"{exc.code}: {exc.detail}")
            failures.append(f"tool calling failed: {exc.code}")

        # --- 4. Structured output ------------------------------------------ #
        _line("4. Structured output (format = JSON Schema)")
        try:
            structured = await client.chat(
                [
                    ChatMessage(
                        role="user",
                        content="¿Cuál es la capital de España? Devuélvelo con el esquema pedido.",
                    )
                ],
                response_format=DECISION_SCHEMA,
            )
            try:
                payload = json.loads(structured.content)
                ok = isinstance(payload, dict) and "answer" in payload and "confidence" in payload
                _result("structured output parses", ok, structured.content[:160])
                if not ok:
                    failures.append("structured output did not match the requested schema")
            except json.JSONDecodeError:
                _result("structured output parses", False, "content was not valid JSON")
                failures.append("structured output was not valid JSON")
        except OllamaError as exc:
            _result("structured output parses", False, f"{exc.code}: {exc.detail}")
            failures.append(f"structured output failed: {exc.code}")

    # --- 5. Verdict --------------------------------------------------------- #
    _line("Verdict")
    if not failures:
        print("RESULT: PASS — chat, tool calling and structured output all work.")
        print("A10: PASS. Record this output in the Phase 1 report.")
        return EXIT_OK

    for item in failures:
        print(f"  - {item}")
    if any("tool calling" in item for item in failures):
        print(
            "RESULT: FAIL — tool calling is not usable with this model.\n"
            "A10: FAIL. Apply the ordered fallback in PLAN.md §10 Phase 1:\n"
            "  1. try an alternate cloud model (e.g. gpt-oss:20b-cloud) and re-run this probe;\n"
            "  2. otherwise switch to the schema-driven dispatch path (format=<JSON Schema>);\n"
            "  3. otherwise re-scope Phase 3's conversational tool selection, stated plainly.\n"
            "Do NOT close Phase 1 by claiming tool calling works."
        )
    else:
        print("RESULT: FAIL — see the items above.")
    return EXIT_CAPABILITY_FAILURE


def resolve_config(argv: list[str] | None = None) -> tuple[str, str, str, float]:
    """Resolve ``(base_url, model, api_key, timeout)``.

    Precedence: command-line flags, then the environment, then ``.env`` — the same sources
    the application itself reads, so the probe validates the configuration the service will
    actually run with rather than a built-in default.

    **Changed in the Phase 1 review.** This used to read ``os.environ`` directly, which meant
    a key stored in ``.env`` — the documented local location, and the file ``Settings`` reads
    — was invisible here, and ``--model`` silently fell back to the default instead of the
    configured model. That defeated the point of the probe, whose whole job is to say whether
    *the selected* model can call tools (PLAN.md §10 A10, spec §3).
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=None, help="overrides the configured base URL")
    parser.add_argument("--model", default=None, help="overrides the configured model")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        # A probe is a diagnostic. A configuration fault with nothing to do with model
        # capability — a database DSN pair, a missing production-only value — must not make
        # A10 unrunnable, and must not be reported as "no key configured". Warn, then read the
        # values the probe actually needs. (A *malformed* value still fails below, because then
        # ``Settings`` cannot be constructed at all and there is genuinely nothing to read.)
        print(f"WARNING: the application configuration is not valid: {exc}")
        print("         Probing anyway — A10 asks whether the model can call tools.")
        settings = Settings()

    return (
        args.base_url or settings.ollama_base_url,
        args.model or settings.llm_model,
        settings.api_key.strip(),
        args.timeout,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        base_url, model, api_key, timeout = resolve_config(argv)
    except (ConfigError, ValidationError) as exc:
        # Only reachable when a configured value is malformed enough that ``Settings`` cannot be
        # constructed — there is then nothing to probe with, so say so rather than traceback.
        print(f"RESULT: cannot read configuration — {exc}")
        print("A10 is PENDING EXTERNAL VALIDATION, not passed.")
        return EXIT_PENDING

    if not api_key:
        print("RESULT: PENDING — no OLLAMA_API_KEY is configured, so this probe cannot run.")
        print("A10 is PENDING EXTERNAL VALIDATION, not passed.")
        print("Set it in .env at the repository root, or in the environment:")
        print(
            '  $env:OLLAMA_API_KEY="<tu-clave>"   # PowerShell\n'
            "  uv run python scripts/verify_ollama.py"
        )
        return EXIT_PENDING

    print(f"Endpoint: {base_url}")
    print(f"Model   : {model}")
    return asyncio.run(
        run_probe(base_url=base_url, model=model, api_key=api_key, timeout=timeout)
    )


if __name__ == "__main__":
    raise SystemExit(main())
