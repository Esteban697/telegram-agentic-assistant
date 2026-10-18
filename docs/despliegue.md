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

> **Estado actual del proyecto (2026-10-17).** Ya existen, creados **a mano y con aprobación**: las
> APIs habilitadas, el repositorio `taa-prod-images`, las dos cuentas de servicio, el bucket
> `taa-prod-documents` con su binding, y la imagen publicada. **No** existe todavía ninguna
> instancia de Cloud SQL, ningún servicio de Cloud Run, ningún job ni ningún schedule.
>
> Este runbook y `scripts/deploy.sh` son **idempotentes**: cada creación va precedida de su
> comprobación, así que volver a ejecutarlos sobre lo que ya existe no hace nada (y no produce el
> "already exists" con el que moría la primera versión del script). `--from-step N` reanuda a
> mitad si algo falla.
>
> Los criterios (a), (d) y (e) siguen **pendientes**: hacen falta `TELEGRAM_BOT_TOKEN`,
> `TELEGRAM_WEBHOOK_SECRET` y `TELEGRAM_ALLOWED_USER_IDS`, que son del usuario. (b) y (c) están
> demostrados en local y se dice cómo. Ver §10 al final.

---

## 1. Antes de aprobar nada: el coste

Spec §1 y §13: **no se crea ningún recurso facturable sin aprobación explícita**. El recurso caro
es **Cloud SQL**, y no escala a cero: existe y se factura mientras exista. Los demás son céntimos o
cero a este volumen.

| Recurso | Nombre | ¿Facturable? | Coste |
|---|---|---|---|
| Cloud SQL PostgreSQL 17 + pgvector | `taa-prod-pg` | **Sí — el dominante** | **pendiente de confirmar** |
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

- Cloud SQL: <https://cloud.google.com/sql/pricing> (y la calculadora de la misma página)
- Cloud Run y Jobs: <https://cloud.google.com/run/pricing>
- Artifact Registry: <https://cloud.google.com/artifact-registry/pricing>
- Cloud Storage: <https://cloud.google.com/storage/pricing>
- Secret Manager: <https://cloud.google.com/secret-manager/pricing>
- Cloud Scheduler: <https://cloud.google.com/scheduler/pricing>

> Referencia **no verificada** (una búsqueda devolvió, de una fuente secundaria de octubre de 2026,
> que el tier `db-custom-1-3840` en `europe-west1` cuesta del orden de 49 USD/mes de cómputo y unos
> 51 USD/mes con 10 GB de SSD). Se incluye solo como punto de partida para la conversación de
> coste: **no la uses para aprobar**. La cifra oficial es la de la calculadora.

El tier recomendado por §11.2 es `db-custom-1-3840` (1 vCPU / 3,75 GB, `max_connections` por defecto
100). El motivo no es el rendimiento, es el **aritmético de conexiones**:

```
(DB_POOL_SIZE + CHECKPOINTER_POOL_SIZE) × procesos concurrentes ≤ max_connections − 5
(4            + 3                     ) × (3 instancias + 3 jobs = 6)  =  42   ≤  100 − 5 = 95  ✔
```

El **paso 0** comprueba el aritmético contra el `max_connections` **documentado** del tier
(`scripts/check_pool_budget.py --max-connections 100 --use-app-defaults`) y **se niega a seguir** si
no cabe, imprimiendo las dos salidas (subir el tier o bajar los pools a 2+2). Se hace **antes de
crear nada**: sobresuscribir un pool no es una lentitud, los últimos procesos en arrancar reciben
`too many clients already` y su trabajo falla.

