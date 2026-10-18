# Asistente personal en Telegram

Asistente personal que se usa desde un bot privado de Telegram y responde con un modelo
alojado en Ollama. Este repositorio contiene la **Fase 1** del plan de implementación
(`PLAN.md`): conversación por webhook, autorización por lista de usuarios, comandos básicos y
un contenedor reproducible. Las fases posteriores (persistencia, LangGraph con herramientas y
aprobaciones, RAG documental y despliegue en Google Cloud) se añaden por fases.

> **Estado: Fase 1, solo local.** El almacenamiento es en memoria
> (`STORE_BACKEND=memory`) y el servicio está pensado para ejecutarse en una sola instancia.
> Esa combinación es un **error de arranque** en `APP_ENV=production`, así que esta fase no
> se puede desplegar por accidente. La persistencia en PostgreSQL llega en la Fase 2.

---

## Qué puede hacer ahora mismo

- Conversar con el modelo a través del webhook de Telegram, con contexto por chat.
- Autorizar únicamente a los usuarios de `TELEGRAM_ALLOWED_USER_IDS` (denegación por defecto).
- Validar el secreto del webhook en cada petición y limitar el tamaño del cuerpo.
- Procesar cada `update_id` **una sola vez**, aunque Telegram lo reintente.
- Limitar la frecuencia por usuario y avisar una sola vez por ventana.
- Guardar y consultar **preferencias explícitas** (`/memory`) y borrarlas (`/forget`).
- Crear, listar, completar y cancelar **recordatorios** con zona horaria IANA (`/tasks`).
- Enviar un **documento** (PDF, texto o Markdown) y preguntar por su contenido: se indexa en
  fragmentos con su número de página y las respuestas citan el archivo y la página. Si no hay
  evidencia suficiente, lo dice en lugar de inventar.
- Registrar cada `update_id` en una tabla: la idempotencia sobrevive a los reinicios.
- Despachar recordatorios con la lógica de reclamo, reconciliación y avance de estado de
  `PLAN.md` §5.2 (`python -m app.jobs.dispatch_reminders`).
- Comandos: `/start`, `/help`, `/reset`, `/tasks`, `/memory`, `/forget`.
- Registrar logs estructurados en JSON, sin secretos, sin contenido de mensajes y sin
  embeddings.

## Arquitectura (Fase 1)

```
Telegram  ──HTTPS──▶  app/api/telegram_webhook.py
                        │ 1. secreto del webhook (comparación en tiempo constante)
                        │ 2. límite de tamaño del cuerpo
                        │ 3. idempotencia por update_id
                        │ 4. lista de usuarios autorizados
                        │ 5. límite de frecuencia por usuario
                        ▼
                      app/bot/handlers.py ──▶ app/agent/graph.py (LangGraph, 1 nodo)
                        │                              │
                        │                              ▼
                        │                     app/llm/ollama_client.py ──▶ Ollama
                        ▼
                app/storage/stores.py (contexto e idempotencia, en memoria)
```

Regla de dependencias que se comprueba con una prueba (`tests/unit/test_layering.py`):
`agent/` no importa nunca `app.storage.stores` ni `httpx`. **El handler** lee el contexto de la
conversación y pasa al grafo la lista de mensajes ya preparada. Por eso la Fase 2 solo tiene
que **adjuntar el checkpointer** (`compile(checkpointer=...)` y un `thread_id` en
`ChatAgent.run`) y sustituir el almacén por el estado persistido: el grafo no se reestructura.
La regla de dependencias garantiza que el cambio no arrastre el almacén al interior del
agente, no que `graph.py` no se toque.

---

## Requisitos

