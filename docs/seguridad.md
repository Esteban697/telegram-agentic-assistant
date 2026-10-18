# Seguridad y política de aprobación — Telegram personal assistant

Este documento es el entregable de la spec §18.8: el modelo de seguridad y la política de
aprobación, tal como están **implementados** (no como se describieron en el plan). Cada afirmación
nombra el módulo y la prueba que la sostiene.

---

## 1. Modelo de confianza

| Elemento | Confianza | Qué implica |
|---|---|---|
| El **usuario autorizado** (uno solo) | de confianza, tras autenticarse | puede pedir todo lo que la política permite |
| Cualquier **otro usuario de Telegram** | no | se deniega por defecto, sin llamar al modelo y sin responder contenido |
| El **texto de los documentos** | **datos**, nunca instrucciones | puede contener órdenes; no puede cambiar políticas ni autorizar acciones |
| La **salida del modelo** | propuesta | el backend decide permisos y efectos; el modelo no ejecuta nada por sí solo |
| Los **proveedores** (Ollama, Vertex, Telegram) | no fiables en sus mensajes | sus textos de error no se registran ni se muestran tal cual |
| Los **nombres de archivo y el texto de Telegram** | no fiables | se sanean y se escapan antes de entrar en cualquier prompt o ruta |

---

## 2. Validación del webhook

- **Cabecera `X-Telegram-Bot-Api-Secret-Token`**, comparada con `hmac.compare_digest` **sobre
  bytes** (`app/api/telegram_webhook.py::_secret_matches`). La comparación es en tiempo constante; se
  codifica con `surrogateescape` para que una cabecera no ASCII produzca un **403 limpio** y no un
  500 (que haría que Telegram reintentara).
- Una petición sin secreto o con secreto incorrecto recibe **403 con cuerpo vacío**; el valor
  presentado no se registra nunca.
- **Tamaño**: `MAX_WEBHOOK_BODY_BYTES` (1 MiB) se comprueba por cabecera y por cuerpo real antes de
  parsear.
- **Idempotencia primero**: el `update_id` se inserta antes de cualquier otro efecto; un reintento de
  Telegram contesta 200 sin hacer nada (`inbox_updates`).
- **Ritmo**: cubo de fichas por usuario (`RATE_LIMIT_PER_MINUTE`, 20/min por instancia) con **un solo
  aviso por ventana**.
- Pruebas: `tests/unit/test_webhook_secret.py`, `tests/unit/test_limits.py`,
  `tests/integration/test_webhook_flow.py`.

## 3. Allow-list y denegación por defecto

- La identidad es **siempre** `effective_user.id` (numérico). Nunca el nombre visible, el `username`
  ni el texto del mensaje (`app/policy/authz.py`, `app/bot/auth.py`).
- La lista se parsea y se valida al arrancar (`parse_allow_list`); una entrada no numérica es un
  error de configuración, no un valor ignorado en silencio.
- **Denegación por defecto:** sin lista, nadie entra. En `APP_ENV=production` una lista vacía
  **impide arrancar** (`app/config.py::problems`), y `DEV_ALLOW_ANY_USER=true` también es un error de
  arranque.
- Un usuario no autorizado recibe 200 sin respuesta y **no** provoca ninguna llamada al modelo.
- Pruebas: `tests/unit/test_authz.py`, `tests/unit/test_config.py`.

## 4. Los cuatro niveles de riesgo (spec §7)

Definidos como datos en `app/policy/risk.py`, y **toda** herramienta declara el suyo: una herramienta
sin nivel es un fallo de arranque (`assert_registry_complete`), no un caso a tratar en ejecución.

| Nivel | Significado | Política | Herramientas registradas |
|---|---|---|---|
| **0 — lectura** | consultar sin efecto | permitido al usuario autorizado | `search_documents`, `get_document_status`, `search_personal_memory`, `list_memories`, `list_tasks`, `search_web`, `search_restaurants`, `check_restaurant_availability` |
| **1 — escritura interna reversible** | cambia estado propio, con registro | permitido, auditado en `tool_invocations` | `save_preference`, `delete_memory`, `create_reminder`, `complete_task`, `cancel_reminder`, `request_document_deletion` |
| **2 — compromiso externo** | el efecto sale del sistema | **aprobación explícita, de un solo uso y caducable** | `submit_reservation` |
| **3 — alto impacto** | pagos, compras, cancelaciones, borrado amplio | **deshabilitado en el MVP** (el registro lo rechaza) | ninguna |

Las políticas se aplican **en código**: el `ToolRegistry` se niega a cargar una herramienta de nivel
2 sin camino de aprobación (`ApprovalRequired` si se invoca directamente) y rechaza cualquier nivel 3.

## 5. Protocolo de aprobación

Una acción de nivel 2 se **prepara**, nunca se ejecuta sola. La fila en `pending_actions` lleva:

