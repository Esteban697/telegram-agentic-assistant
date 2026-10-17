# Personal AI Assistant --- Especificación de implementación

**Tipo de documento:** especificación funcional y técnica para un agente
de programación\
**Idioma del producto y documentación:** español\
**Estado:** MVP por implementar\
**Objetivo:** construir un asistente personal en Telegram, impulsado por
Ollama Cloud, capaz de consultar documentos personales mediante RAG,
gestionar tareas y recordatorios, y ejecutar acciones externas con
controles de aprobación.

------------------------------------------------------------------------

## 1. Instrucciones principales para el agente implementador

Implementa el proyecto de forma incremental, ejecutable y verificable.
No te limites a generar una estructura de archivos ni pseudocódigo:
entrega código funcional, migraciones, pruebas, Dockerfile,
configuración de despliegue y documentación.

### Reglas de implementación

1.  Inspecciona primero el repositorio existente. Conserva código
    reutilizable, convenciones, configuración y el cliente Ollama
    existente cuando esté disponible. No sobrescribas funcionalidad útil
    sin justificarlo.
2.  Antes de cambiar archivos, presenta un breve plan y detecta qué
    secretos, servicios o datos externos necesitarás. Nunca solicites ni
    escribas secretos reales en el repositorio.
3.  Implementa una fase cada vez. Al finalizar cada fase, ejecuta las
    pruebas y comandos de validación pertinentes, corrige errores y
    resume los resultados reales.
4.  No afirmes que una integración, reserva, despliegue o prueba
    funciona si no se ha ejecutado y verificado.
5.  No inventes APIs de proveedores. Si una integración externa no está
    definida o no dispone de API viable, implementa una
    interfaz/adaptador y documenta qué falta. No simules una reserva
    real.
6.  Prioriza componentes gestionados y sencillos de Google Cloud. No
    introduzcas microservicios, Kubernetes, un sistema multiagente,
    Redis, una segunda base vectorial o frameworks adicionales sin una
    necesidad demostrable.
7.  El sistema debe ser seguro para uso personal desde el primer
    despliegue: autorización por usuario de Telegram, validación de
    entradas, gestión de secretos, idempotencia, límites de ejecución y
    aprobación de acciones sensibles.
8.  Mantén la configuración por variables de entorno. Añade
    `.env.example` con valores vacíos o no sensibles. Añade
    `.gitignore`.
9.  Usa tipado, funciones pequeñas, interfaces claras, logs
    estructurados y errores explícitos. Fija versiones compatibles de
    dependencias.
10. Si faltan credenciales o acceso a una cuenta, desarrolla y prueba el
    código hasta el límite posible; marca claramente la validación
    pendiente y proporciona instrucciones reproducibles para
    completarla.

### Orden de trabajo obligatorio

-   Fase 0: inspección del entorno y plan.
-   Fase 1: bot Telegram, FastAPI y Ollama Cloud.
-   Fase 2: PostgreSQL, persistencia y tareas.
-   Fase 3: LangGraph, herramientas y aprobaciones.
-   Fase 4: ingesta documental y RAG.
-   Fase 5: despliegue, jobs y endurecimiento.
-   Fase 6: evaluación integral y documentación final.

No avances silenciosamente si una fase no pasa sus criterios de
aceptación.

------------------------------------------------------------------------

## 2. Visión del producto

El usuario conversa con un bot privado de Telegram. Puede hacer
preguntas, guardar preferencias, consultar documentos, pedir
recordatorios, investigar opciones y preparar acciones. El asistente
debe poder recuperar el estado de una tarea tras reinicios o escalado a
cero de Cloud Run.

### Casos de uso MVP

1.  Conversar con un modelo de Ollama Cloud.
2.  Guardar y recuperar preferencias personales explícitas.
3.  Crear, listar, completar y cancelar recordatorios.
4.  Subir un PDF o documento compatible y hacer preguntas sobre su
    contenido con referencias a fuentes.
5.  Consultar información web mediante una herramienta de búsqueda
    desacoplada del proveedor.
6.  Preparar una acción externa y solicitar aprobación explícita antes
    de ejecutarla.
7.  Reanudar workflows interrumpidos y comunicar el resultado en
    Telegram.
8.  Ejecutar tareas programadas mediante Cloud Scheduler y Cloud Run
    Jobs.

### Fuera del alcance inicial

