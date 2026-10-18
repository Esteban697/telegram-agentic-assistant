# Informe final — Telegram personal assistant

**Fecha:** 2026-10-18 · **Alcance:** Fases 1–6 del `PLAN.md` · **Estado:** desplegado y operativo

Este es el informe que pide la spec §18.10: archivos entregados, pruebas **realmente ejecutadas**
con sus resultados, tareas pendientes, requisitos externos y costes. También contiene el mapa de la
lista de la §17 (definición de terminado), el de los diez entregables de la §18, y la revisión de
coste / IAM / límites / datos personales de la Fase 6.

> **Regla de honestidad de este documento.** Cada afirmación de "hecho" tiene detrás una ejecución
> registrada o una prueba con nombre. Lo que no se ha ejecutado se dice como **pendiente** o
> **documentado pero no ejercitado**, con la dependencia exacta que falta. No hay ninguna casilla
> marcada por haber leído el código.

---

## 1. Resumen ejecutivo

El asistente está **desplegado y funcionando** en `https://taa-prod-api-hzlxaxcstq-ew.a.run.app`:
un bot privado de Telegram que conversa a través de Ollama Cloud, guarda preferencias y
recordatorios en PostgreSQL, indexa documentos y responde con la página de la que saca cada dato,
y prepara acciones externas que solo se ejecutan con una aprobación explícita.

Evidencia de extremo a extremo, producida **por el usuario** contra el sistema desplegado:

- `/start` → `200` y `update handled`;
- un mensaje normal → `turn prepared` → `model turn complete` → respuesta, ~**1,6 s**;
- **un recordatorio creado y entregado** (Scheduler → Cloud Run Job → despachador → Telegram). Eso
  cierra empíricamente el Apéndice B.6 del plan, que hasta ahora era el único externo sin verificar.

Infraestructura: Cloud Run (servicio + 3 jobs), 3 schedulers habilitados, 4 secretos con versión
fijada, webhook registrado sin `last_error_message`, Supabase (PostgreSQL 17.11 + pgvector 0.8.2).

---

## 2. Arquitectura entregada (resumen)

```
Telegram ──HTTPS──▶ app/api/telegram_webhook.py
                      │ secreto del webhook (tiempo constante) · tamaño · dedupe por update_id
                      │ allow-list (denegación por defecto) · rate limit
                      ▼
                    app/bot/handlers.py ──▶ app/agent/graph.py (LangGraph)
                                              │ call_model → run_tools / await_approval
                                              ▼
                    app/tools/registry.py (nivel de riesgo + ledger) ──▶ PostgreSQL / GCS / providers
```

- **Un binario, cuatro comandos**: `uvicorn app.main:app` (servicio) y los tres jobs
  `dispatch_reminders`, `process_documents`, `resume_work`.
- **PostgreSQL** (Supabase, pooler Supavisor en modo sesión, TLS) para todo el estado: conversación,
  memorias, tareas, documentos y chunks, aprobaciones, ledger de herramientas, `work_items` y los
  checkpoints de LangGraph.
- **Vertex AI** (`gemini-embedding-001`, 768 dimensiones) para embeddings; **Ollama Cloud** para el
  modelo de chat; **Cloud Storage** para los originales.

---

## 3. Archivos creados y modificados

Inventario del repositorio entregado. Las rutas son las del repositorio; el "para qué" es la
primera línea del docstring de cada módulo, abreviada.

### 3.1 Aplicación (`app/`)