| Campo | Por qué |
|---|---|
| `action_id` | aleatorio y no predecible (el botón lo referencia; el servidor no confía en él como autorización por sí solo) |
| `user_id`, `chat_id` | la decisión solo vale para esa persona y ese chat |
| `action_type`, `args_json`, `args_canonical` | los argumentos **exactos** que se ejecutarían |
| `args_hash`, `args_revision` | el botón lleva el hash y la revisión; si cambian, no vale |
| `preview_text` | lo que se le muestra al usuario, congelado |
| `expires_at` | caducidad (`APPROVAL_TTL_MINUTES`, 30 min) |
| `state` | `pending → approved → executing → succeeded / failed / unknown` (o `rejected` / `expired`) |
| `idempotency_key` | evita apilar avisos idénticos |

- **Un solo uso y atómico.** La decisión la gana **un** CAS: `UPDATE … WHERE state='pending' AND
  args_revision=… AND args_hash=…`; dos pulsaciones simultáneas ejecutan una y la otra recibe su
  causa. La ejecución se reclama con otro CAS (`approved → executing`) antes de llamar a nada.
- **Auditable.** Quedan la creación, la decisión, el ejecutor y el resultado; hay un disparador que
  **impide modificar los argumentos** de una fila ya aprobada (un `UPDATE` directo de `args_json`
  falla en la base, no solo en el código).
- **Siete causas de cero filas, siete mensajes distintos** (`app/bot/messages.py`): acción
  desconocida, caducada, ya rechazada, en curso, ya resuelta, argumentos cambiados, identidad
  equivocada. Nunca se informa de una sola causa conjeturada.
- **Nivel 2 sin camino directo.** `ToolRegistry.invoke` lanza `ApprovalRequired` para cualquier
  herramienta de nivel 2; el único camino es `execute_approved`, que exige un *claim* que consta como
  ganado.
- Pruebas: `tests/integration/test_phase3_approvals.py` (incluye dos callbacks concurrentes, caducidad,
  otro usuario y argumentos modificados), `tests/unit/test_phase2_units.py`.

## 6. Inyección de prompt: documentos y web son **datos**

La defensa es estructural, porque las instrucciones del prompt no son una frontera de seguridad
(spec §9, PLAN.md §6.6):

1. **El texto del documento nunca entra en el prompt del sistema.** Los fragmentos van en el turno
   del usuario, dentro de un bloque etiquetado como **no confiable**, y el prompt dice que instrucciones
   dentro de ese bloque se ignoran y se **reportan** (`app/agent/prompts.py`).
2. **El delimitador no se puede falsificar.** El texto se escapa separando **cada** `<` de una
   secuencia (`<<<<` → `< < < <`), de modo que no puede reconstruirse ni la apertura ni el cierre; y
   el **nombre del archivo** se escapa igual (comillas, `<`, `>`, saltos de línea), porque un nombre
   era suficiente para cerrar la cabecera. Dos versiones anteriores fueron falsificables y ambas
   están cubiertas por pruebas (`test_a_run_of_four_or_more_angle_brackets_still_cannot_forge_the_close_marker`,
   `test_a_hostile_filename_cannot_close_the_header_attribute`).
3. **Ninguna capacidad peligrosa es alcanzable desde el texto.** Las herramientas de nivel 2 exigen la
   aprobación ligada al usuario y el chat; un PDF no puede fabricar un callback de Telegram.
   `search_web` recibe una *consulta*, no una URL; `submit_reservation`, criterios estructurados.
4. **Los argumentos se validan** con el esquema de la herramienta; lo que no valida es un error
   tipado, no un reintento ciego.
5. **El contexto está acotado** (`RAG_MAX_CONTEXT_CHARS`, 6000), lo que limita cuánto texto inyectado
   cabe de golpe.
6. **La comprobación mecánica N12** (`tests/evals/conftest.py::poisoned_write_arguments`): toma
   **todos** los literales del bloque no confiable y falla si alguno aparece en los argumentos de una
   herramienta de escritura de nivel 1 y **no** en el turno del usuario. Se ejecuta sobre el contexto
   real y sobre un turno real con el modelo.
7. **El texto recuperado nunca se registra**: el log lleva documento, fragmento, página y puntuación.
8. **Los resultados de la búsqueda web son datos no confiables.** El prompt ordena ignorar
   instrucciones contenidas en ellos y citar la fuente; el proveedor devuelve título, URL y
   extracto, y el recorte (`WEB_SEARCH_MAX_*`) limita cuánto texto entra de golpe. La API key viaja
   en `Authorization`, nunca en la URL, y ni la consulta ni los resultados se registran en los logs.

## 7. Gestión de secretos

- **Secret Manager** para el token del bot, el secreto del webhook, la clave de Ollama y la DSN de
  Supabase. La versión se **fija numéricamente** al desplegar (nunca `latest`): una variable con
  secreto se lee una vez al arrancar la instancia.
