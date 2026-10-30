"""Task and reminder tools (spec §10, PLAN.md §2.1).

Layer 4. `create_reminder` is the interesting one:

* the due date is a **local time plus an IANA zone**, resolved through
  :mod:`app.policy.timezones`, so the DST edges are rejected explicitly rather than shifted;
* the creation key is derived from the content (`user`, `title`, `instant`), so a retried tool
  call resolves to the existing task instead of creating a second reminder — that is what the
  `(user_id, idempotency_key)` unique index enforces (§3.3);
* `max_attempts` is copied onto the row, because the dispatcher's claim predicate compares
  against the row, not against configuration (B1).
"""

from __future__ import annotations

import hashlib
import uuid
import zoneinfo
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.policy.relative_dates import UnknownDayExpression, parse_clock, resolve_day
from app.policy.timezones import InvalidLocalTime, ResolvedDue, load_zone, parse_local, resolve_local
from app.storage import repositories
from app.policy.risk import RiskLevel
from app.tools.base import ToolResult
from app.tools.registry import ToolSpec

REMINDER_PIPELINE_VERSION = "v1"


def creation_key(*, user_id: int, title: str, due_at_utc: datetime) -> str:
    """A stable key for one logical reminder (user + title + instant)."""

    payload = f"{user_id}|{title.strip().lower()}|{due_at_utc.isoformat()}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{REMINDER_PIPELINE_VERSION}:{digest}"


