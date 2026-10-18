# Despliegue, operación y reversión — Telegram personal assistant

Este documento es el runbook del despliegue en Google Cloud (PLAN.md §11, spec §13). Está escrito
para ejecutarse **a mano, paso a paso**, o para leerse mientras `scripts/deploy.sh` imprime el plan
que ejecutaría.

**El proyecto es `telegram-assistant-esteban` y todos los comandos lo dicen explícitamente.** En
esta máquina `gcloud config get-value project` responde `presupuesto-esteban`, que es **otro**
proyecto: un comando sin `--project` crearía recursos facturables en el sitio equivocado. Por eso
cada comando de este documento lleva `--project telegram-assistant-esteban`, y por eso
`scripts/deploy.sh` lo lleva en **todas** sus invocaciones además de exportar
`CLOUDSDK_CORE_PROJECT`. Lo comprueba `tests/unit/test_deploy_artifacts.py`, que recorre el script:
no es una convención que haya que recordar.

> **Estado actual del proyecto (2026-10-18, Fase 6).** **Desplegado y operativo**: el servicio
> `taa-prod-api` en `https://taa-prod-api-hzlxaxcstq-ew.a.run.app`, los tres Cloud Run Jobs, los tres
> schedulers habilitados, los cuatro secretos con versión fijada y el webhook registrado. La base de
> datos es **Supabase** (PostgreSQL 17.11 + pgvector 0.8.2, alembic `0007`), ya provisionada y
> migrada. **Cloud SQL no forma parte del despliegue**: la decisión de 2026-10-18 lo sustituyó por
> Supabase. El estado real de cada criterio de aceptación, con sus pruebas, está en
> `docs/informe-final.md`; los incidentes, en `docs/operacion.md`; la seguridad, en
> `docs/seguridad.md`.
>
> Este runbook y `scripts/deploy.sh` son **idempotentes**: cada creación va precedida de su
> comprobación, así que volver a ejecutarlos sobre lo que ya existe no hace nada (y no produce el
> "already exists" con el que moría la primera versión del script). `--from-step N` reanuda a
> mitad si algo falla.
>
> Los criterios de aceptación están casi todos demostrados: (a) y (c) en local, (d) contra el
> servicio desplegado y (e) con el simulacro de reversión del §8 (el camino de código; la
> restauración del esquema sigue dependiendo de una copia). (b) está demostrado por mitades: el
> código es determinista y la mitad del modelo es una tasa medida. Ver `docs/informe-final.md`.

---

## 1. Antes de aprobar nada: el coste

Spec §1 y §13: **no se crea ningún recurso facturable sin aprobación explícita**. El recurso con
coste fijo es **Supabase**, que ya está provisionado (no lo crea este despliegue): en el plan
gratuito **se pausa** por inactividad y **no tiene copias de seguridad**, así que para un asistente
personal que debe responder a cualquier hora la recomendación es el **plan Pro**. Los demás
recursos son céntimos o cero a este volumen.

| Recurso | Nombre | ¿Facturable? | Coste |
|---|---|---|---|
| **Supabase** (PostgreSQL 17.11 + pgvector 0.8.2) | proyecto del usuario, pooler `aws-0-eu-west-1.pooler.supabase.com:5432` | **Sí — coste fijo mensual** | **pendiente de confirmar**; **recomendado el plan Pro** (el gratuito se pausa y no tiene backups) |
| Cloud Run Service (webhook) | `taa-prod-api` | Sí, por petición y por CPU/memoria-segundo; 0 en reposo (`min-instances=0`) | pendiente de confirmar |
| Cloud Run Jobs ×3 | `taa-prod-job-reminders`, `-job-documents`, `-job-resume` | Sí, por ejecución | pendiente de confirmar |
| Cloud Scheduler ×3 | `<job>-tick` | 3 jobs/mes dentro del nivel gratuito | 0 con estas cadencias |
| Artifact Registry | `taa-prod-images` | Sí, por GB almacenado | pendiente de confirmar |
| Cloud Storage | `taa-prod-documents` | Sí, por GB y operaciones | pendiente de confirmar |
| Secret Manager | 4 secretos | Sí, por versión y por acceso | pendiente de confirmar |
| Cloud Logging / métricas | `taa-prod-errors` | Gratis hasta la franquicia | 0–bajo |
| Vertex AI (embeddings) | — | Sí, por llamada | pendiente de confirmar |
| Ollama Cloud | — | Sí, por token (cuenta del usuario) | pendiente de confirmar |
| Telegram Bot API | — | No | — |

