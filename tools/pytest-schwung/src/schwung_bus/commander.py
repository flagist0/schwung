"""Command-pattern infrastructure for UI tests.

Each test action that mutates Move's UI state is a ``Command`` with:

  - ``precondition(bus, commander)`` — raise ``PreconditionError`` if the
    current state isn't valid for this action. Catches state drift
    loudly instead of silently doing the wrong thing.
  - ``execute(bus, commander)`` — apply the action.
  - ``undo(bus, commander)`` — reverse the action.

Commands are run through a ``Commander`` which keeps a LIFO stack and
calls ``.undo()`` on every executed command in reverse order at the
end of the test (via the ``commander`` autouse fixture). Nested
composite commands work automatically: a composite that runs its
sub-commands via ``commander.do(...)`` puts each on the same stack,
so the natural LIFO unwind handles them in the right order without
the composite needing its own ``.undo()``.

Failure semantics: undo errors are NOT swallowed. If undo fails, the
test fixture re-raises and the next test sees the unbalanced state
(in practice it'll fail at its first precondition check, naming what's
wrong). This is by design — silent partial undo would leave the device
in an undefined state and contaminate subsequent tests invisibly.

See ``move_commands.py`` for concrete commands.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .client import SchwungBus


class PreconditionError(RuntimeError):
    """Command's precondition() found the system in an unexpected state."""


class UndoError(RuntimeError):
    """Command's undo() failed mid-stack — remaining stack is .remaining."""
    def __init__(self, msg: str, remaining: List["Command"]):
        super().__init__(msg)
        self.remaining = remaining


class Command(ABC):
    """Abstract base for a reversible UI action.

    Concrete subclasses override execute/undo (required) and
    precondition (optional). ``name`` should be a short, stable
    identifier used in logs; defaults to the class name.
    """

    name: str = ""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls.name:
            cls.name = cls.__name__

    def precondition(self, bus: "SchwungBus", commander: "Commander") -> None:
        """Default: no check. Override and raise PreconditionError if needed."""

    @abstractmethod
    def execute(self, bus: "SchwungBus", commander: "Commander") -> None: ...

    @abstractmethod
    def undo(self, bus: "SchwungBus", commander: "Commander") -> None: ...

    def __repr__(self) -> str:
        return f"<{self.name}>"


@dataclass
class Commander:
    """LIFO command stack with autouse-fixture-driven teardown.

    Tests usually receive this as the ``commander`` fixture:

        def test_x(bus, commander):
            commander.do(EnterTrackMenu())
            commander.do(SelectTrack(1))
            ...  # at test end, commander.undo_all() runs in fixture teardown
    """
    bus: "SchwungBus"
    stack: List[Command] = field(default_factory=list)
    log: List[str] = field(default_factory=list)

    def do(self, cmd: Command) -> Command:
        """Run a command's precondition + execute, then push onto the stack.

        Raises PreconditionError if precondition fails (command is NOT
        pushed). Any other exception from execute propagates with the
        command already pushed — undo_all() will still try to undo it.
        """
        self.log.append(f"do {cmd!r}")
        cmd.precondition(self.bus, self)
        cmd.execute(self.bus, self)
        self.stack.append(cmd)
        return cmd

    def undo_all(self) -> None:
        """Undo every executed command in reverse order. Fail loud on errors."""
        failed: List[Command] = []
        while self.stack:
            cmd = self.stack.pop()
            self.log.append(f"undo {cmd!r}")
            try:
                cmd.undo(self.bus, self)
            except Exception as e:
                failed.append(cmd)
                # Bail immediately — partial undo leaves worse state than no undo.
                raise UndoError(
                    f"undo failed for {cmd!r}: {e}; "
                    f"stack remaining: {self.stack!r}, also-failed: {failed!r}",
                    remaining=list(self.stack),
                ) from e