-   Reservas reales completamente automatizadas hasta seleccionar e
    implementar un proveedor con una integración verificable.
-   Envío real de correos, compras, pagos, cancelación de servicios o
    publicación en redes.
-   WhatsApp automatizado.
-   Sistema multiagente, interfaz web de administración, aplicación
    móvil propia o voz en tiempo real.
-   Indexación masiva de carpetas sin límites.
-   Ejecución arbitraria de código, shell o SQL generado por el modelo.
-   Memoria automática e indiscriminada de todas las conversaciones.

------------------------------------------------------------------------

## 3. Stack y decisiones técnicas

  -----------------------------------------------------------------------
  Área                                Decisión para el MVP
  ----------------------------------- -----------------------------------
  Lenguaje                            Python, con tipado

  API                                 FastAPI

  Interfaz                            Telegram Bot API mediante webhook
                                      HTTPS

  Orquestación                        LangGraph

  LLM                                 Ollama Cloud API

  Modelo inicial                      `gpt-oss:120b-cloud`, configurable
                                      y validado en ejecución

  Embeddings iniciales                `embeddinggemma`, configurable y
                                      validado en ejecución

  Persistencia                        PostgreSQL gestionado en Cloud SQL

  Búsqueda vectorial                  `pgvector` en PostgreSQL

  Checkpoints de LangGraph            Checkpointer PostgreSQL persistente

  Documentos originales               Google Cloud Storage

  Contenedores                        Docker

  Ejecución                           Cloud Run Service

  Tareas periódicas                   Cloud Scheduler + Cloud Run Jobs o
                                      endpoint autenticado, según la
                                      tarea

  Secretos                            Google Secret Manager

  Observabilidad                      Cloud Logging y logs estructurados

  Región inicial                      `europe-west1`, configurable

  Pruebas                             pytest, pruebas unitarias,
                                      integración y evaluaciones de
                                      RAG/agente
  -----------------------------------------------------------------------

**Compatibilidad del modelo:** no asumir que un modelo soporta
correctamente tool calling, JSON estructurado o cualquier modalidad solo
por su nombre. Implementar una prueba de compatibilidad que valide
llamadas a herramientas, argumentos, errores y respuestas. Si el modelo
elegido no cumple, documentar el resultado y permitir cambiar
`LLM_MODEL` sin cambiar la lógica de negocio.

**Ollama:** reutilizar el cliente existente si está en el repositorio.
Si no existe, implementar un cliente pequeño y aislado para la API de
Ollama. No mezclar llamadas HTTP al proveedor dentro de los nodos de
negocio. La URL base, autenticación, timeout y modelo deben ser
configurables.

**PostgreSQL:** usar migraciones versionadas. Confirmar que la versión
elegida de PostgreSQL soporta la extensión `vector` y que la integración
de checkpoints de LangGraph es compatible con las versiones fijadas. No
usar almacenamiento en memoria como persistencia de producción.

**Coste:** documentar el coste potencial de Cloud SQL y otros servicios
gestionados. No crear recursos facturables automáticamente sin
aprobación del usuario.

------------------------------------------------------------------------

## 4. Arquitectura lógica

Flujo de mensajes:

1.  Telegram envía un update al webhook HTTPS.
2.  FastAPI valida el secreto del webhook y filtra el usuario/chat
    permitido.
3.  El handler normaliza el mensaje y comprueba idempotencia.
4.  Se carga o crea el contexto conversacional persistente.
5.  LangGraph interpreta la petición y puede consultar memoria,
    recuperar documentos, usar herramientas o pedir aprobación.
6.  Las herramientas validan sus argumentos y aplican políticas de
    permisos.
7.  El resultado se guarda y se responde por Telegram.
8.  Si una ejecución necesita continuar después, el estado se conserva
    en PostgreSQL y una tarea programada puede retomarla o notificarla.

Separar responsabilidades:

-   **Transporte:** Telegram, webhooks y formato de mensajes.
-   **Agente:** estado, prompts, enrutamiento y flujo de herramientas.
-   **Políticas:** autorización, riesgo, aprobación y límites.
-   **Herramientas:** operaciones explícitas y validadas.
-   **Servicios:** lógica de recordatorios, aprobación, notificaciones e
    ingesta.
-   **Persistencia:** repositorios, migraciones, documentos, memoria y
    checkpoints.
-   **Jobs:** trabajos programados e idempotentes.