**Por qué "pendiente de confirmar" en casi todo:** PLAN.md §12 lo exige — «si un coste no se puede
citar de una lista de precios vigente, se reporta como "coste por confirmar" en vez de estimarlo de
memoria» (spec §1.4 aplica el mismo criterio a las integraciones). Durante esta fase se intentó leer
`https://cloud.google.com/sql/pricing` y la página no devolvió datos de precios utilizables, así que
**no se cita ninguna cifra**. Antes de aprobar, confirma cada línea en:

- Supabase: <https://supabase.com/pricing> (y la calculadora de la propia página)
- Cloud Run y Jobs: <https://cloud.google.com/run/pricing>
- Artifact Registry: <https://cloud.google.com/artifact-registry/pricing>
- Cloud Storage: <https://cloud.google.com/storage/pricing>
- Secret Manager: <https://cloud.google.com/secret-manager/pricing>
- Cloud Scheduler: <https://cloud.google.com/scheduler/pricing>

**El límite que manda es el del pooler, no el del servidor.** El proyecto está detrás de
**Supavisor en modo sesión, puerto 5432** (IPv4: Cloud Run no tiene salida IPv6, así que el host
directo no sirve), y ese modo admite **14 conexiones de cliente** — medido: de la 1 a la 14 entran
en ~0,3 s, y la 15 falla al instante con `EMAXCONNSESSION`. El `max_connections` del servidor es 60 y
es irrelevante aquí: dimensionar contra 60 sobresuscribe el pooler cuatro veces.

```
(DB_POOL_SIZE + CHECKPOINTER_POOL_SIZE) × procesos concurrentes + reserva ≤ 14
(2            + 1                     ) × (1 servicio + 3 jobs = 4)   + 2      = 14  ✔
```

`--max-instances=1` **es obligatorio**, no una elección de coste: con más instancias el producto no
cabe. Los pools desplegados son `DB_POOL_SIZE=2` y `CHECKPOINTER_POOL_SIZE=1`, y se pasan
explícitamente en `--set-env-vars` (los defectos de la aplicación, 4 y 3, darían 7 × 4 + 2 = 30).

El **paso 0** comprueba el aritmético contra `TAA_POOLER_MAX_CONNECTIONS` (14 por defecto) con
`scripts/check_pool_budget.py` y **se niega a seguir** si no cabe, diciendo cuánto puede sumar cada
proceso. Se hace **antes de crear nada**: el pooler no espera, la conexión 15 es un fallo inmediato
en el arranque, no una lentitud.

---

## 2. Requisitos previos

```bash
# Sesión y proyecto. `gcloud auth login` es interactivo y no crea recursos.
gcloud auth login
gcloud config set project telegram-assistant-esteban      # comodidad para el operador
```

`gcloud config set project` es **solo comodidad**: `scripts/deploy.sh` no lee el proyecto por
defecto de `gcloud`, lo lleva escrito (`--project telegram-assistant-esteban` en cada comando).
Tener el defecto bien puesto evita que un `gcloud` suelto, escrito a mano, se vaya al proyecto
equivocado.

Herramientas: `gcloud` (SDK), `docker`, `python` 3.13 con `uv`, y `bash` (Git Bash en Windows). El
script resuelve **él mismo** el intérprete de los scripts auxiliares (`.venv/Scripts/python.exe`,
`.venv/bin/python`, `python3`, `python`, o `TAA_PYTHON` si lo fijas) y lo imprime al empezar: la
validación del entorno depende de la versión de `pydantic-settings` que ese intérprete tenga, y
llamar a un `python` cualquiera del PATH daba un resultado distinto según la máquina.

Y hay que exportar la lista de usuarios autorizados, o el despliegue **se niega a empezar**:

```bash
export TAA_ALLOWED_USER_IDS=123456789    # tu id numérico de Telegram, separados por comas si hay varios
```

### Supabase: lo que ya está hecho y lo que hay que saber

El proyecto de Supabase **ya está provisionado y migrado**, y estas son sus propiedades (medidas,
no supuestas):