class CreateReminderArgs(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    due_local: str | None = Field(
        default=None,
        min_length=8,
        max_length=32,
        description="Local due date and time, e.g. '2026-11-03 09:30'",
    )
    day_expression: str | None = Field(
        default=None,
        max_length=40,
        description="Relative day: today, tomorrow, this_monday, next_week, ...",
    )
    time: str | None = Field(
        default=None,
        max_length=5,
        description="Local time HH:MM, used with day_expression",
    )
    timezone: str | None = Field(
        default=None,
        max_length=64,
        description="IANA zone; falls back to the configured DEFAULT_TIMEZONE",
    )
    description: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _due_source(self) -> CreateReminderArgs:
        if self.time:
            parse_clock(self.time)
        if not self.due_local and not (self.day_expression and self.time):
            raise ValueError("provide due_local or day_expression + time")
        return self


class ListTasksArgs(BaseModel):
    status: str | None = Field(default=None, pattern="^(pending|completed|cancelled|failed)$")


class TaskIdArgs(BaseModel):
    task_id: uuid.UUID


def resolve_reminder_due(
    args: CreateReminderArgs, *, default_timezone: str, now: datetime
) -> ResolvedDue:
    """Resolve the due instant, including server-side relative days.

    Kept pure so the date logic is testable without a database or a model.
    """

    zone_name = args.timezone or default_timezone
    if args.day_expression and args.time:
        zone = load_zone(zone_name)
        day = resolve_day(args.day_expression, now, zone)
        naive = datetime.combine(day, parse_clock(args.time))
        return resolve_local(naive, zone_name)
    return parse_local(args.due_local or "", zone_name)


@dataclass
class TaskTools:
    """Reminders, scoped to one user by construction."""

    sessions: async_sessionmaker[AsyncSession]
    max_attempts: int = 5
    default_timezone: str = "Europe/Madrid"
    now: Callable[[], datetime] | None = None

    async def create_reminder(
        self, *, user_id: int, args: CreateReminderArgs, **_: Any
    ) -> ToolResult[dict[str, Any]]:
        moment = self.now() if self.now is not None else datetime.now(UTC)
        try:
            resolved = resolve_reminder_due(
                args, default_timezone=self.default_timezone, now=moment
            )
        except InvalidLocalTime as exc:
            # A DST edge is a clarification request, not a crash and not a silent shift.
            return ToolResult.failure(
                "invalid_local_time",
                message_key="error.invalid_due_time",
                detail=exc.detail,
                reason=exc.reason,
            )
        except UnknownDayExpression as exc:
            return ToolResult.failure(
                "invalid_day_expression",
                message_key="error.invalid_due_time",
                detail=str(exc),
            )

        if resolved.due_at_utc <= moment:
            # The model used to guess dates; a past reminder would fire immediately or never.
            return ToolResult.failure(
                "reminder_in_past",
                message_key="error.reminder_in_past",
                detail="the due time is not in the future",
            )

        key = creation_key(user_id=user_id, title=args.title, due_at_utc=resolved.due_at_utc)
        async with self.sessions() as session, session.begin():
            task, created = await repositories.create_task(
                session,
                user_id=user_id,
                title=args.title,
                description=args.description,
                due_at=resolved.due_at_utc,
                timezone=resolved.timezone,
                idempotency_key=key,
                max_attempts=self.max_attempts,
            )
            payload = {
                "task_id": str(task.id),
                "title": task.title,
                "due_at": resolved.due_at_utc.isoformat(),
                "local": resolved.local_text,
                "created": created,
            }
        return ToolResult.success(payload, created=created)

    async def list_tasks(
        self, *, user_id: int, args: ListTasksArgs | None = None, **_: Any
    ) -> ToolResult[list[dict[str, Any]]]:
        status = (args or ListTasksArgs()).status
        async with self.sessions() as session:
            rows = await repositories.list_tasks(session, user_id=user_id, status=status)
            payload = [
                {
                    "task_id": str(row.id),
                    "title": row.title,
                    "state": row.state,
                    "due_at": row.due_at.isoformat(),
                    "local": row.due_at.astimezone(zoneinfo.ZoneInfo(row.timezone)).strftime(
                        "%d/%m/%Y %H:%M"
                    ),
                    "timezone": row.timezone,
                }
                for row in rows
            ]
        return ToolResult.success(payload, count=len(payload))

    async def complete_task(
        self, *, user_id: int, args: TaskIdArgs, **_: Any
    ) -> ToolResult[dict[str, Any]]:
        return await self._set_state(
            user_id=user_id, task_id=args.task_id, state=repositories.TASK_COMPLETED
        )

    async def cancel_reminder(
        self, *, user_id: int, args: TaskIdArgs, **_: Any
    ) -> ToolResult[dict[str, Any]]:
        return await self._set_state(
            user_id=user_id, task_id=args.task_id, state=repositories.TASK_CANCELLED
        )

    async def _set_state(
        self, *, user_id: int, task_id: uuid.UUID, state: str
    ) -> ToolResult[dict[str, Any]]:
        async with self.sessions() as session, session.begin():
            task = await repositories.set_task_state(
                session, user_id=user_id, task_id=task_id, state=state
            )
            if task is None:
                # Another user's task id is a not-found, never a permission error that
                # confirms existence (the same non-disclosure rule as documents, §6.5).
                return ToolResult.failure(
                    "task_not_found", message_key="error.task_not_found", task_id=str(task_id)
                )
            payload = {"task_id": str(task.id), "title": task.title, "state": task.state}
        return ToolResult.success(payload)


def specs(tools: TaskTools) -> list[ToolSpec]:
    """Bind the reminder tools into registry entries."""

    return [
        ToolSpec(
            name="create_reminder",
            description=(
                "Create a reminder. For relative days use day_expression ('today', 'tomorrow', "
                "'this_monday', ...) plus time (HH:MM); otherwise due_local is 'YYYY-MM-DD HH:MM'. "
                "Dates in the past are refused."
            ),
            args_model=CreateReminderArgs,
            risk_level=RiskLevel.REVERSIBLE,
            timeout_seconds=10.0,
            handler=tools.create_reminder,
        ),
        ToolSpec(
            name="list_tasks",
            description="List the user's tasks, optionally filtered by state.",
            args_model=ListTasksArgs,
            risk_level=RiskLevel.READ,
            timeout_seconds=10.0,
            handler=tools.list_tasks,
        ),
        ToolSpec(
            name="complete_task",
            description="Mark a task as completed.",
            args_model=TaskIdArgs,
            risk_level=RiskLevel.REVERSIBLE,
            timeout_seconds=10.0,
            handler=tools.complete_task,
        ),
        ToolSpec(
            name="cancel_reminder",
            description="Cancel a pending reminder.",
            args_model=TaskIdArgs,
            risk_level=RiskLevel.REVERSIBLE,
            timeout_seconds=10.0,
            handler=tools.cancel_reminder,
        ),
    ]


__all__ = [
    "specs",
    "CreateReminderArgs",
    "ListTasksArgs",
    "REMINDER_PIPELINE_VERSION",
    "TaskIdArgs",
    "TaskTools",
    "creation_key",
]