El modelo propone decisiones; el backend conserva el control de permisos
y efectos externos. Nunca permitir que el LLM ejecute SQL arbitrario,
comandos del sistema o URLs de callback no verificadas.

------------------------------------------------------------------------

## 5. Estructura objetivo del repositorio

Adaptar la estructura a lo que ya exista. Evitar crear archivos vacíos
sin implementación.

``` text
personal-ai-assistant/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── logging_config.py
│   ├── api/
│   │   ├── telegram_webhook.py
│   │   └── health.py
│   ├── bot/
│   │   ├── handlers.py
│   │   ├── keyboards.py
│   │   ├── messages.py
│   │   └── auth.py
│   ├── agent/
│   │   ├── graph.py
│   │   ├── state.py
│   │   ├── prompts.py
│   │   ├── routing.py
│   │   └── policies.py
│   ├── llm/
│   │   ├── ollama_client.py
│   │   └── embeddings.py
│   ├── tools/
│   │   ├── memory.py
│   │   ├── documents.py
│   │   ├── tasks.py
│   │   ├── web_search.py
│   │   └── restaurants.py
│   ├── services/
│   │   ├── approvals.py
│   │   ├── reminders.py
│   │   ├── document_ingestion.py
│   │   └── notifications.py
│   ├── storage/
│   │   ├── postgres.py
│   │   ├── repositories.py
│   │   └── checkpointing.py
│   └── jobs/
│       ├── process_documents.py
│       └── dispatch_reminders.py
├── migrations/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── evals/
├── scripts/
├── Dockerfile
├── .dockerignore
├── .gitignore
├── .env.example
├── pyproject.toml
└── README.md
```

------------------------------------------------------------------------

## 6. Telegram y seguridad de entrada

### Requisitos

-   Crear el bot mediante BotFather; el token se almacena en Secret
    Manager.
-   Configurar webhook HTTPS en Cloud Run.
-   Validar `X-Telegram-Bot-Api-Secret-Token` con comparación segura.
-   Autorizar explícitamente `TELEGRAM_ALLOWED_USER_IDS`; denegar por
    defecto si está vacío en producción.
-   No confiar en el nombre visible, username ni texto del mensaje como
    identidad.
-   Limitar tamaño de mensajes y archivos; manejar mensajes editados,
    callbacks, updates inesperados y errores de Telegram.
-   Registrar `update_id` o identificador equivalente para evitar
    procesar el mismo update dos veces.
-   Responder a callbacks de botones de forma rápida y ejecutar trabajo
    largo de forma desacoplada cuando sea necesario.
-   Implementar comandos `/start`, `/help`, `/reset`, `/tasks`,
    `/memory` y `/forget` o equivalentes documentados.
-   `/reset` limpia el contexto conversacional temporal, pero no debe
    borrar documentos ni preferencias persistentes sin confirmación.
-   `/forget` debe permitir eliminar una memoria explícita o un
    documento y sus fragmentos indexados, con confirmación cuando el
    alcance sea amplio.

### Privacidad

No imprimir tokens, claves, documentos completos ni contenido personal
innecesario en logs. Restringir el acceso a la base de datos y al
bucket. Usar TLS. Definir retención razonable y explicar cómo borrar
datos y documentos.

------------------------------------------------------------------------

## 7. Política de autonomía y aprobaciones

Las políticas se aplican en código, no dependen exclusivamente del
prompt.

  -----------------------------------------------------------------------
  Nivel                   Ejemplos                Política
  ----------------------- ----------------------- -----------------------
  0 --- Lectura           Consultar documentos,   Permitido para usuario
                          buscar información,     autorizado
                          comparar opciones       

  1 --- Acción interna    Crear recordatorio,     Permitido, con registro
  reversible              guardar preferencia     
                          explícita               

  2 --- Compromiso        Reserva, enviar correo, Aprobación explícita
  externo                 crear evento compartido requerida

  3 --- Alto impacto      Pagos, compras,         Deshabilitado en MVP
                          cancelaciones, borrado  
                          amplio                  
  -----------------------------------------------------------------------

### Protocolo de aprobación

Cada acción pendiente debe tener:

-   `action_id` aleatorio y no predecible.
-   `user_id` y `chat_id` autorizados.
-   Tipo de acción y argumentos exactos que se van a ejecutar.
-   Revisión/versionado de los argumentos.
-   Fecha de creación y caducidad.
-   Estado: `pending`, `approved`, `rejected`, `expired`, `executing`,
    `succeeded`, `failed` o `unknown`.
