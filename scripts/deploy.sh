#!/usr/bin/env bash
# ============================================================================================
#  Despliegue en Google Cloud — Telegram personal assistant (PLAN.md §11, §12; spec §13)
# ============================================================================================
#
# Este script **imprime** todos los comandos `gcloud` y `docker` que haría falta ejecutar. No
# ejecuta nada hasta que se le pasa `--yes-i-have-approval`.
#
#     bash scripts/deploy.sh                          # imprime el plan (no toca nada)
#     bash scripts/deploy.sh --yes-i-have-approval    # ejecuta lo que se acaba de imprimir
#     bash scripts/deploy.sh --from-step 8            # reanuda a partir del paso 8
#     bash scripts/deploy.sh --dump-env               # el entorno exacto de cada componente
#
# Por qué existe el flag (spec §1, §13): "No crear recursos facturables automáticamente sin
# aprobación del usuario". Imprimir es el entregable; ejecutar es una decisión del usuario.
#
# -------------------------------------------------------------------------------------------
#  Cuatro reglas que este script no puede romper, y cómo se garantizan
# -------------------------------------------------------------------------------------------
#
# 1. **El proyecto destino nunca es el de por defecto de `gcloud`.** En esta máquina
#    `gcloud config get-value project` devuelve `presupuesto-esteban`, que es *otro* proyecto. El
#    proyecto está escrito **literalmente** en cada invocación — no hay variable que lo sustituya,
#    así que no hay forma de apuntar a otro sitio ni por accidente ni a propósito — y además se
#    exporta `CLOUDSDK_CORE_PROJECT` por si quedara un `gcloud` suelto. Lo comprueba
#    `tests/unit/test_deploy_artifacts.py`, que recorre este fichero.
#
# 2. **Nada se crea antes de que todo lo gratis esté validado.** Los pasos 0–3 no crean recursos
#    facturables: comprueban las herramientas, los ficheros de secreto, el **entorno exacto que va
#    a recibir cada componente** contra la propia validación de la aplicación, el aritmético de
#    conexiones, y construyen y auditan la imagen. Un fallo antes de la primera creación **no
#    cuesta nada**; antes, la puerta del aritmético corría *después* de crear Cloud SQL y podía
#    dejar la instancia —el recurso dominante— en marcha sin nada que la usara.
#
# 3. **Todo es idempotente y reanudable.** Cada creación va precedida de su comprobación: si el
#    recurso existe, no se toca (y no se intenta un `create` que fallaría con "already exists").
#    `--from-step N` reanuda a mitad. En este proyecto los pasos 1, 4, 5 y 7 **ya se ejecutaron a
#    mano**, así que un script que no se puede correr dos veces no se puede correr ni una.
#
# 4. **Ningún valor secreto se imprime nunca.** Este script no *lee* secretos: los valores se los
#    pasa a `gcloud` como `--data-file=<ruta>` (los lee gcloud) o como `--prompt-for-password`
#    (los lee el terminal), así que nunca son argumentos nuestros, ni aparecen en el historial ni
#    en `ps`. `scripts/lib/redact.sh` es el cinturón que acompaña a esos tirantes: aunque una
#    edición futura pasara un valor en línea, la sustituye por `<redacted>`. `set -x` **no se
#    activa en ningún punto** de este script: el trazado expande los argumentos.
#
# -------------------------------------------------------------------------------------------

# `-E` (errtrace) matters: without it the ERR trap is **not** inherited by shell functions, so a
# command that failed inside `run` would exit the script without printing the redacted last command
# — the failure path is exactly where that message is worth having.
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/.." && pwd)"
# shellcheck source=scripts/lib/redact.sh
. "${HERE}/lib/redact.sh"

# --- Parámetros ----------------------------------------------------------------------------- #
# El proyecto es **literal**: ver la regla 1 de la cabecera.
PROJECT="telegram-assistant-esteban"
REGION="${TAA_REGION:-europe-west1}"
ENV_NAME="${TAA_ENV:-prod}"
PREFIX="taa-${ENV_NAME}"

SERVICE="${PREFIX}-api"
JOB_REMINDERS="${PREFIX}-job-reminders"
JOB_DOCUMENTS="${PREFIX}-job-documents"
JOB_RESUME="${PREFIX}-job-resume"
JOB_BILLING="${PREFIX}-job-billing"
RUNTIME_SA="${PREFIX}-runtime"
SCHEDULER_SA="${PREFIX}-scheduler"
BUCKET="${PREFIX}-documents"
AR_REPO="${PREFIX}-images"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}/${PREFIX}-app"
IMAGE_TAG="${TAA_IMAGE_TAG:-$(date -u +%Y%m%d%H%M%S)}"

# Cinco secretos, aunque `DATABASE_SYNC_URL` y `DATABASE_URL` montan **el mismo** secreto
# (la DSN es la misma cadena; el driver `postgresql+psycopg` sirve para las dos), así que no hay
# un secreto "de checkpointer" que nadie lea. El quinto es el JSON de OAuth de Google Calendar.
SECRET_BOT_TOKEN="${PREFIX}-telegram-bot-token"
SECRET_WEBHOOK="${PREFIX}-telegram-webhook-secret"
SECRET_OLLAMA="${PREFIX}-ollama-api-key"
SECRET_DATABASE_URL="${PREFIX}-database-url"
SECRET_GOOGLE_OAUTH="${PREFIX}-google-oauth-credentials"

# Directorio con los ficheros de valores (modo 600). El script solo usa las **rutas**: ver la
# regla 4. Ver `docs/despliegue.md` §Secretos para cómo se crean.
SECRETS_DIR="${TAA_SECRETS_DIR:-${HOME}/.taa/secrets}"

# Ids de Telegram autorizados. **La aplicación no arranca en producción sin ellos**
# (`TELEGRAM_ALLOWED_USER_IDS` vacío es un error de arranque), así que el despliegue no puede
# inventárselos: si faltan, el paso 0 lo dice y el modo ejecución se niega a seguir.
ALLOWED_USER_IDS="${TAA_ALLOWED_USER_IDS:-}"

# Versiones de secreto: se **fijan numéricamente** (nunca `latest`; Apéndice A.8) y se resuelven
# en el paso 6. Al imprimir se muestra el valor por defecto.
SECRET_VERSION_DEFAULT="${TAA_SECRET_VERSION:-1}"
VERSION_BOT_TOKEN="$SECRET_VERSION_DEFAULT"
VERSION_WEBHOOK="$SECRET_VERSION_DEFAULT"
VERSION_OLLAMA="$SECRET_VERSION_DEFAULT"
VERSION_DATABASE_URL="$SECRET_VERSION_DEFAULT"
VERSION_GOOGLE_OAUTH="$SECRET_VERSION_DEFAULT"

