"""The Spanish user-facing string catalogue — the single source of every reply.

Spec §1: "Idioma del producto y documentación: español". Code, comments and docstrings are
English; **everything the user reads comes from here** (PLAN.md §9.1: "Every user-facing
string comes from ``app/bot/messages.py`` (AST walk over ``app/``)").

Keys are also used by the error taxonomy: :class:`~app.errors.AssistantError.message_key`
names an entry in this catalogue, so an error raised deep in the stack still renders through
one table instead of a string scattered at the raise site.

Phase 3 adds the ``approval.*`` keys and the seven zero-row diagnosis keys of §4.5; they are
absent here on purpose, because nothing in Phase 1 can produce them.
"""

from __future__ import annotations

from typing import Final

CATALOGUE: Final[dict[str, str]] = {
    # --- Commands ---------------------------------------------------------- #
    "start": (
        "Hola. Soy tu asistente personal.\n\n"
        "Puedes escribirme con normalidad. Ahora mismo puedo mantener una conversación; "
        "los recordatorios, la memoria y los documentos llegarán en una fase posterior."
    ),
    "help": (
        "Comandos disponibles:\n"
        "/start — presentación\n"
        "/help — esta ayuda\n"
        "/reset — borra el contexto de esta conversación\n\n"
        "Todavía no disponibles: /tasks, /memory y /forget."
    ),
    "reset.done": (
        "He borrado el contexto de esta conversación. "
        "Tus datos y preferencias guardados no se han tocado."
    ),
    # --- Pending capabilities (Phase 1 command surface) -------------------- #
    "error.not_available": (
        "El comando {command} todavía no está disponible. Llegará en una fase posterior."
    ),
    # --- Conversation-level outcomes --------------------------------------- #
    "busy.previous_turn_in_progress": (
        "Estoy terminando tu mensaje anterior; vuelve a enviarlo en unos segundos."
    ),
    "edited.notice": (
        "He recibido una edición de un mensaje ya procesado. Si quieres que tenga en cuenta "
        "el texto nuevo, envíamelo de nuevo."
    ),
    "callback.not_available": "Esa opción todavía no está disponible.",
    "unsupported.content": (
        "Por ahora solo entiendo mensajes de texto. Los documentos llegarán en una fase "
        "posterior."
    ),
    # --- Errors ------------------------------------------------------------ #
    "error.generic": (
        "Ha ocurrido un error inesperado. Vuelve a intentarlo en unos segundos."
    ),
    "error.timeout": (
        "He tardado demasiado en responder y he tenido que cancelar la operación. "
        "Inténtalo de nuevo, por favor."
    ),
    "error.model_unavailable": (
        "Ahora mismo no puedo contactar con el modelo. Inténtalo de nuevo en unos minutos."
    ),
    "error.rate_limited": (
        "Vas demasiado rápido; inténtalo en un minuto."
    ),
    "error.message_too_long": (
        "Tu mensaje es demasiado largo. Divídemelo en varios mensajes más cortos."
    ),
}


def render(key: str, **params: object) -> str:
    """Render a catalogue entry, raising ``KeyError`` for an unknown key.

    A missing key must be a loud failure: falling back to a generic string would hide a
    broken catalogue, and falling back to the key itself would show the user a raw
    identifier.
    """

    template = CATALOGUE[key]
    if not params:
        return template
    try:
        return template.format(**params)
    except (KeyError, IndexError) as exc:  # pragma: no cover - guarded by tests
        raise KeyError(f"message {key!r} has no placeholder {exc}") from exc


def has(key: str) -> bool:
    """Return True if the key exists in the catalogue."""

    return key in CATALOGUE


__all__ = ["CATALOGUE", "has", "render"]