-   Fecha de decisión, ejecutor y resultado.
-   Clave de idempotencia.

Los botones de Telegram deben referenciar el `action_id`; el servidor
vuelve a comprobar usuario, chat, estado, caducidad y argumentos. Una
aprobación solo autoriza la revisión exacta presentada. Cualquier cambio
en los argumentos invalida esa aprobación.

La aprobación debe consumirse de forma atómica. Dos callbacks repetidos
no pueden ejecutar dos veces una misma acción. Si una llamada externa
termina con timeout y no se sabe si el proveedor completó la operación,
registrar `unknown` y verificar el estado antes de reintentar. No asumir
que un timeout significa que la acción falló.

En el MVP, las reservas se pueden investigar y preparar, pero no se debe
afirmar que se realizó una reserva hasta que exista integración viable y
confirmación verificable del proveedor.

------------------------------------------------------------------------

## 8. Modelo de datos mínimo

Diseñar migraciones para estas entidades o equivalentes:

### `conversations`

-   Identificador de conversación o thread.
-   Usuario/chat de Telegram.
-   Fechas de creación y última actividad.
-   Metadatos mínimos necesarios.

### `messages`

-   ID interno y `telegram_update_id` único cuando aplique.
-   Conversación, rol, contenido necesario y timestamps.
-   No almacenar innecesariamente archivos o datos sensibles duplicados.

### `memories`

-   ID, usuario, clave, valor o contenido.
-   Tipo de memoria, fuente, fecha, nivel de confianza y timestamps.
-   Indicar si la memoria fue guardada explícitamente por el usuario.
-   Soportar actualización y borrado.

### `documents`

-   ID, usuario propietario, nombre original saneado, MIME type, tamaño.
-   URI de Cloud Storage.
-   Estado de ingesta: `pending`, `processing`, `ready`, `failed`.
-   Hash del contenido, timestamps y metadatos.
-   Estado de borrado y error de ingesta cuando corresponda.

### `document_chunks`

-   ID, documento, número de fragmento, texto, número de página si está
    disponible.
-   Metadatos y embedding vectorial.
-   Índices para filtrar por propietario/documento y recuperar por
    similitud.
-   No permitir que una consulta de un usuario recupere documentos de
    otro.

### `tasks`

-   ID, usuario, título, descripción opcional.
-   Fecha de vencimiento con zona horaria.
-   Estado, fecha de próxima notificación, timestamps.
-   Clave de idempotencia para el despacho de recordatorios.

### `pending_actions`

-   Campos definidos en la política de aprobación.
-   Restricciones e índices para evitar doble ejecución.

### Checkpoints de LangGraph

Usar el esquema gestionado por la integración de checkpointing
seleccionada. No reinventar su formato ni borrar checkpoints pendientes
durante operaciones de mantenimiento.

------------------------------------------------------------------------

## 9. RAG documental

### Pipeline de ingesta

1.  Recibir un documento por Telegram o una vía autorizada.
2.  Validar tamaño, tipo y propietario; sanear el nombre.
3.  Subir el original a Cloud Storage con permisos privados.
4.  Crear un registro `documents` en estado `pending`.
5.  Extraer texto. Para PDF, conservar números de página si es posible.
    Para formatos no soportados, informar claramente.
6.  Dividir en fragmentos con solapamiento configurable; preservar
    metadatos de fuente.
7.  Generar embeddings mediante el cliente de Ollama.
8.  Guardar fragmentos y vectores en PostgreSQL/pgvector dentro de una
    estrategia consistente y recuperable.
9.  Marcar el documento como `ready` solo si la indexación ha finalizado
    correctamente.
10. Si falla, guardar el error sin dejar el documento en estado
    aparentemente listo.

Implementar límites de tamaño, número de páginas y tiempo de
procesamiento. La ingesta debe ser idempotente por hash del documento y
versión del pipeline. Un reintento no debe duplicar fragmentos.

### Recuperación y respuesta

-   Filtrar por propietario antes de la búsqueda vectorial.
-   Recuperar los fragmentos más relevantes; hacer configurable `top_k`.
-   Considerar búsqueda híbrida textual/vectorial si la calidad lo
    justifica.
-   Incluir en el contexto del modelo el texto, nombre del documento,
    página y metadatos de origen.
