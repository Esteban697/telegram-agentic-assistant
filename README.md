# Asistente personal en Telegram

Asistente personal privado para Telegram. Un único usuario autorizado conversa con un modelo
alojado en Ollama Cloud, busca en la web datos actuales, y puede guardar preferencias, crear
recordatorios, enviar documentos para preguntar por su contenido y preparar acciones externas que
solo se ejecutan con aprobación explícita.

Está desplegado en Google Cloud: el servicio y los tres jobs corren en Cloud Run, el estado vive en
PostgreSQL (Supabase) y los embeddings se calculan con Vertex AI. La documentación de despliegue,
operación, seguridad y el informe final están en [`docs/`](docs/).

---

## Qué hace

- **Conversación con contexto persistente.** Cada chat tiene su hilo; el estado del grafo
  (LangGraph) se guarda en PostgreSQL, así que sobrevive a reinicios y a la escala a cero.
- **Búsqueda web.** Consulta datos que cambian —precios, noticias, horarios, disponibilidad— con
  la API de búsqueda de Ollama Cloud y responde citando el título y la URL de la fuente. Los
  resultados se tratan como datos no confiables y se recortan antes de entrar en el contexto.
- **Preferencias explícitas.** Guarda lo que le pidas recordar y lo consulta después. Solo guarda
  lo que se le pide: no convierte la conversación entera en memoria.
- **Recordatorios.** Con fecha, hora y zona horaria IANA. Se entregan mediante un job programado;
  se pueden listar, completar y cancelar.
- **Documentos con fuentes.** Acepta PDF, texto plano y Markdown (hasta 20 MB / 200 páginas), los
  indexa por fragmentos y responde citando **el documento y la página** de cada dato. Si los
  pasajes recuperados no responden, lo dice en vez de inventar.
- **Aprobaciones.** Las acciones con efecto externo se **preparan**, nunca se ejecutan solas. El
  usuario recibe un botón Aprobar / Rechazar; la aprobación caduca y solo vale para los argumentos
  exactos que se mostraron.
- **Acceso privado.** Solo los IDs de `TELEGRAM_ALLOWED_USER_IDS` pueden usar el bot; sin lista,
  nadie entra. El webhook valida un secreto en tiempo constante, limita el tamaño del cuerpo y
  procesa cada `update_id` una sola vez.
- **Operación observable.** Logs JSON estructurados, sin secretos ni contenido de mensajes, y
  endpoints separados de liveness y readiness.

## Cómo se usa desde Telegram

Abre el chat con el bot y envía `/start`. A partir de ahí puedes escribir con normalidad.

### Conversación

No hace falta ningún comando:

- `Ayúdame a redactar un correo para pedir un presupuesto a un fontanero.`
- `Explícame la diferencia entre un préstamo a tipo fijo y uno variable, en cinco líneas.`
- `Resume esto en tres puntos: <texto pegado>`

Responde en español por defecto. Para datos que cambian —precios, noticias, horarios,
disponibilidad— usa la búsqueda web (ver abajo) y cita la fuente; si la búsqueda falla, lo dice en
vez de inventar una respuesta.

### Preferencias

- `Recuerda que mi aerolínea preferida es Iberia.`
- `Guarda que mi talla de camiseta es L.`
- `/memory` — lista las preferencias guardadas con su clave exacta.
- `/forget talla_camiseta` — borra una.
- `/forget all` — pide confirmación; `/forget all confirm` borra todas.

### Recordatorios

- `Recuérdame llamar al dentista el 20 de octubre a las 9:30.`
- `Ponme un recordatorio para renovar el DNI el 3 de noviembre a las 18:00.`
- `Recuérdame sacar la basura mañana a las 20:00.`

La zona por defecto es `Europe/Madrid` (`DEFAULT_TIMEZONE`); si estás en otra, dila explícitamente
(`... a las 9:30 hora de Buenos Aires`). Si la fecha es ambigua, pregunta antes de crear nada.