# **La base de datos es Supabase, no Cloud SQL** (decisión del 2026-10-18). El límite que manda
# no es el `max_connections` del servidor (60) sino el del **pooler Supavisor en modo sesión: 14
# conexiones de cliente**, medido — la 15 falla al instante con EMAXCONNSESSION, que es un fallo y
# no una espera. Se declara como entrada para que el aritmético no dependa de un número escondido.
POOLER_MAX_CONNECTIONS="${TAA_POOLER_MAX_CONNECTIONS:-14}"
# `--max-instances=1` es obligatorio: el pooler no admite más. Los procesos concurrentes son el
# servicio más los tres jobs cuando se solapan.
CONCURRENT_PROCESSES="${TAA_CONCURRENT_PROCESSES:-4}"
POOL_RESERVE="${TAA_POOL_RESERVE:-2}"
# Los pools que se despliegan. (2 + 1) x 4 + 2 = 14, exactamente el límite, con la reserva de 2.
DB_POOL_SIZE="${TAA_DB_POOL_SIZE:-2}"
CHECKPOINTER_POOL_SIZE="${TAA_CHECKPOINTER_POOL_SIZE:-1}"
SERVICE_URL=""

export CLOUDSDK_CORE_PROJECT="$PROJECT"

APPROVED=0
FROM_STEP=0
DUMP_ENV=0
LAST_COMMAND=""

usage() {
  cat <<EOF
Uso: bash scripts/deploy.sh [opciones]

  (sin opciones)            imprime el plan completo; no ejecuta nada
  --yes-i-have-approval     ejecuta el plan (crea recursos facturables)
  --from-step N             empieza en el paso N (reanudar tras un fallo)
  --dump-env                imprime el entorno exacto de cada componente y termina
  --help                    esta ayuda

Variables: TAA_REGION (europe-west1), TAA_ENV (prod), TAA_SECRETS_DIR (~/.taa/secrets),
           TAA_ALLOWED_USER_IDS (obligatorio para ejecutar), TAA_IMAGE_TAG.
El proyecto NO es configurable: es telegram-assistant-esteban.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes-i-have-approval) APPROVED=1 ;;
    --from-step)
      shift
      FROM_STEP="${1:-}"
      [[ "$FROM_STEP" =~ ^[0-9]+$ ]] || { printf -- '--from-step necesita un número\n' >&2; exit 2; }
      ;;
    --dump-env) DUMP_ENV=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'argumento desconocido: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# --- Impresión, ejecución y errores ---------------------------------------------------------- #
on_error() {
  local code=$?
  printf '\n*** FALLÓ (código %s). No se ejecuta nada más. ***\n' "$code" >&2
  printf '    último comando: %s\n' "$LAST_COMMAND" >&2
  printf '    Nada de lo anterior se deshace solo: revisa docs/despliegue.md §8.\n' >&2
  printf '    Para reanudar: bash scripts/deploy.sh --yes-i-have-approval --from-step N\n' >&2
  exit "$code"
}
trap on_error ERR

CURRENT_STEP=0
STEP_SKIPPED=0

step() {
  CURRENT_STEP="$1"
  # `--from-step N`: el plan se imprime entero siempre (es el entregable), pero **no se ejecuta**
  # nada de los pasos anteriores a N. Es lo que permite reanudar tras un fallo sin repetir la
  # contraseña de Cloud SQL ni docenas de comprobaciones ya hechas.
  if (( CURRENT_STEP < FROM_STEP )); then
    STEP_SKIPPED=1
  else
    STEP_SKIPPED=0
  fi
  if (( CURRENT_STEP == 0 )); then
    # **El paso 0 no se omite nunca.** Es el que valida el entorno con la validación de la
    # aplicación y el que comprueba el aritmético de conexiones: las dos cosas que deben ocurrir
    # *antes* de crear el recurso dominante. Reanudar en el paso 8 no puede saltárselas, o la
    # promesa de que "un fallo antes de la primera creación no cuesta nada" dejaría de ser cierta
    # justo en el camino (reanudar) para el que la frase se escribió.
    STEP_SKIPPED=0
  fi
  printf '\n=================================================================================\n'
  printf '  PASO %s. %s%s%s\n' "$1" "$2" \
    "$( (( STEP_SKIPPED )) && printf '   [OMITIDO: --from-step %s]' "$FROM_STEP" )" \
    "$( (( CURRENT_STEP == 0 && FROM_STEP > 0 )) && printf '   [SIEMPRE: las comprobaciones gratis no se omiten]' )"
  printf '  coste: %s\n' "$3"
  printf '=================================================================================\n'
}

note() { printf '  %s\n' "$1"; }

fail() { printf '  ERROR: %s\n' "$1" >&2; exit 1; }

run() {
  # $1 = qué hace, $2 = coste, resto = argv
  local what="$1" cost="$2"
  shift 2
  render_into "$@"
  LAST_COMMAND="$RENDERED_COMMAND"
  printf '\n  · %s\n      %s\n      coste: %s\n' "$what" "$RENDERED_COMMAND" "$cost"
  if (( STEP_SKIPPED )); then
    printf '    -> omitido (--from-step %s)\n' "$FROM_STEP"
    return 0
  fi
  if [[ "$APPROVED" -eq 0 ]]; then
    return 0
  fi
  printf '    -> ejecutando\n'
  "$@"
}

run_ensure() {
  # $1 = qué asegura, $2 = coste, $3 = descripción de la comprobación, $4 = función de
  # comprobación (sin argumentos; lee las globales del bucle), resto = argv de creación.
  # Si el recurso ya existe no se toca: la regla 3.
  local what="$1" cost="$2" check_desc="$3" check_fn="$4"
  shift 4
  render_into "$@"
  LAST_COMMAND="$RENDERED_COMMAND"
  printf '\n  · %s\n      si no existe: %s\n      comprobación: %s\n      coste: %s\n' \
    "$what" "$RENDERED_COMMAND" "$check_desc" "$cost"
  if (( STEP_SKIPPED )); then
    printf '    -> omitido (--from-step %s)\n' "$FROM_STEP"
    return 0
  fi
  if [[ "$APPROVED" -eq 0 ]]; then
    return 0
  fi
  if "$check_fn" >/dev/null 2>&1; then
    printf '    -> ya existe; se deja como está\n'
    return 0
  fi
  printf '    -> no existe; creando\n'
  "$@"
}