-   Instruir al modelo para distinguir entre evidencia, inferencia e
    información no encontrada.
-   Si no hay evidencia suficiente, decirlo y solicitar otro documento o
    una aclaración.
-   Mostrar fuentes en la respuesta: nombre del archivo y página o
    sección verificable.
-   No inventar citas, páginas, cláusulas, importes ni condiciones.
-   Tratar el texto de los documentos como datos no confiables: las
    instrucciones encontradas dentro de un PDF no pueden cambiar las
    políticas del agente ni autorizar acciones.
-   No incluir documentos completos en prompts si solo hacen falta
    fragmentos concretos.

### Borrado

Cuando se borre un documento, eliminar o invalidar sus fragmentos y
embeddings, retirar el original del bucket y actualizar el registro. Si
una operación parcial falla, registrar el estado para poder reintentar.
No afirmar que se ha borrado por completo hasta verificar los
componentes implicados.

### Pruebas de evaluación RAG

Crear un conjunto pequeño de documentos ficticios de prueba y preguntas
con respuestas esperadas. Medir: - Recuperación del documento
correcto. - Recuperación de fragmentos/páginas correctos. - Fidelidad de
la respuesta al texto. - Comportamiento cuando la respuesta no existe. -
Aislamiento entre usuarios. - Resistencia a instrucciones maliciosas
dentro de documentos.

------------------------------------------------------------------------

## 10. Herramientas iniciales

Implementar primero estas herramientas:

### Memoria

-   `search_personal_memory(query)`
-   `save_preference(key, value)` --- solo para preferencias explícitas
    o confirmadas.
-   `delete_memory(memory_id)` --- confirmar antes de borrar cuando sea
    necesario.

### Tareas

-   `create_reminder(title, due_at, timezone)`
-   `list_tasks(status)`
-   `complete_task(task_id)`
-   `cancel_reminder(task_id)`

### Documentos

-   `search_documents(query, document_ids=None)`
-   `get_document_status(document_id)`
-   `request_document_deletion(document_id)`

### Investigación

-   `search_web(query)` detrás de una interfaz de proveedor
    configurable. Si no se ha seleccionado proveedor o credencial,
    devolver un error de capacidad claro, no datos inventados.
-   `search_restaurants(criteria)` y
    `check_restaurant_availability(criteria)` como
    interfaces/adaptadores. La disponibilidad debe venir de un proveedor
    real y quedar marcada con hora/fuente de consulta.

Todas las herramientas deben tener esquemas de argumentos, validación,
timeout, límites, manejo de errores y resultados estructurados. Las
herramientas de escritura deben comprobar la identidad y política de
autorización en servidor.

------------------------------------------------------------------------

## 11. Recordatorios y tareas programadas

No usar `asyncio.sleep`, timers en memoria ni procesos de larga duración
como mecanismo de recordatorio en producción.

-   Guardar recordatorios en PostgreSQL.
-   Usar Cloud Scheduler para lanzar un Cloud Run Job o endpoint de
    despacho autenticado a intervalos definidos.
-   El despachador busca recordatorios vencidos, reclama lotes de forma
    segura, envía mensajes y marca el resultado.
-   Implementar idempotencia para evitar notificaciones duplicadas.
-   Definir zona horaria explícita; usar `Europe/Madrid` como valor
    inicial configurable.
-   Manejar horario de verano, retrasos, errores de Telegram y
    reintentos acotados.
-   Evitar que un trabajo procese dos veces el mismo recordatorio si hay
    ejecuciones concurrentes.
-   Registrar estado y número de intentos.
-   No ejecutar directamente acciones externas de alto impacto desde un
    job sin una aprobación válida y vigente.

------------------------------------------------------------------------

## 12. Configuración

Crear `.env.example` con estas variables, adaptándolas a la
implementación:

``` dotenv
APP_ENV=development
LOG_LEVEL=INFO
GOOGLE_CLOUD_PROJECT=
GOOGLE_CLOUD_REGION=europe-west1

TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=
TELEGRAM_ALLOWED_USER_IDS=

OLLAMA_BASE_URL=https://ollama.com
OLLAMA_API_KEY=
LLM_MODEL=gpt-oss:120b-cloud
EMBED_MODEL=embeddinggemma

DATABASE_URL=
GCS_BUCKET=

APPROVAL_TTL_MINUTES=30
DEFAULT_TIMEZONE=Europe/Madrid
MAX_UPLOAD_MB=20
RAG_TOP_K=5
```

