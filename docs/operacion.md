# Operación e incidentes — Telegram personal assistant

Este documento cubre los modos de fallo que exige la spec §14, cada uno con **síntoma** (qué ve el
usuario y qué queda en los logs), **respuesta** (qué se hace) y **regla** (lo que no se debe hacer).
El despliegue, las migraciones y la reversión están en `docs/despliegue.md`; el modelo de seguridad
y de aprobación, en `docs/seguridad.md`.

Los logs son **JSON, una línea por evento**, con `request_id`, `update_id`, `conversation_id`,
`thread_id`, nombre de herramienta, duración, `status` y `error_code` cuando proceden. Nunca
contienen tokens, claves, documentos, embeddings ni el contenido de los mensajes (salvo con
`LOG_CONTENT_DEBUG=true`, que es opt-in y no se usa en producción).

---

## 1. Ollama: timeout

- **Síntoma.** El usuario recibe el mensaje de `error.timeout` ("He tardado demasiado en responder y
  he tenido que cancelar la operación…"). En los logs: `model call failed` o `turn exceeded
  REQUEST_BUDGET_SECONDS`, con `error_code: ollama_timeout` o `request_budget_exceeded`.
- **Qué ocurre por dentro.** El cliente tiene `LLM_TIMEOUT_SECONDS` (18 s) y `LLM_MAX_RETRIES` (1);
  el peor caso (18 × 2 + reintento + margen = 38,25 s) cabe en `REQUEST_BUDGET_SECONDS=45`, que a su
  vez queda por debajo del plazo de 60 s de Telegram. Si se agota el **presupuesto del turno**, el
  grafo se cancela y el turno se entrega a la cola de reanudación (`work_items`), que el job
  `resume_work` retoma y contesta.
- **Respuesta.** Comprobar la latencia del proveedor en los logs (`duration_ms` de `model turn
  complete`). Si es sistemática, subir `LLM_TIMEOUT_SECONDS` **solo** si el peor caso sigue cabiendo
  en el presupuesto (la validación de arranque lo comprueba y lo explica). No reintentar a mano el
  mensaje: si el turno quedó en la cola, el job lo reanudará sin duplicar la respuesta.
- **Regla.** No aumentar `REQUEST_BUDGET_SECONDS` por encima de ~50 s: Telegram corta a los 60 s y
  el webhook debe contestar antes.

## 2. Ollama: límite de tasa o proveedor no disponible

- **Síntoma.** `error.model_unavailable` ("Ahora mismo no puedo contactar con el modelo…"). Logs:
  `error_code: ollama_rate_limited` u `ollama_unavailable`, con `status` del proveedor.
- **Qué ocurre.** Los reintentos son **acotados** y solo sobre llamadas idempotentes; un 429 no se
  reintenta en bucle. El turno falla con un mensaje claro en lugar de colgarse.
- **Respuesta.** Mirar el `status` y el `retry_after` si lo hay; esperar a que el proveedor se
  recupere. Si persiste, cambiar `LLM_MODEL` por configuración (no hay ningún nombre de modelo
  cableado en el código) y volver a desplegar.
- **Regla.** No poner un bucle de reintentos nuevo en el handler: el presupuesto es la garantía de
  que el webhook contesta.

## 2.1 Búsqueda web: timeout, límite o clave inválida

- **Síntoma.** El asistente dice que no ha podido buscar. Logs: `web search timed out`,
  `web search rate limited`, `web search authentication failed` o `web search returned an error
  status`, con `error_code`, `status`, `duration_ms` y `query_chars` (nunca la consulta ni el
  resultado). En el turno, el modelo ve `search_web: error ollama_timeout` (o el código que
  corresponda).
- **Qué ocurre.** `POST https://ollama.com/api/web_search` con la misma `OLLAMA_API_KEY` del chat,
  timeout de 10 s y **sin reintentos ocultos**; los resultados se recortan antes de entrar en el
  contexto y se tratan como datos no confiables. Si el proveedor falla, el modelo está instruido
  para decirlo y no sustituirlo por conocimiento general.
- **Respuesta.** Comprobar el estado de Ollama Cloud y la cuota de la cuenta. Si el código es
  `ollama_unavailable` con `status: 401/403`, rotar `OLLAMA_API_KEY` en Secret Manager y actualizar
  el servicio (`gcloud run services update --update-secrets`); si es `ollama_rate_limited`, esperar.
  Si se quiere desactivar la búsqueda sin desplegar código, `WEB_SEARCH_PROVIDER=none` deja la
  herramienta respondiendo `CapabilityUnavailable`.
- **Regla.** No añadir un bucle de reintentos dentro del proveedor: el presupuesto del turno es la
  garantía de que el webhook contesta. No registrar la consulta ni los resultados: son contenido
  del usuario y datos de terceros.

## 2.2 Google Calendar: timeout, cuota, autenticación o evento cambiado

- **Síntoma.** El asistente dice que no puede consultar o modificar el calendario. Logs:
  `google access token refreshed` (normal), o errores con `error_code: google_timeout`,
  `google_unavailable`, `google_rate_limited`, `google_auth`, `calendar_event_not_found` o
  `calendar_event_changed`. En una escritura aprobada, el estado puede quedar **`unknown`** si el
  timeout es ambiguo.
- **Qué ocurre.** El proveedor (`app/integrations/google_calendar.py`) refresca el access token y
  llama a la Calendar API. Los errores **definitivos** (400, 401/403, 404, cuota, evento cambiado)
  devuelven `ToolResult.failure`; los **ambiguos** (timeout, transporte, 5xx) se propagan y el
  handler marca la aprobación como `unknown`, sin reintento automático.
- **Respuesta.**
  - `google_auth`: el refresh token se revocó o caducó. Vuelve a ejecutar
    `scripts/google_auth.py` y despliega.
  - `google_rate_limited`: espera; Google Calendar tiene cuotas por usuario y por proyecto.
  - `google_timeout`/`google_unavailable`: comprueba el estado de Google; no reintentes la escritura
    desde la base de datos.
  - `calendar_event_changed`: el evento cambió entre la lectura y la aprobación; vuelve a leerlo y
    prepara una acción nueva.
  - **Argumentos inválidos del modelo:** el grafo devuelve `invalid_arguments` como error de la
    herramienta y el turno continúa; el modelo puede corregir la llamada. No es un "error
    inesperado" y no debe reiniciarse el servicio por ello.
- **Regla.** No aumentar reintentos ocultos ni convertir `unknown` en `failed`. No registrar
  títulos, descripciones, correos de invitados ni tokens.

## 3. Telegram: límite de tasa (429)

- **Síntoma.** Un mensaje de respuesta no se entrega. Logs: `telegram rate limited` con
  `error_code: telegram_rate_limited` y `retry_after`, y el `update` marcado como `failed` en el
  ledger de updates.
- **Qué ocurre.** El cliente lanza `TelegramRateLimited` **sin esperar ni reintentar** (decisión
  documentada en el plan: esperar el `retry_after` dentro del presupuesto es trabajo de una fase
  posterior). La respuesta de ese turno se pierde; el `update` no se reintenta solo.
- **Respuesta.** Es un incidente de **entrega**, no de estado: el turno se ejecutó y está en el
  historial. Si el usuario no recibió la respuesta, puede volver a preguntar. La mitigación
  estructural es `RATE_LIMIT_PER_MINUTE` (20/min por usuario y por instancia) para no acercarse al
  límite de Telegram.
- **Regla.** No reintentar el mismo `update_id`: la idempotencia de entrada está pensada para los
  reintentos de Telegram, no para reenviar una respuesta perdida.

## 4. Telegram: updates duplicados

- **Síntoma.** Ninguno visible; es lo normal cuando Telegram reintenta. Logs: `duplicate update
  ignored`, con `status: ignored`.
- **Qué ocurre.** El **primer** efecto de la petición es insertar el `update_id` en
  `inbox_updates`; si ya estaba, la petición se contesta 200 y no se hace nada más. Es lo que hace
  seguro el escalado a cero y los reintentos.
- **Respuesta.** Ninguna. Si se ve un duplicado *procesado*, es un incidente: revisar que el
  `DedupeStore` es el de PostgreSQL (`STORE_BACKEND=postgres`), porque el de memoria no sobrevive a
  un reinicio ni comparte estado entre instancias.
- **Regla.** En producción, `STORE_BACKEND=memory` **impide arrancar**; no se relaja esa validación.

## 5. Aprobaciones: callback duplicado, caducado, ajeno o con argumentos cambiados

- **Síntoma.** El usuario ve uno de los siete mensajes del catálogo, uno por causa real:
  `approval.unknown_action`, `approval.expired`, `approval.already_rejected`,
  `approval.in_progress`, `approval.already_resolved`, `approval.arguments_changed`,
  `approval.wrong_identity`.
- **Qué ocurre.** La decisión es un **CAS atómico** (`UPDATE … WHERE state='pending' AND
  args_revision=… AND args_hash=…`); dos pulsaciones simultáneas ejecutan **una**. La aprobación
  solo autoriza la revisión exacta que se mostró: si los argumentos cambian, la aprobación no vale
  (un disparador impide además modificar los argumentos de una fila ya aprobada).
- **Respuesta.** Si el mensaje es `expired`, el usuario vuelve a pedir la acción (TTL de 30 min). Si
  es `arguments_changed`, es una protección, no un fallo: no se ejecuta y se puede volver a preparar.
  `wrong_identity` significa que el botón no es de esa persona/chat: no se toca la fila.
- **Regla.** Nunca ejecutar una acción de nivel 2 "a mano" en la base de datos para evitar el CAS.

## 6. PostgreSQL: caída o indisponibilidad (Supabase)

- **Síntoma.** `/readyz` responde **503** con `reason: database` (o `embedding_model` si lo que falla
  es la fila del modelo). Los turnos fallan con `error.storage_unavailable`; el job de recordatorios
  falla en su `JOB_REPORT` con `ok: false`.
- **Qué ocurre.** El servicio **no** debe recibir tráfico: Cloud Run y Telegram deben ver el 503.
  El estado ya escrito está en PostgreSQL, así que no se pierde nada de lo confirmado.
- **Respuesta.** Comprobar el panel de Supabase (proyecto pausado por inactividad en el plan
  gratuito, o mantenimiento). Si está pausado, reactivarlo; el plan gratuito **se pausa tras 7 días
  sin actividad** y **no tiene copias**. Si el fallo es de credenciales, rotar el secreto y
  `gcloud run services update --update-secrets`. Si es de conexiones (`EMAXCONNSESSION`), ver §7.
- **Regla.** No "arreglar" el 503 haciendo que `/readyz` no compruebe la base de datos: es la señal
  que impide entregar updates que no se pueden procesar.

## 7. Límite del pooler (14 conexiones)

- **Síntoma.** Errores inmediatos de conexión con `EMAXCONNSESSION` en los logs del servicio o de un
  job; no es una espera, es un fallo instantáneo.
- **Qué ocurre.** Supavisor en modo sesión admite **14** conexiones de cliente. El despliegue usa
  `DB_POOL_SIZE=2` y `CHECKPOINTER_POOL_SIZE=1` con 4 procesos (servicio + 3 jobs) y 2 de reserva:
  `(2+1)×4+2 = 14`. `max-instances=1` es obligatorio por esto.
- **Respuesta.** Comprobar que nadie ha subido los pools ni las instancias: `scripts/check_pool_budget.py`
  recalcula y **se niega** si no cabe. Si hay migraciones abiertas, cerrarlas antes de lanzar carga.
  Si hace falta más capacidad, se sube el plan de Supabase (más conexiones) — no se sobresuscribe.
- **Regla.** No aumentar `--max-instances` sin rehacer el aritmético y el plan de Supabase.

## 8. Ingesta de documentos: fallo de extracción o de embeddings

- **Síntoma.** El usuario recibe `error.unsupported_document`, `error.empty_text_layer` (escaneo sin
  capa de texto) o `error.document_too_large`; o `documents.queued` si no cupo en el presupuesto en
  línea. En la base, el documento queda `failed` con `ingest_error`, **nunca** `ready`.
- **Qué ocurre.** `ready` solo se escribe después de que todos los chunks estén confirmados; un fallo
  deja `failed` y el **job `process_documents`** lo reintenta (misma regla de reclamo que la web).
  Si el original no está en el bucket, el job lo marca `failed` con `original_missing`: una
  re-subida de los mismos bytes lo repara (resuelve a la misma fila y vuelve a guardar el original).
- **Respuesta.** Mirar `ingest_error`; si es un formato, pedir otro documento. Si es `original_missing`,
  volver a subirlo. El job avanza por `attempts` para que un documento imposible no bloquee a los
  nuevos.
- **Regla.** No marcar `ready` a mano: la recuperación compara el `pipeline_version` del documento con
  el de sus chunks, y un `ready` sin chunks es exactamente la inconsistencia que el esquema evita.

## 9. Timeout ambiguo de un proveedor externo: `unknown`, nunca "failed"

- **Síntoma.** El usuario ve `approval.unknown` ("No sé si la acción llegó a completarse…"). En la
  base, la acción queda en estado **`unknown`**; en el ledger de herramientas, `unknown` con
  `error.tool_unknown`.
- **Qué ocurre.** Un timeout **no** dice si el proveedor completó la operación. El sistema registra
  `unknown` y **no reintenta solo**; el default para un código de error no reconocido es *ambiguo*,
  no *fallido* (una acción de nivel 2 que quizá se ejecutó es peor que una que consta como incierta).
- **Respuesta.** Verificar en el proveedor (correo enviado, reserva existente) **antes** de
  reintentar. Después, decidir: si no se ejecutó, volver a preparar la acción; si se ejecutó, no
  repetirla.
- **Regla.** No convertir `unknown` en `failed` para "limpiar" la tabla; eso es exactamente lo que la
  spec §7 prohíbe.

## 10. Recordatorios: estado `unknown` del despachador

- **Síntoma.** En `reminder_dispatches`, una fila `claimed` con el lease vencido pasa a **`unknown`**
  y se reporta en el `JOB_REPORT` del job (`reconciled_items`). El usuario puede o no haber recibido
  el aviso.
- **Qué ocurre.** La ventana es la que hay entre insertar el derecho a enviar (el ledger, con
  restricción única por ocurrencia) y `sendMessage`. El **fase 0 — reconciliar** la cierra: la fila
  vencida se marca `unknown` y **nada se reenvía automáticamente**, porque Telegram no tiene clave de
  idempotencia y un reenvío duplicaría el aviso.
- **Respuesta.** Si el usuario dice que no lo recibió, crear un recordatorio nuevo o pedirle que lo
  confirme; el `unknown` queda como registro. Revisar por qué el job no terminó (timeout, crash).
- **Regla.** No "reintentar" una fila `unknown` cambiándola a `claimed`: el lease y la restricción
  única existen para que no se entregen dos avisos.

## 11. Turno interrumpido (cold start, SIGTERM, presupuesto)

- **Síntoma.** El webhook contesta 200 sin respuesta; el usuario recibe la respuesta unos segundos
  después. Logs: `update handled` con `status`, y el job `resume_work` con `JOB_REPORT`.
- **Qué ocurre.** Antes de ejecutar el grafo se inserta una fila `work_items` (watchdog) con
  `run_at = ahora + presupuesto`; si el turno muere en cualquier punto, la fila queda pendiente y el
  job reanuda el hilo desde el checkpoint y entrega la respuesta. Un segundo mensaje con un turno en
  curso recibe `busy.previous_turn_in_progress`.
- **Respuesta.** Ninguna si el job está habilitado (lo está, cada minuto). Si el usuario no recibe la
  respuesta en ~1–2 minutos, revisar que el schedule `taa-prod-job-resume-tick` esté habilitado y que
  el job pueda conectar a la base de datos.
- **Regla.** No desactivar el job de reanudación: es la otra mitad del presupuesto de 45 s.

## 12. Salud y comprobaciones rápidas

| Qué | Cómo | Qué significa |
|---|---|---|
| Liveness | `GET /healthz` (o `/health`) | el proceso está vivo; no toca dependencias |
| Readiness | `GET /readyz` | base de datos alcanzable, `vector` ≥ 0,8, **una** fila activa en `embedding_models` |
| Webhook | `python scripts/set_webhook.py --info` | `last_error_message` debe estar vacío |
| Jobs | `gcloud run jobs executions list --job=… --region=europe-west1 --project=telegram-assistant-esteban` | última ejecución y su resultado |
| Base de datos | `python scripts/check_database.py --database-url-file …` | conectividad, pgvector y resolución de `vector` |

`/readyz` no revela DSN, bucket, versiones ni recuentos: solo `{"status": …}` y un motivo fijo.

## 13. Lo que nunca aparece en los logs

Tokens, claves, contraseñas, el secreto del webhook, el contenido de los mensajes, el texto de los
documentos, los embeddings ni los DSN. La redacción es por **clave** y por **forma** (bearer, token de
Telegram, hex largo, cabecera PEM), y los errores del proveedor nunca incluyen la URL con el token.
Si hiciera falta depurar contenido, `LOG_CONTENT_DEBUG=true` es opt-in, limitado y documentado; no se
activa en producción.

## 14. Copias y restauración

No hay copias de Cloud SQL porque no hay Cloud SQL. La base es **Supabase**:

- **Plan Pro:** copias **diarias** desde el panel del proyecto (*Database → Backups*). Es el camino de
  restauración documentado.
- **Plan gratuito** (el actual): **no hay copias**. La única red es un `pg_dump` manual a través del
  pooler (modo sesión) **antes** de una migración destructiva.
- `scripts/migrate.py downgrade` se niega en producción sin `--i-have-a-backup`; esa bandera es una
  promesa del operador, no una comprobación.

## 15. Lista de "no hacer"

1. No ejecutar acciones de nivel 2 a mano en la base de datos para saltarse el CAS.
2. No convertir `unknown` en `failed` ni reintentar solo una acción o un recordatorio incierto.
3. No subir `--max-instances` ni los pools sin rehacer el aritmético del pooler.
4. No poner `STORE_BACKEND=memory` ni `EMBED_PROVIDER=stub` en producción (el arranque lo impide).
5. No relajar `/readyz` para "pasar" un despliegue.
6. No pasar secretos por `argv` ni escribirlos en un fichero versionado.
7. No desactivar el job de reanudación ni el de recordatorios.
