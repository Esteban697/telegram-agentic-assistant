# Plan Review Log: Telegram personal AI assistant (`PROJECT_SPEC.md`)

Started 2026-10-16 (local time). MAX_ROUNDS=5. PLAN_FILE=PLAN.md.

**Adaptation from the stock claudex-loop skill:** the user has no Codex CLI available and
asked for subagents to fill both roles. The loop is therefore run as:

| Stock claudex-loop | This run |
|---|---|
| Claude (orchestrator + builder, inline) | Orchestrator stays inline; **plan drafting and every revision are done by a builder subagent** ("Claude Code role") |
| `codex exec` / `codex exec resume` (read-only critic, persistent session) | **A persistent, read-only critic subagent** ("Codex role"), resumed across rounds via SendMessage so it does not re-litigate settled points |
| `-s read-only` / `-c sandbox_mode="read-only"` sandbox flag | Prompt-level read-only constraint + post-round verification that the repo file set/hashes are unchanged |
| Human gate #1 (kickoff), Human gate #2 (sign-off) | Unchanged. **No code is written during the loop.** |

The invariant that matters is preserved: the critic has no write access of any kind, and the
loop terminates unconditionally at MAX_ROUNDS.

---

## Round 1 — Critic (adversarial review)

# 1. Blocking flaws

**B1. The reminder ledger protects the *send* but nothing ever advances the *task* — a delivered reminder is re-claimed forever and finally reported as `failed`.**
Where: `PLAN.md` §5.2 (the dispatcher SQL) and §3.2 (`tasks`, `reminder_dispatches`).
The claim: §5.2's dispatcher does claim → `INSERT ... ON CONFLICT (task_id, occurrence_at) DO NOTHING RETURNING id` → `sendMessage` → `UPDATE reminder_dispatches SET state='sent'`. The claim `UPDATE tasks SET claimed_at, claimed_by, attempts = attempts + 1` never touches `next_notification_at`, `tasks` has no delivered/notified column, and no statement anywhere advances the task's state after a successful dispatch. The ledger's unique index prevents the second *send*, but nothing prevents the second *claim*.
Failure scenario: a one-shot reminder is delivered at 09:00. `state` stays `'pending'`, `next_notification_at` stays in the past. Every minute the dispatcher claims it again (`WHERE state='pending' AND next_notification_at <= now()` still matches), increments `attempts`, inserts nothing (conflict), skips, and does not send. After `REMINDER_MAX_ATTEMPTS` the task flips to `'failed'` with `last_error` — so `/tasks` and the dispatcher report tell the user a *delivered* reminder failed, and the per-minute job churns on a growing set of forever-due rows. Spec §11 requires "marca el resultado" and "Registrar estado y número de intentos"; spec §8 requires "fecha de próxima notificación" — the column exists and is never maintained. Separately, the claim query has no `AND attempts < %(max_attempts)s` predicate, so the "attempts cap" §5.2 relies on is never enforced by any shown SQL.
One-line fix: in the same transaction as the ledger mark, `UPDATE tasks SET last_notified_at = now(), next_notification_at = <next occurrence | NULL>, state = CASE WHEN <no further occurrence> THEN 'completed' ELSE state END WHERE id = $1`, add `last_notified_at` to `tasks`, and add `AND attempts < %(max_attempts)s` to the claim.

**B2. A crash between the ledger insert and `sendMessage` silently drops the reminder forever, and decision 5's "ambiguous → `unknown`" claim does not cover it.**
Where: `PLAN.md` §5.2, §3.2 (`reminder_dispatches`).
The claim: decision 5 and §5.2 state that ambiguous outcomes are recorded `unknown` and surfaced. But the crash window *after* `INSERT reminder_dispatches ... state='claimed'` and *before* `sendMessage` leaves the row at `'claimed'` permanently: no code path transitions a stale `claimed` row, and the unique constraint means no later run can re-insert for that occurrence, so the dispatcher skips it in silence. This is not an "ambiguous" outcome — the send provably never happened — yet it is neither retried nor reported as `unknown`. Compare §5.4, which *does* give documents a reclaim (`claimed_at < now() - interval '15 minutes'`); `reminder_dispatches` has no equivalent, and `DISPATCHER_LEASE_SECONDS` is defined in §8.1 but used nowhere.
One-line fix: add a reconciliation pass that takes `state='claimed' AND claimed_at < now() - DISPATCHER_LEASE_SECONDS` to `unknown` (never auto-resend, since Telegram has no idempotency key) and includes it in the dispatcher report.

**B3. Phase 4's stated acceptance criteria cannot be met with the stub embedding provider, so "the blocker is narrow" is an over-claim.**
Where: Key decision 15, §9.2 (last bullet), §10 Phase 4 (acceptance (a), (b) and blockers), §13.1.
The claim: decision 15 defines the stub as producing "**hash-derived unit vectors**"; §10 Phase 4 says extraction, chunking, storage, retrieval ordering, isolation, deletion and evaluation scoring "is built and tested with `EMBED_PROVIDER=stub` in the meantime, so the blocker is narrow"; Phase 4 acceptance (a) requires "a question answerable from a fixture returns the correct file name **and page**" and §9.2 asserts the stub run has "citations carry the right page".
Failure scenario: hash-derived vectors bear no relation to text similarity, so `search_chunks` returns arbitrary chunks. Acceptance (a) can pass only by luck, and "citations carry the right page" is unprovable. The same applies to §9.3's "correct document retrieved for a known question". A builder following the plan will be unable to demonstrate Phase 4's acceptance and will either fudge the fixture or be stuck — which is exactly what spec §1.3/§1.4 forbid.
One-line fix: define the stub as a deterministic **lexical** embedding (hashed token-count vector, L2-normalised) so cosine similarity tracks term overlap on fixtures with distinct vocabulary, and mark (a)/(b) explicitly as blocked on §13.1 instead of "narrow".

**B4. The approval *consumption* statement drops two of the five re-checks spec §7 requires, and its zero-row outcome is reported with a specific, possibly wrong cause.**
Where: `PLAN.md` §4.2 step 2.
The claim: the plan calls this statement the one that satisfies spec §7's atomic-consumption requirement, but its `WHERE` carries only `action_id`, `state='approved'`, `args_revision`, `expires_at > now()`. Spec §7 requires the server to re-check "usuario, chat, estado, caducidad y argumentos". Step 1 (the decision CAS) does check `user_id`/`chat_id`; step 2 does not. And zero rows is asserted to mean "somebody else has it — already being executed or completed", although zero rows is equally produced by `expires_at <= now()` (the `approved → expired` transition of §4.1) or by a mismatched `args_revision`.
Failure scenario: (i) any future executor that is not the same handler holding step 1's returned row (a job, a retry path, a reconciliation flow) can claim any `approved` action by `action_id` alone; (ii) a user whose approval TTL elapsed between the decision and the claim is told "ya se está ejecutando o se ha completado" for an action that in fact expired and never ran.
One-line fix: add `AND user_id = %(user_id)s AND chat_id = %(chat_id)s` to the claim, and on zero rows perform the diagnostic read (already described in step 1) choosing the Spanish message from the row's actual state instead of asserting a cause.

**B5. `tool_invocations` is keyed on the model-generated `tool_call_id`, which the plan never establishes is stable across a resumed node — the "side effect runs at most once" guarantee fails exactly in the case it exists for.**
Where: `PLAN.md` §3.2 (`tool_invocations`), §5.3, §3.3, §10 Phase 3 acceptance (f).
The claim: §3.2 says `tool_call_id` is "The model's tool-call identifier", and `UNIQUE (thread_id, tool_call_id)` is asserted to make "a tool call run at most once per graph run". §5.3's re-entry rules all key on that pair.
Failure scenario: the budget interrupts a node *before* the model's tool call is checkpointed. On resume the node re-executes the model call; the model returns a new (non-deterministic) `tool_call_id` with possibly re-worded arguments. The ledger has no row for the new id, so the side effect executes a second time — for a level-1 write tool that is a duplicate reminder/preference/deletion. Nothing in the plan verifies that the id is replayed identically (Appendix B.2 defers checkpoint internals, and the plan never says the id comes from the checkpointed state rather than from a fresh model response).
One-line fix: key the ledger on LangGraph's stable task/checkpoint identifier (or hash of node + canonical args) rather than on the model's `tool_call_id`, and state that the key must be read from the checkpointed state, never from a fresh model response.

