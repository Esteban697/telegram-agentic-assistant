"""Memory tools (spec §10, PLAN.md §2.1).

Layer 4. Each tool is a typed argument model plus an async method returning a
:class:`~app.tools.base.ToolResult`; there are no bare strings and no raised provider errors.

Spec §10 is explicit that `save_preference` is for **explicit or user-confirmed** preferences
only, so the model is never the source of a memory here: the value comes from the user's own
message, and the row records `source='explicit_user'`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage import repositories
from app.tools.base import ToolResult, ToolSpec


class SearchMemoryArgs(BaseModel):
    query: str = Field(min_length=1, max_length=200)


class SavePreferenceArgs(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=4000)


class DeleteMemoryArgs(BaseModel):
    memory_id: uuid.UUID


class ListMemoriesArgs(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)


@dataclass
class MemoryTools:
    """Explicit memory, scoped to one user by construction."""

    sessions: async_sessionmaker[AsyncSession]

    async def search_personal_memory(
        self, *, user_id: int, args: SearchMemoryArgs
    ) -> ToolResult[list[dict[str, Any]]]:
        async with self.sessions() as session:
            rows = await repositories.search_memories(
                session, user_id=user_id, query=args.query, limit=10
            )
        return ToolResult.success(
            [
                {
                    "id": str(row.id),
                    "key": row.key,
                    "value": row.value,
                    "type": row.memory_type,
                }
                for row in rows
            ],
            count=len(rows),
        )

    async def save_preference(
        self, *, user_id: int, args: SavePreferenceArgs
    ) -> ToolResult[dict[str, Any]]:
        async with self.sessions() as session, session.begin():
            row = await repositories.upsert_memory(
                session,
                user_id=user_id,
                key=args.key,
                value=args.value,
                memory_type="preference",
                source="explicit_user",
            )
            key = row.key
        return ToolResult.success({"key": key, "saved": True})

    async def delete_memory_by_key(self, *, user_id: int, key: str) -> ToolResult[dict[str, Any]]:
        """Delete one memory by its exact key.

        `/forget <clave>` uses this rather than searching first and deleting by id: a search is
        a `LIKE` with a row cap, so a key that is a substring of many memories could be reported
        "not found" because ten unrelated rows filled the cap. Matching the key exactly, in SQL,
        removes that failure mode and the extra round trip with it.
        """

        async with self.sessions() as session, session.begin():
            deleted = await repositories.delete_memory_by_key(session, user_id=user_id, key=key)
        return ToolResult.success({"deleted": deleted > 0, "key": key}, deleted=deleted)

    async def delete_memory(
        self, *, user_id: int, args: DeleteMemoryArgs
    ) -> ToolResult[dict[str, Any]]:
        async with self.sessions() as session, session.begin():
            deleted = await repositories.delete_memory(
                session, user_id=user_id, memory_id=args.memory_id
            )
        # Idempotent by design: deleting what is already gone is a success, not an error.
        return ToolResult.success({"deleted": deleted, "memory_id": str(args.memory_id)})

    async def count_memories(self, *, user_id: int) -> ToolResult[dict[str, Any]]:
        async with self.sessions() as session:
            count = await repositories.count_memories(session, user_id=user_id)
        return ToolResult.success({"count": count})

    async def delete_all_memories(self, *, user_id: int) -> ToolResult[dict[str, Any]]:
        """Broad deletion. The caller must have confirmed it (spec §6)."""

        async with self.sessions() as session, session.begin():
            deleted = await repositories.delete_all_memories(session, user_id=user_id)
        return ToolResult.success({"deleted": deleted})

    async def list_memories(
        self, *, user_id: int, args: ListMemoriesArgs | None = None
    ) -> ToolResult[list[dict[str, Any]]]:
        limit = (args or ListMemoriesArgs()).limit
        async with self.sessions() as session:
            rows = await repositories.list_memories(session, user_id=user_id, limit=limit)
        return ToolResult.success(
            [{"id": str(row.id), "key": row.key, "value": row.value} for row in rows],
            count=len(rows),
        )


SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="search_personal_memory",
        description="Search the user's explicit memories by key or value.",
        args_model=SearchMemoryArgs,
        risk_level=0,
    ),
    ToolSpec(
        name="save_preference",
        description="Save an explicit preference the user stated.",
        args_model=SavePreferenceArgs,
        risk_level=1,
    ),
    ToolSpec(
        name="delete_memory",
        description="Delete one explicit memory by id.",
        args_model=DeleteMemoryArgs,
        risk_level=1,
    ),
)

__all__ = [
    "SPECS",
    "DeleteMemoryArgs",
    "ListMemoriesArgs",
    "MemoryTools",
    "SavePreferenceArgs",
    "SearchMemoryArgs",
]