- `/tasks` — lista los recordatorios con hora local, zona y estado.
- `Marca como hecho el recordatorio del dentista.`
- `Cancela el recordatorio del DNI.`

El job de recordatorios se ejecuta cada minuto en el despliegue, así que el aviso llega en torno a
esa marca.

### Documentos

Envía un **PDF, `.txt` o `.md`** como adjunto (máximo 20 MB y 200 páginas). Después:

- `¿Cuál es la cláusula de renovación del contrato?`
- `¿Qué dice el documento sobre el plazo de preaviso? Nombra el archivo y la página.`
- `Resume los puntos principales del PDF que te acabo de enviar.`
- `¿Hay algo en mis documentos sobre dietas de viaje?`

Responde solo con los pasajes recuperados y cita el archivo y la página. Si no hay evidencia
suficiente, lo dice. Si un documento contiene instrucciones dirigidas al asistente, las ignora y lo
advierte.

- `Borra el documento contrato.pdf.` — elimina fragmentos, original y fila, por etapas.
- `¿Está listo el documento que te envié?` — consulta el estado de la ingesta.

Un PDF escaneado sin capa de texto se rechaza con un mensaje claro; las fotos y las notas de voz no
se procesan.

### Búsqueda web

- `¿A cuánto está el bitcoin ahora mismo?`
- `Busca el horario de apertura del Museo del Prado y dime la fuente.`
- `¿Qué ha pasado hoy con <tema>? Responde con enlaces.`

Usa `POST https://ollama.com/api/web_search` de Ollama Cloud con la misma `OLLAMA_API_KEY` del
chat; cada consulta cuenta en la cuenta de Ollama. Devuelve hasta 10 resultados (5 por defecto) con
título, URL y un extracto acotado. Responde citando título y URL, y si no hay resultados o el
proveedor falla, lo dice en vez de rellenar con conocimiento general presentado como actual.

### Aprobaciones

- `Reserva una mesa para dos el viernes a las 21:00 en Casa Pepe.`

El asistente prepara la acción y muestra **Aprobar / Rechazar** con una vista previa y una caducidad
de 30 minutos. Nada se ejecuta hasta que se pulsa Aprobar, y dos pulsaciones simultáneas ejecutan
una sola vez.

Aviso importante: **no hay proveedor de reservas integrado**. El flujo de aprobación funciona de
extremo a extremo, pero la ejecución responde `CapabilityUnavailable`; el asistente no finge que ha
reservado.

### Comandos

| Comando | Efecto |
|---|---|
| `/start` | Presentación y menú |
| `/help` | Lista de comandos |
| `/reset` | Borra el contexto de la conversación; no toca preferencias, tareas ni documentos |
| `/tasks` | Lista los recordatorios |
| `/memory` | Lista las preferencias guardadas |
| `/forget <clave>` | Borra una preferencia |
| `/forget all` | Pide confirmación para borrarlas todas |

Si se envía un segundo mensaje mientras el primero se está procesando, el bot responde que está
terminando el anterior y pide reintentar en unos segundos.

---

## Arquitectura

```
Telegram ──HTTPS──▶ app/api/telegram_webhook.py
                      │ secreto · tamaño · dedupe por update_id · allow-list · rate limit · presupuesto
                      ▼
                    app/bot/handlers.py ──▶ app/agent/graph.py (LangGraph)
                                              │ call_model → run_tools / await_approval
                                              ▼
                    app/tools/registry.py (nivel de riesgo + ledger de invocaciones)
                      │
                      ├──▶ PostgreSQL (Supabase)  conversación, mensajes, memoria, tareas,
                      │                           documentos y chunks, aprobaciones, ledger,
                      │                           work_items y checkpoints del grafo
                      ├──▶ Cloud Storage          originales de los documentos
                      ├──▶ Vertex AI              embeddings
                      └──▶ Ollama Cloud           chat y búsqueda web
```