**B6. The Phase 1 in-memory `DedupeStore`/`ConversationStore` have no production fail-fast guard, so the plan's own safety pattern is not applied to its weakest component.**
Where: `PLAN.md` §10 Phase 1 deliverable 10 (and §7.2's validation table, decision 15/§8.1's `EMBED_PROVIDER=stub` guard, §7.2's `DEV_ALLOW_ANY_USER` guard).
The claim: decision 19 ships in-memory dedupe and context "explicitly marked single-instance-dev-only". §8.1's validators refuse `stub`, `DEV_ALLOW_ANY_USER=true`, `ENABLE_LEVEL3=true` and an empty allow-list under `APP_ENV=production` — but nothing refuses the in-memory stores. With `--min-instances=0 --max-instances=3` (§11.2), a Phase 1 image deployed to Cloud Run loses the update-id idempotency that Risks item 5 and §5.1 call the safety basis for scale-to-zero, and A3's "second turn's prompt contains the first turn's messages" silently breaks across instances. Spec §1 rule 7 requires the system be "seguro para uso personal desde el primer despliegue".
One-line fix: make the in-memory store selection a startup error when `APP_ENV=production` (the same validator pattern already used for `EMBED_PROVIDER=stub`), so the unsafe configuration cannot be deployed by accident — and state that Phase 1 is local-only.

**B7. The active embedding model has two sources of truth (`embedding_models` row and `RAG_EMBED_MODEL_ACTIVE`), and their disagreement is the failure mode decision 9 exists to prevent.**
Where: `PLAN.md` §6.4 (retrieval "filters `embed_model = <active model>`" from the `embedding_models` table) versus §8.1 (Agent group lists `RAG_EMBED_MODEL_ACTIVE`) and §8.2 (`/readyz` checks "active embedding model resolvable").
Failure scenario: ingestion writes chunks stamped `embed_model = EMBED_MODEL` (config) while retrieval filters on the `embedding_models` row; if they disagree, retrieval returns zero rows forever and the user sees "hay que reindexar" with no way to know which knob to turn — or, if the retrieval filter is implemented from config instead, the plan's guarantee that "changing `EMBED_MODEL` degrades to no results rather than plausible-but-meaningless neighbours" (decision 9) is not actually enforced by the row-level provenance it claims.
One-line fix: delete `RAG_EMBED_MODEL_ACTIVE` and make the DB row the single authority (written at ingest, read by retrieval and `/readyz`), or drop the table and filter on config — but not both.

# 2. Non-blocking flaws / improvements

**N1. The `args_hash` predicate is absent from the claim and its immutability is convention-only.** Decision 4 says the cost of the guarded UPDATE is that "losing a predicate silently downgrades to double execution", yet the claim guards `args_revision` but not `args_hash`, and §4.1's guarantee that "there is no code path that writes to `args_json` of a row in approved state" is enforced by nothing in the schema. *Fix:* add a `BEFORE UPDATE` trigger raising when `args_json`/`args_revision` change outside the creation flow, or include `args_hash` in `callback_data` and guard on it (it is compared against the row, so it adds no trust).

**N2. §4.2 contradicts itself on when the callback is answered.** Bullet 5 says `answerCallbackQuery` is sent *before* step 2; bullet 3 says the answer to a zero-row *claim* says "already being executed or completed". An answer sent before the claim cannot report the claim's result. *Fix:* ack the callback immediately with a neutral message and communicate the claim outcome by editing the decision message.

**N3. `REQUEST_BUDGET_SECONDS=90` exceeds Telegram's 60-second webhook deadline** (verified; `getWebhookInfo` then shows "Read timeout expired"). Telegram re-POSTs while the first request is still inside its budget; the plan absorbs this via dedupe but Risks item 5 analyses only cold starts. *Fix:* default the budget below 60s (e.g. 45s) and state that the budget is the hand-off point, not the response deadline.

**N4. Dead configuration.** `RATE_LIMIT_PER_MINUTE` and `RETENTION_DAYS` appear only in §8.1 and have no implementation, test or consumer anywhere (§13.7 defers retention; nothing implements rate limiting, which spec §1 rule 7 lists under "límites de ejecución"). This contradicts §1.4 ("no file is created without an implementation") and §2.4's "no dead configuration". *Fix:* implement a per-user token bucket in the webhook (a handful of lines, credential-free test) or delete both vars and record the deferral.

**N5. Count drift between sections.** Decision 18 says "one Docker image, **two** entrypoints"; §11.1 says "one image, **three** entrypoints" and lists four commands. §2.1 and decision 12 say the Telegram client has "**five** methods"; Phase 1 deliverable 6 lists seven to eight (`setWebhook`/`deleteWebhook`/`getWebhookInfo`). *Fix:* state the authoritative lists once and reference them.

**N6. `delete_memory` is missing from the risk-level table.** Spec §10 requires `delete_memory(memory_id)` and §2.1 places it in `tools/memory.py`, but §4.3's level table omits it, so the first run of the registry test ("every tool maps to a level") fails. *Fix:* add it at level 1.

**N7. Inconsistent pins worth re-checking before betting on them.** `langgraph-checkpoint-postgres 3.2.0` was released 2026-10-14 — two days before this plan — and the design's resume/exactly-once machinery depends on its internals, which the plan itself defers to Appendix B.2. *Fix:* pin 3.1.3 unless a 3.2.0 feature is required, or record the upgrade risk explicitly.

**N8. Decision 6's IVFFlat rationale is factually overstated.** pgvector *permits* creating an IVFFlat index on an empty table and only *recommends* building after load for recall; it does not "contradict" migrating from an empty database. The conclusion (HNSW) is still correct, for the real reasons in Appendix A.2 (recall, and `hnsw.iterative_scan = strict_order` from 0.8.x). *Fix:* correct the justification before a reviewer dismantles it.

**N9. Decision 8's cost claim is stronger than reality.** `PostgresSaver`/`AsyncPostgresSaver` do accept an injected connection object; the practical blocker is psycopg `autocommit=True`/`row_factory=dict_row` semantics (Appendix A.5), not that the library "does not cleanly support" injection. The two-pool conclusion survives, the reasoning should be restated.

**N10. Connection-pool arithmetic is never computed.** §11.2 sets `--concurrency=8` and `--max-instances=3` while Risks item 4 defers sizing; with modest pools (5 + 5) that is 30 connections against an instance tier that may not have them — a hard outage, not a slowdown. *Fix:* state `(DB_POOL_SIZE + CHECKPOINTER_POOL_SIZE) × max_instances <= Cloud SQL max_connections` in §11.2 and derive the tier from it in §12.

**N11. `hmac.compare_digest` on `str` raises `TypeError` for non-ASCII input**, so a crafted `X-Telegram-Bot-Api-Secret-Token` produces a 500 rather than the documented 403-empty-body, and the error path logs an exception instead of a clean denial. *Fix:* compare bytes with a guarded encode, wrap in `try/except -> 403`, and add the test (the plan's §7.1 tests cover missing/wrong/case only).

**N12. The poisoned-document eval only asserts level-2 safety.** §6.6's eval asserts no level-2 action reaches `executing` and no destination appears in a tool call, but injected text can still drive level-1 *destructive* tools (`request_document_deletion`, `delete_memory`, `create_reminder`), which are allowed-with-logging by §4.3. *Fix:* add an assertion that no level-1 write tool is invoked with arguments that appear only inside the untrusted block.

**N13. Phase 1's A10 has muddled pass/fail semantics and no fallback.** `verify_ollama.py` "must exit non-zero if tool calling is unsupported" while A10 is claimed satisfiable with a failing probe ("document the result and confirm `LLM_MODEL` can be swapped") — but Phase 3/4's entire tool graph presumes tool calling, and no alternative model is named. *Fix:* name at least one alternate cloud model and/or a JSON-schema fallback dispatch path, and state explicitly that a failed probe re-scopes Phase 3's acceptance.

**N14. Phase 1 implementation gaps a builder would have to guess at.** (i) The `Store` protocols and in-memory implementations (Phase 1 deliverable 10) have **no module path** in §2.1's tree and **no layer** in §2.2's matrix, although §1.4 promises every addition is named in §2.3. (ii) No config values exist for the context cap/TTL or the dedupe TTL/bound. (iii) `/reset`'s Phase 1 behaviour is undefined (thread rotation only exists from Phase 2) while A11 asserts a `/reset` reply. *Fix:* add `app/storage/stores.py` to §2.3 and §2.2 with a layer, add `CONTEXT_MAX_MESSAGES`/`CONTEXT_TTL_SECONDS`/`UPDATE_DEDUPE_TTL_SECONDS`, and specify Phase 1 `/reset` = drop that chat's context list.

**N15. `tool_invocations` rows stuck at `started` have no resolution path.** A crash between the ledger commit and the side effect leaves a row that §5.3 treats as `unknown` forever; unlike approvals (which have an on-demand verify flow, §4.2 step 3), there is no user-visible way to clear it, so the thread can never re-run that call. *Fix:* give the user an explicit retry that mints a new ledger key and marks the old row `unknown` with the reason.

# 3. Over-engineering verdict

| Contested addition | Verdict | Reasoning (one line) |
|---|---|---|
| `work_items` + `jobs/resume_work.py` + the third Scheduler job | **Cut, or re-justify on a correct premise** | Decision 2's rejection of Cloud Tasks rests on calling it a forbidden "framework" and on OIDC plumbing in app code; Cloud Tasks is a *managed Google Cloud* component that spec §1 rule 6 explicitly prefers, spec §3 expressly permits "endpoint autenticado", Cloud Run enforces invoker IAM at the platform level (no `google-auth` in the app), and Cloud Tasks has native per-task-name dedupe (`ALREADY_EXISTS`) — i.e. it reimplements `work_items`' partial unique index natively, plus delay scheduling and bounded retry. Dropping it removes a table, a job, a Scheduler schedule and its IAM binding. |
| `tool_invocations` ledger | **Keep** | Genuinely required by decision 2's re-executed node; but fix the key (B5). |
| Two schema mechanisms (Alembic + `PostgresSaver.setup()`) | **Keep** | Spec §8 forbids reinventing the checkpointer format; the `include_object` exclusion is the honest price. |
| Hand-rolled `httpx` Telegram client | **Keep** | Rule 6 restraint is real and the surface is small; fix the method-count drift and note `ok:false` envelope + multipart `getFile` handling. |
| `embedding_models` table + per-chunk `embed_model` | **Keep** | Row-level provenance is the right cure for the worst RAG failure mode; resolve the duplicate knob (B7). |
| `app/policy/` leaf + the five AST tests (layering, vector chokepoint, SQL parameterisation, messages catalogue, secret scan) | **Keep** | ~60 lines of stdlib each, credential-free, and they convert spec requirements into checkable invariants — cheaper than the linters they replace. |
| `inbox_updates` + `messages.telegram_update_id` redundancy | **Keep** | Spec §8 asks for the latter; one nullable btree, and the plan's own Risks item 11 offers the trade. |
| 3 Scheduler jobs at 1-minute cadence | **Trim to two** | The reminders sweep justifies itself; the resume sweep disappears with Cloud Tasks; the documents sweep is fine. |
| The plan's 2,301 lines against "presenta un breve plan" | **Not material on its own, but the drift is** | Length is defensible for a contract document; what is not defensible is that the same fact is restated in three places and *disagrees* (entrypoint count, method count, missing tool in the risk table, two active-model sources of truth) — that is the noise argument, and the fix is de-duplication, not truncation. |

# 4. Spec requirements with no plan coverage (explicit orphans)

1. **§6 "manejar mensajes editados"** — no policy anywhere; `inbox_updates.kind` has no `edited_message` value, and §7.1's `allowed_updates` "limited to the updates we handle" silently excludes the type, which is ignoring, not handling.
2. **§9 "Considerar búsqueda híbrida textual/vectorial si la calidad lo justifica"** — never mentioned; and `document_chunks` has no `tsvector`/GIN index, so the fallback would need a migration later. One line recording the consideration (deferred, with a recall threshold) closes it.
3. **§13 "reglas de ciclo de vida [del bucket] solo si son apropiadas"** — the plan is silent both ways; a lifecycle rule is arguably unnecessary because deletion is explicit, but the decision is never recorded.
4. **§10 `delete_memory(memory_id)`** — present in §2.1's tree, absent from §4.3's risk registry (N6).
5. **§14/§1.7 "límites de ejecución"** — `RATE_LIMIT_PER_MINUTE` is declared and never implemented (N4).
6. **§12/§11 "Registrar estado y número de intentos" for reminders** — nominally covered, but incoherently enough that it does not hold (B1).

Everything else maps: I cross-checked all 15 spec §17 items against §14's table (each has a real demonstration, including item 3's migration gate, item 8's restart-resume test and item 13's no-in-memory-timer test), all 10 spec §18 deliverables, every file in spec §5's tree (the plan contains all of them at matching paths, plus the documented additions), all 8 spec §2 use cases, all 8 spec §4 flow steps, all 7 spec §15 unit areas, all integration items and all 8 agent/RAG eval assertions. Traceability is genuinely strong — this is not a hand-waving plan.

# 5. Independent verification results

**Ollama Cloud embeddings (highest-impact claim): the plan is right, and the blocker is real.** I re-checked independently. [Issue #14496](https://github.com/ollama/ollama/issues/14496) is closed; maintainer `rick-github` states "No, there are no embedding models in Ollama Cloud", "There is currently no public roadmap for adding embeddings to Cloud", and "Embeddings are supported on local models"; the reported response was `404 {"error": "path \"/v1/embeddings\" not found"}`. [Issue #17129](https://github.com/ollama/ollama/issues/17129) is open, opened 2026-07-11, last activity 2026-09-21, and reports a chat-only cloud catalogue with no `/v1/embeddings` route — but **no maintainer statement is present on that issue**, and the plan's Risks item 1 phrase "two GitHub issues — #14496, where a maintainer states …" reads as if both carried maintainer authority. Minor attribution flaw, not a factual one. The mitigation (`EmbeddingProvider` port, `vector(768)` common to every candidate, `EMBED_DIM` assertion) is sound and does not need restructuring — the phase order is fine; only Phase 4's *acceptance* claim is wrong (B3).

**Pinned versions: the pin table holds up.** Live index checks during this round: `langgraph-checkpoint-postgres` **3.2.0** is the current release (uploaded 2026-10-14) with exactly the dependencies the plan lists (`langgraph-checkpoint>=4.3.0,<5.0.0`, `orjson>=3.11.5`, `psycopg>=3.2.0`, `psycopg-pool>=3.2.0`, Python >=3.10) — Appendix A.4 is accurate. `pgvector 0.5.1` exists and is current, `requires_python >=3.10`, with no declared dependencies. `sqlalchemy 2.1.4` is current, `requires_python >=3.11` (matching the plan's note). `fastapi 0.143.3` is current, `requires_python >=3.10`. So Appendix A.4 is not invented; only the freshness risk (N7) and the `VECTOR` vs `Vector` export ambiguity (which the plan correctly defers to Appendix B.4) remain.

**pgvector limits and index behaviour: correct, except decision 6's IVFFlat premise.** The 16,000-dimension storage / 2,000-dimension index limits, HNSW buildable on an empty table, and `hnsw.iterative_scan` requiring 0.8.x are as stated in Appendix A.2; the plan's `SET LOCAL hnsw.iterative_scan = strict_order` inside the retrieval transaction is the correct usage. Decision 6's claim that IVFFlat "contradicts" migrating from an empty database is an overstatement (N8).

**Telegram: secret-token format and header confirmed; the 60-second deadline confirms N3.** `secret_token` is 1–256 chars of `[A-Za-z0-9_-]`, sent as `X-Telegram-Bot-Api-Secret-Token`, and a Bot API server maintainer states plainly "The timeout is 60 seconds" (exceeded -> `getWebhookInfo.last_error_message = 'Read timeout expired'`). A budget default of 90 therefore sits past Telegram's deadline.

**Cloud Tasks (relevant to the over-engineering verdict): dedupe confirmed.** Creating a task with an existing (or recently deleted/executed) task ID fails with `ALREADY_EXISTS`; documented dedupe windows conflict across pages (4 hours, up to 24 hours, ~1 hour in the older v2beta3 reference) — any of them covers the resume window. This is the capability `work_items` reimplements by hand, and it is a managed Google Cloud service, not a spec-forbidden component.

**Not re-verified by me** (and therefore flagged as carried on the plan's authority): Cloud SQL's pgvector 0.8.5 availability (self-checking, because the migration asserts `extversion >= 0.8`), Vertex `text-embedding-005` output dimensions (only relevant if §13.1 chooses option (b)), `--set-secrets` version-pinning semantics, Cloud Run CPU throttling (Appendix A.10 — consistent with known behaviour and the basis for decision 2), and the checkpointer's `setup()` DDL (the plan already lists it in Appendix B).

Sources: [ollama/ollama#14496](https://github.com/ollama/ollama/issues/14496), [ollama/ollama#17129](https://github.com/ollama/ollama/issues/17129), [Telegram webhook guide](https://core.telegram.org/bots/webhooks), [tdlib/telegram-bot-api#341](https://github.com/tdlib/telegram-bot-api/issues/341), [Cloud Tasks: create](https://cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues.tasks/create), [Cloud Tasks quotas](https://docs.cloud.google.com/tasks/docs/quotas), [langgraph-checkpoint-postgres on PyPI](https://pypi.org/project/langgraph-checkpoint-postgres/), [pgvector on PyPI](https://pypi.org/project/pgvector/).

---

The plan's architecture, layering, owner-isolation chokepoint, approval CAS at READ COMMITTED, prompt-injection stance and traceability are all sound and better than average. But it cannot be implemented as written: a delivered reminder becomes a `failed` task (B1) and a crash silently drops one (B2); Phase 4's own acceptance is unreachable with the stub it specifies (B3); the approval consumption statement omits spec-mandated identity predicates (B4); and the resume-safety ledger is keyed on an id the plan never establishes as stable (B5).

VERDICT: REVISE

---

### Claude's response (round 1)

Verdict accepted: **REVISE**. Rulings below. The revised `PLAN.md` is **2,994 lines / 240,728 bytes**
(up from 2,301 / 175,142).

**Accepted and revised:** all of B1–B7, all of N1–N15, and all six orphaned spec requirements, plus
my five additions (A1–A5) — see the builder's own change list in the round-2 prompt.

**Rejected, with reasons** (the plan implements none of these):

- **Cloud Tasks in place of `work_items`.** Kept `work_items`. The critic's own alternative was
  "cut, **or re-justify on a correct premise**"; I took the second branch. Its two stated reasons
  for the plan's rejection were genuinely wrong and were withdrawn: Cloud Tasks is not a
  spec-forbidden "framework" (it is a managed Google Cloud service), and Cloud Run enforces invoker
  IAM at the platform level, so OIDC verification adds no `google-auth` to application code. But
  `work_items` still wins on grounds that hold: it adds **zero** new GCP products (spec rule 6 is a
  cap on additions, and Cloud Tasks would be the only service introduced solely for resumption),
  it keeps the queue in the same database and the same transaction as the checkpoint it resumes,
  and it preserves the "no internal HTTP endpoint at all" property rather than re-adding a surface.
  **This is a judgment call, not a certainty** — Cloud Tasks is genuinely more managed, and the
  plan now records that counter-position rather than hiding it.
- **Trimming three Scheduler jobs to two.** Follows from keeping `work_items`.
- **Truncating the document.** The critic did not treat length as material and named the real
  defect as restated facts *disagreeing*. Fixed by de-duplication (§15 "Stated once" + the
  authority index), not deletion.

**Rulings on the three points the builder disputed rather than absorbing.** All three were good
objections; two went the builder's way.

1. **N7 (pin `3.1.3`) — resolved with evidence.** The builder refused to pin a version it had never
   verified, on the grounds that asserting an unverified version number violates spec rule 5 — a
   correct objection to the critic's *specific* fix. I checked PyPI directly:
   `3.1.3` exists (uploaded 2026-10-12), `requires_python >=3.10`, and its dependency set
   (`langgraph-checkpoint>=4.3.0,<5.0.0`, `orjson>=3.11.5`, `psycopg>=3.2.0`, `psycopg-pool>=3.2.0`)
   is **identical** to `3.2.0`'s. So the critic's fix is safe but nearly worthless — `3.1.3` is
   itself only four days old against `3.2.0`'s two. **Ruling:** the builder's position stands (pin
   the verified-current `3.2.0`, record the freshness risk, verify at implementation), and the plan
   gains one evidence-backed note neither party had: because the dependency sets are identical,
   downgrading to `3.1.3` is a **zero-dependency-cost fallback** if `3.2.0` misbehaves.
2. **A3 (Cloud SQL tier) — the builder's objection upheld.** The connection arithmetic's
   "minimum tier that satisfies the constraint" lands on a shared-core tier that Google **excludes
   from the Cloud SQL SLA**. Minimum-cost and appropriate-for-a-component-that-must-not-lose-
   reminders are different answers. **Ruling:** the first dedicated-core tier becomes the
   *recommendation*; the shared-core tier stays costed as the cheaper alternative with the SLA
   exclusion stated; the final choice remains the user's at §13.6.
3. **Ground 4 for `work_items` — demoted.** "The dedupe we need is the one we must write anyway" is
   the one ground a round-2 critic can legitimately attack, because Cloud Tasks' native
   `ALREADY_EXISTS` genuinely would remove the *enqueue-time* need for the partial unique index.
   **Ruling:** grounds 1–3 are load-bearing; ground 4 is supporting colour, and the acknowledged
   counter-position stays visible in the plan.

**Round 2 focus:** do the fixes actually fix the flaws (verified, not assumed); what the 30%
growth broke; whether grounds 1–3 survive attack; and whether §15 removed the drift or relocated it.

---

## Round 2 — Critic (adversarial review)

# 1. Blocking flaws

**RB1. §5.5's ground 2 asserts an atomicity that the plan's own design (decision 8 / Risks item 4) makes impossible, and the budget handoff can therefore lose a turn silently.**
Where: `PLAN.md` §5.5 ground 2 vs decision 8 and Risks item 4.
The claim: "Resume work is already inside the same transactional boundary as the checkpoint it resumes, so a turn's state, its queue entry and its ledger row commit or roll back together." Decision 8 and Risks item 4 state the opposite: the checkpointer is constructed from a connection string on its own pool, the ORM has a second pool, and "the checkpointer and the ORM cannot share a pool". LangGraph commits each checkpoint in its own transaction on the checkpointer connection; the handler inserts the `work_items` row afterwards, on the ORM connection. Two pools, two transactions, ordered — not one boundary.
Failure scenario: the process dies (or is SIGTERM-drained, which §11.1 names as *the reason the resume queue exists*) after the last checkpoint write and before the `work_items` insert. The thread is checkpointed but never queued, so the resume job never sees it, the user gets no reply to that message, and nothing is logged as failed — the silent-no-reply case decision 2 was built to eliminate, in exactly the scenario §11.1 cites. The same is true of the "ledger row" half of the claim: `tool_invocations` is written by the tool wrapper in the ORM transaction, never in the checkpointer's.
One-line fix: stop claiming atomicity and make the window crash-safe — insert a watchdog `work_items(thread_id, kind='resume_thread', run_at = now() + REQUEST_BUDGET_SECONDS)` *before* entering the graph and mark it `done` on a successful reply, so any death inside the turn leaves a queued resume rather than a silent drop.

**RB2. §4.5's "every distinct cause gets its own message" collapses two branches onto one key, so §9.1's exhaustive-distinctness test cannot pass as written.**
Where: `PLAN.md` §4.5 rows 4 and 8, against §9.1's "Zero-row diagnosis" row.
The claim: §4.5 states each branch "maps to its **own** message key", and §9.1 asserts "**Every zero-row branch of §4.5 maps to a distinct catalogue key (asserted exhaustively)**". But row 4 (`state IN ('executing')`) and row 8 ("claim returned zero rows but the re-read shows `state='approved'`") both map to `approval.in_progress`.
Failure scenario: the builder implements §4.5 literally, the exhaustive-distinctness test fails on its first run, and the builder must guess which of the two contradictory instructions wins — the exact "a phase cannot pass its own acceptance criteria" condition. It also degrades the user experience B4 was raised to fix: the two causes a user is most likely to hit are reported identically.
One-line fix: merge rows 4 and 8 into one row with the shared cause ("another caller holds, or has just taken, the claim") and assert distinctness over the merged set, or mint a separate key (`approval.just_claimed`) for row 8.

**RB3. A same-model re-index leaves both pipeline versions' chunks retrievable, so the RAG context silently contains stale duplicates.**
Where: `PLAN.md` §6.4 (re-index flow and the retrieval filter) and §3.2 (`documents.pipeline_version`, `document_chunks.pipeline_version`, `embedding_models.pipeline_version`).
The claim: §6.4 says re-indexing is "`EMBED_MODEL` change → re-run ingestion with `pipeline_version + 1`, which the `(user_id, content_sha256, pipeline_version)` unique index already permits without touching the old version's rows", and that retrieval "filters `embed_model = <row's active model>`". Nothing ever reads `pipeline_version` on the retrieval path (grep confirms the only occurrences are the storage/ingest side), yet the old version's chunk rows survive the re-index by design.
Failure scenario: the user re-indexes with the *same* model (a pipeline fix, a chunker change, an accidental re-upload after `EMBED_MODEL` is set back), or switches to a different model and back. Both the v(N) and v(N+1) chunks of the same document carry the same `embed_model`, retrieval returns both, and the model is handed two versions of the same passage in one context — stale text presented as current, with citations that are individually real but mutually contradictory. The spec's "No inventar citas, páginas, cláusulas..." protection does not cover this because no citation is invented; both are genuine.
One-line fix: filter `document_chunks.pipeline_version = <active `embedding_models` row's `pipeline_version`>` inside the single chokepoint query (§6.5) — the column exists on both sides for exactly this — and delete the superseded version's chunks when the new version reaches `ready`.

**RB4. §5.3's re-entry rules have no stated precedence, so A1 cannot detect which rule fired, and `run_key` is not injective when a resumed node re-emits its tool calls in a different order.**
Where: `PLAN.md` §5.3 (rules 1–3 and the A1 test descriptions), §3.2 (`tool_invocations`).
The claim: rule 1 ("existing row with `state='succeeded'` → return the recorded `result_json` verbatim") and rule 2 ("existing row with a mismatching `args_hash` → hard error") both apply to the same row in A1 direction 1, whose stub model deliberately "returns a *different* `tool_call_id` and re-worded arguments on the second call". The plan never says which rule wins, and both outcomes satisfy "exactly one side effect", so the test as described cannot distinguish them. Separately, `run_key = parent_checkpoint_id:node_name:tool_call_ordinal` identifies a call by position only: if the resumed model re-emits the same two calls reordered, ordinal 0 now denotes the other tool, and if that tool's `args_hash` happens to match a row (identical arguments under a reused key is exactly what rule 2 exists for), the wrapper can return *tool A's* recorded result as *tool B's* answer via rule 1.
Failure scenario: a re-executed node returns the wrong tool's recorded result for a write tool — a silent wrong answer where the plan promises a loud `ToolCallArgsMismatch`.
One-line fix: state the precedence explicitly ("a mismatching `args_hash` on any existing row is a hard error, including `succeeded`; replay happens only on an exact hash match") and include `tool_name` in `run_key` so reordering becomes a loud mismatch.

# 2. Non-blocking flaws, and what the 30% growth broke

**RN1. The drift §15 was created to kill survives inside §15's own target list.** `PLAN.md` §2.1's tree still reads `telegram_client.py (+) # typed httpx Bot API client (5 methods)`, while §15 declares the seven-method list the single authority and explicitly names "§2.1's tree comment" as a location that "must reference it, never restate it". *Fix:* change the comment to point at §15.

**RN2. A second stale copy of a fact that moved.** §7.2's fail-fast table still justifies rejecting `EMBED_PROVIDER=stub` with "**Hash-derived** vectors are not a real embedding", contradicting decision 15's lexical redefinition and §6.4. *Fix:* reword to "the stub is a test double, not a real embedding model".

**RN3. Circular authority.** §11.1 says "the count is stated in §15"; §15's table names §11.1 as the authority for "the image's commands / entrypoints". Each points at the other. *Fix:* keep §11.1 as the authority (it already states "one image, four commands") and delete the "stated in §15" sentence.

**RN4. Four config variables have no defaults, while §15 declares §8.1 the authority for "every configuration variable and its default".** §8.1's Ops row lists `REMINDER_BATCH_SIZE`, `REMINDER_MAX_ATTEMPTS`, `DISPATCHER_LEASE_SECONDS`, `STALE_INVOCATION_SECONDS` with no values, unlike every other row. This matters for correctness, not tidiness: B2's fix is only sound if `DISPATCHER_LEASE_SECONDS` exceeds the dispatcher's own maximum run time, otherwise a concurrent run's Phase 0 reconciles an in-flight send to `unknown` while the message is being delivered. *Fix:* give all four defaults and state the "lease ≥ job timeout" constraint next to the lease.

**RN5. The zero-row case of Phase 2's ledger CAS is undefined.** §5.2 shows `UPDATE reminder_dispatches SET state='sent' ... WHERE id = %(dispatch_id)s AND state='claimed'` followed by the task `UPDATE` that appears unconditional. If Phase 0 has just reconciled the same row to `unknown` (a live possibility given RN4), the plan does not say whether the task is still advanced. *Fix:* state the rule — a zero-row ledger CAS means "another run reconciled this occurrence"; still advance the task from the delivery result and log the ledger discrepancy.

**RN6. Attempts are burned by contention.** §5.2's claim does `attempts = attempts + 1` before the ledger insert; a claim whose insert conflicts (the occurrence was already handled by a concurrent run) still consumes an attempt, so a task can reach `max_attempts` through overlapping dispatcher runs rather than delivery failures — now consequential because B1 made the cap a real predicate. *Fix:* increment `attempts` only when the ledger insert returned a row.

**RN7. §11.3's Pub/Sub fallback contradicts decision 11's central property.** The plan says the alternative "keeps the no-public-endpoint property"; a Pub/Sub *push* target is an HTTP endpoint (the surface decision 11 removed) and a *pull* subscriber is a long-running consumer, which spec §11 and the plan's own one-shot-jobs rule forbid. Appendix B.6 repeats it. *Fix:* delete the Pub/Sub fallback; if the Admin-API target proves awkward, re-open decision 11 explicitly rather than silently re-adding a surface.

**RN8. The `reminder_dispatches` justification now contradicts the delivery semantics B1 introduced.** §3.2 still argues the dispatch key's existence from "one occurrence of a **repeating** reminder needs its own key", while B1's success path hard-codes `state='completed'` and `next_notification_at=NULL` on every delivery and there is no recurrence column anywhere. *Fix:* reword to the one-shot rationale, and if recurrence is a future intent, say what would have to change.

**RN9. The `args_hash` predicate added in the claim is a tautology as implemented.** §3.2 states the hash is deliberately never carried in `callback_data` and "the server always re-reads the hash from the row", so the claim compares the row's value against itself; the real guarantees are the N1 trigger and `args_revision`. *Fix:* either drop the predicate and say so, or carry a truncated hash (16 hex chars fit the 47-byte callback budget: `ap:` + 22-char `action_id` + decision + revision + hash) so it can detect a replay of a superseded button.

**RN10. The N1 trigger's third condition is not expressible with the described schema.** It requires "the hash actually matching the new arguments" inside a `BEFORE UPDATE` trigger — which needs a canonical JSON serialisation reproducible in SQL, and the plan defines canonicalisation only on the Python side. *Fix:* have the trigger enforce immutability plus the `revision+1 AND state='pending'` re-presentation rule, and verify hash-vs-args in the application transaction (already asserted by the §9.2 test).

**RN11. §9.2's "pool arithmetic sanity" test cannot catch the oversubscription it claims to catch.** It opens 42 connections against the local `pgvector/pgvector:pg17` container, whose default `max_connections` is 100, not the tier's 25/50 — so it passes locally regardless of the real instance. *Fix:* parameterise on `TEST_DB_MAX_CONNECTIONS` (defaulting to the tier value) and assert `required + 5 ≤ that`, so the test exercises the arithmetic rather than the container.

**RN12. Phase 1's `/readyz` semantics are undefined.** §8.2 says `/readyz` checks DB reachability, the `vector` extension and the active `embedding_models` row; Phase 1 has no database at all, and A8 only exercises `/healthz`. *Fix:* state that Phase 1's `/readyz` reports ready on settings validation alone, with the DB/vector/model checks added in Phases 2 and 4.

**RN13. The `embedding_models` seed path is under-specified.** §3.4 requires the migration to seed exactly one active row (asserted by count), and §6.4 says ingestion "activates the matching `embedding_models` row", but nothing states the create-if-absent behaviour when the configured model has no row (the common case for `EMBED_PROVIDER=stub` locally), nor what literal the hand-written first revision seeds — a migration cannot read app settings. *Fix:* state that ingestion upserts `embedding_models` for the configured model and flips `is_active` inside the same transaction as its first chunk batch, and name the seed literal.

# 3. Grounds 1–3 for `work_items` (attacked as asked)

- **Ground 1 ("zero new GCP products; rule 6 is a cap on additions") — the weakest of the three.** Spec rule 6's sentence is "*Prioriza* componentes gestionados y sencillos de Google Cloud. No introduzcas microservicios, Kubernetes, un sistema multiagente, Redis, una segunda base vectorial o frameworks adicionales sin una necesidad demostrable." The enumerated prohibitions are unmanaged/complex patterns, none of which is Cloud Tasks, and the governing clause *prefers* managed GCP components. So the rule does not cap managed-service additions; it favours them. The ground survives only in the weaker form it can actually support — "this deployment introduces no product that exists solely for background retry" — which is a real but minor virtue, and it is not enough on its own to outweigh a hand-rolled queue. **Weak, not false.**
- **Ground 2 ("one source of truth / same transactional boundary") — fails, and it is the ground the section leans on hardest.** See RB1: decision 8 and Risks item 4 say the pools cannot be shared, so the claimed boundary does not exist, and the failure window it purports to close is still open in the SIGTERM case §11.1 names as the mechanism's raison d'être. This is a self-contradiction inside the plan, not merely an overstatement.
- **Ground 3 ("preserves the no-internal-endpoint property") — survives, with one caveat.** Cloud Tasks delivers to an HTTP target and Cloud Run Jobs do not accept Cloud Tasks targets, so the argument is structurally correct, and spec §13 only requires that internal endpoints not be *unauthenticated* — decision 11 goes further by choice, which is legitimate and was settled. The caveat is the plan's own undermining of it: §11.3 offers a Pub/Sub fallback that re-adds the same surface (RN7). Fix that sentence and the ground stands cleanly.

Net: with ground 4 already demoted by the arbiter and ground 1 weak, the decision now rests essentially on ground 3 plus a preference for keeping the queue in one database. That is thin but legitimate for a single-user assistant — and it is now honestly labelled as a judgment call, which is the right outcome.

# 4. Did §15 remove the drift, or relocate it?

It removed most of it and relocated a little. Verified state of each authority row: entrypoints (circular — RN3), Telegram method list (stale copy survives — RN1), tool→risk mapping (reconciles; §4.3 now names the registry), active embedding model (reconciles across decision 9, §3.2, §8.1, §8.2, §6.4), layering (§2.2 only), config variables (gap: four missing defaults, RN4), production fail-fast list (reconciles; §7.2 is referenced by decisions 15/19, §8.1, §9.1, A13 — but §7.2's *reason* for one rejection is stale, RN2), idempotency constraints (§3.3 referenced correctly), isolation levels (§4.6 referenced by §4.2 and §5.x), zero-row outcomes (internal defect, RB2), reminder dispatch states (§3.2 restated once in §5.2's SQL, which is the mechanism rather than a duplicate), ledger key (§5.3; §3.2 restates the components in prose), Appendices, phases — all reconcile. So: the mechanism is sound, but the document has no mechanical check that its own rule holds, and three violations survived the pass that was supposed to enforce it. *Suggestion:* add the retired phrases ("five methods", "hash-derived", "two entrypoints", "three entrypoints") to §15 as an explicit grep list to run on every future edit; that is cheaper than a test over the plan text and would have caught RN1 and RN2 mechanically.

# 5. Round-1 findings: closed vs open

**Genuinely closed (verified, not assumed):** B3, B6, B2's core, B1's core, N2, N3, N4, N5 (except the §2.1 copy — RN1), N6, N8, N9, N10 (I verified A.11 independently), N11, N12, N13, N14, N15, and all six orphans.

**Fixed in substance, with a residual:** B4 (residual RB2, plus RN9), B5 (residual RB4), B7 (residual RB3), N1 (residual RN10), N7 settled by the arbiter.

**Still open:** RB1, RB2, RB3, RB4, and the non-blocking set above.

# 6. Independent verification this round

- **Appendix A.11 (new, load-bearing for the tier and therefore the cost) — verified.** Cloud SQL for PostgreSQL's default `max_connections` scales with instance memory (~0.5 GB → 25, ~1.7 GB → 50, 3.75–<6 GB → 100, 6–<7.5 GB → 200); `db-f1-micro` and `db-g1-small` are shared-core types excluded from the Cloud SQL SLA; the flag is settable (14–262,143) and changing it requires a restart. The plan's table, its tier derivation and its "first dedicated-core tier → 100" mapping are all accurate.
- **Appendix A.7's 60-second Telegram deadline — unchanged**, and still the basis for `REQUEST_BUDGET_SECONDS=45`, now consistently stated.
- **Appendix A.10b (Cloud Tasks dedupe) — accurate as recorded**, including the conflict between Google's documented windows. The plan records it without denying it, which is the honest use of the fact.
- **§15's claim that §8.1 is the authority for "every configuration variable and its default" — does not hold for four variables** (RN4).
- **Ollama Cloud embeddings, the package pins and the pgvector limits:** re-checked in round 1, unchanged; the corrected attribution of #17129 now reads accurately.

# 7. Is Phase 1 implementable without guessing?

Very nearly, and markedly better than in round 1. The remaining guesses a Phase-1 builder would face are: `/readyz`'s behaviour with no database (RN12), the four missing Ops defaults (RN4), and which of `app/agent/graph.py` or the handler reads `ConversationStore` into the prompt (minor). None is a blocker; all three are one-line additions.

---

The round-1 blocking set is genuinely closed in substance — B1, B2, B3, B4, B5, B6 and B7 were fixed by changes to the SQL, the schema, the tests and the acceptance criteria, not by rewording, and the six orphaned spec requirements are placed and auditable. What remains is small and localised: one false architectural claim that the plan's own decision 8 refutes (RB1), one self-contradiction that would fail a phase's own test on first run (RB2), one retrieval predicate that reads a column the schema already carries but the query forgets (RB3), and one undefined rule precedence in the ledger that the A1 test cannot detect (RB4). All four are one-line fixes, but each is material on the definition given — silent loss of a turn, a phase that cannot pass its acceptance criteria, and a wrong-result replay.

VERDICT: REVISE

---

### Claude's response (round 2)

Verdict accepted: **REVISE**. The revised `PLAN.md` is **3,153 lines / 257,077 bytes** — up 5.3% in
lines for four blocking plus thirteen non-blocking fixes, with no new `##` section. Growth judged
proportionate.

**Accepted and revised:** RB1–RB4, RN1–RN13, the §15 retired-phrase grep list, A6, the A3 tier
ruling, and the ground-4 demotion.

**My own error, recorded.** Ground 2 of §5.5 is withdrawn as false. Responsibility for it is mine,
not the builder's: I supplied it in the round-1 re-justification of `work_items`, and it
contradicted a Risks item the builder had written itself. The claim that resume work shares a
transactional boundary with the checkpoint it resumes cannot hold while decision 8 requires the
checkpointer to have its own pool. Two pools, two ordered transactions — the window is now closed by
RB1's watchdog, not by atomicity. **The lesson worth keeping:** §15's authority table catches
duplicated *values*, not duplicated *reasoning*, so no mechanical check in the plan would have
caught this. Only an adversary reading the plan against itself did.

**Rulings on the builder's five raised points:**

1. **A6 — kept as implemented.** The bounded cleanup lives inside the existing resume job and adds
   no new job and no new configuration variable, so it satisfies the ruling's intent ("do not build
   a new retention mechanism"). `RETENTION_DAYS` stays deleted.
2. **The fifth consideration — ADD it, but not as a ground.** The builder omitted a concrete
   operational cost (a second delivery system is a second place a failed delivery must be
   diagnosed; Cloud Tasks' queue IAM, retry policy and task-name construction must be kept in sync
   with `thread_id`) because it feared pushing against my "state that it is thin" ruling. That is an
   **over-correction.** The ruling forbade *overstating* the case; it was never a licence to
   under-report. Suppressing real evidence is the same class of dishonesty as claiming a false
   atomicity — one inflates the plan's case, the other deflates it. It goes in as a cost entry in
   the honest counter-position ledger, which keeps the "thin" framing intact.
3. **Appendix B.13's `(42 + 5 ≤ 50)` — kept, with a clarifier.** The arithmetic is the constraint's
   minimum, not §12's recommendation, and a reader must not read the two as contradicting.
4. **§4.5's row count — verified independently by me, not taken on trust.** The table has exactly
   seven data rows with seven distinct keys (`approval.unknown_action`, `.expired`,
   `.already_rejected`, `.in_progress`, `.already_resolved`, `.arguments_changed`, `.wrong_identity`),
   and the merged row is internally coherent. The builder's claim is upheld.
5. **The unassigned §7 item — accepted as proposed.** The handler assembles the prompt input from
   `ConversationStore` and the graph receives it already shaped, keeping `agent/` free of store
   imports per §2.2.

**Round 3 focus:** whether rounds 1–2 are genuinely converged, and whether the honest thinness
framing survived being edited.

---

## Round 3 — Critic (adversarial review)

# 1. Blocking flaws

**RB3-follow-on. The `pipeline_version` predicate added in round 2 filters on the wrong authority, so a re-index makes the re-indexed document unretrievable.**
Where: `PLAN.md` §6.5 item 3 (the chokepoint query) and §6.4 (re-index/supersession and `embedding_models` population).
The fix I asked for was "filter `document_chunks.pipeline_version` on the document's current version". What was implemented is `document_chunks.pipeline_version = <**active `embedding_models` row's** pipeline_version>` (§6.5, stated twice; §6.4 calls the same thing "<active version>"), and §6.4's RN13 text defines the model row's write path as "upserts a row for the configured `EMBED_MODEL` (create-if-absent) and flips `is_active` to it" — it never bumps the row's `pipeline_version`, whose only defined value is the migration seed `('embeddinggemma', 768, 1, true)`.
Failure scenario, both readings break:
(a) the model row stays at `pipeline_version = 1` while a pipeline/chunker re-index writes chunks at 2 (§6.4's own re-index flow, `pipeline_version + 1`) → the filter asks for 1, the new chunks are 2, the document returns **zero rows** and the user is told "no encuentro evidencia" about a document the system reports as `ready`. The one thing a re-index must not do is remove the document from RAG.
(b) the builder instead treats the upsert as updating the row's version to 2 → every document *not* re-indexed stays at 1 and silently disappears, so re-indexing one document hides all the others.
Either way the deletion-on-`ready` in §6.1 stage 10 cannot compensate: it removes the *old* rows, and the surviving (or remaining) rows are the ones the predicate excludes. This is also why §6.4's claim that the predicate closes "the window before the deletion commits" is only true by accident — under the model-row reading it closes the window by hiding everything, which is precisely the failure above.
One-line fix: compare against the **document's** current version (`JOIN documents d ON d.id = document_chunks.document_id AND d.pipeline_version = document_chunks.pipeline_version`, with `owner_user_id` still inside the literal), which prevents version mixing *and* keeps the document answerable from the previous index throughout the re-index — and if a global version is genuinely intended, say that a re-index is all-or-nothing across a user's documents and define what the filter returns for a document not yet re-indexed.

**RB1-follow-on. The watchdog's interaction with `work_items`' unique index is asserted, not specified — and what is asserted ("the unique index therefore also serialises a thread against a concurrent second turn") is not something an index does.**
Where: `PLAN.md` §3.2 (`work_items` bullet, line 1047), §5.5 and decision 2.
The claim: the partial unique index on `(thread_id, kind) WHERE state IN ('pending','claimed')` "serialises a thread against a concurrent second turn — acceptable, since one thread at a time is the intended semantics". An index *rejects* the second insert; it does not queue or serialise anything. Because the watchdog row is now `pending` for the whole duration of a normal turn (not just after abandonment), any second message arriving while the first turn runs necessarily hits that constraint, and nothing in the plan says what the handler does next.
Failure scenario, all three plausible implementations are bad:
(a) `ON CONFLICT DO NOTHING` + carry on: the second turn runs the graph **concurrently on the same `thread_id`** as the first — interleaved checkpoint writes per super-step, which is the state the sentence claims is impossible — *and* the second turn has no watchdog row, so if it dies inside its budget there is no queued resume: RB1's whole guarantee silently absent for exactly the message that made the collision happen.
(b) insert failure raised as a 500: Telegram retries, hits the same conflict while turn 1 is still running, and the user's second message is repeatedly rejected.
(c) the handler blocks until turn 1 finishes: correct-ish, but it consumes the second turn's 45 s budget waiting and the plan never says so.
Two rapid messages is ordinary user behaviour, and no acceptance criterion covers it (Phase 1 A3 posts two *sequential* messages), so a wrong guess ships.
One-line fix: state the rule explicitly — the second turn inserts with `ON CONFLICT DO NOTHING`; on zero rows it does **not** enter the graph, and instead replies from a catalogue key ("estoy terminando tu mensaje anterior; vuelve a enviarlo en unos segundos") with no watchdog row created — plus one integration test asserting no second concurrent graph run, one reply, and no orphaned turn.

# 2. Non-blocking flaws, and what round 2's edits broke

**NN1. The new §2.2 prompt-assembly edge collides with decision 3, and a builder still has to guess what `ConversationStore` is in Phase 2.** §2.2 now says "The handler assembles the prompt, the graph receives it shaped … reads `ConversationStore` and builds the prompt input; `agent/` never imports `app.storage.stores`". Phase 2 simultaneously delivers "the PostgreSQL `DedupeStore`/`ConversationStore` implementations swapped in behind the Phase-1 protocols" with "PostgreSQL semantics (durable, multi-instance safe)" (§2.3.8 puts them in `repositories.py`), while decision 3 says the checkpoints are the single source of conversational truth and that `messages` is "never read back into the graph". So either the Phase-2 store reads `messages` (forbidden by decision 3), or it reads the checkpointer's tables (unstated), or the graph is supposed to reconstruct history itself (contradicting the new edge). *Fix:* one sentence — "in Phase 2 the history for prompt assembly is read from the checkpointed thread state (`aget_state`); `ConversationStore` is a thin adapter over it and `messages` is never read as history; the handler passes only the new turn's message."

**NN2. §9.2's tool-ledger bullet was not updated with RB4.** It still reads "replay (A1, direction 1) with a stub model that returns a *new* `tool_call_id` and **re-worded arguments** … exactly one side effect" — the pre-RB4 form, which under the new precedence raises `ToolCallArgsMismatch` instead of replaying. The test still passes (one side effect either way) but proves less than §5.3 and §9.1 now claim ("all three cases"). *Fix:* replace with the three distinguishable cases and their assertions.

**NN3. §5.3's assertion 2 says "zero side effects" where it must mean "zero additional".** In the staging it describes, the first attempt already performed the effect and recorded `succeeded` before the interruption, so a literal "zero side effects" assertion fails. *Fix:* say "zero *additional* side effects (exactly one, from the first attempt)", or stage the interruption between the `started` commit and the effect.

**NN4. §15's own authority row for the ledger key is stale.** The table at §15 still states `run_key = parent_checkpoint_id:node_name:tool_call_ordinal` while §5.3 (line 1501) and §3.2 define the four-part key with `tool_name`. The single-authority index carries a copy of the fact RB4 changed — and the retired-phrase grep list has no entry that would catch it. *Fix:* update the row and add the old three-part formula to the retired list.

**NN5. §11.2 contradicts §9.2 on the pool-arithmetic test.** §11.2 still says the §9.2 suite "reads its real `max_connections`" from the container; §9.2 (RN11-fixed) says `TEST_DB_MAX_CONNECTIONS` "defaults to the §12 tier's value (50) and is set explicitly, **not read** from the container". *Fix:* reword §11.2's bullet to match.

**NN6. §5.5's "the user never gets two replies to one message" is over-claimed.** If the process dies (or the write fails) between `sendMessage` and the `done` mark, the watchdog row is still `pending` past `run_at`, the resume job re-enters and sends a second reply. This is the *right* failure direction — a lost reply is worse than a duplicate one, and side effects are protected by the tool ledger — but it is the mirror of decision 5's honest limit and should be stated, not asserted away. *Fix:* add the caveat and have the resume job log "re-entering after an unconfirmable reply" so the duplicate is diagnosable.

**NN7. The honest-thinness framing survives in §5.5 but not in decision 2.** Decision 2's Cloud Tasks paragraph still says it is "not adopted, on grounds that do hold", restates the withdrawn reading ("**spec rule 6 is a cap on *additions***") and lists ground 4's "trading a partial unique index we must write anyway" among them, with no mention of the thin framing. §5.5 states the opposite for both. Since "Key decisions" is what a builder reads first, the headline sells a case the body has withdrawn. *Fix:* replace decision 2's three-ground sentence with a pointer to §5.5 ("thin judgment call; grounds and counter-position in §5.5") and add "cap on additions" to the retired-phrase list.

**NN8. One Phase-1 gap remains.** `REQUEST_BUDGET_SECONDS` is configured in §8.1 but has no consumer named anywhere in Phase 1 (the watchdog is Phase 3, and Phase 1 has no database), which also brushes against §9.1's own "every variable named in §8.1 has at least one non-test reader" invariant. *Fix:* state that in Phase 1 the budget is applied as a hard timeout around the graph call with the existing Spanish timeout reply, the queue handoff arriving in Phase 3.

# 3. Answers to your specific questions

**Do the round-2 fixes fix their flaws?**
- **RB1** — yes for the SIGTERM window: enqueue-before-graph genuinely closes it, because the row exists before the first checkpoint-affecting statement and survives any death inside the turn; the withdrawn atomicity claim is replaced by the correct ordering argument (decision 8 and Risks 4 now agree with §5.5). *Residual:* the unique-index interaction above.
- **RB2** — yes. §4.5 has seven rows, one key each; rows 4 and 8 are merged with a single stated cause; §9.1's assertion is over seven. No duplicates remain.
- **RB3** — half. The mixing problem is closed (predicate inside the literal, plus delete-on-`ready` in the same transaction as the `documents` flip, plus a §3.3 row). But the window question has a worse answer than "both versions retrievable": under the specified predicate, after any re-index *neither* version is retrievable for that document.
- **RB4** — yes. `tool_name` in `run_key`, ordered precedence with rule 1 checked first, and three assertions distinguishable by which rule fired (replay asserts on `state`/`result_json`; the mismatch cases assert the raise). *Residuals:* the two staleness items above, which make §9.2 prove less than §5.3 claims.

**What the edits broke:** NN1 (a new cross-section ambiguity in the conversational core), NN4/§11.2 staleness (fixes that relocated a stale copy), NN2/NN3 (test descriptions not carried along), NN7 (the thin framing contradicted by the headline decision). Appendix B.13 is sound and consistent with §11.2/§12.

**Did the honest-thinness framing survive editing?** In §5.5, intact and improved (ground 1 weakened, ground 2 withdrawn with the reason, ground 3 standing, ground 4 demoted, counter-position and tradeoff recorded). In decision 2, no — see NN7.

**Anything material not already raised?** Only the two blocking items above.

# 4. Explicit close-list

**Round-1 blocking:** B1 **closed** (nullable `next_notification_at`, `last_notified_at`, `completed` on delivery, `attempts < max_attempts` in the claim, symmetric failure path, two-run integration test) — a delivered one-shot reminder can no longer be re-claimed or misreported; B2 **closed** (Phase 0 reconciliation, `reconciled_at`, report, never auto-resend, lease constraint asserted at startup); B3 **closed**; B4 **closed, including RB2** (all five predicates in one statement, seven distinct zero-row causes diagnosed from the row); B5 **closed, including RB4**; B6 **closed**; B7 **closed**.
**Round-1 non-blocking:** N1–N6, N8–N15 **closed**; N7 settled by your ruling and recorded (Risks 16 + the `3.1.3` fallback note).
**Round-2 blocking:** RB1 **substantially closed** — one residual (the second-turn rule); RB2 **closed**; RB3 **not closed** — the predicate's RHS is the model row, not the document (blocking item 1); RB4 **closed** — two residuals in wording/tests (NN2, NN3).
**Round-2 non-blocking:** RN1, RN2, RN3, RN5, RN6, RN7, RN9, RN10, RN11, RN12, RN13 **closed** (checked individually: §2.1's comment, §7.2's reason, the §15 circularity, the zero-row CAS rule, attempts-on-insert, the Pub/Sub fallback deleted in both §11.3 and B.6, the 16-hex hash prefix against `substr(args_hash,1,16)` with the 48-byte callback arithmetic, the trigger scoped to what SQL can express, `TEST_DB_MAX_CONNECTIONS`, `/readyz` in Phase 1, the seed literal plus create-if-absent upsert); RN4 **closed** (all four defaults present, `REMINDER_MAX_ATTEMPTS` mirrored onto `tasks.max_attempts`, lease >= job timeout asserted); RN8 — the repeating-reminder rationale in §3.2 — **closed**; RN10's hash-vs-args check now lives in the application transaction **closed**.
**Grounds for `work_items`:** ground 1 weakened honestly, ground 2 withdrawn, ground 3 stands (and is now clean, since the Pub/Sub sentence that undermined it is deleted), ground 4 demoted — not re-opened, per your ruling.

# 5. Independent verification

No new external facts were introduced in this round (the retired-phrase list and Appendix B.13 are internal), so nothing new needed re-checking; last round's verification of Appendix A.11's `max_connections`-by-tier table, the 60-second Telegram deadline, the pgvector limits and the pinned versions still stands, and B.13's parenthetical ("the constraint's minimum tier, not §12's recommended dedicated-core tier") correctly reconciles §11.2's arithmetic with §12's recommendation.

# 6. Bottom line

Round 2's work is real: seven of seven round-1 blocking findings and the whole round-2 non-blocking set are closed by changes to SQL, schema, precedence and tests rather than by rewording, and the honest-thinness framing in §5.5 is the most candid section in the document. Two items remain material, and both are one-line fixes rather than redesigns: the round-2 `pipeline_version` predicate reads the model row when it must read the document, which hides a document from RAG after its own re-index; and the watchdog's unique-index interaction is asserted as serialisation when an index only rejects, leaving the second turn's behaviour — and RB1's guarantee for it — unspecified.

VERDICT: REVISE

---

### Claude's response (round 3)

Verdict accepted: **REVISE** — two blocking items, both **follow-ons of the round-2 fixes**, plus NN1–NN8.
All ten are one-line fixes; none is a redesign. Accepted in full.

**A pattern worth recording for the implementer:** both blocking items are defects *introduced by
fixes*. A fix is a change, and a change carries its own bug. The round-2 edits closed seven of seven
round-1 blocking findings — by changing SQL, schema, precedence and tests, not by rewording — and
still introduced two new material problems. Any future reviewer should read a fix as new code.

**Attribution matters here, so it is recorded exactly.** The defective right-hand side in RB3's
predicate — comparing against the *active `embedding_models` row's* `pipeline_version` instead of the
*document's* — originated in the critic's **own** round-2 one-line fix, which the builder implemented
faithfully. Neither the builder nor I introduced it; the critic caught its own error one round later.
That is the loop working, but it is also a caution: **the critic is not infallible, and a fix must be
read against the system it lands in, not only against the flaw it answers.** The builder, by
contrast, authored the "the unique index therefore serialises a thread" error on its own.

**Why both items are genuinely material** (they pass the bar I set): RB3's follow-on makes a document
**unretrievable from RAG after its own re-index** while the system reports it `ready`, and tells the
user "no encuentro evidencia" — the exact failure a re-index must not cause, inverted. RB1's follow-on
leaves the behaviour of **two rapid messages unspecified**, with three plausible implementations all
bad, one of which silently drops RB1's guarantee for precisely the message that triggered the
collision. Both are ordinary-use paths, and neither is covered by an existing acceptance criterion.

**Round 4 focus:** whether the two follow-ons are closed and, if the only remaining items are
one-line follow-ons that do not risk security, data loss, double execution or phase acceptance, an
APPROVED verdict with those items carried forward as non-blocking.

---

### Round 3 revision — outcome and rulings

Builder applied all ten accepted items: `PLAN.md` now **3,258 lines / 266,886 bytes** (+2.7% lines,
no new section). One disagreement was raised, and it is upheld.

**NN1 placement — the builder is right and my instruction was wrong.** I asked for the Phase-2
history sentence in "§2.2 and the Phase 1 deliverables". The builder instead wrote the full sentence
**once** in §10 Phase 1 deliverable 9 — where a builder implementing `ConversationStore` actually
reads it — and made §2.2 *point* to it. That is correct, and my instruction contradicted §15's own
stated-once rule: duplicating the sentence across two sections would have reproduced precisely the
drift pattern rounds 1–3 dismantled (NN4, NN5, RN1 and RN2 were all relocated stale copies). Ruling:
the builder's placement stands; §2.2 keeps the layering edge and references §10.

**The builder's own diagnosis, recorded because it is the most useful line in the whole exchange.**
Across three rounds, three claims were made about database behaviour that the database does not have:

| Round | Claim | Author | Reality |
|---|---|---|---|
| 1 | resume work shares a transaction with the checkpoint it resumes | **me** (in the re-justification) | two pools, two ordered transactions (RB1) |
| 2 | the unique index "serialises" a thread against a concurrent second turn | builder | an index rejects an insert; it queues nothing (RB1-follow-on) |
| 3 | the retrieval predicate closes the mixing window | **the critic** (its own one-line fix) | the RHS excluded the document it was meant to rank (RB3-follow-on) |

The pattern, not any single finding, is the honest summary of what these rounds fixed: **every one
was a claim about a mechanism, made without reading the mechanism.** Each author in the loop
contributed one — arbiter, builder and critic alike. The implementer should treat any sentence of the
form "the database therefore X" as unverified until it is traced to the SQL that does it.

**A process defect in this log, found and fixed.** Rounds 2 and 3 were appended by anchoring on the
previous round's verdict line, which pushed earlier content down and scrambled the section order —
all three critiques grouped together, then the responses in the order 2, 3, 1. I rebuilt the file by
segment (Round 1 — Critic, response 1, Round 2 — Critic, response 2, Round 3 — Critic, response 3) and
verified 444 lines before and after, so no content was lost. Later rounds must append at end-of-file.

---

## Round 4 — Critic (adversarial review)

# 1. Are the two follow-ons closed?

**RB1-follow-on — closed.** §3.2 now states the rule properly ("an index rejects, it does not serialise"): insert with `ON CONFLICT DO NOTHING`; on zero rows the handler does not enter the graph, creates no watchdog row, and replies from `busy.previous_turn_in_progress`; §5.5 repeats it as "a concurrent second graph run on one `thread_id` cannot occur"; Phase 3 acceptance **(h)** exists and tests exactly the properties I asked for — no second graph run, no checkpoint writes from it, exactly one reply, no watchdog row, **no orphaned turn**, and both updates recorded exactly once by the inbox path. The busy path cannot weaken update-id dedupe: the second message is a different `update_id`, and it is the inbox insert (the handler's first write) that records it once; the decline happens afterwards, at the watchdog insert. Reachability is fine, and the reply is bounded by the per-instance rate limiter. Intended-product-behaviour framing is right and matches spec §6.

**RB3-follow-on — the join is right, but the thing it reads is still internally contradictory (this is my one material finding).**
`PLAN.md` §3.2 (`document_chunks`), §5.4 chunk-level, §6.1 stages 9–10, §6.4, §6.5 item 3.
The join is now correct and the invariant is stated and tested ("a `ready` document is always retrievable", §6.5, with §9.2 asserting non-empty retrieval during and after a re-index). But three statements that feed that join still cannot all be true:
(i) **§3.2 line 859: `document_chunks.pipeline_version` is "Copied from the document"** — while §6.4/§6.1 stage 10 require the chunks to carry `pipeline_version + 1` *before* the documents row flips at `ready`.
(ii) **`UNIQUE (document_id, chunk_index)` with `ON CONFLICT (document_id, chunk_index) DO UPDATE`** (§3.2 line 862, §5.4, §6.1 stage 9) — an in-place *overwrite* of the previous version's rows.
(iii) **§6.4/§6.5: "the superseded version's chunk rows are left in place … they are what keeps the document answerable during its own re-index"** — which (ii) makes impossible for any chunk index the new version also produces.
Failure scenarios, both material:
- Under (i): a re-index stamps the new chunks with the document's *current* version, `finalize` then flips the documents row to N+1, and the join `d.pipeline_version = document_chunks.pipeline_version` matches **nothing, permanently** — a document reported `ready` with `chunk_count=N` returns "no encuentro evidencia" for every question thereafter. That is the exact failure the round-3 correction was meant to make impossible, arriving through the stamping rule instead of the filter.
- Under (ii)+(iii): because the conflict target is two-part, the re-index rewrites chunk rows in place, so the previous index is progressively destroyed rather than kept: the retrievable set shrinks as chunks flip to the new version, reaching zero in the window between the last chunk batch and the `finalize` commit — and if the re-index crashes there (§6.1 stage 10 says it is reclaimed after the lease, §5.4's 15 minutes) or keeps failing (plausible while §13.1 is unresolved), the user's previously working document stays unanswerable for that whole period. The stated safety net does not exist.
§9.2's new test only *appears* to catch this: "non-empty at every point — during the re-index (served from the previous version's chunks)" is timing-dependent under (ii), and would pass or fail depending on how far the write phase happens to have run; under (i) it passes during and fails after.
One-line fix: choose the parallel-row representation the narrative already assumes — add the version to the chunk key and stamp the version being written: `UNIQUE (document_id, pipeline_version, chunk_index)` with the matching `ON CONFLICT` target, and change §3.2's "Copied from the document" to "stamped with the pipeline version being written". Then the old version's rows genuinely survive under the same `document_id`, the join answers from them throughout the re-index, `finalize`'s delete removes a real set, and a same-version retry still converges (spec §9's "un reintento no debe duplicar fragmentos" holds, since the conflict target is unchanged within a version). Update §3.3's "A chunk is not duplicated by a retried ingest" row to the three-part key, and make §9.2's "during" assertion deterministic (assert non-empty after the write phase and before the `finalize` commit, with an injectable pause) so it actually pins the invariant.

# 2. Did round 3's edits introduce anything new?

No new contradictions. The three items you named all hold together:
- **A7's three obligations** (the index is subject to the retired-phrase check; three mandatory edits per change; the check runs over the index and appendices) are stated, and the list has been extended with the superseded forms that rotted the index (`parent_checkpoint_id:node_name:tool_call_ordinal`, `document_chunks.pipeline_version = <active`, `cap on additions`) with their finding IDs — I verified the `run_key` row in the table now carries the four-part form and that the retired three-part form appears only in the correction note.
- **Decision 2's pointer** resolves to §5.5 (the authority), no longer summarises the withdrawn grounds, and §5.5 remains the only place the grounds live. (Cosmetic only: the `*Cost:*` sentence at the end of that paragraph is glued onto the same source line — no content effect.)
- **The §2.2 → §10 Phase 1 deliverable 9 reference resolves**: §2.2 points at it and states nothing itself; deliverable 9 carries the `aget_state` answer, the "`messages` is never read as history" rule, and the handler-passes-only-the-new-message statement — so decision 3 is now consistent with the prompt-assembly edge, and NN1 is closed.
NN2 and NN3 are closed (§9.2 now lists the three precedence cases asserted on *which rule fired*, with "zero **additional** side effects"), NN4–NN8 are closed, and the Phase-1 budget consumer (NN8) is placed without duplicating §8.1's values.

# 3. Any material flaw not yet raised?

None beyond the one above. Everything else I checked this round reconciles: Phase 3's (a)–(h), §9.2's new bullets, §15's self-rule and its list, Appendix B.13 (consistent with §11.2/§12), and the honest-thinness framing in §5.5 with decision 2 now deferring to it.

# 4. Explicit close-list

- **Round-1 blocking:** B1, B2, B3, B4, B5, B6, B7 — **all closed** (verified in their own SQL, schema, tests and acceptance criteria).
- **Round-1 non-blocking:** N1–N6, N8–N15 **closed**; N7 settled by your ruling and recorded.
- **Round-2 blocking:** RB1 **closed** (watchdog enqueue before the graph, atomicity claim withdrawn, second-turn rule + Phase 3 (h)); RB2 **closed** (seven rows, one key each); **RB3 not fully closed** — the predicate is right, the storage model it reads is not (the finding above); RB4 **closed** (precedence, `tool_name` in the key, three distinguishable assertions, and the tests now carried into §9.2).
- **Round-2 non-blocking:** RN1–RN13 **all closed** (each verified individually, including RN11's `TEST_DB_MAX_CONNECTIONS`, RN12's Phase-1 `/readyz`, RN13's seed literal and create-if-absent upsert).
- **Round-3 findings:** RB3-follow-on **partially closed** (join and invariant correct; the contradiction above is what remains); RB1-follow-on **closed**; NN1–NN8 **closed**.

# 5. Bottom line

Three rounds of real work landed: the approval CAS, the reminder lifecycle, the ledger key and precedence, the watchdog handoff, the fail-fast set, the single-authority index and the honest-thinness framing are all sound and now verified against their own text. One material item remains, and it is narrow and textual rather than architectural: the plan says a re-index leaves the previous version's chunk rows in place to keep the document answerable, but the schema it specifies (`Copied from the document` for the chunk's version, plus a two-part `ON CONFLICT … DO UPDATE` target) either overwrites those rows in place or leaves the document permanently unmatched by its own retrieval predicate. One schema line plus two sentence changes fixes it, and the fix makes the plan's existing narrative, invariant and test true rather than adding anything new.

Non-blocking carry-forwards for the implementer: decision 2's `*Cost:*` line-wrapping; §9.2's "during the re-index" assertion needs the deterministic staging described above; and §15's retired list should gain the retired two-part chunk index once the key changes.

VERDICT: REVISE

---

### Claude's response (round 4)

Verdict accepted: **REVISE** — one material finding (RB3, third order) plus three carry-forwards.
All accepted. Builder applied: the three-part chunk key
`UNIQUE (document_id, pipeline_version, chunk_index)` with the matching `ON CONFLICT` target at all
three sites; "stamped with the pipeline version being written"; §3.3's row; the deterministic §9.2
staging; §15's retired list gaining the two-part index and "Copied from the document"; decision 2's
cosmetic wrapping; plus a consistency sentence. `PLAN.md` is now **3,281 lines / 269,665 bytes**
(+1.0%).

**Rulings on the builder's three disclosures:**

1. **The extra §15 authority row — KEPT.** It is precisely what A7 requires, and its *absence* is why
   a two-part chunk key survived three rounds of review. Removing it would delete the evidence.
2. **A7 gains a fourth obligation — references.** The builder's measurement is the strongest
   argument in the exchange for it: the critic sized this fix as "one schema line plus two
   sentences", and it was in fact an **eight-site edit** (§3.2 description, §3.2 index, §3.3 row,
   §5.4, §6.1 stage 9, §9.2 staging, §15's row and list). A rule that records only *who owns* a value
   cannot tell you *what a change to it costs*; A7 must record where each owned value is
   **referenced**.
3. **Abandoned-version residue — accepted, tightly scoped.** A permanently failed re-index leaves its
   partial rows forever, and repeated retries accumulate a full set per attempt. The residue is inert
   (the join excludes it, `chunk_count` still describes the current version), so this is **not a
   correctness fix** and is reported as such. Scope the clause exactly: delete a document's chunk
   rows for versions **other than** its current `pipeline_version` once that attempt has reached its
   attempt cap — never the current version, never an attempt still within its cap. One clause in the
   existing job, no new component, consistent with A6's precedent.

**The honest summary of RB3, recorded for the implementer.** RB3 took three rounds, and each fix
landed beside a statement that contradicted it — visible only from a different section. Round 2 fixed
the filter's authority; round 3 fixed the join; round 4 fixed the storage model the join reads. The
blind spot was never carelessness, it was **reading one section at a time**. This is the same class as
the "database therefore X" table above: a claim about a mechanism, made without reading the mechanism.
It is also what A7's references rule is designed to make impossible, which is why that rule is the
most transferable thing this loop produced.

**Round 5 is the final round permitted by MAX_ROUNDS=5.**

---

## Round 5 — Critic (adversarial review)

I read the file in full (3,281 lines) and verified round 4's changes against the DDL, the tests and the index rather than the summary.

## 1. Is RB3 finally closed? — Yes, and now by construction.

Verified at all three sites and the two consumers:
- **§3.2** `document_chunks.pipeline_version` now reads "**Stamped with the pipeline version being written**", with the superseded wording and why it was wrong recorded inline (line 867), and the key is `UNIQUE (document_id, pipeline_version, chunk_index)` with the matching `ON CONFLICT` target, with the parallel-row semantics and the within-version retry convergence spelled out (lines 870–877).
- **§5.4** and **§6.1 stage 9** carry the same three-part target and the same stamping rule ("stamps each row with the version being written … while the previous version's rows survive untouched for the rest of the re-index"); **§6.1 stage 10** still flips the document and deletes the superseded rows in one transaction.
- **§6.5** keeps the join on the document's own version, **§3.3** carries the three-part key, **§15** gained the authority row for chunk identity plus the retired forms `(document_id, chunk_index)` and `Copied from the document`.

Why it is structural rather than merely asserted: each chunk row carries exactly one version, and the join matches a chunk **iff its version equals its own document's current version**. So at any instant at most one version can match — two versions being retrievable simultaneously is not prevented by ordering, it is unrepresentable. Neither-version is prevented too: before the flip, version N's rows are untouched (the re-index writes parallel rows, it does not overwrite); the flip happens only after all chunks commit, and N's rows are deleted in the same transaction as the flip — so whichever value `documents.pipeline_version` holds has a complete set of rows under it. A crashed re-index now costs nothing: its partial N+1 rows are invisible and its N rows still answer.
The test is now genuinely probative: the injectable pause at "after the write phase and before the `finalize` commit" creates the only state that can falsify the predicate — a window in which both versions exist as rows and exactly one must be returned — then re-asserts after `finalize` that the superseded rows are gone and the new ones answer. Under the old two-part key that pause would have been an empty window; under the new key it is the real test. **Closed.**

## 2. Did round 4 introduce anything material? — No. But three attributed edits are not in the file I reviewed.

I checked the new material specifically:
- **Abandoned-version residue cleanup — not present.** I swept the document for it under every phrasing I could construct (`cleanup`, `residue`, `abandoned`, `other than`, `accumulate`, `prune`, `garbage`, `orphan`, and every occurrence of `cap`) and read the three natural homes (§5.4, §6.1 stages 9–11 and the closing paragraph, the `documents` table region). It is not there; the only "cleanup" in the plan is A6's `done`/`dead` `work_items` sweep. **This is not material**: the rows it would remove are exactly the ones the join excludes by definition (chunks whose version differs from their document's current version), so retrieval, the invariant and every acceptance criterion are unaffected. What it would have prevented is invisible, bounded row accumulation after repeated failed re-indexes — storage hygiene, carried forward below.
- **A7's fourth obligation — not present** (the list has items 1–3 only). Its *substance* is already in place: every row of the authority table has a third column naming where the value is referenced, and the "three mandatory edits" item already covers the change path. Non-material; carried forward as a wording gap, not a behavioural one.
- **Decision 2's consistency sentence — not present** as a sentence in decision 2, which now correctly contains only the pointer to §5.5 (its cost-line wrapping is repaired, line 148). The by-construction reasoning it describes does exist where it belongs: §3.2's key bullet and §6.5's join rationale. So the record attributes a third edit I cannot find in the file, but the property it was meant to state is stated, in the right places, and is true.
If the cleanup and the A7 clause were applied to a different copy, they are absent from `PLAN.md` as submitted; the honest statement for the record is that the review covers the file, and none of the three omissions meets the material bar.
- Answering the substantive question anyway, since it will be implemented: **the rule as you describe it is safe, provided it is keyed on the document's current version.** `DELETE … WHERE document_id = $1 AND pipeline_version <> (SELECT pipeline_version FROM documents WHERE id = $1)` can never touch the current version (the predicate excludes it), and gating on the attempt having reached its cap keeps retryable attempts' partial rows intact. The one wrong improvisation to guard against: keying the delete on "the newest version present in `document_chunks`" instead of the document's version — during a re-index that is N+1, so it would delete the N rows that keep the document answerable. One line in the clause should say so.

## 3. Any material flaw not yet raised? — None.

I found nothing new that meets the bar. Across four rounds every mechanism has been verified against its own text: the approval CAS and the seven zero-row diagnoses, the reminder lifecycle end to end, the tool ledger and its precedence, the watchdog handoff and the second-turn rule, the single-authority index and its retired-phrase guard, the owner-isolation chokepoint, the owner/version predicates, the fail-fast set, the pool arithmetic, and the traceability against spec §1–§18 (all fifteen §17 items and all six round-1 orphans placed and demonstrable). What remains is hygiene and wording.

## 4. Close-list and carry-forwards

**Closed across all four rounds** — round 1: B1–B7 and N1–N6, N8–N15 (N7 recorded per your ruling); round 2: RB1, RB2, RB4 and RN1–RN13; round 3: RB1-follow-on (including Phase 3 acceptance (h)) and NN1–NN8; round 4: **RB3-follow-on**, the last material item, closed by the parallel-row representation, the three-part key at all sites, the corrected stamping rule, the document-version join, the deterministic test window, §3.3's row, §15's new authority row and the retired forms.

**Carry-forwards for the implementer (all non-material):**
1. Add the abandoned-version residue cleanup to `app/jobs/process_documents.py`, keyed on `documents.pipeline_version` (never on the newest version present in `document_chunks`) and gated on `attempts >= max` — otherwise non-current chunk rows accumulate silently after repeated failed re-indexes.
2. Record A7's fourth obligation (the index names where each owned value is referenced) as a numbered item, so the existing third column is a rule rather than a convention.
3. Keep decision 2's pointer intact if the consistency sentence is re-added — it belongs in §3.2/§6.5, where the mechanism lives, not in the decision list.
4. Implementation-time confirmations already tracked in Appendix B (checkpointer table names, `Vector` vs `VECTOR`, the socket DSN, the Scheduler→Jobs IAM shape, current pricing, the real instance's `max_connections`, the LangGraph run-config keys for `run_key`).
5. The plan's own phase gates remain the real test: Phase 2 (a)–(g), Phase 3 (a)–(h), Phase 4 (a)–(g) with (a)/(b) blocked on §13.1 and the embedding-provider decision still open at §13.1 — a user decision, not a plan defect.

The plan is sound enough to implement. Its load-bearing mechanisms are specified to a level where each is checkable by reading one statement, its acceptance criteria are stated as things that can actually be run, its unknowns are labelled rather than papered over, and four rounds of adversarial work moved it from "cannot be implemented as written" to "implementable, with a short list of hygiene items". I would build from it.

VERDICT: APPROVED

---

### Claude's response (round 5)

**Verdict: APPROVED. The loop converged at round 5 of 5.** Plan reviewed, approved, and no code
written during the loop. Final state: `PLAN.md` **3,281 lines / 269,665 bytes**;
`PLAN-REVIEW-LOG.md` **84,510 bytes**. `PROJECT_SPEC.md` unmodified throughout (sha256
`ea78f910…b75449`, identical to the first check of the session).

**A briefing error of mine, disclosed and recorded.** In the round-5 brief I described three changes
as *applied* that were not in the file. Two were never applied: the abandoned-version cleanup clause
(the builder explicitly reported it had **not** added it and wanted a ruling) and A7's fourth
obligation (I ruled on it, but round 5 was a review round — no revision was ever instructed). The
third, decision 2's consistency sentence, I had asked to be placed where the critic says it does not
belong. The cause was bookkeeping in my brief, not in the plan: the builder's round-4 revision
finished before I wrote the round-5 prompt, and I wrote my *rulings* into that prompt as though they
were *edits*.

**The critic did not take my word.** It swept `PLAN.md` under nine phrasings, found the clause absent,
reported the discrepancy, and judged all three omissions non-material **on its own reasoning** — which
is precisely the property that makes a read-only verifier worth running, and the reason this error
did not corrupt the outcome. I verified its finding independently: `residue` and `abandoned` do not
occur in `PLAN.md` at all.

**The loop's own summary of itself.** Material findings by round: **7 → 4 → 2 → 1 → 0**. Every one of
those 14 findings was a claim about a mechanism that had not been read against the mechanism. Each of
the three participants contributed at least one: the arbiter supplied the false atomicity (ground 2),
the builder supplied "an index serialises" and the defective chunk stamping, and the critic supplied
RB3's wrong-authority predicate in its own one-line fix and then caught it a round later. The most
transferable artefact this produced is A7's rule: **an authority index that records who owns a value
must also record where it is referenced**, because the critic's "one schema line plus two sentences"
was in fact an eight-site edit.

**Loop closed.** Three non-material carry-forwards remain for the implementer (the cleanup clause with
the critic's exact safe form and its one wrong improvisation named; A7's fourth obligation as a
numbered item; and leave decision 2 pointing at §5.5).
