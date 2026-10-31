"""The Spanish user-facing string catalogue — the single source of every reply.

Spec §1: "Idioma del producto y documentación: español". Code, comments and docstrings are
English; **everything the user reads comes from here**.

Keys are also used by the error taxonomy: :class:`~app.errors.AssistantError.message_key` names
an entry in this catalogue, so an error raised deep in the stack still renders through one table
instead of a string scattered at the raise site.

Three groups deserve a note:

* ``approval.*`` — the seven zero-row diagnosis keys of §4.5 (one per cause, never one asserted
  cause) plus the outcomes;
* ``state.*`` — the raw enum must never reach a user;
* ``error.*`` — one key per typed error, checked exhaustively by a unit test.
"""

from __future__ import annotations

from typing import Final

CATALOGUE: Final[dict[str, str]] = {
    # --- Commands ---------------------------------------------------------- #
    "start": (
        "Hola. Soy tu asistente personal.\n\n"
        "Puedes escribirme con normalidad. También puedo guardar tus preferencias "
        "(/memory), gestionar recordatorios (/tasks) y leer los documentos que me envíes "
        "(PDF o texto) para responderte con la página de la que saco cada dato."
    ),
    "help": (
        "Comandos disponibles:\n"
        "/start — presentación\n"
        "/help — esta ayuda\n"
        "/tasks — tus recordatorios\n"
        "/memory — tus preferencias guardadas\n"
        "/forget — borra una preferencia (o todas, con confirmación)\n"
        "/reset — borra el contexto de esta conversación\n\n"
        "Cuando vaya a hacer algo con efecto externo (por ejemplo una reserva), te pediré "
        "aprobación antes de ejecutarlo."
    ),
    "reset.done": (
        "He borrado el contexto de esta conversación. "
        "Tus datos y preferencias guardados no se han tocado."
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
        "No he podido leer eso. Envíame un mensaje de texto, una imagen (JPEG, PNG o WebP) o un "
        "documento en PDF o texto plano y te respondo sobre su contenido."
    ),
    "images.unavailable": (
        "No puedo analizar imágenes ahora mismo. Inténtalo de nuevo en un momento."
    ),
    "images.unsupported": "Solo puedo leer imágenes JPEG, PNG o WebP.",
    "images.too_large": "La imagen supera el límite de {max_mb} MB.",
    "images.unreadable": "No he podido leer la imagen; prueba con otra o pregúntame en texto.",
    # --- Tasks / reminders -------------------------------------------------- #
    "tasks.empty": "No tienes recordatorios pendientes.",
    "tasks.header": "Tus recordatorios:",
    "tasks.line": "· {title}\n  {local} ({timezone}) · {state}",
    "tasks.completed": "Hecho: «{title}».",
    "tasks.cancelled": "Cancelado: «{title}».",
    "tasks.not_found": "No encuentro ese recordatorio.",
    "reminder.due": "Recordatorio: {title}\nVencía: {when}",
    # --- Memory ------------------------------------------------------------ #
    "memory.empty": "Todavía no has guardado ninguna preferencia.",
    "memory.header": "Preferencias guardadas:",
    "memory.line": "· {key}: {value}",
    "memory.saved": "Guardado: {key} = {value}",
    "forget.done": "He borrado la preferencia «{key}».",
    "forget.not_found": "No tengo ninguna preferencia con la clave «{key}».",
    "forget.needs_key": (
        "Dime qué quieres borrar: /forget <clave>. Para borrar todas tus preferencias, "
        "/forget all (te pediré confirmación)."
    ),
    "forget.confirm_all": (
        "Esto borrará tus {count} preferencias guardadas y no se puede deshacer. "
        "Si estás seguro, envía /forget all confirm."
    ),
    "forget.all_done": "He borrado {count} preferencias.",
    # --- Approvals --------------------------------------------------------- #
    "approval.prompt": (
        "Necesito tu aprobación antes de ejecutar esta acción:\n\n"
        "{preview}\n\n"
        "Caduca en {minutes} minutos. Solo se ejecutará si pulsas Aprobar."
    ),
    "approval.approved": "Aprobada. Ejecutando…",
    "approval.rejected": "Rechazada. No se ha ejecutado nada.",
    "approval.executing": "Ejecutando la acción aprobada…",
    "approval.succeeded": "Hecho: {detail}",
    "approval.failed": "No he podido completarla: {detail}",
    "approval.unknown": (
        "No sé si la acción llegó a completarse (el proveedor no respondió a tiempo). "
        "Lo dejo marcado como no confirmado y no lo reintentaré sin que me lo pidas."
    ),
    "approval.capability_unavailable": (
        "La acción está preparada y aprobada, pero no hay ningún proveedor integrado que pueda "
        "ejecutarla. No se ha realizado nada."
    ),
    # The seven zero-row causes of §4.5 — one key each, so the reply names the actual cause.
    "approval.unknown_action": "Esa acción ya no existe o el botón es antiguo.",
    "approval.expired": "Esa aprobación ha caducado; vuelve a pedirme la acción.",
    "approval.already_rejected": "Esa acción ya estaba rechazada.",
    "approval.in_progress": "Ya se está ejecutando o acaba de ejecutarse.",
    "approval.already_resolved": "Esa acción ya se resolvió antes.",
    "approval.arguments_changed": (
        "Los argumentos han cambiado desde que se te mostró la aprobación; no la ejecuto."
    ),
    "approval.wrong_identity": "Esa acción no está disponible.",
    # --- State labels: the raw enum never reaches the user ------------------ #
    "state.pending": "pendiente",
    "state.completed": "completado",
    "state.cancelled": "cancelado",
    "state.failed": "fallido",
    # --- Errors ------------------------------------------------------------ #
    "error.generic": "Ha ocurrido un error inesperado. Vuelve a intentarlo en unos segundos.",
    "error.timeout": (
        "He tardado demasiado en responder y he tenido que cancelar la operación. "
        "Inténtalo de nuevo, por favor."
    ),
    "error.model_unavailable": (
        "Ahora mismo no puedo contactar con el modelo. Inténtalo de nuevo en unos minutos."
    ),
    "error.rate_limited": "Vas demasiado rápido; inténtalo en un minuto.",
    "error.message_too_long": (
        "Tu mensaje es demasiado largo. Divídemelo en varios mensajes más cortos."
    ),
    "error.invalid_due_time": (
        "Esa hora no existe o es ambigua en la zona horaria indicada (cambio de hora). "
        "Dime otra hora, por favor."
    ),
    "error.reminder_in_past": (
        "Esa fecha ya ha pasado; dime una fecha y una hora futuras para el recordatorio."
    ),
    "error.task_not_found": "No encuentro ese recordatorio.",
    "error.storage_unavailable": (
        "Ahora mismo no puedo acceder a tus datos. Inténtalo de nuevo en unos minutos."
    ),
    "documents.indexed": (
        "He indexado «{name}»: {chunks} fragmentos de {pages} páginas. Ya puedes preguntarme "
        "sobre él."
    ),
    "documents.already_indexed": "Ese documento ya estaba indexado.",
    "documents.queued": (
        "El documento queda en cola: todavía no está indexado, así que aún no puedo buscarlo. "
        "Lo procesaré en segundo plano; vuelve a preguntarme dentro de un momento."
    ),
    "documents.unavailable": (
        "No puedo procesar documentos en esta configuración: falta el proveedor de embeddings."
    ),
    "error.unsupported_document": (
        "No puedo leer ese archivo. Envíame un PDF o un texto plano."
    ),
    "error.empty_text_layer": (
        "Ese documento no contiene texto extraíble (parece un escaneo sin capa de texto)."
    ),
    "error.document_too_large": (
        "Ese documento supera el límite de tamaño o de páginas que puedo procesar."
    ),
    "error.document_not_found": "No encuentro ese documento entre los tuyos.",
    "error.deletion_incomplete": (
        "No he podido borrarlo del todo; lo dejo marcado para reintentarlo y te aviso."
    ),
    "error.reindex_required": (
        "Los documentos están indexados con otro modelo de embeddings; hay que reindexarlos "
        "antes de poder buscarlos."
    ),
    "error.tool_args_changed": (
        "La llamada a esa herramienta no coincide con la que ya quedó registrada; no la repito."
    ),
    "error.tool_unknown": (
        "No sé si esa herramienta llegó a ejecutarse. Dímelo y lo reviso antes de reintentar."
    ),
    "error.tool_not_allowed": "Esa acción no está permitida en esta versión.",
    "error.invalid_arguments": (
        "La llamada a esa herramienta no tiene los argumentos correctos; la reviso y lo "
        "intento de nuevo."
    ),
    "error.capability_unavailable": (
        "Todavía no hay ningún proveedor configurado para esa consulta."
    ),
    "error.calendar_unavailable": (
        "No puedo contactar con Google Calendar ahora mismo. Inténtalo en un momento."
    ),
    "error.google_auth": (
        "No puedo autenticarme con Google; hay que volver a autorizar el acceso al calendario."
    ),
    "error.calendar_bad_request": (
        "Google Calendar ha rechazado la petición; revisa los datos del evento."
    ),
    "error.calendar_conflict": (
        "Ese evento ya existe en el calendario; consúltalo antes de volver a crearlo."
    ),
    "error.calendar_event_not_found": "No encuentro ese evento en el calendario.",
    "error.calendar_event_changed": (
        "El evento ha cambiado desde que lo leí; vuelve a consultarlo antes de modificarlo."
    ),
    "error.calendar_limit": (
        "La petición supera el límite de eventos, días o invitados; reduce el rango."
    ),
    "error.reservation_not_supported": (
        "No hay ninguna integración de reservas, así que no puedo completarla."
    ),
    "error.not_available": (
        "El comando {command} todavía no está disponible. Llegará en una fase posterior."
    ),
}


def render(message_key: str, **params: object) -> str:
    """Render a catalogue entry, raising ``KeyError`` for an unknown key.

    The first parameter is named ``message_key`` rather than ``key`` on purpose: several entries
    interpolate a ``{key}`` placeholder (a memory key), and a parameter called ``key`` would
    collide with it.

    A missing key must be a loud failure: falling back to a generic string would hide a broken
    catalogue, and falling back to the key itself would show the user a raw identifier.
    """

    template = CATALOGUE[message_key]
    if not params:
        return template
    try:
        return template.format(**params)
    except (KeyError, IndexError) as exc:  # pragma: no cover - guarded by tests
        raise KeyError(f"message {message_key!r} has no placeholder {exc}") from exc


def has(message_key: str) -> bool:
    """Return True if the key exists in the catalogue."""

    return message_key in CATALOGUE


def state_label(state: str) -> str:
    """Spanish label for a task state.

    The raw enum (`pending`, `completed`, …) is an implementation detail; a user-facing list must
    not leak it.
    """

    return CATALOGUE.get(f"state.{state}", state)


__all__ = ["CATALOGUE", "has", "render", "state_label"]