La medición **real** (`SHOW max_connections`) es el paso 9, y es **no fatal**: necesita una conexión
desde dentro de la VPC (o el Cloud SQL Auth Proxy), que la máquina del operador normalmente no
tiene. Si no puede medir, avisa y sigue — pero después de crear Cloud SQL, así que no es la puerta:
la puerta es el paso 0.

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
| 0 | comprobaciones previas: intérprete, ficheros de secreto, `TAA_ALLOWED_USER_IDS`, **validación de la aplicación** y **aritmético de conexiones** | ninguno | sin coste |
| 1 | `gcloud services enable …` | habilita las APIs | gratis; habilita lo facturable de después |
| 2 | `docker build` | la imagen, en local | gratis |
| 3 | `scripts/audit_image.py --self-test` + `--image` | auditoría de secretos | sin coste |
| 4 | `artifacts repositories create` + `docker push` | Artifact Registry + imagen | **primera creación facturable**; pendiente de confirmar |
| 5 | `iam service-accounts create` ×2 | `taa-prod-runtime`, `taa-prod-scheduler` | gratis |
| 6 | `secrets create` ×4 + `versions add` + resolver versión | 4 secretos | pendiente de confirmar |
| 7 | `storage buckets create` + binding | bucket privado | pendiente de confirmar |
| 8 | `sql instances create` (+ database, user, password) | Cloud SQL 17 | **el dominante**; pendiente de confirmar |
| 9 | `check_pool_budget.py --database-sync-url-file` | medición real de `max_connections` (no fatal) | gratis |
| 10 | `add-iam-policy-binding` (cloudsql, aiplatform, secretos, jobs) | IAM mínimo | gratis |
| 11 | `run deploy taa-prod-api` | Cloud Run Service | pendiente de confirmar |
| 12 | `run jobs create` ×3 | los tres jobs | pendiente de confirmar |
| 13 | `scheduler jobs create http` ×3 | los tres disparadores | 0 (nivel gratuito) |
| 14 | `logging metrics create` | métrica de errores | gratis hasta la franquicia |
| 15 | `migrate.py upgrade head` | esquema y tablas del checkpointer | sin coste adicional |
| 16 | resuelve la URL y ejecuta `set_webhook.py --set` | registro del webhook | gratis |
| 17 | `set_webhook.py --info` + `curl /readyz` | comprobación de (a) y (d) | gratis |

La salida completa, comando por comando, está en el informe de la fase.

---

## 4. Secretos

**Los valores no se pasan como argumento nunca.** El script espera ficheros en
`~/.taa/secrets` (configurable con `TAA_SECRETS_DIR`), en modo 600:

```
~/.taa/secrets/telegram-bot-token        # el token de @BotFather
~/.taa/secrets/telegram-webhook-secret   # 1-256 caracteres de [A-Za-z0-9_-]
~/.taa/secrets/ollama-api-key            # la clave de Ollama Cloud
~/.taa/secrets/database-url              # la DSN del socket de Cloud SQL
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

### La contraseña de Cloud SQL

El paso 8 la fija con `--prompt-for-password`, que la lee del terminal y no la pone nunca en argv.
Si vas a necesitarla otra vez (para reconstruir la DSN, por ejemplo), guárdala tú antes de
escribirla, en un fichero de modo 600 que **tú** creas:

```bash
umask 077
read -r -s -p 'contraseña de Cloud SQL para el usuario taa: ' SQL_PASSWORD; echo
printf '%s' "$SQL_PASSWORD" > ~/.taa/sql-password
unset SQL_PASSWORD
```

La DSN del socket tiene esta forma (confirmada localmente: SQLAlchemy la analiza y psycopg recibe
`host=/cloudsql/…`, ver Apéndice B.5 de PLAN.md):

```
postgresql+psycopg://taa:<PASSWORD>@/taa?host=/cloudsql/telegram-assistant-esteban:europe-west1:taa-prod-pg
```

Créala sin que la contraseña entre en el historial:

```bash
umask 077
python - <<'PY' > ~/.taa/secrets/database-url
import pathlib
password = pathlib.Path.home().joinpath(".taa/sql-password").read_text().strip()
print(
    "postgresql+psycopg://taa:%s@/taa?host=/cloudsql/"
    "telegram-assistant-esteban:europe-west1:taa-prod-pg" % password
)
PY
```

El mismo valor que guardaste en `~/.taa/sql-password` es el que hay que teclear en ese prompt.

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

**Esto es el paso 15 de `scripts/deploy.sh`**, no un apunte suelto: sin él `/readyz` responde 503 y
no hay tablas. Necesita acceso a Cloud SQL, y desde la máquina del operador eso significa el Cloud
SQL Auth Proxy en marcha apuntando a la instancia:

```bash
cloud-sql-proxy telegram-assistant-esteban:europe-west1:taa-prod-pg --port 5432 &

