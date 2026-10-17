"""Agent layer (PLAN.md §2.1). Imports only layers 0-4 plus ``llm.protocols``.

Hard rule from §2.2: nothing under ``app/agent/`` may import ``app.storage.stores``,
``app.bot``, ``app.api``, ``fastapi`` or ``httpx``. The handler assembles the shaped prompt
input and passes it in; the graph never reads a store.
"""

from __future__ import annotations