| Archivo | Para qué |
|---|---|
| `app/main.py` | Fábrica de la aplicación y cableado de dependencias (único sitio que elige implementaciones). |
| `app/config.py` | Configuración tipada, validación fail-fast agregada y lista de prohibiciones de producción. |
| `app/errors.py` | Taxonomía de errores tipados; nunca contienen el valor ofensor. |
| `app/logging_config.py` | Logs JSON estructurados con redacción por clave y por forma. |
| `app/agent/graph.py` | Grafo LangGraph: respuesta / herramienta / aprobación / fallo. |
| `app/agent/policies.py` | Pegamento de política del grafo (decisión de herramienta y preparación de aprobación). |
| `app/agent/prompts.py` | Prompt del sistema en español (evidencia vs inferencia vs no encontrado; documento = datos). |
| `app/agent/routing.py` | Aristas condicionales del grafo. |
| `app/agent/state.py` | Estado tipado del grafo y conversión a/desde el checkpoint. |
| `app/api/telegram_webhook.py` | Webhook: secreto, tamaño, dedupe, allow-list, rate limit, presupuesto. |
| `app/api/health.py` | `/healthz` (liveness), `/health` (alias) y `/readyz` (BD, `vector` ≥ 0.8, modelo activo). |
| `app/bot/auth.py` | Autorización por petición (allow-list, denegación por defecto). |
| `app/bot/handlers.py` | Comandos, turnos, documentos y callbacks de aprobación. |
| `app/bot/keyboards.py` | Teclados en línea (menú y aprobar/rechazar). |
| `app/bot/messages.py` | Catálogo de textos en español: **única** fuente de lo que lee el usuario. |
| `app/bot/telegram_client.py` | Cliente de la Bot API (siete métodos, token nunca en logs). |
| `app/jobs/__init__.py` | Utilidades de los jobs (`executor_id`, línea `JOB_REPORT`). |
| `app/jobs/dispatch_reminders.py` | Job de recordatorios (una pasada, idempotente). |
| `app/jobs/process_documents.py` | Job de ingestas pendientes (una pasada, reclama con la misma regla que la web). |
| `app/jobs/resume_work.py` | Job de reanudación de turnos interrumpidos. |
| `app/llm/embeddings.py` | Provedores de embeddings: Vertex (producción) y el doble léxico (pruebas). |
| `app/llm/errors.py` | Taxonomía de errores de proveedor (referencia, no segunda definición). |
| `app/llm/ollama_client.py` | Cliente HTTP de Ollama aislado (timeout, reintentos acotados, sin URLs del token en errores). |
| `app/llm/protocols.py` | Puertos del LLM (`ChatModel`, `EmbeddingProvider`) — capa 0. |
| `app/policy/approval.py` | Máquina de estados de aprobación como funciones puras. |
| `app/policy/authz.py` | Parseo de la allow-list, extracción de identidad, denegación por defecto. |
| `app/policy/limits.py` | Cubo de fichas detrás de `RATE_LIMIT_PER_MINUTE`. |
| `app/policy/risk.py` | Niveles de autonomía y registro herramienta→nivel. |
| `app/policy/timezones.py` | Resolución de zonas horarias y vencimientos (DST incluido). |
| `app/services/approvals.py` | Crear, decidir, reclamar y ejecutar aprobaciones (CAS atómico). |
| `app/services/conversation.py` | Ciclo de la conversación, hilo y `/reset`. |
| `app/services/document_ingestion.py` | Ingesta documental: las once etapas del §6.1 del plan. |
| `app/services/notifications.py` | Entrega de mensajes (Telegram o el transporte de grabación). |
| `app/services/reminders.py` | Despachador: reconciliar, reclamar, ledger, avanzar. |
| `app/services/work_queue.py` | Cola durable de reanudación (`work_items`). |
| `app/storage/checkpointing.py` | Persistencia de checkpoints de LangGraph (`AsyncPostgresSaver`). |
| `app/storage/gcs.py` | Originales en Cloud Storage (bucket privado). |
| `app/storage/models.py` | Modelos SQLAlchemy — el esquema canónico. |
| `app/storage/postgres.py` | Motores y factorías de sesión (async para la app, sync para Alembic). |
| `app/storage/repositories.py` | **Todo** el SQL en un solo módulo, incluido el único punto de recuperación vectorial. |
| `app/storage/stores.py` | Protocolos de almacén y sus implementaciones en memoria (desarrollo/pruebas). |
| `app/tools/base.py` | Validación de argumentos y resultados tipados. |
| `app/tools/documents.py` | Búsqueda, estado y borrado de documentos; renderizado de citas y delimitador no falsificable. |
| `app/tools/memory.py` | Buscar, guardar y borrar preferencias. |
| `app/tools/registry.py` | Registro de herramientas, ledger de invocaciones y esquemas para el modelo. |
| `app/tools/restaurants.py` | Búsqueda y disponibilidad (interfaces) y la acción de nivel 2 `submit_reservation`. |
| `app/tools/tasks.py` | Crear, listar, completar y cancelar recordatorios. |
| `app/tools/web_search.py` | Búsqueda web tras una interfaz de proveedor configurable. |

### 3.2 Migraciones (`migrations/versions/`)

| Revisión | Contenido |
|---|---|
| `0001_extensions` | `vector` (y `pgcrypto` si hiciera falta), con la comprobación de versión ≥ 0.8. |
| `0002_conversations` | Conversaciones, mensajes, ledger de updates. |
| `0003_memories` | Memorias explícitas. |
| `0004_documents` | Documentos, chunks, `embedding_models` e índice HNSW. |
| `0005_tasks` | Tareas y ledger de despachos. |
| `0006_actions` | `pending_actions`, `tool_invocations`, `work_items`, y el disparador de inmutabilidad de argumentos. |
| `0007_args_canonical` | Columna canónica de argumentos y su índice único. |