run_soft() {
  # Igual que `run`, pero un fallo se avisa y **no** aborta el despliegue. Se usa solo donde el
  # trabajo se puede completar después sin coste añadido (las migraciones).
  local what="$1" cost="$2"
  shift 2
  render_into "$@"
  LAST_COMMAND="$RENDERED_COMMAND"
  printf '\n  · %s\n      %s\n      coste: %s\n' "$what" "$RENDERED_COMMAND" "$cost"
  if (( STEP_SKIPPED )); then
    printf '    -> omitido (--from-step %s)\n' "$FROM_STEP"
    return 0
  fi
  if [[ "$APPROVED" -eq 0 ]]; then
    return 0
  fi
  if "$@"; then
    printf '    -> hecho\n'
  else
    printf '    -> FALLÓ; el despliegue continúa. Repítelo con --from-step %s\n' "$CURRENT_STEP" >&2
  fi
  return 0
}

sa_email() { printf '%s@%s.iam.gserviceaccount.com' "$1" "$PROJECT"; }

resolve_python() {
  # **Which interpreter runs the helper scripts is part of the deployment, not an accident.**
  # Calling bare `python` picked up whatever the machine's PATH resolved to — on this one, a
  # different interpreter with a different `pydantic-settings`, where `Settings(**kwargs)` silently
  # drops every keyword and validates the *defaults* instead of the deployment. The failure
  # direction was safe (it aborted) but the mechanism was a coin flip. The virtualenv the project
  # pins comes first, and `scripts/validate_deploy_env.py` additionally proves the environment was
  # actually applied, so a wrong interpreter cannot produce a false `ok`.
  local candidate
  for candidate in \
    "${TAA_PYTHON:-}" \
    "${REPO_ROOT}/.venv/Scripts/python.exe" \
    "${REPO_ROOT}/.venv/bin/python" \
    "$(command -v python3 || true)" \
    "$(command -v python || true)"
  do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      PYTHON="$candidate"
      return 0
    fi
  done
  PYTHON="python"
}

resolve_version() {
  # Imprime el comando y lo ejecuta **una sola vez**, dejando el resultado en `RESOLVED_VERSION`.
  #
  # One invocation on purpose: the earlier version both printed it through `run` and captured it in
  # a separate command substitution, so the command the operator saw and the value that was pinned
  # were two different reads — a version added in between would silently disagree with the plan.
  local secret="$1"
  local query=(
    gcloud secrets versions list "$secret" \
      --project "$PROJECT" \
      --filter='state=ENABLED' \
      --sort-by=~createTime \
      --limit=1 \
      --format='value(name)'
  )
  render_into "${query[@]}"
  LAST_COMMAND="$RENDERED_COMMAND"
  printf '\n  · resolver la versión activa de %s\n      %s\n      coste: gratis\n' \
    "$secret" "$RENDERED_COMMAND"
  RESOLVED_VERSION=""
  if [[ "$APPROVED" -eq 0 ]]; then
    return 0
  fi
  printf '    -> ejecutando\n'
  RESOLVED_VERSION="$("${query[@]}")"
  printf '    -> %s\n' "${RESOLVED_VERSION:-<vacío>}"
}

RUNTIME_SA_EMAIL="$(sa_email "$RUNTIME_SA")"
SCHEDULER_SA_EMAIL="$(sa_email "$SCHEDULER_SA")"

# --- El entorno de los cuatro comandos: **una sola autoridad** ------------------------------- #
# La aplicación valida su configuración al arrancar (`Settings.problems()`), y en `production` esa
# lista exige, entre otras cosas, `TELEGRAM_ALLOWED_USER_IDS`, `TELEGRAM_BOT_TOKEN`,
# `TELEGRAM_WEBHOOK_SECRET` y el par `DATABASE_URL`/`DATABASE_SYNC_URL`. Los cuatro componentes
# reciben **el mismo** entorno, construido aquí una sola vez: cuando los artefactos y la
# aplicación discrepan, lo que no arranca es el servicio (cada petición de Telegram, 502) y los
# tres jobs (una salida con código 1 cada minuto, para siempre). El paso 0 valida exactamente lo
# que hay aquí abajo, con la propia validación de la aplicación.
COMMON_ENV=(
  "APP_ENV=production"
  "GOOGLE_CLOUD_PROJECT=${PROJECT}"
  "GOOGLE_CLOUD_REGION=${REGION}"
  "STORE_BACKEND=postgres"
  "EMBED_PROVIDER=vertex"
  "EMBED_MODEL=gemini-embedding-001"
  "EMBED_DIM=768"
  "GCS_BUCKET=${BUCKET}"
  "MIGRATIONS_AUTORUN=false"
  "NOTIFICATIONS_TRANSPORT=telegram"
  "TELEGRAM_ALLOWED_USER_IDS=${ALLOWED_USER_IDS}"
  # Los pools **desplegados**: sin esto los contenedores usarían los defectos (4 y 3), que son
  # 7 x 4 + 2 = 30 conexiones contra un pooler de 14.
  "DB_POOL_SIZE=${DB_POOL_SIZE}"
  "CHECKPOINTER_POOL_SIZE=${CHECKPOINTER_POOL_SIZE}"
  # Calendar is enabled only when the OAuth credentials secret is present; step 0 requires the
  # file, so a deploy cannot silently ship the integration disabled.
  "GOOGLE_CALENDAR_ENABLED=true"
  "GOOGLE_CALENDAR_ID=primary"
)

common_env_payload() {
  local IFS=,
  printf '%s' "${COMMON_ENV[*]}"
}

common_secrets() {
  printf '%s\n' \
    "TELEGRAM_BOT_TOKEN=${SECRET_BOT_TOKEN}:${VERSION_BOT_TOKEN}" \
    "TELEGRAM_WEBHOOK_SECRET=${SECRET_WEBHOOK}:${VERSION_WEBHOOK}" \
    "OLLAMA_API_KEY=${SECRET_OLLAMA}:${VERSION_OLLAMA}" \
    "DATABASE_URL=${SECRET_DATABASE_URL}:${VERSION_DATABASE_URL}" \
    "DATABASE_SYNC_URL=${SECRET_DATABASE_URL}:${VERSION_DATABASE_URL}" \
    "GOOGLE_OAUTH_CREDENTIALS=${SECRET_GOOGLE_OAUTH}:${VERSION_GOOGLE_OAUTH}"
}