- **Nunca como argumento.** `gcloud` lee los valores de un fichero (`--data-file=`) o del terminal
  (`--prompt-for-password`); el script de despliegue no los lee, no los imprime y no los pasa por
  `argv`. `scripts/lib/redact.sh` redacta además por valor literal y por forma.
- **En el repositorio** solo hay `.env.example` con valores vacíos; `.env` está en `.gitignore`.
- **En la imagen** no hay `.env`, ni claves, ni ficheros de cuenta de servicio; la auditoría por
  capas (`scripts/audit_image.py`) lo comprueba, y su `--self-test` prueba que el detector detecta.
- **Sin claves de cuenta de servicio.** Cloud Run usa la identidad de la cuenta de servicio
  directamente; Vertex y GCS se autentican con ADC/identidad, no con un JSON.
- **La cuenta de servicio por defecto de Compute no tiene roles.** Se le retiró el `roles/editor`
  heredado el 2026-10-18 y se comprobó que no conserva ninguna concesión; ningún recurso la usa
  (servicio y jobs corren con `taa-prod-runtime`).
- **Logs sin secretos.** La redacción es por clave y por forma (bearer, token de Telegram, hex largo,
  base64, cabecera PEM); los errores del proveedor nunca llevan la URL con el token.

## 8. Datos personales, retención y borrado

- **Qué se guarda:** ids de Telegram, texto de los mensajes (transcripción de auditoría), preferencias
  explícitas, tareas y su ledger de despachos, documentos y sus fragmentos con embeddings, originales
  en GCS, aprobaciones y ledger de herramientas, y los checkpoints del grafo.
- **Minimización:** el modelo recibe fragmentos, nunca documentos completos; los logs no llevan
  contenido; `LOG_CONTENT_DEBUG=false` por defecto.
- **Borrado:** `/forget <clave>` borra una preferencia; `/forget all confirm` borra todas; el borrado
  de un documento es **por etapas y verificado** (fragmentos → objeto en GCS verificado ausente →
  fila marcada `deleted_at`), y un fallo parcial queda registrado y es reintentable. `/reset` rota el
  hilo de conversación, **no** borra datos persistentes.
- **Retención:** no hay purga automática. La decisión sigue abierta (PLAN.md §13.7); hasta entonces los
  datos se conservan hasta que el usuario los borra con los comandos anteriores o con el
  procedimiento manual documentado. Las copias dependen del plan de Supabase (Pro: diarias; gratuito:
  ninguna).

## 9. Imagen y cadena de suministro

- Base fijada **por digest** (no por etiqueta), dos etapas, `--no-cache-dir`, usuario sin privilegios
  (uid 10001), `CMD` en forma exec para recibir `SIGTERM` y drenar.
- `.dockerignore` con patrones independientes de la profundidad (`**/`): un `.pem` anidado o un
  `__pycache__` no entran.
- La auditoría recorre las **capas** de `docker save` (no el sistema de ficheros fusionado de un
  contenedor), de modo que un secreto copiado y borrado en una capa posterior sigue apareciendo.

## 10. Límites como controles

`REQUEST_BUDGET_SECONDS` (45 s, por debajo del plazo de Telegram), timeout y reintentos del modelo
validados contra el presupuesto al arrancar, ingesta en línea acotada, 20 MB / 200 páginas por
documento, 20 peticiones/minuto por usuario, TTL de 30 min para aprobaciones, lease de 900 s en el
despachador, y el límite de **14 conexiones** del pooler con `max-instances=1`.

## 11. Puntos ciegos conocidos (sin suavizar)

- **Un solo usuario.** El sistema no está pensado para múltiples inquilinos; el aislamiento por
  propietario existe y está probado, pero la autorización es una lista.
- **El rate limit es por instancia.** El límite efectivo es `RATE_LIMIT_PER_MINUTE` × instancias (hoy
  una), y un arranque en frío reinicia el cubo.
- **No hay canal de alertas.** La métrica de errores existe; la política de alerta espera un canal que
  es decisión del usuario.
- **No hay purga automática** de datos (decisión de retención abierta).
- **`audit_image.py` se excluye a sí mismo** del escaneo (contiene los patrones que busca): un punto
  ciego estrecho y documentado.
- **El "no hay evidencia" del modelo es una tasa medida, no una garantía**: 5/5 en la pregunta que
  recupera pasajes que no responden; en la totalmente ajena, 0/5 de consultas y 2–3/5 de fórmula de
  "no encuentro" (nunca cita ni inventa).
- **La reversión de código se ejercitó** el 2026-10-18 (`00002` y vuelta a `00003`). El hallazgo fue
  que `00002`, anterior al arreglo de `/health`, pierde esa ruta y solo conserva `/readyz` y el
  webhook. La restauración del esquema no se ha ejercitado: el plan gratuito no tiene copias.
- **El plan gratuito de Supabase se pausa y no tiene copias** (Pro recomendado).
