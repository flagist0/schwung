"""Unit tests for Command / Commander logic — no daemon required.

Uses a fake bus to exercise the stack/undo semantics in isolation.
On-hardware tests of concrete commands live in tests/e2e/.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import pytest

from schwung_bus import Command, Commander, PreconditionError, UndoError


@dataclass
class FakeBus:
    """Records each operation in order so tests can assert on the trace."""
    log: List[str] = field(default_factory=list)


@dataclass(repr=False)  # let Command.__repr__ win (uses self.name)
class RecordCmd(Command):
    """Test command — records what it did into the bus's log."""
    tag: str = ""
    fail_pre: bool = False
    fail_exec: bool = False
    fail_undo: bool = False

    def __post_init__(self):
        self.name = f"rec({self.tag})"

    def precondition(self, bus, commander):
        if self.fail_pre:
            raise PreconditionError(f"{self.tag}: pre fail")

    def execute(self, bus, commander):
        if self.fail_exec:
            raise RuntimeError(f"{self.tag}: exec fail")
        bus.log.append(f"exec {self.tag}")

    def undo(self, bus, commander):
        if self.fail_undo:
            raise RuntimeError(f"{self.tag}: undo fail")
        bus.log.append(f"undo {self.tag}")


def test_do_runs_precondition_then_execute():
    bus = FakeBus()
    c = Commander(bus=bus)
    c.do(RecordCmd(tag="A"))
    assert bus.log == ["exec A"]
    assert len(c.stack) == 1


def test_precondition_failure_does_not_push():
    bus = FakeBus()
    c = Commander(bus=bus)
    with pytest.raises(PreconditionError, match="A: pre fail"):
        c.do(RecordCmd(tag="A", fail_pre=True))
    assert bus.log == []
    assert c.stack == []


def test_undo_all_runs_lifo():
    bus = FakeBus()
    c = Commander(bus=bus)
    c.do(RecordCmd(tag="A"))
    c.do(RecordCmd(tag="B"))
    c.do(RecordCmd(tag="C"))
    c.undo_all()
    assert bus.log == ["exec A", "exec B", "exec C", "undo C", "undo B", "undo A"]
    assert c.stack == []


def test_undo_failure_bails_loud():
    bus = FakeBus()
    c = Commander(bus=bus)
    c.do(RecordCmd(tag="A"))
    c.do(RecordCmd(tag="B", fail_undo=True))
    c.do(RecordCmd(tag="C"))
    with pytest.raises(UndoError) as exc_info:
        c.undo_all()
    # C undid successfully, B failed, A still on the stack
    assert "rec(B)" in str(exc_info.value)
    assert exc_info.value.remaining and exc_info.value.remaining[0].name == "rec(A)"
    assert bus.log == ["exec A", "exec B", "exec C", "undo C"]


def test_exec_failure_leaves_command_off_stack():
    """If execute raises, the command isn't on the stack — undo won't touch it."""
    bus = FakeBus()
    c = Commander(bus=bus)
    c.do(RecordCmd(tag="A"))
    with pytest.raises(RuntimeError, match="B: exec fail"):
        c.do(RecordCmd(tag="B", fail_exec=True))
    # Only A should be on the stack and undone
    c.undo_all()
    assert bus.log == ["exec A", "undo A"]


def test_command_name_defaults_to_class():
    class MyCmd(Command):
        def execute(self, bus, c): pass
        def undo(self, bus, c): pass
    assert MyCmd().name == "MyCmd"


def test_repr_uses_name():
    cmd = RecordCmd(tag="X")
    assert repr(cmd) == "<rec(X)>"


def test_log_records_do_and_undo():
    bus = FakeBus()
    c = Commander(bus=bus)
    c.do(RecordCmd(tag="A"))
    c.do(RecordCmd(tag="B"))
    c.undo_all()
    assert c.log == ["do <rec(A)>", "do <rec(B)>", "undo <rec(B)>", "undo <rec(A)>"]