common_secrets_payload() {
  local out="" entry
  while IFS= read -r entry; do
    out="${out:+$out,}$entry"
  done < <(common_secrets)
  printf '%s' "$out"
}

if [[ "$DUMP_ENV" -eq 1 ]]; then
  # El entorno exacto que recibe cada componente, en un formato que se puede comprobar.
  for component in service "$JOB_REMINDERS" "$JOB_DOCUMENTS" "$JOB_RESUME" "$JOB_BILLING"; do
    for entry in "${COMMON_ENV[@]}"; do
      printf 'DUMP %s env %s\n' "$component" "$entry"
    done
    while IFS= read -r entry; do
      printf 'DUMP %s secret %s\n' "$component" "$entry"
    done < <(common_secrets)
  done
  exit 0
fi

# --- Comprobaciones de existencia (para la idempotencia de la regla 3) ----------------------- #
repo_exists() {
  gcloud artifacts repositories describe "$AR_REPO" \
    --location="$REGION" --project "$PROJECT" >/dev/null 2>&1
}
runtime_sa_exists() {
  gcloud iam service-accounts describe "$RUNTIME_SA_EMAIL" --project "$PROJECT" >/dev/null 2>&1
}
scheduler_sa_exists() {
  gcloud iam service-accounts describe "$SCHEDULER_SA_EMAIL" --project "$PROJECT" >/dev/null 2>&1
}
secret_exists() {
  gcloud secrets describe "$secret" --project "$PROJECT" >/dev/null 2>&1
}
bucket_exists() {
  gcloud storage buckets describe "gs://${BUCKET}" --project "$PROJECT" >/dev/null 2>&1
}
job_exists() {
  gcloud run jobs describe "$job" --region="$REGION" --project "$PROJECT" >/dev/null 2>&1
}
schedule_exists() {
  gcloud scheduler jobs describe "$tick" \
    --location="$REGION" --project "$PROJECT" >/dev/null 2>&1
}
metric_exists() {
  gcloud logging metrics describe "${PREFIX}-errors" --project "$PROJECT" >/dev/null 2>&1
}

# --- Cabecera -------------------------------------------------------------------------------- #
printf 'PLAN DE DESPLIEGUE\n'
printf '  proyecto : %s  (literal en cada comando)\n' "$PROJECT"
printf '  región   : %s\n' "$REGION"
printf '  entorno  : %s (prefijo %s)\n' "$ENV_NAME" "$PREFIX"
printf '  imagen   : %s:%s\n' "$IMAGE" "$IMAGE_TAG"
printf '  secretos : %s\n' "$SECRETS_DIR"
if [[ "$APPROVED" -eq 1 ]]; then
  printf '  modo     : EJECUTAR (--yes-i-have-approval)\n'
else
  printf '  modo     : IMPRIMIR — no se ejecuta nada hasta pasar --yes-i-have-approval\n'
fi
if [[ -n "$FROM_STEP" && "$FROM_STEP" -gt 0 ]]; then
  printf '  desde    : paso %s (los anteriores se dan por hechos)\n' "$FROM_STEP"
fi
printf '\n  Regla de coste: los pasos 0-3 no crean nada facturable. Si algo va a fallar, falla\n'
printf '  ahí, y un fallo antes de la primera creación no cuesta nada.\n'

# ============================================================================================ #
step 0 "Comprobaciones previas: nada se crea todavía" "sin coste"
# ============================================================================================ #
for tool in gcloud docker python; do
  if command -v "$tool" >/dev/null 2>&1; then
    note "ok: $tool presente"
  else
    note "FALTA: $tool no está en PATH"
    if [[ "$APPROVED" -eq 1 ]]; then exit 1; fi
  fi
done
resolve_python
note "intérprete para los scripts auxiliares: ${PYTHON}"
note "requisito previo, manual y no facturable: haber iniciado sesión con 'gcloud auth login'."
note "Este script no ejecuta 'gcloud config' ni 'gcloud auth': son comandos que no aceptan"
note "--project, y la regla de este despliegue es que **toda** invocación de gcloud lo lleve."

note ""
note "Ficheros de secreto esperados en ${SECRETS_DIR} (modo 600):"
declare -a SECRET_FILES_NEEDED=(
  "telegram-bot-token" "telegram-webhook-secret" "ollama-api-key" "database-url"
  "google-oauth-credentials"
)
for file in "${SECRET_FILES_NEEDED[@]}"; do
  if [[ -r "${SECRETS_DIR}/${file}" ]]; then
    note "  ok: ${file}"
  else
    note "  FALTA: ${file}"
    if [[ "$APPROVED" -eq 1 ]]; then
      fail "faltan ficheros de secreto en ${SECRETS_DIR} (ver docs/despliegue.md §4)"
    fi
  fi
done

note ""
if [[ -n "$ALLOWED_USER_IDS" ]]; then
  note "ok: TELEGRAM_ALLOWED_USER_IDS=${ALLOWED_USER_IDS}"
else
  note "FALTA: TAA_ALLOWED_USER_IDS (la aplicación NO arranca en producción sin lista de"
  note "      usuarios autorizados). Obtén tu id numérico y exporta:"
  note "      export TAA_ALLOWED_USER_IDS=123456789"
  if [[ "$APPROVED" -eq 1 ]]; then
    fail "TAA_ALLOWED_USER_IDS es obligatorio para desplegar (denegación por defecto)"
  fi
fi

note ""
note "Validación del entorno con la propia validación de la aplicación (Settings.problems()):"
run "validar el entorno de los cuatro componentes" "sin coste" \
  "$PYTHON" "${REPO_ROOT}/scripts/validate_deploy_env.py"

note ""
note "Aritmético de conexiones contra el límite del **pooler** (Supavisor, modo sesión):"
note "(${DB_POOL_SIZE} + ${CHECKPOINTER_POOL_SIZE}) x ${CONCURRENT_PROCESSES} procesos + ${POOL_RESERVE} de reserva <= ${POOLER_MAX_CONNECTIONS}."
run "comprobar el aritmético de conexiones" "sin coste" \
  "$PYTHON" "${REPO_ROOT}/scripts/check_pool_budget.py" \
    --pooler-max-connections "$POOLER_MAX_CONNECTIONS" \
    --db-pool-size "$DB_POOL_SIZE" \
    --checkpointer-pool-size "$CHECKPOINTER_POOL_SIZE" \
    --concurrent-processes "$CONCURRENT_PROCESSES" \
    --reserve "$POOL_RESERVE"