- Python 3.13 o superior
- [uv](https://docs.astral.sh/uv/) (gestor de entornos y dependencias)
- Docker (solo para la imagen y la comprobación del contenedor)
- Opcional: una clave de API de Ollama (`OLLAMA_API_KEY`) para hablar con el modelo real

## Puesta en marcha local

Los comandos siguientes funcionan tal cual en PowerShell y en bash, desde la raíz del
repositorio.

```bash
# 1. Entorno virtual e instalación de dependencias (incluidas las de desarrollo)
uv venv
uv pip install -e ".[dev]"

# 2. Configuración local: copia la plantilla y rellena lo que tengas
#    (en Windows/PowerShell: Copy-Item .env.example .env)
cp .env.example .env

# 3. Arranca el servicio en http://127.0.0.1:8000
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

# 4. Comprueba que está vivo
curl http://127.0.0.1:8000/healthz
# {"status":"ok"}
```

El servicio arranca **sin secretos**: sin `TELEGRAM_BOT_TOKEN`, sin `OLLAMA_API_KEY` y con la
lista de usuarios vacía. En ese estado `/healthz` responde y ninguna petición de Telegram se
atiende, porque la lista está vacía (denegación por defecto).

## Base de datos (Fase 2)

```bash
# 1. PostgreSQL 17 con pgvector
bash scripts/run_local_db.sh          # o el `docker run` equivalente del README de más abajo

# 2. DSN en .env
#   DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa
#   DATABASE_SYNC_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa

# 3. Esquema desde una base vacía + tablas del checkpointer + verificación
uv run python scripts/bootstrap_db.py

#    OK: 12 application tables + 4 checkpoint tables, pgvector 0.8.7, HNSW index present,
#        trigger present, 1 active embedding model.

# 4. Servicio con almacenamiento duradero
STORE_BACKEND=postgres uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`scripts/migrate.py` envuelve Alembic con una comprobación previa y un guardián de producción:

```bash
uv run python scripts/migrate.py current
uv run python scripts/migrate.py upgrade head
uv run python scripts/migrate.py downgrade -1 --i-have-a-backup   # solo en producción si hace falta
```

Las tablas del checkpointer de LangGraph las crea la propia librería (`PostgresSaver.setup()`),
no Alembic: la especificación §8 pide no reinventar su formato.

### Despacho de recordatorios

```bash
uv run python -m app.jobs.dispatch_reminders
# JOB_REPORT {"claimed": 1, "sent": [...], "skipped": [], "failed": [], ...}
```

### Comandos de prueba

```bash
# Suite por defecto: sin credenciales, sin red y sin base de datos.
# Debe pasar en un clon limpio y sin fichero .env (criterio A1).
uv run pytest -m "not integration and not live and not eval"

# Pruebas de integración: levantan la aplicación completa con transportes simulados.
# Tampoco necesitan credenciales.
uv run pytest -m integration

# Las mismas pruebas de integración, contra PostgreSQL en lugar de la memoria.
TEST_STORE_BACKEND=postgres TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa   uv run pytest -m integration

# Solo las pruebas que necesitan base de datos (se saltan si no hay TEST_DATABASE_URL).
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa   uv run pytest tests/integration/test_phase2_db.py

# Cobertura de la suite sin credenciales
uv run pytest -m "not integration and not live and not eval" --cov=app --cov-report=term-missing

# Evaluaciones de RAG (deterministas: base de datos y proveedor léxico de prueba, sin red).
# Ojo: `-m "eval and not live"` es lo que no necesita nada; `-m eval` incluye las
# evaluaciones del agente, que llevan también el marcador `live` porque llaman al modelo.
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa   uv run pytest -m "eval and not live"

# Evaluaciones del agente: hacen llamadas reales al modelo (necesitan OLLAMA_API_KEY).
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa   uv run pytest -m eval tests/evals/test_agent_evals.py -s

# Criterios de aceptación (a) y (b) de la Fase 4, con embeddings reales de Vertex.
GOOGLE_CLOUD_PROJECT=telegram-assistant-esteban GOOGLE_CLOUD_REGION=europe-west1 RUN_LIVE_TESTS=1   uv run pytest -m live
```

Los marcadores `integration`, `eval` y `live` están declarados en `pyproject.toml`; las
pruebas que exigen credenciales reales llevan `live` y quedan excluidas por defecto.

Las evaluaciones viven en `tests/evals/`, con documentos ficticios y diminutos en
`tests/fixtures/` (sin datos personales reales). Las de RAG son deterministas; las del agente
hacen llamadas reales al modelo y por eso llevan el marcador `eval` y no forman parte de la
suite por defecto. Una evaluación del agente que falle se reporta como fallo: no se ajusta el
fixture para que pase.

## Configuración

Toda la configuración va por variables de entorno (o por un fichero `.env` local).
`.env.example` documenta cada variable y **no contiene ningún secreto**: los valores vacíos son
intencionados. `.env` está en `.gitignore`.

Las variables que la Fase 1 lee de verdad son:

| Variable | Para qué sirve |
|---|---|
| `APP_ENV` | `development`, `test` o `production`. Activa las validaciones de arranque. |
| `LOG_LEVEL`, `LOG_CONTENT_DEBUG` | Nivel de log y depuración de contenido (por defecto, nunca). |
| `TELEGRAM_BOT_TOKEN` | Token del bot. Se obtiene con @BotFather. |
| `TELEGRAM_WEBHOOK_SECRET` | Secreto del webhook (1-256 caracteres de `A-Za-z0-9_-`). |
| `TELEGRAM_ALLOWED_USER_IDS` | IDs numéricos autorizados, separados por comas. |
| `DEV_ALLOW_ANY_USER` | Solo para pruebas locales. Prohibido en `production`. |
| `TELEGRAM_API_BASE_URL` | Base de la Bot API (se cambia solo para pruebas). |
| `MAX_MESSAGE_CHARS`, `MAX_WEBHOOK_BODY_BYTES` | Límites de tamaño. |
| `OLLAMA_BASE_URL`, `OLLAMA_API_KEY`, `LLM_MODEL` | Endpoint, credencial y modelo. |
| `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`, `LLM_THINK` | Comportamiento frente al proveedor. |
| `REQUEST_BUDGET_SECONDS` | Presupuesto de tiempo de una petición. |
| `RATE_LIMIT_PER_MINUTE` | Límite por usuario y ventana. |
| `STORE_BACKEND` y los TTL/cupos de los almacenes | Almacenes en memoria de la Fase 1. |

Las variables de las fases siguientes (base de datos, embeddings, RAG, recordatorios) se
añaden en la fase que las consume. No hay configuración declarada que nadie lea: lo comprueba
`tests/unit/test_logging_and_config_consumers.py`.

## Comprobar el modelo (Fase 1, criterio A10)

`scripts/verify_ollama.py` es la prueba de compatibilidad que exige la especificación §3: no
se da por hecho que un modelo sepa llamar a herramientas solo por su nombre.

```bash
# PowerShell
$env:OLLAMA_API_KEY="<tu-clave>"
uv run python scripts/verify_ollama.py

# bash
OLLAMA_API_KEY="<tu-clave>" uv run python scripts/verify_ollama.py
```

El script imprime el catálogo real de `/api/tags` (el nombre autoritativo del modelo, que
puede no coincidir con el sufijo `:cloud` que usa la CLI), prueba una conversación normal, una
llamada a herramienta, una respuesta con `format` (JSON Schema) y mide la latencia.

- Salida `0`: todo funciona; A10 se puede dar por pasado.
- Salida `1`: el modelo no sabe llamar a herramientas; aplica el plan alternativo del
  `PLAN.md` §10 (otro modelo de la nube, o despacho guiado por esquema JSON) y **no** cierres
  la Fase 1 afirmando que funciona.
- Salida `2`: falta `OLLAMA_API_KEY`; la comprobación queda **pendiente de validación externa**.

## Bot de Telegram

1. Abre un chat con **@BotFather** en Telegram y envía `/newbot`. Guarda el token.
2. Averigua tu ID numérico de usuario: escribe a tu bot y mira el log
   (`telegram_user_id` aparece solo en el aviso de usuario no autorizado), o usa
   `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. Rellena en `.env`:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_ALLOWED_USER_IDS` con tu ID
   - `TELEGRAM_WEBHOOK_SECRET` con una cadena de `A-Za-z0-9_-`
4. Registra el webhook. Telegram exige **HTTPS público**, así que para probar en local hace
   falta un túnel (por ejemplo `cloudflared tunnel --url http://127.0.0.1:8000`):

```bash
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://<tu-tunel>/telegram/webhook","secret_token":"<SECRET>","allowed_updates":["message","edited_message","callback_query"]}'

# Comprobar el estado (last_error_message debe estar vacío)
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```

Si el webhook no responde a tiempo, Telegram muestra `Read timeout expired` en
`getWebhookInfo`. El presupuesto por defecto (45 s) está por debajo del límite de 60 s de
Telegram precisamente para evitarlo.

## Docker

```bash
docker build --no-cache -t telegram-assistant:phase5 .

# El contenedor arranca sin secretos y responde a /healthz
docker run --rm -p 8080:8080 telegram-assistant:phase5
curl http://127.0.0.1:8080/healthz

# Auditoría de la imagen: no debe contener secretos (Fase 5, criterio (c))
python scripts/audit_image.py --image telegram-assistant:phase5   # => AUDIT OK
python scripts/audit_image.py --self-test                        # prueba que el detector no es inerte
```

Un solo binario, **cuatro comandos** (decisión 18 del plan): `uvicorn app.main:app` para el
servicio, y `python -m app.jobs.dispatch_reminders`, `-m app.jobs.process_documents` y
`-m app.jobs.resume_work` para los tres jobs programados.

La imagen se construye en dos etapas, corre como usuario sin privilegios (uid 10001), lleva la base
fijada **por digest** (`python:3.13-slim@sha256:70729b46…`), instala con `--no-cache-dir`, no
incluye `.env`, claves ni ficheros de servicio, y su `CMD` es exec-form para que uvicorn reciba
`SIGTERM` y pueda drenar.

## Despliegue en Google Cloud (Fase 5)

El runbook completo está en **[`docs/despliegue.md`](docs/despliegue.md)**: recursos, coste, secretos,
migraciones, operación, reversión y el estado de cada criterio de aceptación.

```bash
bash scripts/deploy.sh                          # IMPRIME todos los comandos gcloud; no ejecuta nada
bash scripts/deploy.sh --yes-i-have-approval    # ejecuta, tras aprobar el coste
```

Dos reglas que no son convenciones, sino tests:

- **Todos los comandos llevan `--project telegram-assistant-esteban`.** En esta máquina el proyecto
  por defecto de `gcloud` es **otro** (`presupuesto-esteban`), así que un flag olvidado crearía
  recursos facturables en el sitio equivocado. Lo comprueba `tests/unit/test_deploy_artifacts.py`
  recorriendo el script.
- **Ningún valor secreto se imprime nunca**, ni en el camino de éxito ni en el de error: los valores
  los lee `gcloud` de un fichero (`--data-file=`) o el terminal (`--prompt-for-password`), y
  `scripts/lib/redact.sh` sustituye por `<redacted>` cualquier valor conocido o con forma de
  credencial. Hay una prueba con un centinela, y otra que demuestra que la prueba puede fallar.

**Estado**: las APIs están habilitadas y ya existen (creados a mano, con aprobación) el repositorio
de imágenes, las dos cuentas de servicio, el bucket y la imagen publicada. **No** hay todavía
instancia de Cloud SQL, servicio, jobs ni schedules: los criterios (a), (d) y (e) siguen pendientes
de las credenciales del usuario — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` y
`TAA_ALLOWED_USER_IDS`, que sin él no despliega nada (la aplicación no arranca sin lista de
autorizados). (b) y (c) están demostrados en local — ver `docs/despliegue.md` §10.

## Limitaciones conocidas de la Fase 1

- **`STORE_BACKEND=memory` no persiste nada** y no soporta varias instancias (es un error de
  arranque en `production`). Con ese backend, `/tasks`, `/memory` y `/forget` responden que no
  pueden acceder a los datos, en lugar de fingir que los han guardado.
- **Sin herramientas en el grafo.** El modelo no puede crear recordatorios todavía: los
  comandos funcionan, pero la selección de herramientas por el modelo llega en la Fase 3.
- **RAG con un solo proveedor de embeddings.** Los documentos (PDF, texto, Markdown) se indexan y
  se buscan por similitud con Vertex AI (`gemini-embedding-001`, 768 dimensiones); sin credenciales
  de Google no hay recuperación, y el bot avisa cuando falta el proveedor. `RAG_MIN_SCORE` es un
  filtro de coincidencias degeneradas, **no** un umbral de relevancia: la decisión de "no hay
  evidencia" la toma el modelo con los fragmentos y sus puntuaciones delante (ver `.env.example`).
- **El despachador no está programado.** `python -m app.jobs.dispatch_reminders` se ejecuta a
  mano; Cloud Scheduler y el Cloud Run Job son Fase 5.
- **`RATE_LIMIT_PER_MINUTE` es por instancia** (el cubo de fichas es local al proceso).
- **`429` de Telegram: la respuesta se pierde.** Si la Bot API responde `429`, el cliente
  lanza `TelegramRateLimited` con el `retry_after` que indique Telegram, pero **no se espera ni
  se reintenta**: la respuesta de ese turno no se entrega y el `update` queda marcado como
  fallido en el registro. Consumir el `retry_after` dentro del presupuesto (y encolar la
  respuesta cuando no quepa) es trabajo de la Fase 3, junto con el traspaso a la cola de
  reanudación. Se documenta aquí para que sea una limitación conocida y no un
  comportamiento que se descubra en producción.
- **Límite de frecuencia por instancia.** El cubo de fichas es local al proceso, así que el
  límite efectivo es `RATE_LIMIT_PER_MINUTE` multiplicado por el número de instancias.
- **Las imágenes que no sean texto** reciben un aviso que explica qué formatos sí se procesan; las
  fotos no se indexan.

- **La ingesta dentro de la petición tiene un tope de tiempo.** El trabajo en línea se limita al
  menor valor entre `INGEST_INLINE_BUDGET_SECONDS` y una fracción de `REQUEST_BUDGET_SECONDS`; si se
  agota, el documento queda en cola y lo procesa el trabajo en segundo plano (Fase 5).
- **A10 pendiente de validación externa** hasta que se ejecute `scripts/verify_ollama.py` con
  una clave real.

## Seguridad

- El secreto del webhook se compara en tiempo constante y sobre bytes; una cabecera inválida
  devuelve 403 con el cuerpo vacío.
- La identidad es siempre `effective_user.id`. Nunca el nombre visible, el usuario ni el texto.
- Denegación por defecto: sin lista de usuarios, en `production` el servicio no arranca.
- Los secretos no se registran nunca; `LOG_CONTENT_DEBUG=false` es el valor por defecto y ni
  siquiera al activarlo se muestran tokens o claves.
- Ningún secreto real se escribe en ficheros versionados.

## Estructura

```
app/
  main.py               fábrica de la aplicación y cableado de dependencias
  config.py             configuración tipada y validaciones de arranque
  logging_config.py     logs JSON estructurados y redacción
  errors.py             taxonomía de errores tipados
  policy/               funciones puras: autorización y límites
  api/                  webhook de Telegram y endpoints de salud
  bot/                  cliente de la Bot API, handlers, catálogo de textos en español
  agent/                grafo LangGraph, estado, prompts
  llm/                  puertos (protocolos) y cliente de Ollama
  storage/              modelos, migraciones de datos, repositorios, checkpointer y almacenes
  services/             conversación, recordatorios y entrega de mensajes
  tools/                memoria, recordatorios y documentos: esquemas y resultados tipados
  jobs/                 tres jobs de una sola ejecución, sin bucles: recordatorios,
                        ingestas pendientes y reanudación de turnos
migrations/             revisiones de Alembic (0001..0006)
docs/despliegue.md      runbook del despliegue, operación y reversión
scripts/
  deploy.sh             imprime (y solo con aprobación ejecuta) el despliegue completo
  set_webhook.py        registra el webhook sin imprimir nunca el token
  audit_image.py        audita la imagen en busca de secretos
  check_pool_budget.py  puerta del aritmético de conexiones (§11.2)
  migrate.py, bootstrap_db.py, verify_ollama.py, run_local_db.sh
tests/unit/  tests/integration/  tests/evals/  tests/live/
```