- pooler **Supavisor en modo sesión, puerto 5432** (`aws-0-eu-west-1.pooler.supabase.com`), IPv4 —
  Cloud Run no tiene salida IPv6, así que el host directo no se puede usar;
- PostgreSQL **17.11**, pgvector **0.8.2** (por tanto `hnsw.iterative_scan` existe);
- **TLS obligatorio**: la DSN lleva `sslmode=require`;
- rol de aplicación **`taa_app`** (`LOGIN`, no superusuario, sin `createdb` ni `createrole`), dueño
  de las tablas de la aplicación y con `USAGE`+`CREATE` en `public`;
- `vector` está instalado en el esquema **`extensions`**, no en `public`.

En un proyecto Supabase **nuevo**, esas dos últimas cosas no vienen así. Hay que ejecutar, como
administrador del proyecto:

```sql
grant usage on schema extensions to taa_app;
alter database postgres set search_path = public, extensions;
```

y **reconectar** después (el `search_path` de base de datos se aplica a las conexiones nuevas). Sin
esto el despliegue no falla al arrancar: falla en el primer embedding, con un `type "vector" does not
exist` que no dice nada de la causa. Por eso `scripts/check_database.py` lo comprueba antes de crear
nada.

**Plan y copias:** en el plan **gratuito** el proyecto **se pausa** por inactividad y **no tiene
copias de seguridad**. Para un asistente que debe responder a cualquier hora, y para tener un camino
de restauración, la recomendación es el **plan Pro** (copias diarias).

---

## 3. Lo que hace `scripts/deploy.sh`

```bash
bash scripts/deploy.sh                          # IMPRIME el plan; no ejecuta nada
bash scripts/deploy.sh --yes-i-have-approval    # lo ejecuta
bash scripts/deploy.sh --yes-i-have-approval --from-step 8   # reanuda en el paso 8
bash scripts/deploy.sh --dump-env               # el entorno exacto de cada componente
```

- **Imprimir es el comportamiento por defecto.** Sin el flag, el script termina sin tocar Google
  Cloud: es el artefacto que permite aprobar antes de crear.
- **`--yes-i-have-approval` es la aprobación explícita** que exige spec §13.
- **`--from-step N` reanuda** tras un fallo: el plan se imprime entero, pero los pasos anteriores a
  N se marcan `[OMITIDO]` y no se ejecutan (ni sus comprobaciones ni sus lecturas de versión).
- **`--dump-env`** imprime, en un formato comprobable, el entorno y los secretos que reciben los
  cuatro componentes. Es lo que usa `scripts/validate_deploy_env.py`, que a su vez es el paso 0 del
  despliegue: **la validación de arranque de la aplicación se ejecuta antes de crear nada**, así que
  un entorno al que le falte una variable obligatoria se detecta sin gastar un céntimo.
- **La primera creación facturable es el paso 4.** Los pasos 0-3 (comprobaciones, APIs, construcción
  de la imagen y auditoría) no crean nada: un fallo ahí no cuesta nada.
- **Ningún valor secreto se imprime nunca.** El script no lee secretos: los valores los lee `gcloud`
  de un fichero (`--data-file=`) o el terminal (`--prompt-for-password`), así que no son argumentos
  del script, ni están en el historial, ni en `ps`. `scripts/lib/redact.sh` sustituye por
  `<redacted>` cualquier valor conocido y cualquier cosa con forma de credencial (token de Telegram,
  bearer, hex largo, cabecera PEM). `set -x` no se activa en ningún punto.
- Si un comando falla, el script imprime **el comando ya redactado** y se detiene; no sigue a medias.

### Los pasos, en orden