### 3.3 Scripts (`scripts/`)

| Archivo | Para qué |
|---|---|
| `deploy.sh` | Imprime (y solo con aprobación ejecuta) el despliegue completo; idempotente y reanudable. |
| `lib/redact.sh` | Redacción literal y por forma de credenciales en todo lo que se imprime. |
| `validate_deploy_env.py` | Valida el entorno exacto de cada componente con la propia validación de la aplicación. |
| `check_pool_budget.py` | Puerta del aritmético de conexiones contra el límite del pooler (14). |
| `check_database.py` | Conectividad, pgvector ≥ 0.8, `vector` resoluble desde el rol y rol sin privilegios. |
| `audit_image.py` | Audita la imagen por capas en busca de secretos; `--self-test` prueba el detector. |
| `set_webhook.py` | Registra, consulta y da de baja el webhook sin imprimir nunca el token. |
| `migrate.py` | Envoltorio de Alembic con comprobación previa y bloqueo de `downgrade` en producción. |
| `bootstrap_db.py` | Esquema desde una base vacía + tablas del checkpointer + verificación. |
| `run_local_db.sh` | Contenedor local `pgvector/pgvector:pg17` para desarrollo y pruebas. |
| `verify_ollama.py` | Sonda de compatibilidad del modelo (A10). |

### 3.4 Pruebas (`tests/`)

| Archivo | Qué cubre |
|---|---|
| `tests/unit/test_authz.py`, `test_webhook_secret.py` | Allow-list y secreto del webhook (comparación en tiempo constante). |
| `tests/unit/test_config.py`, `test_logging_and_config_consumers.py` | Configuración, arranque fail-fast, redacción y "ninguna variable muerta". |
| `tests/unit/test_contract_guards.py` | Guardas estructurales: chokepoint vectorial, SQL sin interpolación. |
| `tests/unit/test_layering.py` | Matriz de importación entre capas, con sus dos reglas negativas. |
| `tests/unit/test_limits.py`, `test_timezones.py` | Cubo de fichas y zonas horarias. |
| `tests/unit/test_messages.py`, `test_readme.py` | Catálogo en español (única fuente), y que el README no miente sobre el código. |
| `tests/unit/test_ollama_client.py`, `test_verify_ollama_probe.py`, `test_telegram_client.py` | Clientes y sonda, sin red real. |
| `tests/unit/test_phase2_units.py`, `test_stores.py` | Persistencia: firmas congeladas y almacenes. |
| `tests/unit/test_phase4_guards.py`, `test_phase4_review_fixes.py`, `test_review_hardening.py` | Guardas de la Fase 4 (ingesta, citas, delimitador, plazos). |
| `tests/unit/test_deploy_artifacts.py` | El contrato del despliegue: `--project`, secretos nunca impresos, aritmético, idempotencia, `--from-step`, ejecución accidental, aislamiento de las pruebas. |
| `tests/unit/test_deploy_secrets.py` | `set_webhook.py` y `audit_image.py`, sin red ni Docker. |
| `tests/integration/test_phase2_db.py`, `test_phase2_review.py` | Persistencia real: idempotencia, concurrencia de despachos, reanudación. |
| `tests/integration/test_phase3_approvals.py`, `test_phase3_review.py`, `test_phase3_webhook.py`, `test_webhook_flow.py` | Grafo, aprobaciones, CAS, webhook y comandos. |
| `tests/integration/test_phase4_documents.py`, `test_phase4_review_db.py` | Ingesta, recuperación, aislamiento, borrado y re-indexado. |
| `tests/integration/test_phase5_jobs.py` | Job de documentos, presupuesto de conexiones y aritmético. |
| `tests/integration/test_dockerignore.py`, `test_image_audit.py` | `.dockerignore` y auditoría de imagen contra Docker de verdad. |
| `tests/evals/test_golden_scorer.py` | El scorer de las evaluaciones (modo 1: transcripciones grabadas, sin credenciales). |
| `tests/evals/test_rag_evals.py` | Recuperación real sobre el corpus ficticio, con orden y página. |
| `tests/evals/test_agent_evals.py` | Comportamiento del agente con el modelo real (marcador `live`). |
| `tests/live/test_vertex_embeddings.py` | Vertex real: dimensión, página correcta y relación de puntuaciones. |

### 3.5 Raíz, documentación y configuración

