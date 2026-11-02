"""Resume job: finish turns the webhook could not complete (PLAN.md §5.5, §11.3).

Built in Phase 3; **not scheduled** until Phase 5 creates the Cloud Run Job and the Scheduler
trigger. It is the other half of the watchdog: the handler enqueues before the graph runs, this
job finds anything still queued and finishes it, so a turn interrupted by the request budget or a
SIGTERM drain still produces exactly one reply.

    python -m app.jobs.resume_work
"""

from __future__ import annotations

import asyncio
import sys

from app.config import load_settings
from app.jobs import executor_id, report_line
from app.logging_config import configure_logging
from app.services.work_queue import WorkQueue
from app.storage.postgres import build_database, use_compatible_event_loop

RESUME_BATCH = 10
RESUME_LEASE_SECONDS = 300


async def run() -> dict[str, object]:
    settings = load_settings().validate_or_raise()
    configure_logging(
        level=settings.log_level,
        app_env=settings.app_env,
        content_debug=settings.log_content_debug,
    )

    database = build_database(settings)
    queue = WorkQueue(sessions=database.sessions, lease_seconds=RESUME_LEASE_SECONDS)

    resumed: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []

    items = await queue.claim_due(batch=RESUME_BATCH, executor=executor_id("resume"))
    if not items:
        # Nothing queued: exit before importing the agent stack or opening the
        # checkpointer. This job is scheduled, so an idle tick should cost a
        # database round-trip rather than a LangGraph import.
        await database.dispose()
        return {"resumed": resumed, "failed": failed}

    # Imported here so the idle path above stays cheap.
    from app.agent.graph import ChatAgent
    from app.agent.prompts import build_system_prompt
    from app.bot import messages
    from app.bot.telegram_client import TelegramClient
    from app.services.notifications import build_notifier
    from app.storage.checkpointing import build_checkpointer

    telegram = TelegramClient(token=settings.bot_token, base_url=settings.telegram_api_base_url)
    notifier = build_notifier(settings, telegram)
    checkpointer = await build_checkpointer(settings)
    agent = ChatAgent(
        _UnavailableModel(),
        system_prompt=build_system_prompt(),
        budget_seconds=settings.request_budget_seconds,
        checkpointer=checkpointer.saver,
    )
    try:
        for item in items:
            thread_id = str(item["thread_id"])
            try:
                result = await agent.resume(thread_id)

                # A turn that ended at an approval prompt has no reply text but is *not* a failure:
                # the prompt was already delivered by the handler. Sending `error.generic` there
                # would have been a misleading message (Phase 3 review, item 13).
                if not result.content.strip():
                    pending = await agent.pending_approval(thread_id)
                    if pending is not None:
                        await queue.finish(item_id=int(item["id"]), state="done")
                        resumed.append(
                            {"thread_id": thread_id, "delivered": False, "reason": "awaiting_approval"}
                        )
                        continue

                chat_id = int((item.get("payload") or {}).get("chat_id") or 0)
                if not chat_id:
                    # Without a chat id there is nowhere to deliver: fail the item loudly rather
                    # than calling the Bot API with chat_id 0 (which is what the review caught).
                    await queue.finish(
                        item_id=int(item["id"]), state="failed", error="missing_chat_id"
                    )
                    failed.append({"thread_id": thread_id, "error": "missing_chat_id"})
                    continue

                text = result.content.strip() or messages.render("error.generic")
                delivery = await notifier.send(chat_id=chat_id, text=text)
                if not delivery.delivered:
                    # At-most-once, as with reminders: report it rather than claim success.
                    await queue.finish(item_id=int(item["id"]), state="failed", error=delivery.error)
                    failed.append({"thread_id": thread_id, "error": delivery.error})
                    continue
                await queue.finish(item_id=int(item["id"]), state="done")
                resumed.append({"thread_id": thread_id, "message_id": delivery.message_id})
            except Exception as exc:  # noqa: BLE001 - one bad item must not stop the batch
                await queue.finish(item_id=int(item["id"]), state="failed", error=type(exc).__name__)
                failed.append({"thread_id": thread_id, "error": type(exc).__name__})
        return {"resumed": resumed, "failed": failed}
    finally:
        await checkpointer.aclose()
        await telegram.aclose()
        await database.dispose()


class _UnavailableModel:
    """The resume path must not invent a model: it continues a thread whose model call is done.

    If a resumed thread still needs a fresh model turn, saying so is better than silently
    producing text — that case needs the real model and belongs to a run with credentials.
    """

    model = "resume-no-model"

    async def chat(self, messages_, **_: object):  # noqa: ANN001, ANN202
        from app.errors import OllamaUnavailable

        raise OllamaUnavailable("the resume job has no chat model wired")

    async def list_models(self) -> list[str]:  # pragma: no cover - unused
        return []


def main(argv: list[str] | None = None) -> int:
    use_compatible_event_loop()
    try:
        payload = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - a job exits non-zero with a clear reason
        print(report_line({"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}))
        return 1
    print(report_line({"ok": True, **payload}))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