La lista de usuarios debe parsearse y validarse. En producción, cargar
credenciales desde Secret Manager y usar la identidad de servicio de
Cloud Run para acceder a GCS y otros servicios. No incorporar claves de
cuentas de servicio como archivos dentro de la imagen.

------------------------------------------------------------------------

## 13. Despliegue en Google Cloud

### Servicio principal

-   Crear imagen Docker reproducible.
-   Configurar Cloud Run con una cuenta de servicio de mínimo
    privilegio.
-   Configurar webhook HTTPS de Telegram y su secreto.
-   Inyectar secretos desde Secret Manager.
-   Conectar con Cloud SQL usando el método soportado y seguro.
-   Configurar límites de concurrencia, CPU/memoria, timeout y escalado
    según pruebas.
-   Añadir endpoints de salud que no revelen secretos ni información
    interna.
-   Configurar logs estructurados y alertas básicas para errores
    persistentes.

### Recursos auxiliares

-   Cloud SQL PostgreSQL con extensión `vector` compatible.
-   Bucket privado de Cloud Storage, con políticas de acceso mínimas y
    reglas de ciclo de vida solo si son apropiadas.
-   Secret Manager.
-   Cloud Scheduler y Cloud Run Jobs para despachar recordatorios y
    procesar ingestas pesadas si hace falta.
-   IAM de mínimo privilegio.

### Seguridad del webhook y jobs

-   El endpoint del webhook valida el secreto de Telegram.
-   Los endpoints internos de mantenimiento no quedan abiertos sin
    autenticación.
-   Cloud Scheduler usa autenticación admitida por el destino; no
    confiar solo en una URL secreta.
-   No desplegar automáticamente recursos que generen costes sin mostrar
    antes los cambios y obtener autorización.
-   Documentar cómo configurar el webhook, migraciones, variables,
    secretos, despliegue y rollback.

------------------------------------------------------------------------

## 14. Observabilidad y operación

Implementar logs estructurados con: - `request_id`, `update_id`,
`conversation_id` y `thread_id` cuando proceda. - Nombre de herramienta,
duración, estado y código de error. - `action_id` para operaciones
sujetas a aprobación. - Estado de ingesta documental y número de
fragmentos. - Latencia y errores del proveedor LLM.

No registrar tokens, claves, documentos completos, embeddings ni
contenido personal sensible por defecto. Si se necesita depuración de
contenido, debe ser opt-in, limitado y documentado.

Manejar al menos: - Fallo o timeout de Ollama. - Límite de rate o error
de Telegram. - Caída o indisponibilidad de PostgreSQL. - Fallo de
extracción/embedding de un documento. - Callback duplicado o caducado. -
Timeout ambiguo de un proveedor externo. - Reintentos sin duplicar
acciones.

------------------------------------------------------------------------

## 15. Estrategia de pruebas

### Unitarias

-   Configuración y validación de entorno.
-   Autorización de usuario.
-   Validación de esquemas de herramientas.
-   Política de riesgo.
-   Transiciones de estado de aprobación.
-   Conversión de fechas y zonas horarias.
-   Segmentación de documentos y metadatos.

### Integración

-   FastAPI con Telegram simulado.
-   Cliente Ollama mediante mock y una prueba opcional de conectividad
    real.
-   PostgreSQL real de test, migraciones y checkpoints.
-   Guardado, recuperación y borrado de documentos.
-   Ciclo completo de aprobación.
-   Despacho idempotente de recordatorios.

### Evaluaciones de agente

-   El agente pide datos cuando faltan.
-   Selecciona la herramienta adecuada.
-   No afirma haber realizado acciones que solo preparó.
-   No ejecuta una acción de nivel 2 sin aprobación.
-   Rechaza aprobaciones de otro usuario, caducadas o modificadas.
-   Responde "no encontrado" cuando el RAG carece de evidencia.
-   Cita fuentes reales y no inventa páginas.
-   Ignora instrucciones maliciosas encontradas dentro de documentos.

Las pruebas unitarias deben poder ejecutarse sin credenciales externas.
Las pruebas que requieren Ollama Cloud, Telegram o Google Cloud deben
estar claramente separadas y documentadas.

------------------------------------------------------------------------

## 16. Fases y criterios de aceptación

### Fase 0 --- Inspección