| Archivo | Para qué |
|---|---|
| `README.md` | Arquitectura, puesta en marcha local, comandos de prueba, configuración de Telegram y despliegue. |
| `docs/despliegue.md` | Runbook de despliegue, migraciones, operación y reversión. |
| `docs/operacion.md` | Incidentes y recuperación (spec §14), con síntoma y respuesta por modo de fallo. |
| `docs/seguridad.md` | Modelo de seguridad y política de aprobación (spec §18.8). |
| `docs/informe-final.md` | Este documento. |
| `Dockerfile`, `.dockerignore` | Imagen reproducible (base por digest, no-root, sin secretos) y qué no entra en el contexto. |
| `pyproject.toml`, `uv.lock` | Dependencias fijadas exactamente. |
| `.env.example`, `.gitignore` | Variables sin secretos y exclusión de `.env`. |
| `alembic.ini`, `migrations/` | Migraciones versionadas. |
| `PLAN.md`, `PLAN-REVIEW-LOG.md`, `PROJECT_SPEC.md` | Plan aprobado, su historial de revisión y la especificación. |

---

## 4. Pruebas ejecutadas y resultados reales

### 4.1 Matriz (ejecutada el 2026-10-18)

| Suite | Comando | Resultado real |
|---|---|---|
| Unitaria, sin credenciales, sin BD, sin red | `pytest -m "not integration and not live and not eval"` | **434 pasan** |
| Integración contra PostgreSQL | `pytest -m integration` | **130 pasan, 1 se salta** |
| Evaluaciones deterministas | `pytest -m "eval and not live"` | **23 pasan** |
| Vertex real | `GOOGLE_CLOUD_PROJECT=telegram-assistant-esteban GOOGLE_CLOUD_REGION=europe-west1 RUN_LIVE_TESTS=1 pytest -m "live and not eval"` | **2 pasan** |
| Agente sobre Ollama Cloud | `RUN_LIVE_TESTS=1 pytest -m "eval and live"` | **7 pasan** |

El salto de integración es **explícito**: `tests/integration/test_phase4_documents.py`
(`test_a_and_b_page_level_citations_and_no_evidence`) está marcado como `skip` porque la calidad de
recuperación se demuestra contra Vertex real en `tests/live/test_vertex_embeddings.py`, y su motivo
lo dice con el comando exacto. No hay ningún salto silencioso.

### 4.2 Evidencia del sistema desplegado

- Servicio `taa-prod-api` en `https://taa-prod-api-hzlxaxcstq-ew.a.run.app`, con escala a cero y
  `max-instances=1`; `/health` y `/readyz` en verde.
- Tres Cloud Run Jobs (recordatorios, documentos, reanudación), tres Scheduler habilitados.
- Cuatro secretos en Secret Manager con **versión fijada numéricamente** (nunca `latest`).
- Webhook registrado; `getWebhookInfo` sin `last_error_message`.
- `/start` → 200, `update handled`; mensaje normal → respuesta en ~1,6 s.
- **Recordatorio creado y entregado** de extremo a extremo.

### 4.3 Criterios de aceptación de la Fase 4 (RAG)

| | Evidencia |
|---|---|
| (a) documento y página correctos | 9 preguntas del golden, recuperación real, **1,00** incluido el orden a partir del puesto 2; Vertex real sitúa la página que responde la primera (0,7332) |
| (b) sin evidencia, sin cita | **código**: nada por encima del suelo ⇒ `found=false`, `evidence="none"`, sin citas y contexto vacío (determinista). **Modelo**: **5/5** en la pregunta cuya respuesta no está pero que sí recupera pasajes; en la pregunta totalmente ajena el modelo **no consulta** los documentos (0/5) y usa la fórmula de "no encuentro" en 2–3 de 5 ejecuciones |
| (c) aislamiento | prueba de fuga cruzada con el chunk del otro usuario como vecino más cercano |
| (d) idempotencia | re-subida deduplicada antes de subir; reintento convergente |
| (e) documento envenenado | recuperado como datos, sin acción, y la comprobación mecánica N12 sobre el contexto **real** |
| (f) borrado verificado | por etapas, objeto verificado ausente, fallo parcial reintentable |
| (g) un solo modelo activo | leído de la fila `embedding_models`; discrepancia ⇒ mensaje de re-indexado |

### 4.4 Evaluaciones: qué haría falta para que fallaran

- El **scorer** (modo 1) falla si se invierte o se rota el orden de las citas, si se inventa una
  página o un documento, si se acepta evidencia donde no la hay, o si una pregunta respondible no
  recupera nada.