| # | Paso del script | Recurso | Coste |
|---|---|---|---|
| 0 | comprobaciones previas: intérprete, ficheros de secreto, `TAA_ALLOWED_USER_IDS`, **validación de la aplicación**, **aritmético de conexiones** y **conectividad + esquema de Supabase** | ninguno | sin coste |
| 1 | `gcloud services enable …` | habilita las APIs | gratis; habilita lo facturable de después |
| 2 | `docker build` | la imagen, en local | gratis |
| 3 | `scripts/audit_image.py --self-test` + `--image` | auditoría de secretos | sin coste |
| 4 | `artifacts repositories create` + `docker push` | Artifact Registry + imagen | **primera creación facturable**; pendiente de confirmar |
| 5 | `iam service-accounts create` ×2 | `taa-prod-runtime`, `taa-prod-scheduler` | gratis |
| 6 | `secrets create` ×4 + `versions add` + resolver versión | 4 secretos | pendiente de confirmar |
| 7 | `storage buckets create` + binding | bucket privado | pendiente de confirmar |
| 8 | `add-iam-policy-binding` (aiplatform, secretos, jobs) | IAM mínimo | gratis |
| 9 | `run deploy taa-prod-api` (con `--max-instances=1`) | Cloud Run Service | pendiente de confirmar |
| 10 | `run jobs create` ×3 | los tres jobs | pendiente de confirmar |
| 11 | `scheduler jobs create http` ×3 | los tres disparadores | 0 (nivel gratuito) |
| 12 | `logging metrics create` | métrica de errores | gratis hasta la franquicia |
| 13 | `migrate.py upgrade head` | esquema y tablas del checkpointer | sin coste adicional |
| 14 | resuelve la URL y ejecuta `set_webhook.py --set` | registro del webhook | gratis |
| 15 | `set_webhook.py --info` + `curl /readyz` | comprobación de (a) y (d) | gratis |

Cloud SQL **no aparece**: la base de datos es Supabase y ya está provisionada, así que el
despliegue no crea una instancia. El paso 0 comprueba su conectividad y su esquema.

La salida completa, comando por comando, está en el informe de la fase.

---

## 4. Secretos

**Los valores no se pasan como argumento nunca.** El script espera ficheros en
`~/.taa/secrets` (configurable con `TAA_SECRETS_DIR`), en modo 600:

```
~/.taa/secrets/telegram-bot-token        # el token de @BotFather
~/.taa/secrets/telegram-webhook-secret   # 1-256 caracteres de [A-Za-z0-9_-]
~/.taa/secrets/ollama-api-key            # la clave de Ollama Cloud
~/.taa/secrets/database-url              # la DSN de Supabase (pooler, TLS)
```

**Cuatro ficheros, no cinco.** Un solo secreto sirve para `DATABASE_URL` y `DATABASE_SYNC_URL`
(la DSN es la misma cadena, y `postgresql+psycopg` vale para el motor async y para Alembic). La
primera versión de este documento pedía además un `checkpoint-url`: se eliminó porque no lo leía
ningún componente — el checkpointer usa la misma DSN (`app/storage/checkpointing.py`). Si tu
proyecto llegó a tener el secreto `taa-prod-checkpoint-url`, bórralo; estaba creado, versionado y
autorizado, y no lo montaba nadie:

```bash
gcloud secrets delete taa-prod-checkpoint-url --project telegram-assistant-esteban
```

### La DSN de Supabase

El fichero `database-url` contiene la DSN del **pooler**, con el rol de aplicación y TLS:

```
postgresql+psycopg://taa_app.<project-ref>:<PASSWORD>@aws-0-eu-west-1.pooler.supabase.com:5432/postgres?sslmode=require
```

`sslmode=require` **no es opcional** y el host es el del **pooler** (no el directo): Cloud Run no
tiene salida IPv6, y el host directo de Supabase es IPv6. Escríbela en un fichero de modo 600 sin
que la contraseña entre en el historial:

```bash
umask 077
read -r -s -p 'contraseña del rol taa_app: ' SQL_PASSWORD; echo
printf 'postgresql+psycopg://taa_app.<project-ref>:%s@aws-0-eu-west-1.pooler.supabase.com:5432/postgres?sslmode=require' \
  "$SQL_PASSWORD" > ~/.taa/secrets/database-url
unset SQL_PASSWORD
```

Ese mismo valor es el que hay que tener a mano para `scripts/migrate.py` (§5). El pooler se abre en
**modo sesión**; el modo transacción no sirve para este proyecto (el checkpointer y el
`SET LOCAL hnsw.iterative_scan` necesitan una sesión de verdad).

### Rotación

Las versiones se **fijan numéricamente** al desplegar (nunca `latest`): una variable con secreto se
lee una sola vez al arrancar la instancia, así que `latest` congelaría una versión antigua sin
avisar (Apéndice A.8).

