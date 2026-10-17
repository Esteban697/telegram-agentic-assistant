"""Autonomy levels and the tool→level registry (spec §7, PLAN.md §4.3).

Layer 0 and **pure**: no I/O, no imports from other ``app`` packages.

Spec §7: "Las políticas se aplican en código, no dependen exclusivamente del prompt." The table
lives here as data so that "every tool has a level" is a checkable invariant rather than a
property of scattered decorators.

| Level | Meaning | Policy |
|---|---|---|
| 0 | read | allowed for an authorised user, logged |
| 1 | reversible internal write | allowed, logged |
| 2 | external commitment | **explicit approval required** |
| 3 | high impact | disabled in the MVP |
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class RiskLevel(IntEnum):
    READ = 0
    REVERSIBLE = 1
    EXTERNAL = 2
    HIGH_IMPACT = 3


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    """What a level means operationally."""

    level: RiskLevel
    requires_approval: bool
    enabled: bool


POLICIES: dict[RiskLevel, RiskPolicy] = {
    RiskLevel.READ: RiskPolicy(RiskLevel.READ, requires_approval=False, enabled=True),
    RiskLevel.REVERSIBLE: RiskPolicy(RiskLevel.REVERSIBLE, requires_approval=False, enabled=True),
    RiskLevel.EXTERNAL: RiskPolicy(RiskLevel.EXTERNAL, requires_approval=True, enabled=True),
    # Spec §7: "Nivel 3 — Alto impacto ... Deshabilitado en MVP".
    RiskLevel.HIGH_IMPACT: RiskPolicy(RiskLevel.HIGH_IMPACT, requires_approval=True, enabled=False),
}


class Level3Disabled(RuntimeError):
    """A level-3 tool was requested while the MVP has level 3 disabled."""

    code = "level3_disabled"


class UnknownTool(RuntimeError):
    """A tool name with no registered risk level. A startup-time failure, not a runtime one."""

    code = "unknown_tool"


def policy_for(level: RiskLevel) -> RiskPolicy:
    return POLICIES[RiskLevel(level)]


def requires_approval(level: RiskLevel) -> bool:
    return policy_for(level).requires_approval


def assert_enabled(level: RiskLevel, *, allow_level3: bool = False) -> None:
    """Raise when a tool's level may not run at all."""

    policy = policy_for(level)
    if policy.enabled:
        return
    if level is RiskLevel.HIGH_IMPACT and allow_level3:  # pragma: no cover - refused in prod
        return
    raise Level3Disabled(
        f"level {int(level)} tools are disabled in the MVP (spec §7); "
        "payments, purchases and broad deletions have no implementation"
    )


def assert_registry_complete(levels: dict[str, RiskLevel], *, allow_level3: bool = False) -> None:
    """Every registered tool must carry a level, and no level-3 tool may be registered.

    Called at startup and asserted by a unit test: a tool without a level would bypass the
    policy table entirely, which is the one failure this module exists to prevent.
    """

    missing = sorted(name for name, level in levels.items() if level is None)
    if missing:  # pragma: no cover - the type system already prevents this
        raise UnknownTool(f"tools without a risk level: {missing}")

    if not allow_level3:
        forbidden = sorted(
            name for name, level in levels.items() if RiskLevel(level) is RiskLevel.HIGH_IMPACT
        )
        if forbidden:
            raise Level3Disabled(
                f"level-3 tools must not be registered in the MVP: {forbidden} (spec §7)"
            )


__all__ = [
    "POLICIES",
    "Level3Disabled",
    "RiskLevel",
    "RiskPolicy",
    "UnknownTool",
    "assert_enabled",
    "assert_registry_complete",
    "policy_for",
    "requires_approval",
]