- La **evaluación de página** (modo 2) falla si la recuperación real devuelve otro documento u otra
  página, incluido cualquier intercambio a partir del puesto 2; se comprobó revirtiendo el orden de
  recuperación (1,00 → 0,40).
- La **evaluación de "no hay evidencia"** falla si el suelo deja pasar basura o si `found`/`evidence`
  dejan de ser explícitos; la mitad del modelo falla si menos de 3 de 5 ejecuciones consultan y
  dicen "no encuentro" sin citar.

---

## 5. Mapa de la lista de la §17 (definición de terminado)

Los 15 elementos, uno a uno. **Verificado** = hay una ejecución o una prueba con nombre; **pendiente**
= falta una dependencia concreta.

| # | Elemento | Estado | Evidencia / dependencia |
|---|---|---|---|
| 1 | Se puede ejecutar localmente siguiendo el README | **Verificado** | README (setup, `uv sync`, `.env`, `docker`/`uvicorn`) + la suite unitaria corre en una máquina limpia sin credenciales (434) |
| 2 | `.env.example` sin secretos reales | **Verificado** | `.env.example` versionado con valores vacíos; `test_env_example_contains_no_real_looking_secret` |
| 3 | Migraciones desde una base vacía | **Verificado** | `0001`…`0007` aplicadas al proyecto Supabase nuevo (17.11) y al contenedor local; `scripts/bootstrap_db.py` verifica tablas, `vector` ≥ 0.8, HNSW y una fila activa |
| 4 | Las pruebas unitarias pasan | **Verificado** | 434 pasan, sin credenciales, sin BD, sin red |
| 5 | Integración implementada y dependencias documentadas | **Verificado** | 130 pasan, 1 se salta; README documenta el contenedor y `TEST_DATABASE_URL` |
| 6 | Telegram limitado a usuarios autorizados | **Verificado** | Allow-list + denegación por defecto; `test_authz`, `test_webhook_secret`; en producción una lista vacía **impide arrancar**; desplegado con `TELEGRAM_ALLOWED_USER_IDS` |
| 7 | El LLM se puede cambiar por configuración | **Verificado** | `LLM_MODEL`; `test_model_name_is_never_hard_coded_outside_configuration`; sonda A10 documentada |
| 8 | El estado de LangGraph es persistente | **Verificado** | `AsyncPostgresSaver` sobre la DSN; pruebas de reanudación en `test_phase2_db`; el servicio desplegado lo usa |
| 9 | Memoria y tareas se conservan entre reinicios | **Verificado** | PostgreSQL (no memoria) para ambas; pruebas de persistencia y concurrencia; el recordatorio desplegado se creó y se entregó |
| 10 | El RAG presenta fuentes verificables y filtra por propietario | **Verificado** | Golden 1,00 con documento y página; aislamiento probado en dos suites; Vertex real (0,7332) |
| 11 | Aprobaciones específicas, caducables, auditables e idempotentes | **Verificado** | CAS atómico, TTL, hash y revisión de argumentos, siete causas de cero filas con su mensaje; `test_phase3_approvals`; el camino de nivel 2 **no** se ejercitó en vivo (ver §7) |
| 12 | No hay automatización de reservas ficticia | **Verificado** | `submit_reservation` es una interfaz que responde `CapabilityUnavailable`; las pruebas exigen que no se afirme disponibilidad sin proveedor |
| 13 | Los jobs no dependen de procesos en memoria | **Verificado** | Tres jobs de una sola pasada, disparados por Scheduler; **un recordatorio entregado en vivo** |
| 14 | Instrucciones de despliegue, webhook, migraciones, operación y reversión | **Verificado** (reversión documentada, **no ejercitada**) | `docs/despliegue.md` + `docs/operacion.md`; la vuelta a una revisión anterior está escrita y **nunca se ha ejecutado** (ver §7) |
| 15 | Limitaciones y pasos manuales documentados | **Verificado** | §7 de este informe, el README y el runbook (secretos, allow-list, plan de Supabase, B.6…) |

---

## 6. Mapa de los diez entregables de la §18