```bash
gcloud secrets versions add taa-prod-telegram-bot-token \
  --data-file="$HOME/.taa/secrets/telegram-bot-token" \
  --project telegram-assistant-esteban

gcloud run services update taa-prod-api \
  --update-secrets=TELEGRAM_BOT_TOKEN=taa-prod-telegram-bot-token:2 \
  --region=europe-west1 --project telegram-assistant-esteban
```

---

## 5. Migraciones

Nunca automáticas al arrancar en producción (`MIGRATIONS_AUTORUN=false`): un arranque de la
aplicación no puede ser lo que cambia el esquema (PLAN.md §11.6).

**Esto es el paso 13 de `scripts/deploy.sh`**, no un apunte suelto: sin él `/readyz` responde 503 y
no hay tablas. La base de datos es Supabase y se alcanza por **TCP con TLS** desde cualquier sitio
que tenga salida a internet, así que no hay proxy que montar:

```bash
# La DSN ya está en el fichero del §4; se exporta solo para este comando, sin pasar por argv.
export DATABASE_SYNC_URL="$(cat ~/.taa/secrets/database-url)"

python scripts/migrate.py current                       # dónde estamos
python scripts/migrate.py upgrade head                  # aplicar
python scripts/bootstrap_db.py                          # tablas del checkpointer + verificación
```

Ese `export` es **local y transitorio**: `migrate.py` lee su configuración del entorno, y la regla
de "un secreto nunca viaja como argumento" se cumple igual — el valor no pasa por `argv`, ni por el
historial, ni por `ps`.

**Ojo con el pooler mientras migras:** `migrate.py` abre una conexión, y el límite de Supavisor en
modo sesión es 14. No lances migraciones y el servicio a la vez en un proyecto pequeño; si el
pooler responde `EMAXCONNSESSION`, cierra sesiones y reintenta.

La alternativa es ejecutar el mismo comando **dentro de Cloud Run** (un Cloud Run Job de una sola
vez con la misma imagen, `--command=python --args=-m,…`), que es lo que el plan §11.6 deja abierto.

`bootstrap_db.py` es idempotente y termina verificando: extensión `vector` ≥ 0,8, índice HNSW y
exactamente una fila activa en `embedding_models`.

---

## 6. Registro del webhook

`scripts/set_webhook.py` es el único sitio del despliegue que tiene el token en memoria, porque la
API de Telegram lo lleva en la **URL** (`https://api.telegram.org/bot<TOKEN>/setWebhook`) — la misma
forma del defecto de la Fase 1, donde `httpx` puso un token en una línea de log al reproducir la
URL. Por eso: el token se lee de un **fichero** (`--token-file`), nunca de un argumento; la forma del
comando se imprime con `<redacted>`; y toda ruta de error lo redacta.

```bash
python scripts/set_webhook.py --print \
  --token-file "$HOME/.taa/secrets/telegram-bot-token" \
  --url "https://<URL-DEL-SERVICIO>/telegram/webhook" \
  --secret-file "$HOME/.taa/secrets/telegram-webhook-secret"

python scripts/set_webhook.py --set \
  --token-file "$HOME/.taa/secrets/telegram-bot-token" \
  --url "https://<URL-DEL-SERVICIO>/telegram/webhook" \
  --secret-file "$HOME/.taa/secrets/telegram-webhook-secret"

python scripts/set_webhook.py --info \
  --token-file "$HOME/.taa/secrets/telegram-bot-token"
```

La URL del servicio se obtiene con:

```bash
gcloud run services describe taa-prod-api \
  --region=europe-west1 --format='value(status.url)' \
  --project telegram-assistant-esteban
```

---

## 7. Operación

- **Salud.** En Cloud Run: `GET /health` (liveness) y `GET /readyz` (readiness).
  **No uses `GET /healthz` en Cloud Run**: Google Frontend intercepta las rutas que terminan en `z`
  (known issues de Cloud Run y el issue tracker 544098208), así que la petición nunca llega al
  contenedor y responde un 404 HTML de Google. Localmente `/healthz` sí funciona, y se conserva.
  `/readyz` comprueba la base de
  datos, la extensión `vector` ≥ 0,8 y que exista **una** fila activa en `embedding_models`; no
  revela DSN, nombre de bucket ni versiones. Si está en 503, Telegram no debe entregar.
- **Logs.** JSON estructurado, sin contenido de mensajes, documentos, embeddings ni secretos
  (`LOG_CONTENT_DEBUG=false` por defecto). La métrica `taa-prod-errors` cuenta `severity>=ERROR`.