note ""
note "Base de datos Supabase: conectividad (select 1) y esquema vectorial (pgvector >= 0.8 en el"
note "esquema 'extensions', y el tipo resoluble desde el rol de la aplicación). Las dos cosas"
note "fallan en el primer embedding si están mal, así que se comprueban antes de crear nada."
run "comprobar conectividad y esquema de Supabase" "sin coste" \
  "$PYTHON" "${REPO_ROOT}/scripts/check_database.py" \
    --database-url-file "${SECRETS_DIR}/database-url"

# ============================================================================================ #
step 1 "Habilitar las APIs necesarias" \
     "gratis por sí mismo; habilita productos facturables de los pasos siguientes"
# ============================================================================================ #
note "APIs: run, sqladmin, secretmanager, artifactregistry, cloudscheduler, storage, logging,"
note "monitoring, aiplatform, calendar-json y bigquery (habilitar es idempotente)."
run "habilitar APIs (idempotente, no crea ningún recurso facturable)" "gratis" \
  gcloud services enable \
    run.googleapis.com \
    sqladmin.googleapis.com \
    secretmanager.googleapis.com \
    artifactregistry.googleapis.com \
    cloudscheduler.googleapis.com \
    storage.googleapis.com \
    logging.googleapis.com \
    monitoring.googleapis.com \
    aiplatform.googleapis.com \
    calendar-json.googleapis.com \
    bigquery.googleapis.com \
    --project "$PROJECT"

# ============================================================================================ #
step 2 "Construir la imagen (en local, gratis)" "local: gratis"
# ============================================================================================ #
note "Se construye **antes** de crear el repositorio: un Dockerfile roto es el fallo más común y"
note "no debe costar un recurso. La base está fijada por digest, así que el mismo fichero produce"
note "la misma imagen."
run "construir la imagen" "local: gratis" \
  docker build --no-cache -t "${IMAGE}:${IMAGE_TAG}" -t "${IMAGE}:latest" "$REPO_ROOT"

# ============================================================================================ #
step 3 "Auditar la imagen en busca de secretos (aceptación (c))" "sin coste"
# ============================================================================================ #
run "probar el detector de la auditoría" "sin coste" \
  "$PYTHON" "${REPO_ROOT}/scripts/audit_image.py" --self-test
run "auditar la imagen construida" "sin coste" \
  "$PYTHON" "${REPO_ROOT}/scripts/audit_image.py" --image "${IMAGE}:${IMAGE_TAG}"

# ============================================================================================ #
step 4 "Repositorio de imágenes (Artifact Registry) y publicación" \
     "facturable: almacenamiento por GB (pendiente de confirmar la tarifa vigente)"
# ============================================================================================ #
note "PRIMERA CREACIÓN FACTURABLE DEL PLAN. Todo lo que se podía validar sin gastar, ya está"
note "validado en los pasos 0-3."
run_ensure "crear el repositorio Docker (si no existe)" "pendiente de confirmar" \
  "gcloud artifacts repositories describe ${AR_REPO}" repo_exists \
  gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker \
    --location="$REGION" \
    --description="Imágenes del asistente personal (taa)" \
    --project "$PROJECT"
run "autenticar docker contra Artifact Registry" "gratis" \
  gcloud auth configure-docker "${REGION}-docker.pkg.dev" --project "$PROJECT"
run "publicar la imagen" "facturable (GB almacenados)" \
  docker push "${IMAGE}:${IMAGE_TAG}"

# ============================================================================================ #
step 5 "Cuentas de servicio" "gratis"
# ============================================================================================ #
note "La identidad de ejecución NO usa clave: Cloud Run usa la identidad de la cuenta de"
note "servicio directamente (spec §12/§13: ninguna clave de servicio en la imagen)."
run_ensure "crear la cuenta de servicio de ejecución (si no existe)" "gratis" \
  "gcloud iam service-accounts describe ${RUNTIME_SA_EMAIL}" runtime_sa_exists \
  gcloud iam service-accounts create "$RUNTIME_SA" \
    --display-name="Ejecución del asistente (taa)" \
    --project "$PROJECT"
run_ensure "crear la cuenta de servicio de Scheduler (si no existe)" "gratis" \
  "gcloud iam service-accounts describe ${SCHEDULER_SA_EMAIL}" scheduler_sa_exists \
  gcloud iam service-accounts create "$SCHEDULER_SA" \
    --display-name="Cloud Scheduler (taa)" \
    --project "$PROJECT"

# ============================================================================================ #
step 6 "Secret Manager: cinco secretos y su versión" \
     "facturable: céntimos/mes por secreto y por operación de acceso (pendiente de confirmar)"
# ============================================================================================ #
note "Los valores NO se pasan como argumento: gcloud los lee del fichero (--data-file)."
note "Un solo secreto sirve para DATABASE_URL y DATABASE_SYNC_URL: la DSN es la misma cadena."
for secret in "$SECRET_BOT_TOKEN" "$SECRET_WEBHOOK" "$SECRET_OLLAMA" "$SECRET_DATABASE_URL" "$SECRET_GOOGLE_OAUTH"; do
  run_ensure "crear el secreto ${secret} (si no existe)" "pendiente de confirmar" \
    "gcloud secrets describe ${secret}" secret_exists \
    gcloud secrets create "$secret" \
      --replication-policy=automatic \
      --project "$PROJECT"
done

declare -A SECRET_FILES=(
  ["$SECRET_BOT_TOKEN"]="${SECRETS_DIR}/telegram-bot-token"
  ["$SECRET_WEBHOOK"]="${SECRETS_DIR}/telegram-webhook-secret"
  ["$SECRET_OLLAMA"]="${SECRETS_DIR}/ollama-api-key"
  ["$SECRET_DATABASE_URL"]="${SECRETS_DIR}/database-url"
  ["$SECRET_GOOGLE_OAUTH"]="${SECRETS_DIR}/google-oauth-credentials"
)
for secret in "$SECRET_BOT_TOKEN" "$SECRET_WEBHOOK" "$SECRET_OLLAMA" "$SECRET_DATABASE_URL" "$SECRET_GOOGLE_OAUTH"; do
  # `versions add` es aditivo y por tanto idempotente en el sentido que importa: añade una versión
  # nueva y las anteriores siguen existiendo. Reejecutarlo es seguro; lo que **no** se hace es
  # montar `latest`, porque una versión se congela al arrancar la instancia.
  run "añadir una versión de ${secret} desde ${SECRET_FILES[$secret]}" \
      "facturable (pendiente de confirmar)" \
    gcloud secrets versions add "$secret" \
      --data-file="${SECRET_FILES[$secret]}" \
      --project "$PROJECT"