- **Un binario, cuatro comandos**: `uvicorn app.main:app` para el servicio y
  `python -m app.jobs.dispatch_reminders`, `-m app.jobs.process_documents` y
  `-m app.jobs.resume_work` para los tres jobs de una sola pasada.
- **El estado no vive en el proceso.** El servicio escala a cero y los jobs son independientes; la
  conversación, la memoria, las tareas y los checkpoints están en PostgreSQL. El checkpointer de
  LangGraph (`AsyncPostgresSaver`) se adjunta al compilar el grafo y usa la misma base de datos.
- **Una sola instancia del servicio** (`max-instances=1`) porque el pooler de Supabase en modo
  sesión admite 14 conexiones de cliente; servicio y jobs comparten ese presupuesto
  (`scripts/check_pool_budget.py`).
- **El SQL vive en un solo módulo** (`app/storage/repositories.py`), incluido el único punto de
  recuperación vectorial. Una prueba estructural impide interpolación de SQL fuera de él.
- **Los textos que lee el usuario** están en `app/bot/messages.py`; los prompts en
  `app/agent/prompts.py`. Ambos en español.

## Requisitos

- Python 3.13 o superior y [uv](https://docs.astral.sh/uv/).
- Docker, solo para la base de datos local y para construir/auditar la imagen.
- Para hablar con el modelo real: `OLLAMA_API_KEY`.
- Para embeddings y documentos en producción: un proyecto de Google Cloud con Vertex AI y un
  bucket de Cloud Storage. Sin credenciales se puede usar `EMBED_PROVIDER=stub` en pruebas.

## Puesta en marcha local

Los comandos de esta sección están escritos para bash. En PowerShell, usa `Copy-Item` en el paso 2
y exporta las variables de entorno con `$env:NAME="valor"` en lugar del prefijo `NAME=valor`. Todos
se ejecutan desde la raíz del repositorio.

```bash
# 1. Entorno e instalación (incluidas las dependencias de desarrollo)
uv venv
uv pip install -e ".[dev]"

# 2. Configuración local: copia la plantilla y rellena lo que tengas
#    (en Windows/PowerShell: Copy-Item .env.example .env)
cp .env.example .env

# 3. Base de datos local (PostgreSQL 17 + pgvector, puerto 55432)
bash scripts/run_local_db.sh

# 4. Esquema desde una base vacía, tablas del checkpointer y verificación
uv run python scripts/bootstrap_db.py

# 5. Servicio con almacenamiento duradero
#    (pon STORE_BACKEND=postgres en .env)
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

# 6. Comprobaciones
curl http://127.0.0.1:8000/health     # {"status":"ok"}
curl http://127.0.0.1:8000/readyz     # {"status":"ready"}
```

Para la base de datos local, los DSN son:

```
DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa
DATABASE_SYNC_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa
```

Sin base de datos, `STORE_BACKEND=memory` ofrece un **modo solo local** (contexto, idempotencia y
almacenes en memoria). Es un error de arranque en `APP_ENV=production`, y con él `/tasks`, `/memory`
y `/forget` responden que no pueden acceder a los datos en lugar de fingir que los han guardado.
Sin `TELEGRAM_ALLOWED_USER_IDS` el servicio también arranca y responde a las comprobaciones de
salud, pero no atiende ninguna petición de Telegram (denegación por defecto); en `production` una
lista vacía impide arrancar.

`/health` es el liveness que funciona también en Cloud Run. `/healthz` existe y funciona en local,
pero el frontend de Google intercepta las rutas que terminan en `z`, así que no se usa en el
despliegue. `/readyz` comprueba base de datos, `vector` ≥ 0.8 y una fila activa de embedding.

## Configuración

Toda la configuración va por variables de entorno o por un `.env` local. La referencia completa y
comentada es **[`.env.example`](.env.example)**, que **no contiene secretos** (los valores vacíos son
intencionados); `.env` está en `.gitignore`. Una prueba comprueba que no hay variables declaradas
sin consumidor.

| Grupo | Variables principales |
|---|---|
| Aplicación | `APP_ENV`, `LOG_LEVEL`, `LOG_CONTENT_DEBUG` |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `TELEGRAM_ALLOWED_USER_IDS`, `DEV_ALLOW_ANY_USER`, `TELEGRAM_API_BASE_URL`, `MAX_MESSAGE_CHARS`, `MAX_WEBHOOK_BODY_BYTES` |
| Modelo | `OLLAMA_BASE_URL`, `OLLAMA_API_KEY`, `LLM_MODEL`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`, `LLM_THINK`, `REQUEST_BUDGET_SECONDS`, `RATE_LIMIT_PER_MINUTE` |
| Búsqueda web | `WEB_SEARCH_PROVIDER`, `WEB_SEARCH_BASE_URL`, `WEB_SEARCH_MAX_RESULTS`, `WEB_SEARCH_TIMEOUT_SECONDS`, `WEB_SEARCH_MAX_CONTENT_CHARS`, `WEB_SEARCH_MAX_TOTAL_CHARS` |
| Persistencia | `STORE_BACKEND`, `DATABASE_URL`, `DATABASE_SYNC_URL`, `DB_POOL_SIZE`, `CHECKPOINTER_POOL_SIZE`, `MIGRATIONS_AUTORUN`, `DEFAULT_TIMEZONE` |
| Embeddings y documentos | `EMBED_PROVIDER`, `EMBED_MODEL`, `EMBED_DIM`, `EMBED_TIMEOUT_SECONDS`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_REGION`, `GCS_BUCKET`, `MAX_UPLOAD_MB`, `MAX_PDF_PAGES`, `MAX_INGEST_SECONDS`, `INGEST_INLINE_BUDGET_SECONDS`, `DOCUMENT_JOB_BATCH_SIZE`, `RAG_*` |
| Recordatorios | `REMINDER_BATCH_SIZE`, `REMINDER_MAX_ATTEMPTS`, `DISPATCHER_LEASE_SECONDS` |
| Aprobaciones | `APPROVAL_TTL_MINUTES`, `STALE_INVOCATION_SECONDS` |
| Notificaciones | `NOTIFICATIONS_TRANSPORT`, `NOTIFY_RECORD_PATH` |

Las validaciones de arranque son deliberadamente estrictas: en `production` no arranca con
`STORE_BACKEND=memory`, con la lista de usuarios vacía, con `DEV_ALLOW_ANY_USER=true` ni con una
combinación de timeout y reintentos que no quepa en `REQUEST_BUDGET_SECONDS`.

## Bot de Telegram

1. Abre un chat con **@BotFather**, envía `/newbot` y guarda el token.
2. Averigua tu ID numérico de usuario: escribe al bot y mira el aviso de usuario no autorizado en
   los logs, o usa `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. Rellena en `.env` `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` y
   `TELEGRAM_WEBHOOK_SECRET` (1-256 caracteres de `A-Za-z0-9_-`).
4. Registra el webhook. Telegram exige **HTTPS público**; para local hace falta un túnel (por
   ejemplo `cloudflared tunnel --url http://127.0.0.1:8000`).

```bash
# Ver los comandos que se ejecutarían, sin hacer ninguna llamada
python scripts/set_webhook.py --print --url https://<tu-url>/telegram/webhook \
  --token-file ~/.taa/secrets/telegram-bot-token \
  --secret-file ~/.taa/secrets/telegram-webhook-secret

# Registrar, consultar y dar de baja (nunca imprime el token)
python scripts/set_webhook.py --set    --url https://<tu-url>/telegram/webhook --token-file ... --secret-file ...
python scripts/set_webhook.py --info   --token-file ...
python scripts/set_webhook.py --delete --token-file ...
```

`getWebhookInfo` debe quedar sin `last_error_message`. El presupuesto de 45 s está por debajo del
límite de 60 s de Telegram para evitar los `Read timeout expired`.

## Base de datos y migraciones

- `migrations/versions/0001`–`0007` crean el esquema (extensiones, conversaciones, memoria, tareas,
  documentos y chunks, aprobaciones, ledger y `work_items`).
- Las tablas del checkpointer de LangGraph las crea la propia librería
  (`AsyncPostgresSaver.setup()`), no Alembic.
- `scripts/bootstrap_db.py` aplica el esquema desde una base vacía, crea las tablas del checkpointer
  y verifica tablas, `vector` ≥ 0.8, índice HNSW, disparador de inmutabilidad y modelo activo.
- `scripts/migrate.py` envuelve Alembic con comprobación previa y un guardián de producción:

```bash
uv run python scripts/migrate.py current
uv run python scripts/migrate.py upgrade head
uv run python scripts/migrate.py downgrade -1 --i-have-a-backup   # solo si hay copia
```

En Supabase, `vector` vive en el esquema `extensions`; el rol de aplicación necesita `USAGE` sobre
ese esquema y el `search_path` debe incluirlo. `scripts/check_database.py` lo comprueba.

## Jobs

Cada job es una pasada idempotente y termina; no hay bucles ni estado en memoria:

```bash
uv run python -m app.jobs.dispatch_reminders   # recordatorios vencidos
uv run python -m app.jobs.process_documents    # ingestas pendientes o fallidas
uv run python -m app.jobs.resume_work          # turnos interrumpidos
```

Cada ejecución imprime una línea `JOB_REPORT` con lo reclamado, enviado, omitido y fallido. En el
despliegue los dispara Cloud Scheduler: recordatorios y reanudación cada minuto, documentos cada
cinco.

## Pruebas

```bash
# Suite por defecto: sin credenciales, sin red y sin base de datos
uv run pytest -m "not integration and not live and not eval"

# Integración: aplicación completa con transportes simulados (sin credenciales)
uv run pytest -m integration

# Integración contra PostgreSQL
TEST_STORE_BACKEND=postgres \
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa \
  uv run pytest -m integration

# Evaluaciones deterministas de RAG (base de datos local, proveedor léxico de prueba)
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa \
  uv run pytest -m "eval and not live"

# Proveedor real de embeddings (facturable)
GOOGLE_CLOUD_PROJECT=telegram-assistant-esteban GOOGLE_CLOUD_REGION=europe-west1 \
RUN_LIVE_TESTS=1 uv run pytest -m "live and not eval"

# Agente con el modelo real (necesita OLLAMA_API_KEY y base de datos)
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa \
RUN_LIVE_TESTS=1 uv run pytest -m "eval and live"

# Búsqueda web real: modelo + /api/web_search (cada consulta cuenta en Ollama)
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa \
RUN_LIVE_TESTS=1 uv run pytest -m "eval and live" tests/evals/test_web_search_live.py
```

Los marcadores `integration`, `eval` y `live` están declarados en `pyproject.toml`; las pruebas que
exigen credenciales reales llevan `live` y quedan excluidas por defecto. Los prefijos
`VAR=valor` son sintaxis de bash; en PowerShell, exporta cada variable con
`$env:VAR="valor"` en una línea aparte. Las evaluaciones usan documentos ficticios en
`tests/fixtures/` (sin datos personales reales). Los resultados de la última ejecución completa
están en [`docs/informe-final.md`](docs/informe-final.md).

### Sonda del modelo

`scripts/verify_ollama.py` comprueba contra el proveedor real que el modelo conversa, que llama a
herramientas con argumentos parseables y que devuelve JSON cuando el esquema se dicta en el prompt.
Sale con `0` si todo funciona, `1` si el modelo no puede (hay que cambiar de modelo o usar el
respaldo por prompt) y `2` si falta `OLLAMA_API_KEY`, en cuyo caso la comprobación queda pendiente
de validación externa.

## Docker

```bash
docker build --no-cache -t telegram-assistant:latest .

# Arranca sin secretos y responde a /health
docker run --rm -p 8080:8080 telegram-assistant:latest
curl http://127.0.0.1:8080/health

# La imagen no debe contener secretos
python scripts/audit_image.py --image telegram-assistant:latest
python scripts/audit_image.py --self-test
```

La imagen es multi-etapa, con la base fijada **por digest**, usuario sin privilegios (uid 10001),
`--no-cache-dir`, sin `.env`, claves ni ficheros de cuenta de servicio, y `CMD` en forma exec para
recibir `SIGTERM`. En el despliegue se publica en Artifact Registry y Cloud Run la consume por
digest.

## Despliegue y operación

El runbook completo está en **[`docs/despliegue.md`](docs/despliegue.md)**: recursos, coste,
secretos, migraciones, operación y reversión.

```bash
bash scripts/deploy.sh                          # imprime todos los comandos; no ejecuta nada
bash scripts/deploy.sh --yes-i-have-approval    # ejecuta, tras aprobar el coste
```

Dos reglas que se comprueban con pruebas, no por convención:

- **Todos los comandos llevan `--project telegram-assistant-esteban`**, porque el proyecto por
  defecto de `gcloud` en esta máquina es otro.
- **Ningún valor secreto se imprime**, ni en éxito ni en error: `gcloud` los lee de un fichero
  (`--data-file=`) o del terminal (`--prompt-for-password`), y `scripts/lib/redact.sh` redacta
  valores y formas de credencial.

**Estado desplegado:** servicio `taa-prod-api` en Cloud Run
(`https://taa-prod-api-hzlxaxcstq-ew.a.run.app`, escala a cero, `max-instances=1`), tres Cloud Run
Jobs, tres schedulers habilitados, cuatro secretos con versión fijada y el webhook registrado.
Base de datos en Supabase (PostgreSQL 17.11 + pgvector 0.8.2) y embeddings en Vertex AI.

- Incidentes y recuperación: [`docs/operacion.md`](docs/operacion.md).
- Seguridad y política de aprobación: [`docs/seguridad.md`](docs/seguridad.md).
- Informe final, resultados y pendientes: [`docs/informe-final.md`](docs/informe-final.md).

## Seguridad

- El secreto del webhook se compara en tiempo constante y sobre bytes; una cabecera inválida
  devuelve 403 con el cuerpo vacío y no se registra.
- La identidad es siempre `effective_user.id`, nunca el nombre visible, el usuario ni el texto.
- Denegación por defecto: sin lista de usuarios, en `production` el servicio no arranca.
- Las acciones de nivel 2 se preparan y exigen aprobación ligada al usuario, al chat y a los
  argumentos exactos; un CAS atómico impide ejecutarlas dos veces.
- El texto de los documentos es **dato, no instrucción**: se delimita con un marcador no
  falsificable y el prompt ordena ignorar y reportar cualquier orden contenida en él.
- Los resultados de la búsqueda web son datos no confiables: el prompt ordena ignorar instrucciones
  contenidas en ellos y citar la fuente. La API key va en la cabecera, nunca en la URL, y los
  resultados no se registran en los logs.
- Los secretos viven en Secret Manager (versión fijada, nunca `latest`) y nunca se pasan por
  `argv`. No hay claves de cuenta de servicio: Cloud Run usa su identidad.
- Los logs son JSON y no contienen tokens, DSN, mensajes, documentos ni embeddings.

## Limitaciones conocidas

- **Un solo usuario.** Hay aislamiento por propietario y está probado, pero la autorización es una
  lista; el sistema no está pensado para múltiples inquilinos.
- **Sin integraciones de reservas, correo o calendario.** `search_restaurants`,
  `check_restaurant_availability` y `submit_reservation` existen como interfaces y responden
  `CapabilityUnavailable` mientras no haya proveedor. La búsqueda web sí está integrada; no hay
  `web_fetch` ni scraping de la página completa, solo los extractos que devuelve la búsqueda.
- **La búsqueda web depende de Ollama Cloud.** Cada consulta cuenta en la cuenta de Ollama y la
  documentación no publica cuotas ni precios; si el proveedor falla o limita, la herramienta
  devuelve un error y el asistente lo dice. No hay caché ni reintento oculto.
- **Solo texto y documentos.** No se procesan notas de voz, fotos ni vídeo. Los PDF escaneados sin
  capa de texto se rechazan.
- **El proveedor no respeta `format` para salida estructurada.** El *tool calling* funciona; cuando
  se necesita JSON, el esquema se dicta en el prompt y se valida en el servidor.
- **"No hay evidencia" es una tasa medida, no una garantía.** Cuando los documentos recuperan
  pasajes que no responden, el modelo lo dice; ante una pregunta totalmente ajena puede no
  consultar la recuperación y dar una negativa genérica. El código sí es determinista: sin pasajes
  por encima del filtro, `found=false`, `evidence="none"` y ninguna cita.
- **El suelo de recuperación no es un umbral de relevancia.** `RAG_MIN_SCORE` descarta
  coincidencias degeneradas; la decisión de relevancia la toma el modelo leyendo los pasajes.
- **Telegram 429: la respuesta se pierde.** El cliente lanza `TelegramRateLimited` con el
  `retry_after` indicado, pero no espera ni reintenta: la respuesta de ese turno no se entrega y el
  `update` queda marcado como fallido. La idempotencia protege la entrada, no la respuesta perdida.
- **El rate limit es por instancia.** El cubo de fichas es local al proceso; con una sola instancia
  el límite efectivo es `RATE_LIMIT_PER_MINUTE`, y un arranque en frío lo reinicia.
- **El turno no está acotado por un único límite.** El presupuesto cubre la llamada al modelo y el
  bucle de ingesta; la descarga de `getFile` no tiene tope propio, así que un turno dura
  *presupuesto + descarga + respuesta*.
- **Supabase en plan gratuito se pausa tras 7 días de inactividad y no tiene copias.** Para un
  asistente siempre disponible y con restauración se recomienda el plan Pro. La reversión de código
  está ejercitada; la restauración del esquema no, porque no hay copia.
- **Sin purga automática ni canal de alertas.** La retención es una decisión abierta; los datos se
  conservan hasta que se borran con los comandos o el procedimiento manual.
- **Las evaluaciones del agente en vivo son estocásticas.** Una de ellas ha fallado en ejecuciones
  anteriores por citar una referencia numerada en lugar de nombrar archivo y página; la aserción no
  se ha relajado.

## Estructura

```
app/
  main.py               fábrica de la aplicación y cableado de dependencias
  config.py             configuración tipada y validaciones de arranque
  logging_config.py     logs JSON estructurados y redacción
  errors.py             taxonomía de errores tipados
  policy/               autorización, límites, niveles de riesgo, aprobaciones y zonas horarias
  api/                  webhook de Telegram y endpoints de salud
  bot/                  cliente de la Bot API, handlers, teclados y catálogo de textos en español
  agent/                grafo LangGraph, estado, prompts y políticas
  llm/                  puertos, cliente de Ollama y proveedores de embeddings
  storage/              modelos, repositorios, motores, checkpointer, GCS y almacenes en memoria
  services/             conversación, aprobaciones, ingesta, recordatorios y cola de trabajo
  tools/                herramientas de memoria, tareas, documentos, restaurantes y búsqueda web
  jobs/                 tres jobs de una sola pasada
migrations/             revisiones de Alembic (0001..0007)
docs/                   despliegue, operación, seguridad e informe final
scripts/                despliegue, webhook, auditoría de imagen, presupuesto de conexiones,
                        migraciones, bootstrap, sonda del modelo y base de datos local
tests/unit/  tests/integration/  tests/evals/  tests/live/
```