- **Jobs.** Los tres se disparan por Cloud Scheduler contra la API de administración de Cloud Run con
  un token OAuth; **no hay endpoints internos** (decisión 11). Cada uno imprime una línea
  `JOB_REPORT {...}` con sus contadores, que es lo que se lee desde fuera:

  ```bash
  gcloud run jobs execute taa-prod-job-reminders --region=europe-west1 --project telegram-assistant-esteban
  gcloud run jobs execute taa-prod-job-documents --region=europe-west1 --project telegram-assistant-esteban
  gcloud run jobs execute taa-prod-job-resume    --region=europe-west1 --project telegram-assistant-esteban
  ```

- **Alertas.** Sin canal de notificación no se crea ninguna política (PLAN.md §13.11): es una decisión
  del usuario. El comando queda impreso en el paso 13 del plan.

---

## 8. Reversión (criterio (e))

### Código — volver a la revisión anterior

```bash
gcloud run revisions list --service=taa-prod-api --region=europe-west1 \
  --project telegram-assistant-esteban

gcloud run services update-traffic taa-prod-api \
  --to-revisions=<REVISIÓN-ANTERIOR>=100 \
  --region=europe-west1 --project telegram-assistant-esteban
```

Es instantáneo, no requiere reconstruir la imagen y no toca la base de datos. Los jobs se revierten
igual, apuntándolos a la imagen anterior:

```bash
gcloud run jobs update taa-prod-job-reminders \
  --image=europe-west1-docker.pkg.dev/telegram-assistant-esteban/taa-prod-images/taa-prod-app:<TAG-ANTERIOR> \
  --region=europe-west1 --project telegram-assistant-esteban
```

#### Simulacro ejercitado el 2026-10-18

Se hizo el ciclo completo: 100 % del tráfico a `taa-prod-api-00002-g7q`, comprobaciones, y vuelta a
`taa-prod-api-00003-k2s`. Resultados reales:

| Paso | Comprobación | Resultado |
|---|---|---|
| Bajada a `00002` | `status.traffic` | 100 % `00002`, instantáneo |
| `GET /readyz` en `00002` | base de datos + modelo activo | **200** `{"status":"ready"}` |
| `POST /telegram/webhook` sin secreto en `00002` | el handler está vivo | **403**, cuerpo vacío |
| Vuelta a `00003` | `status.traffic` | 100 % `00003` |
| `GET /health` y `GET /readyz` en `00003` | liveness y readiness | **200** y **200** |
| `set_webhook.py --info` | webhook tras el simulacro | `pending_update_count: 0`, sin `last_error_message` |

**Hallazgo del simulacro:** `00002` es **anterior** al arreglo que añadió `/health`, así que en esa
revisión `GET /health` devuelve **404** (y `/healthz` sigue interceptado por el frontend de Google).
La revisión sí sirve: `/readyz` responde 200 y el webhook atiende, pero **pierde el alias de
liveness**. Para una reversión real, apuntar a una revisión **posterior al arreglo** (`00003` o más)
o usar `/readyz` como comprobación de vida. Volver a `00002` restaura el servicio, no la ruta de
salud.

**Consecuencia operativa del simulacro:** `update-traffic` deja `spec.traffic` **fijado** a la
revisión elegida. Un `gcloud run deploy` posterior respeta ese pin: crea la revisión nueva pero
sigue sirviendo la anterior, y Cloud Run retira la nueva. Ocurrió el 2026-10-18 y `/health` no lo
detectó, porque la revisión vieja estaba sana. `scripts/deploy.sh` ahora ejecuta
`gcloud run services update-traffic … --to-latest` tras desplegar y comprueba que la revisión que
sirve es la última lista. Si se revierte a mano, hay que mover el tráfico explícitamente en el
siguiente despliegue.

### Esquema — una migración destructiva no se "deshace"

Las migraciones son hacia delante en la práctica. `scripts/migrate.py downgrade` **se niega** en
producción sin `--i-have-a-backup`, y el camino honesto es restaurar una copia.

**No hay copias de Cloud SQL porque no hay Cloud SQL.** La restauración depende del plan de Supabase:

- **Plan Pro**: Supabase hace **copias diarias** y se restauran desde el panel del proyecto
  (*Database → Backups*). Es el camino de reversión documentado, y la razón principal por la que el
  plan gratuito no basta para producción.
- **Plan gratuito**: **no hay copias**. La única reversión es haber exportado el esquema y los datos
  a mano (`pg_dump` a través del pooler, en modo sesión) antes de migrar. `migrate.py downgrade`
  sigue negándose sin `--i-have-a-backup` porque esa bandera afirma algo que en este plan es falso.

**Antes de cualquier migración destructiva: confirma que hay una copia.** Una restauración pierde
todo lo escrito desde la copia; el `--i-have-a-backup` de `migrate.py` es una promesa del operador,
no una comprobación.

### Webhook

Si la URL cambia de sitio, el webhook apunta a una revisión que ya no responde:

```bash
python scripts/set_webhook.py --delete --token-file "$HOME/.taa/secrets/telegram-bot-token"
```

o se registra la URL anterior con `--set --url`.

### Recursos

El script no borra nada. Si hay que desmontar el despliegue, cada `create` tiene su `delete`, y
**todos** llevan el proyecto explícito (lo comprueba
`tests/unit/test_deploy_artifacts.py` sobre los bloques de código de este documento, para que nadie
que copie y pegue se vaya al proyecto equivocado). **La base de datos (Supabase) no está en esta lista**: no la crea el despliegue y
desmontarla es un acto aparte (borrar el proyecto desde el panel de Supabase). Lo que sí queda aquí
es todo lo que el despliegue crea en Google Cloud:

La lista es **completa para lo que el despliegue crea** (pasos 4–12): servicio, jobs,
disparadores, bucket, secretos, imagen, cuentas de servicio, métrica y los bindings del paso 8.

```bash
# Service, jobs y schedules
gcloud run services delete taa-prod-api --region=europe-west1 --project telegram-assistant-esteban
gcloud run jobs delete taa-prod-job-reminders --region=europe-west1 --project telegram-assistant-esteban
gcloud run jobs delete taa-prod-job-documents --region=europe-west1 --project telegram-assistant-esteban
gcloud run jobs delete taa-prod-job-resume --region=europe-west1 --project telegram-assistant-esteban
gcloud scheduler jobs delete taa-prod-job-reminders-tick --location=europe-west1 --project telegram-assistant-esteban
gcloud scheduler jobs delete taa-prod-job-documents-tick --location=europe-west1 --project telegram-assistant-esteban
gcloud scheduler jobs delete taa-prod-job-resume-tick --location=europe-west1 --project telegram-assistant-esteban

# Datos y secretos (borrar el bucket se lleva los originales: no hay vuelta atrás).
# **`rm --recursive`, no `buckets delete`**: el bucket tiene documentos dentro y
# `buckets delete` se niega a borrar uno que no esté vacío (409 BucketNotEmpty).
gcloud storage rm --recursive gs://taa-prod-documents --project telegram-assistant-esteban
gcloud storage buckets delete gs://taa-prod-documents --project telegram-assistant-esteban
gcloud secrets delete taa-prod-telegram-bot-token --project telegram-assistant-esteban
gcloud secrets delete taa-prod-telegram-webhook-secret --project telegram-assistant-esteban
gcloud secrets delete taa-prod-ollama-api-key --project telegram-assistant-esteban
gcloud secrets delete taa-prod-database-url --project telegram-assistant-esteban

# Imagen y cuentas de servicio (el repositorio se lleva todas las etiquetas de la imagen)
gcloud artifacts repositories delete taa-prod-images --location=europe-west1 --project telegram-assistant-esteban
gcloud iam service-accounts delete taa-prod-runtime@telegram-assistant-esteban.iam.gserviceaccount.com --project telegram-assistant-esteban
gcloud iam service-accounts delete taa-prod-scheduler@telegram-assistant-esteban.iam.gserviceaccount.com --project telegram-assistant-esteban

# Métrica de logs
gcloud logging metrics delete taa-prod-errors --project telegram-assistant-esteban

# Los bindings del paso 8 también se retiran: borrar el servicio o la cuenta de servicio NO
# limpia las políticas, y quedarían sobre un proyecto sin nada que las use.
gcloud storage buckets remove-iam-policy-binding gs://taa-prod-documents \
  --member=serviceAccount:taa-prod-runtime@telegram-assistant-esteban.iam.gserviceaccount.com \
  --role=roles/storage.objectAdmin --project telegram-assistant-esteban
gcloud projects remove-iam-policy-binding telegram-assistant-esteban \
  --member=serviceAccount:taa-prod-runtime@telegram-assistant-esteban.iam.gserviceaccount.com \
  --role=roles/aiplatform.user --project telegram-assistant-esteban
for secret in taa-prod-telegram-bot-token taa-prod-telegram-webhook-secret \
              taa-prod-ollama-api-key taa-prod-database-url; do
  gcloud secrets remove-iam-policy-binding "$secret" \
    --member=serviceAccount:taa-prod-runtime@telegram-assistant-esteban.iam.gserviceaccount.com \
    --role=roles/secretmanager.secretAccessor --project telegram-assistant-esteban
done
for job in taa-prod-job-reminders taa-prod-job-documents taa-prod-job-resume; do
  gcloud run jobs remove-iam-policy-binding "$job" \
    --member=serviceAccount:taa-prod-scheduler@telegram-assistant-esteban.iam.gserviceaccount.com \
    --role=roles/run.invoker --region=europe-west1 --project telegram-assistant-esteban
done
```