-   Identificar el estado del repositorio, código reutilizable,
    versiones y configuración.
-   Presentar plan y riesgos antes de realizar cambios importantes.

**Aceptación:** inventario breve y plan de ejecución concreto.

### Fase 1 --- Telegram + Ollama

-   Webhook, autorización, comandos básicos, respuesta del modelo y
    errores controlados.
-   Dockerfile y prueba local.

**Aceptación:** usuario autorizado puede conversar; usuario no
autorizado no obtiene acceso; tests pasan.

### Fase 2 --- Persistencia y tareas

-   PostgreSQL, migraciones, memoria explícita y CRUD de recordatorios.
-   Checkpoints persistentes.

**Aceptación:** datos y workflows sobreviven al reinicio; los
recordatorios se guardan correctamente.

### Fase 3 --- LangGraph y aprobaciones

-   Grafo con herramientas tipadas, política de riesgo y botones de
    aprobación.
-   Validación de caducidad, usuario, revisión e idempotencia.

**Aceptación:** una acción sensible no se ejecuta antes de la aprobación
y no se duplica ante callbacks repetidos.

### Fase 4 --- RAG

-   GCS, extracción, fragmentación, embeddings, pgvector, recuperación y
    citas.
-   Estados de ingesta, reintentos y borrado.

**Aceptación:** el asistente responde preguntas de documentos de prueba
con referencias correctas y reconoce cuándo no encuentra evidencia.

### Fase 5 --- Despliegue y jobs

-   Cloud Run, Secret Manager, Cloud SQL, GCS y scheduler/job
    configurados con instrucciones reproducibles.

**Aceptación:** despliegue documentado; webhook operativo; job
idempotente; no hay secretos en la imagen ni en logs.

### Fase 6 --- Endurecimiento

-   Evaluaciones integrales, documentación de incidentes y recuperación.
-   Revisión de costes, IAM, límites y datos personales.

**Aceptación:** todos los tests requeridos pasan o los bloqueos externos
quedan claramente identificados y no se presentan como resueltos.

------------------------------------------------------------------------

## 17. Definición de terminado

El proyecto no se considera terminado hasta que:

-   [ ] Se puede ejecutar localmente siguiendo el README.
-   [ ] Existe un `.env.example` sin secretos reales.
-   [ ] Las migraciones se pueden aplicar desde una base vacía.
-   [ ] Las pruebas unitarias pasan.
-   [ ] Las pruebas de integración están implementadas y sus
    dependencias están documentadas.
-   [ ] Telegram está limitado a usuarios autorizados.
-   [ ] El LLM se puede cambiar por configuración.
-   [ ] El estado de LangGraph es persistente.
-   [ ] La memoria explícita y las tareas se conservan entre reinicios.
-   [ ] El RAG presenta fuentes verificables y filtra por propietario.
-   [ ] Las aprobaciones son específicas, caducables, auditables e
    idempotentes.
-   [ ] No hay automatización de reservas ficticia.
-   [ ] Los jobs no dependen de procesos en memoria.
-   [ ] Hay instrucciones de despliegue, configuración del webhook,
    migraciones, operación y rollback.
-   [ ] Se documentan las limitaciones y cualquier paso que requiera
    intervención manual o credenciales del usuario.

------------------------------------------------------------------------

## 18. Entregables que debe producir el agente implementador

1.  Código fuente funcional y tipado.
2.  `pyproject.toml` con dependencias fijadas y comandos de desarrollo.
3.  `Dockerfile` y `.dockerignore`.
4.  Migraciones de base de datos.
5.  Pruebas unitarias, integración y evaluación básica del RAG/agente.
6.  `.env.example` y `.gitignore`.
7.  `README.md` con arquitectura, setup local, comandos de test,
    configuración de Telegram y despliegue.
8.  Documentación de seguridad y política de aprobación.
9.  Scripts o comandos reproducibles para inicialización y migraciones.
10. Informe final que enumere archivos creados/modificados, pruebas
    ejecutadas y sus resultados reales, tareas pendientes, requisitos
    externos y posibles costes.

### Instrucción final

Empieza inspeccionando el repositorio actual y sus herramientas
disponibles. Luego implementa la Fase 1 y verifica sus criterios de
aceptación antes de continuar. No intentes completar todas las fases en
un único cambio masivo. Mantén el sistema pequeño, seguro, observable y
útil desde la primera entrega.