done

# Las versiones se resuelven aquí y las usan los pasos 11-13. Nunca `latest`.
#
# **Siempre, también con `--from-step` por encima del 6.** Una versión por omisión de 1 no es
# inocua: rota el token (versión 2 nueva, la 1 antigua, ambas habilitadas), reanuda un despliegue
# fallido en cualquier paso posterior al 6 — que es exactamente lo que este flag existe para
# hacer — y el servicio y los tres jobs quedan montados sobre la versión **antigua** mientras el
# despliegue informa de éxito. Es una lectura gratis y los pasos 11-13 necesitan el número.
declare -A SECRET_VERSIONS=()
for secret in "$SECRET_BOT_TOKEN" "$SECRET_WEBHOOK" "$SECRET_OLLAMA" "$SECRET_DATABASE_URL" "$SECRET_GOOGLE_OAUTH"; do
  if [[ -n "${TAA_SECRET_VERSION:-}" ]]; then
    SECRET_VERSIONS["$secret"]="$TAA_SECRET_VERSION"
    continue
  fi
  resolve_version "$secret"          # imprime el comando y lo ejecuta **una sola vez**
  if [[ "$APPROVED" -eq 1 ]]; then
    [[ -n "$RESOLVED_VERSION" ]] || fail \
      "no se pudo resolver la versión activa de ${secret}. Crea el secreto (paso 6) o fija una
      versión explícita: TAA_SECRET_VERSION=<N> bash scripts/deploy.sh …"
    SECRET_VERSIONS["$secret"]="$RESOLVED_VERSION"
  else
    SECRET_VERSIONS["$secret"]="$SECRET_VERSION_DEFAULT"
  fi
done
if [[ -n "${TAA_SECRET_VERSION:-}" ]]; then
  note "TAA_SECRET_VERSION=${TAA_SECRET_VERSION}: versión explícita para los cuatro secretos."
else
  note "versiones resueltas de Secret Manager (nunca latest)."
fi

VERSION_BOT_TOKEN="${SECRET_VERSIONS[$SECRET_BOT_TOKEN]:-$SECRET_VERSION_DEFAULT}"
VERSION_WEBHOOK="${SECRET_VERSIONS[$SECRET_WEBHOOK]:-$SECRET_VERSION_DEFAULT}"
VERSION_OLLAMA="${SECRET_VERSIONS[$SECRET_OLLAMA]:-$SECRET_VERSION_DEFAULT}"
VERSION_DATABASE_URL="${SECRET_VERSIONS[$SECRET_DATABASE_URL]:-$SECRET_VERSION_DEFAULT}"
VERSION_GOOGLE_OAUTH="${SECRET_VERSIONS[$SECRET_GOOGLE_OAUTH]:-$SECRET_VERSION_DEFAULT}"
note "versiones fijadas: bot-token:${VERSION_BOT_TOKEN} webhook:${VERSION_WEBHOOK} ollama:${VERSION_OLLAMA} database-url:${VERSION_DATABASE_URL} google-oauth:${VERSION_GOOGLE_OAUTH}"

# ============================================================================================ #
step 7 "Bucket privado de documentos" \
     "facturable: almacenamiento y operaciones por GB (pendiente de confirmar)"
# ============================================================================================ #
run_ensure "crear el bucket (si no existe)" "pendiente de confirmar" \
  "gcloud storage buckets describe gs://${BUCKET}" bucket_exists \
  gcloud storage buckets create "gs://${BUCKET}" \
    --location="$REGION" \
    --uniform-bucket-level-access \
    --project "$PROJECT"

# El permiso se concede **en el bucket**, no en el proyecto: es el mismo conjunto de permisos con
# el alcance mínimo posible y evita mantener un rol personalizado para un despliegue de un solo
# bucket (PLAN.md §11.4 lo prefería como rol personalizado; queda registrado como desviación
# deliberada). Añadir un binding es idempotente: repetirlo no cambia nada.
run "dar al runtime acceso de objetos solo en este bucket" "gratis" \
  gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
    --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
    --role="roles/storage.objectAdmin" \
    --project "$PROJECT"

# ============================================================================================ #
step 8 "IAM de mínimo privilegio" "gratis"
# ============================================================================================ #
note "Runtime: aiplatform.user (Vertex: sin él, **todo** embedding —ingesta y búsqueda— recibe"
note "y aparece como EmbeddingUnavailable), secretmanager.secretAccessor **por secreto**, y el"
note "acceso al bucket del paso 7."
note "403). **No hay 'cloudsql.client'**: la base de datos es Supabase y se alcanza por TCP con"
note "TLS, no por el socket de Cloud SQL."
note "NO se concede: editor, owner, storage.admin, ni secretVersionManager"
note "(rotar es un acto de despliegue, no de ejecución)."
note "**Tampoco se concede iam.serviceAccountUser a la cuenta de Scheduler**: los jobs ya se"
note "ejecutan *como* la cuenta de runtime, y actAs sobre ella dejaría que un segundo principal"
note "se pusiera su identidad, que puede leer los cuatro secretos y la base de datos. Quien crea"
note "los schedules es el operador, que ya tiene ese permiso sobre la cuenta de Scheduler."
run "runtime: usuario de Vertex AI (embeddings)" "gratis" \
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
    --role="roles/aiplatform.user" \
    --condition=None \
    --project "$PROJECT"
run "runtime: ejecutar consultas de BigQuery (informe de gastos)" "gratis" \
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
    --role="roles/bigquery.jobUser" \
    --condition=None \
    --project "$PROJECT"
run "runtime: leer el dataset de facturación" "gratis" \
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
    --role="roles/bigquery.dataViewer" \
    --condition=None \
    --project "$PROJECT"
for secret in "$SECRET_BOT_TOKEN" "$SECRET_WEBHOOK" "$SECRET_OLLAMA" "$SECRET_DATABASE_URL" "$SECRET_GOOGLE_OAUTH"; do
  run "runtime: leer el secreto ${secret}" "gratis" \
    gcloud secrets add-iam-policy-binding "$secret" \
      --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
      --role="roles/secretmanager.secretAccessor" \
      --project "$PROJECT"
done
# The scheduler's `run.invoker` bindings live in step 10, beside the jobs they name: a binding on a
# job that does not exist yet fails with NOT_FOUND — which is exactly what the first live deploy hit.

# ============================================================================================ #
step 9 "Cloud Run Service (el webhook)" \
     "facturable por petición y por CPU/memoria-segundo; 0 cuando está inactivo (min-instances=0)"