**Antes de borrar `taa-prod-database-url`**, si vas a reconstruir la instancia, guarda la
contraseña: el secreto es lo único que la conserva.

---

## 9. Imagen: construcción y auditoría (criterio (c))

```bash
docker build --no-cache -t telegram-assistant:phase5 .
python scripts/audit_image.py --image telegram-assistant:phase5      # debe terminar en AUDIT OK
python scripts/audit_image.py --self-test                            # prueba que el detector no es inerte
```

La base está **fijada por digest** (`python:3.13-slim@sha256:70729b46…`, resuelto el 2026-10-17), se
construye en dos etapas, corre como usuario sin privilegios (uid 10001), no incluye `.env` ni claves,
y su `CMD` es exec-form para que uvicorn reciba `SIGTERM` y pueda drenar.

La auditoría exporta el sistema de ficheros de la imagen y busca: ficheros que no deben existir
(`.env`, `service-account*.json`, `id_rsa*`), **material** de clave privada (una cabecera PEM seguida
de un cuerpo base64 real: un `.pem` de certificado público no es un hallazgo), un JSON de cuenta de
servicio con `private_key`/`private_key_id`, y un valor centinela si se pasa `--sentinel`.

---

## 10. Estado de los criterios de la Fase 5

| Criterio | Estado | Evidencia |
|---|---|---|
| (a) webhook operativo sin `last_error_message` | **pendiente** | requiere aprobación + `TELEGRAM_BOT_TOKEN`/`TELEGRAM_WEBHOOK_SECRET` reales. Se cierra con el paso 15: `gcloud run services describe taa-prod-api --format='value(status.url)'` y `python scripts/set_webhook.py --info`, y un `/start` desde la cuenta autorizada |
| (b) dos ejecuciones concurrentes del job de recordatorios envían **un** mensaje | **demostrado en local** | dos procesos `python -m app.jobs.dispatch_reminders` en paralelo contra el PostgreSQL local: `JOB_REPORT` con `claimed: 0` y `claimed: 1`, y **una sola** entrega en el fichero del transporte. Repetible: `pytest -m integration tests/integration/test_phase2_db.py::test_two_concurrent_dispatchers_send_exactly_one_reminder`. La variante *desplegada* (Cloud Run Job + Scheduler) queda pendiente |
| (c) la imagen y los logs no contienen secretos | **demostrado en local** | `docker build` + `python scripts/audit_image.py --image telegram-assistant:phase5` → `AUDIT OK`, con `--self-test` probando que el detector detecta, y una imagen de prueba con un centinela que **falla** la auditoría. Los logs: `redact()`/`scrub_text` en `app/logging_config.py`, y el registro del webhook sin el token |
| (d) `/readyz` verde desde Cloud Run y `/healthz` sin filtrar nada | **pendiente** | requiere el despliegue. Los tests de `/readyz` y `/healthz` ya existen y pasan contra la base local |
| (e) reversión documentada y ejecutable | **documentada, pendiente de ejecutar** | §8 de este documento; `gcloud run services update-traffic` y el `--i-have-a-backup` de `scripts/migrate.py` |