# La DSN local (TCP al proxy), en un fichero de modo 600 — nunca escrita en la línea de comandos.
umask 077
printf 'postgresql+psycopg://taa:%s@127.0.0.1:5432/taa' "$(cat ~/.taa/sql-password)" \
  > ~/.taa/secrets/database-url-local
export DATABASE_SYNC_URL="$(cat ~/.taa/secrets/database-url-local)"

python scripts/migrate.py current                       # dónde estamos
python scripts/migrate.py upgrade head                  # aplicar
python scripts/bootstrap_db.py                          # tablas del checkpointer + verificación
```

Ese `export` es **local y transitorio**: `migrate.py` lee su configuración del entorno, y la regla
de "un secreto nunca viaja como argumento" se cumple igual — el valor no pasa por `argv`, ni por el
historial, ni por `ps`.

La alternativa sin proxy es ejecutar el mismo comando **dentro de la VPC** (un Cloud Run Job de una
sola vez con la misma imagen, `--command=python --args=-m,…`), que es lo que el plan §11.6 deja
abierto: la decisión depende de si prefieres montar el proxy o crear un job efímero.

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

- **Salud.** `GET /healthz` (liveness) y `GET /readyz` (readiness). `/readyz` comprueba la base de
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

### Esquema — una migración destructiva no se "deshace"

Las migraciones son hacia delante en la práctica. `scripts/migrate.py downgrade` **se niega** en
producción sin `--i-have-a-backup`, y el camino honesto es restaurar la copia anterior:

```bash
gcloud sql backups list --instance=taa-prod-pg --project telegram-assistant-esteban
gcloud sql backups restore <BACKUP_ID> --restore-instance=taa-prod-pg \
  --project telegram-assistant-esteban
```

**Antes de cualquier migración destructiva: toma una copia.** Cloud SQL las hace solo, pero la
política de retención del MVP no se ha decidido (PLAN.md §13.7); hasta entonces, la copia manual es
la única garantía, y una restauración pierde todo lo escrito desde la copia.

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
que copie y pegue se vaya al proyecto equivocado). **Cloud SQL es el único que sigue costando
mientras exista**, así que es el primero en la lista si se quiere parar el gasto:

La lista es **completa**: cada `create` de los pasos 4 a 14 tiene aquí su `delete`. Cloud SQL va
primero porque es el único que sigue costando mientras exista.

```bash
# Cloud SQL y sus datos — lo primero si se quiere parar el gasto
gcloud sql users delete taa --instance=taa-prod-pg --project telegram-assistant-esteban
gcloud sql databases delete taa --instance=taa-prod-pg --project telegram-assistant-esteban
gcloud sql instances delete taa-prod-pg --project telegram-assistant-esteban

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

# Los bindings del paso 10 también se retiran: borrar el servicio o la cuenta de servicio NO
# limpia las políticas, y quedarían sobre un proyecto sin nada que las use.
gcloud storage buckets remove-iam-policy-binding gs://taa-prod-documents \
  --member=serviceAccount:taa-prod-runtime@telegram-assistant-esteban.iam.gserviceaccount.com \
  --role=roles/storage.objectAdmin --project telegram-assistant-esteban
gcloud projects remove-iam-policy-binding telegram-assistant-esteban \
  --member=serviceAccount:taa-prod-runtime@telegram-assistant-esteban.iam.gserviceaccount.com \
  --role=roles/cloudsql.client --project telegram-assistant-esteban
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