# ============================================================================================ #
note "min-instances=0 (escala a cero) y cpu-throttling por defecto (solo se paga CPU durante la"
note "petición): es lo que hace segura la decisión 2 del plan."
note "**max-instances=1 no es una elección de coste**: el pooler de Supabase admite 14 conexiones"
note "de cliente en modo sesión y el aritmético del paso 0 está calculado para una instancia más"
note "los cuatro jobs."
note "--allow-unauthenticated SÍ: Telegram no puede enviar credenciales de IAM; la autenticación"
note "es el secreto del webhook (spec §13)."
note "gcloud run deploy crea o actualiza: es idempotente por sí solo."
run "desplegar el servicio" "facturable (pendiente de confirmar)" \
  gcloud run deploy "$SERVICE" \
    --image="${IMAGE}:${IMAGE_TAG}" \
    --region="$REGION" \
    --platform=managed \
    --cpu=1 \
    --memory=512Mi \
    --concurrency=8 \
    --timeout=300 \
    --min-instances=0 \
    --max-instances=1 \
    --allow-unauthenticated \
    --service-account="$RUNTIME_SA_EMAIL" \
    --set-env-vars="$(common_env_payload)" \
    --set-secrets="$(common_secrets_payload)" \
    --project "$PROJECT"

# Un simulacro de reversión (docs/despliegue.md §8) deja `spec.traffic` fijado a una revisión.
# `gcloud run deploy` respeta ese pin: crea la revisión nueva, pero sigue sirviendo la anterior y
# Cloud Run retira la nueva. Ocurrió el 2026-10-18: el despliegue salió con código 0, `/health`
# pasó (la revisión vieja estaba sana) y el bot seguía con la imagen anterior. Desplegar tiene que
# significar servir lo que se acaba de construir, así que el pin se limpia explícitamente.
run "asegurar que el tráfico sirve la revisión recién desplegada" "gratis" \
  gcloud run services update-traffic "$SERVICE" \
    --to-latest \
    --region="$REGION" \
    --project "$PROJECT"

# ============================================================================================ #
step 10 "Los cuatro Cloud Run Jobs" "facturable por ejecución; fracciones de céntimo"
# ============================================================================================ #
note "Un binario, cinco comandos (decisión 18): los jobs reutilizan la misma imagen y el mismo"
note "entorno que el servicio — si el entorno no valida, no arrancan."
note "Cada job es de una sola pasada: escanea, reclama con FOR UPDATE SKIP LOCKED, trabaja y"
note "termina. Ninguno depende de un temporizador en memoria (spec §11, spec §17)."
note "El job de facturación no abre conexiones a la base: el aritmético del pooler sigue contando"
note "cuatro procesos con base de datos (servicio + tres jobs)."

declare -A JOB_MODULE=(
  ["$JOB_REMINDERS"]="app.jobs.dispatch_reminders"
  ["$JOB_DOCUMENTS"]="app.jobs.process_documents"
  ["$JOB_RESUME"]="app.jobs.resume_work"
  ["$JOB_BILLING"]="app.jobs.billing_report"
)
declare -A JOB_TIMEOUT=(
  ["$JOB_REMINDERS"]="300"
  ["$JOB_DOCUMENTS"]="900"
  ["$JOB_RESUME"]="300"
  ["$JOB_BILLING"]="300"
)
for job in "$JOB_REMINDERS" "$JOB_DOCUMENTS" "$JOB_RESUME" "$JOB_BILLING"; do
  run_ensure "crear el job ${job} (si no existe)" "facturable (pendiente de confirmar)" \
    "gcloud run jobs describe ${job}" job_exists \
    gcloud run jobs create "$job" \
      --image="${IMAGE}:${IMAGE_TAG}" \
      --region="$REGION" \
      --command=python \
      --args="-m,${JOB_MODULE[$job]}" \
      --tasks=1 \
      --max-retries=1 \
      --task-timeout="${JOB_TIMEOUT[$job]}" \
      --service-account="$RUNTIME_SA_EMAIL" \
      --set-env-vars="$(common_env_payload)" \
      --set-secrets="$(common_secrets_payload)" \
      --project "$PROJECT"
done
# `run.invoker` must come **after** the job exists — binding it earlier fails with NOT_FOUND.
# `add-iam-policy-binding` is idempotent, so a re-run is harmless.
for job in "$JOB_REMINDERS" "$JOB_DOCUMENTS" "$JOB_RESUME" "$JOB_BILLING"; do
  run "scheduler: invocar el job ${job}" "gratis" \
    gcloud run jobs add-iam-policy-binding "$job" \
      --member="serviceAccount:${SCHEDULER_SA_EMAIL}" \
      --role="roles/run.invoker" \
      --region="$REGION" \
      --project "$PROJECT"
done

# ============================================================================================ #
step 11 "Cloud Scheduler (dispara los jobs con un token OAuth)" \
     "4 jobs/mes: 3 dentro del nivel gratuito; el cuarto ~0,10 $/mes"
# ============================================================================================ #
note "El destino es la API de administración de Cloud Run, no una URL secreta (spec §13)."
note "No existe ningún endpoint interno público: los health endpoints son los únicos otros caminos."
note "El rol exacto que puede invocar un job está en el Apéndice B.6 del plan: el candidato es"
note "roles/run.invoker y se confirma con 'gcloud scheduler jobs create http --help'."

declare -A SCHEDULE_CRON=(
  ["$JOB_REMINDERS"]="* * * * *"
  ["$JOB_DOCUMENTS"]="*/5 * * * *"
  ["$JOB_RESUME"]="* * * * *"
  ["$JOB_BILLING"]="0 8 * * *"
)
declare -A SCHEDULE_TIMEZONE=(
  ["$JOB_BILLING"]="Europe/Madrid"
)
for job in "$JOB_REMINDERS" "$JOB_DOCUMENTS" "$JOB_RESUME" "$JOB_BILLING"; do
  tick="${job}-tick"
  run_ensure "crear el schedule ${tick} (si no existe)" "0 (nivel gratuito)" \
    "gcloud scheduler jobs describe ${tick}" schedule_exists \
    gcloud scheduler jobs create http "$tick" \
      --schedule="${SCHEDULE_CRON[$job]}" \
      --time-zone="${SCHEDULE_TIMEZONE[$job]:-UTC}" \
      --location="$REGION" \
      --uri="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${job}:run" \
      --http-method=POST \
      --oauth-service-account-email="$SCHEDULER_SA_EMAIL" \
      --max-retry-attempts=1 \
      --project "$PROJECT"
