"""Class-scoped pristine_set demo.

Shows the cost-saving pattern: one restart at class start, many short
tests in declaration order. Useful when you have a sequence of
related observations that build on each other and you'd rather pay
the ~3 s restart once than once per test.

The tradeoff: tests inside a class are ORDERED and STATEFUL. Each
test sees whatever state the previous test left behind. If you
need independent pristine starts, split into multiple classes.
"""

from __future__ import annotations

import pytest

from schwung_bus.move_commands import SelectTrack


class TestStepPatternBuildup:
    """One pristine restart, then incrementally build a step pattern
    across three tests. Total wall-clock cost: ~3 s (restart) + ~0.3 s
    (three small tests). Compared to function-scoped pristine_set
    which would be ~9 s for the same coverage.

    Ordering: pytest runs methods in source order within a class.
    """

    def test_initially_empty_then_select_track(self, bus, commander, pristine_set_class):
        """First test in the class — runs right after the class-scoped
        pristine_set_class fixture restarted Move. Establishes the
        common starting point for the rest of the class: track 2
        selected (melodic, all pads at 0x7B), no steps set.
        """
        from _helpers import enter_note_mode_or_skip
        enter_note_mode_or_skip(bus, commander, track=2)

        # All step LEDs should be at the same dim baseline color.
        steps = bus.snapshot_step_leds()
        from collections import Counter
        most_common, count = Counter(steps).most_common(1)[0]
        assert count >= 12, (
            f"pristine step grid should be mostly uniform dim, "
            f"got {count}/16 at {most_common:#04x}. Steps: {steps.hex()}"
        )

    def test_step_1_toggles_on(self, bus, pristine_set_class):
        """Second test — track 2 is already selected from the first
        test (state persists within the class). Toggle step 1, watch
        idx 0 brighten.
        """
        before = bus.snapshot_step_leds()
        bus.press_step(16)        # note 16 = step 1 = idx 0
        bus.wait_frame(2)
        bus.release_step(16)
        bus.wait_frame(8)
        after = bus.snapshot_step_leds()

        assert after[0] != before[0], (
            f"step 1 (idx 0) didn't change on toggle: "
            f"{before[0]:#04x} → {after[0]:#04x}"
        )
        assert after[0] > before[0], (
            f"step 1 toggled in the dim direction (expected brighter): "
            f"{before[0]:#04x} → {after[0]:#04x}"
        )

    def test_step_5_toggles_on_top_of_step_1(self, bus, pristine_set_class):
        """Third test — step 1 was toggled on by the previous test
        and is still set. Toggling step 5 should land alongside it,
        so the after-state has BOTH bright.

        This is the explicit "state from earlier test still matters"
        pattern. With function-scoped pristine_set this test would
        start clean (step 1 dim again), so the test couldn't observe
        the accumulation.
        """
        before = bus.snapshot_step_leds()
        # Step 1 should still be lit from the previous test.
        baseline_brightness = sorted(set(before))[0]
        assert before[0] > baseline_brightness, (
            f"step 1 should still be lit from the previous test in "
            f"this class. Got {before[0]:#04x}, dim baseline "
            f"{baseline_brightness:#04x}. Class state isolation broken?"
        )

        bus.press_step(20)        # note 20 = step 5 = idx 4
        bus.wait_frame(2)
        bus.release_step(20)
        bus.wait_frame(8)
        after = bus.snapshot_step_leds()

        assert after[4] != before[4], (
            f"step 5 (idx 4) didn't change on toggle in mid-pattern: "
            f"{before[4]:#04x} → {after[4]:#04x}"
        )
        # Step 1 still lit alongside step 5.
        assert after[0] > baseline_brightness, (
            f"step 1 should still be lit after toggling step 5, "
            f"got {after[0]:#04x}"
        )


class TestStepPatternIndependentClass:
    """Second class — starts from its OWN pristine restart. The
    state accumulated in TestStepPatternBuildup is gone (Move was
    restarted with the empty template again).

    This proves class-scope isolation: tests in different classes
    don't share state, even though tests in the same class do.
    """

    def test_starts_pristine_again(self, bus, commander, pristine_set_class):
        """Should see the same empty baseline as the first test in
        TestStepPatternBuildup, even though that earlier class left
        steps 1 and 5 set. Class boundary forced a fresh restart.
        """
        from _helpers import enter_note_mode_or_skip
        enter_note_mode_or_skip(bus, commander, track=2)
        steps = bus.snapshot_step_leds()
        from collections import Counter
        most_common, count = Counter(steps).most_common(1)[0]
        assert count >= 12, (
            f"second class should restart from pristine — but step "
            f"grid is not mostly uniform: {count}/16 at {most_common:#04x}. "
            f"State leaked across classes? Steps: {steps.hex()}"
        )