| # | Entregable | Dónde | Estado |
|---|---|---|---|
| 1 | Código fuente funcional y tipado | `app/` (56 módulos, incluidos los `__init__`), con tipos en las fronteras de datos | Entregado |
| 2 | `pyproject.toml` con dependencias fijadas y comandos | `pyproject.toml` (pins exactos) + `uv.lock`; comandos de prueba en el README | Entregado |
| 3 | `Dockerfile` y `.dockerignore` | Imagen multi-etapa, base por digest, no-root; `.dockerignore` con patrones `**/` | Entregado |
| 4 | Migraciones de base de datos | `migrations/versions/0001`…`0007`; `alembic.ini` | Entregado y aplicado |
| 5 | Pruebas unitarias, integración y evaluación RAG/agente | `tests/unit`, `tests/integration`, `tests/evals`, `tests/live` | Entregado; resultados en §4 |
| 6 | `.env.example` y `.gitignore` | Raíz; `.env` ignorado | Entregado |
| 7 | README con arquitectura, setup, pruebas, Telegram y despliegue | `README.md` | Entregado |
| 8 | Documentación de seguridad y política de aprobación | `docs/seguridad.md` | Entregado |
| 9 | Scripts reproducibles de inicialización y migraciones | `scripts/bootstrap_db.py`, `scripts/migrate.py`, `scripts/deploy.sh`, `scripts/run_local_db.sh` | Entregado |
| 10 | Informe final (archivos, pruebas reales, pendientes, requisitos, costes) | `docs/informe-final.md` (este documento) | Entregado |

---

## 7. Tareas pendientes, limitaciones y verdades incómodas

Se recogen **sin suavizar**, porque la aceptación de esta fase es justamente que los bloqueos no se
presenten como resueltos (spec §16).

1. **A10 FALLÓ en salida estructurada.** La sonda real (2026-10-17) confirma que el *tool calling*
   funciona, pero que Ollama Cloud **ignora `format`**: un turno con JSON Schema impuesto no queda
   restringido. La decisión de respaldo, ya en el plan, es **JSON dictado en el prompt** y validado
   en servidor; A10 **no** se da por pasado.
2. **La fórmula de "no hay evidencia" es una tasa medida, no una garantía.** En la pregunta que sí
   recupera pasajes que no responden: **5/5**. En la pregunta totalmente ajena el modelo ni siquiera
   consulta los documentos (0/5) y usa la fórmula de "no encuentro" en **2–3 de 5** ejecuciones
   (las demás son una negativa genérica, sin citar y sin inventar). Está registrado como número en
   `PLAN.md`.
3. **La reversión (criterio (e) de la Fase 5) está documentada y nunca se ha ejercitado.** El comando
   (`gcloud run services update-traffic … --to-revisions PREV=100`) está escrito y es correcto por
   construcción, pero **no se ha ejecutado**; lo mismo vale para la restauración desde Supabase.
4. **El plan gratuito de Supabase se pausa tras 7 días de inactividad y no tiene copias de
   seguridad.** El proyecto está hoy en el plan gratuito. Para un asistente que debe responder a
   cualquier hora, y para tener un camino de restauración, se recomienda el **plan Pro**; hasta
   entonces, la única copia es un `pg_dump` manual a través del pooler.
5. **Las evaluaciones del agente en vivo son estocásticas.** En esta ejecución pasan **7/7**, pero
   una de ellas (`test_agent_cites_real_sources_and_invents_no_pages`) ha fallado en ejecuciones
   anteriores: el modelo respondió con una referencia numerada estilo `【1】` en lugar de nombrar
   archivo y página. La aserción no se ha relajado; la tasa está registrada.
6. **`scripts/audit_image.py` se excluye a sí mismo del escaneo.** Su propio código contiene los
   patrones que busca (cabeceras PEM, cuerpos de cuenta de servicio), así que se salta su propia
   ruta. Es un punto ciego **estrecho y documentado**: el fichero no contiene secretos y no se
   despliega como código de la aplicación, pero un secreto escondido *dentro de ese fichero* no se
   detectaría.
7. **El camino de nivel 2 no se ejercitó en vivo.** El usuario probó `/start`, un mensaje y un
   recordatorio; ninguna aprobación real se pulsó en el bot desplegado. El CAS, la caducidad y el
   rechazo están cubiertos por pruebas de integración contra PostgreSQL, no por una ejecución
   desplegada.
8. **`pgvector` en el esquema `extensions`.** Es una diferencia con un Postgres "normal" y la causa
   del chequeo del paso 0: si el `search_path` o el `USAGE` del rol se pierden, el fallo aparece en
   el primer embedding, no al arrancar.
9. **Búsqueda híbrida diferida.** Solo hay búsqueda vectorial. El disparador para revisarla está
   escrito en el plan (recall por página < 0,8 en consultas con un token literal raro).
10. **Proveedor de búsqueda web y de restaurantes sin integrar.** Las herramientas existen como
    interfaces y responden `CapabilityUnavailable`; no se ha inventado ninguna API.

---

## 8. Requisitos externos