done

# ============================================================================================ #
step 12 "Observabilidad: métrica de logs y política de alerta" \
     "Cloud Logging es gratis hasta la franquicia; los canales de notificación son aparte"
# ============================================================================================ #
run_ensure "crear la métrica de errores (si no existe)" "gratis hasta la franquicia" \
  "gcloud logging metrics describe ${PREFIX}-errors" metric_exists \
  gcloud logging metrics create "${PREFIX}-errors" \
    --description="Errores registrados por el asistente" \
    --log-filter="resource.type=cloud_run_revision AND severity>=ERROR" \
    --project "$PROJECT"
note "La política de alerta necesita un CANAL DE NOTIFICACIÓN, que es una decisión del usuario"
note "(PLAN.md §13.11). Cuando exista, se añade con:"
note "  gcloud alpha monitoring policies create --notification-channels=CHANNEL --policy-from-file=POLICY.json --project ${PROJECT}"

# ============================================================================================ #
step 13 "Migraciones: el paso que pone /readyz en verde" "sin coste adicional"
# ============================================================================================ #
note "MIGRATIONS_AUTORUN=false en todo el despliegue, así que el esquema NO se crea solo: sin"
note "este paso, /readyz responde 503 y no hay tablas. La base de datos es Supabase y se alcanza"
note "por TCP con TLS (session mode, puerto 5432), así que no hace falta ningún proxy:"
note "  export DATABASE_SYNC_URL=\$(cat ${SECRETS_DIR}/database-url)"
note "El valor no pasa por argv; el export es local y transitorio."
note "Ojo con el pooler: migrate.py abre una conexión más y el límite es ${POOLER_MAX_CONNECTIONS}."
run_soft "aplicar las migraciones y crear las tablas del checkpointer" "sin coste adicional" \
  "$PYTHON" "${REPO_ROOT}/scripts/migrate.py" upgrade head

# ============================================================================================ #
step 14 "Registrar el webhook de Telegram" "gratis"
# ============================================================================================ #
note "La URL se resuelve **del propio Cloud Run** en tiempo de ejecución; en modo imprimir no se"
note "puede conocer, así que se muestra el comando que la obtiene."
# Se resuelve **siempre** en modo ejecución, también con --from-step: es una lectura gratis, y
# saltársela dejaría a los pasos 16 y 17 usando un marcador de posición.
if [[ "$APPROVED" -eq 1 ]]; then
  url_query=(
    gcloud run services describe "$SERVICE" \
      --region="$REGION" \
      --format='value(status.url)' \
      --project "$PROJECT"
  )
  render_into "${url_query[@]}"
  LAST_COMMAND="$RENDERED_COMMAND"
  printf '\n  · resolver la URL del servicio\n      %s\n      coste: gratis\n' "$RENDERED_COMMAND"
  printf '    -> ejecutando\n'
  SERVICE_URL="$("${url_query[@]}")"
  [[ -n "$SERVICE_URL" ]] || fail "no se pudo obtener la URL del servicio"
  printf '    -> %s\n' "$SERVICE_URL"
else
  render_into gcloud run services describe "$SERVICE" --region="$REGION" \
    --format='value(status.url)' --project "$PROJECT"
  printf '\n  · resolver la URL del servicio (en tiempo de ejecución)\n      %s\n      coste: gratis\n' \
    "$RENDERED_COMMAND"
  SERVICE_URL="<URL-DEL-SERVICIO>"
fi

note "El token NO se pasa como argumento: 'scripts/set_webhook.py' lo lee de un fichero, imprime"
note "la forma del comando con el token sustituido por <redacted>, y su ruta de error lo redacta."
run "registrar el webhook (--set, con la URL resuelta)" "gratis" \
  "$PYTHON" "${REPO_ROOT}/scripts/set_webhook.py" \
    --set \
    --token-file="${SECRETS_DIR}/telegram-bot-token" \
    --url="${SERVICE_URL}/telegram/webhook" \
    --secret-file="${SECRETS_DIR}/telegram-webhook-secret"

# ============================================================================================ #
step 15 "Comprobación posterior (aceptación (a) y (d))" "gratis"
# ============================================================================================ #
note "Estos comandos cierran (a) y (d). Se imprimen para que el despliegue no se declare bueno"
note "sin ejecutarlos: si (a) no se cumple, el bot no recibe nada."
run "estado del webhook (sin last_error_message)" "gratis" \
  "$PYTHON" "${REPO_ROOT}/scripts/set_webhook.py" \
    --info \
    --token-file="${SECRETS_DIR}/telegram-bot-token"
run "liveness del servicio (/health: el frontend de Cloud Run intercepta las rutas que acaban en z)" "gratis" \
  curl -fsS "${SERVICE_URL}/health"
run "readiness del servicio" "gratis" \
  curl -fsS "${SERVICE_URL}/readyz"

# Las comprobaciones anteriores prueban la URL, no la revisión que la sirve. Un pin de tráfico
# (por ejemplo, tras un simulacro de reversión) haría que `/health` pasara con la imagen vieja.
if (( APPROVED )) && (( ! STEP_SKIPPED )); then
  SERVING_REVISION="$(gcloud run services describe "$SERVICE" \
    --region="$REGION" --format='value(status.traffic[0].revisionName)' --project "$PROJECT")"
  READY_REVISION="$(gcloud run services describe "$SERVICE" \
    --region="$REGION" --format='value(status.latestReadyRevisionName)' --project "$PROJECT")"
  if [[ "$SERVING_REVISION" != "$READY_REVISION" ]]; then
    fail "el servicio sirve ${SERVING_REVISION}, pero la última revisión lista es ${READY_REVISION}"
  fi
  note "ok: la revisión que sirve es la última lista (${SERVING_REVISION})"
fi

note "y una prueba manual: enviar /start desde la cuenta autorizada."

# ============================================================================================ #
printf '\n=================================================================================\n'
if [[ "$APPROVED" -eq 1 ]]; then
  printf '  Despliegue ejecutado. Revisa la salida anterior y docs/despliegue.md §7.\n'
else
  printf '  FIN DEL PLAN. No se ha ejecutado nada y no se ha creado ningún recurso.\n'
  printf '  Para ejecutarlo: bash scripts/deploy.sh --yes-i-have-approval\n'
  printf '  Los pasos 0-3 no crean nada facturable: ahí es donde debe fallar si algo va mal.\n'
  printf '  Coste: docs/despliegue.md §1 marca qué está citado y qué queda por confirmar.\n'
fi
printf '=================================================================================\n'
