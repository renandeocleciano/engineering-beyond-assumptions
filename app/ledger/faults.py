"""Test-only fault injection for laboratory transfer flows.

Hooks are never enabled by public HTTP requests. Tests set callables on the
module-level registry and clear them afterward.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

FaultPoint = Literal[
    "before_idempotency_reservation",
    "after_reservation_before_transfer",
    "after_debit_before_credit",
    "before_commit",
    "after_commit_before_response",
]

_HOOKS: dict[FaultPoint, Callable[[], None]] = {}


def set_fault(point: FaultPoint, hook: Callable[[], None] | None) -> None:
    if hook is None:
        _HOOKS.pop(point, None)
    else:
        _HOOKS[point] = hook


def clear_faults() -> None:
    _HOOKS.clear()


def maybe_fault(point: FaultPoint) -> None:
    hook = _HOOKS.get(point)
    if hook is not None:
        hook()