| Requisito | Para qué | Estado |
|---|---|---|
| `OLLAMA_API_KEY` | modelo de chat | en uso (cuenta del usuario) |
| Credenciales de Google Cloud (ADC) + APIs habilitadas | Vertex (embeddings) y GCS | en uso |
| Proyecto GCP `telegram-assistant-esteban` | Cloud Run, Jobs, Scheduler, Secret Manager, Artifact Registry, GCS | en uso |
| Bucket `taa-prod-documents` | originales | creado |
| Proyecto Supabase (plan Pro recomendado) | PostgreSQL 17.11 + pgvector 0.8.2 | en uso (gratuito) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `TELEGRAM_ALLOWED_USER_IDS` | el bot y su acceso | en uso, en Secret Manager (la lista va como variable) |
| `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_REGION` | Vertex y el despliegue | en uso |

**Pasos manuales que quedan en manos del usuario:** elegir el plan de Supabase (Pro recomendado),
rotar credenciales cuando toque (`gcloud secrets versions add` + `update-secrets`), y decidir la
política de retención (§9) y el canal de alertas (no hay ninguno configurado: las alertas son una
decisión pendiente).

---

## 9. Revisión de coste, IAM, límites y datos personales (Fase 6)

### 9.1 Coste y disposición

**No hay ningún compromiso recurrente de GCP**: el servicio escala a cero y los jobs se facturan por
ejecución. Las cifras que no se pueden citar de una lista de precios vigente se marcan **pendiente
de confirmar** (spec §1.4; PLAN.md §12).

| Recurso | Coste | Disposición |
|---|---|---|
| Cloud Run Service | ~0 $ en reposo (`min-instances=0`); por petición y CPU/memoria-segundo | **pendiente de confirmar** la tarifa vigente |
| Cloud Run Jobs ×3 | fracciones de céntimo por ejecución | **pendiente de confirmar** |
| Cloud Scheduler ×3 | 0 (nivel gratuito a estas cadencias) | gratis |
| Secret Manager | céntimos/mes por secreto y acceso | **pendiente de confirmar** |
| Artifact Registry | céntimos/mes por GB | **pendiente de confirmar** |
| Cloud Storage | céntimos/mes por GB y operaciones | **pendiente de confirmar** |
| Cloud Logging | gratis hasta la franquicia | 0–bajo |
| Supabase | **plan gratuito** hoy | **se pausa tras 7 días de inactividad y no tiene backups**; **Pro recomendado** (pendiente de confirmar su precio) |
| Vertex AI (embeddings) | por uso, cuenta del usuario | **pendiente de confirmar** |
| Ollama Cloud | por token, cuenta del usuario | **pendiente de confirmar** |
| Telegram Bot API | gratis | — |

### 9.2 IAM concedido (y por qué es mínimo)

| Principal | Rol | Ámbito | Por qué |
|---|---|---|---|
| Runtime SA (`taa-prod-runtime`) | `roles/aiplatform.user` | proyecto | es lo mínimo para llamar a Vertex; sin él, todo embedding recibe 403 |
| Runtime SA | `roles/storage.objectAdmin` | **solo el bucket** | acotado a `taa-prod-documents`; no `storage.admin` |
| Runtime SA | `roles/secretmanager.secretAccessor` | **por secreto** (los 4) | lectura de exactamente los cuatro; sin `secretVersionManager` (rotar es un acto de despliegue) |
| Scheduler SA (`taa-prod-scheduler`) | `roles/run.invoker` | **por job** (los 3) | lo mínimo para disparar un job |
| Humano (`operator@example.com`) | `roles/owner` | proyecto | es el dueño del proyecto y quien despliega; **no** es la identidad de ejecución |

Comprobado el 2026-10-18 con `gcloud projects get-iam-policy`, `gcloud secrets get-iam-policy` y
`gcloud run jobs get-iam-policy`: es la política real, no la descripción del plan.

**Lo que ningún principal de aplicación tiene:** `editor`, `owner`, `storage.admin`,
`cloudsql.client` (ya no hay Cloud SQL), `cloudsql.admin` ni `secretVersionManager`. **No existe
ninguna clave de cuenta de servicio** (solo claves `SYSTEM_MANAGED`): Cloud Run usa la identidad
directamente. `iam.serviceAccountUser` sobre la cuenta de ejecución **no** se concede a Scheduler
(los jobs ya se ejecutan *como* ella; `actAs` sobre esa identidad permitiría leer todos los secretos
y la base de datos).

**Dos concesiones heredadas de GCP, dichas para que no sorprendan:** el humano es `owner` del
proyecto —no un rol de despliegue acotado, y no hay un principal separado para desplegar— y la
cuenta de servicio por defecto `<project-number>-compute@developer.gserviceaccount.com` conserva el
`roles/editor` que GCP asigna al crear el proyecto. **El despliegue no la usa** (servicio y jobs
corren con `taa-prod-runtime`), así que quitarla es una mejora de mínimo privilegio pendiente, no un
riesgo del camino de ejecución.

### 9.3 Límites operativos

| Límite | Valor | Consecuencia |
|---|---|---|
| Conexiones del pooler (Supavisor, modo sesión) | **14** | `(2 + 1) × 4 + 2 = 14`; `--max-instances=1` es obligatorio; la conexión 15 falla al instante |
| Procesos concurrentes | 4 (1 servicio + 3 jobs) | incluido en el aritmético |
| Presupuesto de petición | 45 s | por debajo del plazo de 60 s de Telegram; el turno se entrega a la cola de reanudación |
| Timeout del modelo | 18 s × (1 + 1 reintento) + margen = 38,25 s | validado al arrancar contra el presupuesto |
| Ingesta en línea | 27 s (menor entre 30 y 0,6 × 45) | lo que no cabe queda en cola para el job |
| Subida | 20 MB, 200 páginas, 120 s de ingesta por documento | rechazo explícito con el motivo |
| Rate limit | 20 peticiones/minuto por usuario y por instancia | un aviso por ventana; no es distribuido |
| Aprobaciones | TTL 30 min | una aprobación caducada no ejecuta |
| Despacho de recordatorios | lease 900 s, máx. 5 intentos | se reconcilia como `unknown`; nunca se reenvía solo |
| Contexto RAG | top_k 5, 6000 caracteres | fragmentos, nunca documentos completos |

### 9.4 Datos personales: qué se guarda, cuánto y cómo se borra

| Dato | Dónde | Para qué | Borrado |
|---|---|---|---|
| Ids de Telegram (usuario y chat) | PostgreSQL (`conversations`, `tasks`, `memories`, `documents`, `pending_actions`, `work_items`) | identidad y aislamiento | con la fila correspondiente |
| Texto de los mensajes | `messages` (transcripción de auditoría) y los checkpoints | contexto y auditoría | `/reset` rota el hilo (no borra); el borrado es manual/documentado |
| Preferencias explícitas | `memories` | recordar lo que el usuario pidió guardar | `/forget <clave>` o `/forget all confirm` |
| Tareas y recordatorios | `tasks`, `reminder_dispatches` | avisos | completar/cancelar; borrado manual |
| Documentos y fragmentos | GCS + `documents` + `document_chunks` (con embeddings) | RAG | borrado por etapas y **verificado** (fragmentos, objeto y fila) |
| Aprobaciones y ledger de herramientas | `pending_actions`, `tool_invocations` | auditoría y no-duplicación | retención manual |
| Logs | Cloud Logging | operación | **sin contenido** por defecto (solo ids, herramienta, duración, estado, puntuaciones) |

**Retención:** no hay purga automática. La decisión de retención sigue **abierta** (PLAN.md §13.7):
`RETENTION_DAYS` se retiró para no declarar una variable sin consumidor y volverá con la purga. Hoy
los datos se conservan hasta que el usuario los borra con `/forget` (memorias), con el borrado de
documento (documentos) o con el procedimiento manual documentado. Los logs no contienen contenido de
mensajes, documentos, embeddings ni secretos; `LOG_CONTENT_DEBUG=false` es el valor por defecto.

---

## 10. Cómo reproducir todo

```bash
# Suite sin credenciales, sin base de datos y sin red
uv run pytest -m "not integration and not live and not eval"          # 434

# Integración contra el contenedor local
bash scripts/run_local_db.sh
TEST_STORE_BACKEND=postgres TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa \
  uv run pytest -m integration                                         # 130 + 1 skip

# Evaluaciones deterministas (base de datos local)
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/taa \
  uv run pytest -m "eval and not live"                                 # 23

# Proveedor real de embeddings y modelo real (facturables)
GOOGLE_CLOUD_PROJECT=telegram-assistant-esteban GOOGLE_CLOUD_REGION=europe-west1 \
  RUN_LIVE_TESTS=1 uv run pytest -m "live and not eval"                # 2
RUN_LIVE_TESTS=1 uv run pytest -m "eval and live"                      # 7

# Despliegue (imprime primero; ejecuta solo con aprobación)
bash scripts/deploy.sh
bash scripts/deploy.sh --yes-i-have-approval
```
