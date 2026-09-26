"""Four-hour auto-off. The clock is injected, so nothing here sleeps."""

from __future__ import annotations

import inspect
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auto_off import AUTO_OFF_AFTER_SECONDS, AUTO_OFF_MESSAGE, AutoOff  # noqa: E402
from main import run_forever  # noqa: E402

LIMIT = AUTO_OFF_AFTER_SECONDS
THREE_HOURS_FIFTY_NINE_MINUTES = 3 * 60 * 60 + 59 * 60


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Listener:
    def __init__(self) -> None:
        self.ip = "192.0.2.5"
        self.port = 9123


class AutoOffTests(unittest.TestCase):
    def test_limit_constant_is_four_hours_of_wall_clock(self):
        self.assertEqual(AUTO_OFF_AFTER_SECONDS, 4 * 60 * 60)
        clock_default = inspect.signature(AutoOff.__init__).parameters["clock"].default
        self.assertIs(clock_default, time.time)

    def test_fires_only_after_four_hours(self):
        clock = FakeClock()
        guard = AutoOff(clock)
        self.assertTrue(guard.decide(True).on)

        clock.now = LIMIT - 1
        before = guard.decide(True)
        self.assertTrue(before.on)
        self.assertFalse(before.auto_off)

        clock.now = LIMIT
        exact = guard.decide(True)
        self.assertTrue(exact.on)
        self.assertFalse(exact.auto_off)

        clock.now = LIMIT + 1
        fired = guard.decide(True)
        self.assertFalse(fired.on)
        self.assertTrue(fired.auto_off)

    def test_stays_off_while_the_camera_is_still_on(self):
        clock = FakeClock()
        guard = AutoOff(clock)
        guard.decide(True)
        clock.now = LIMIT + 1
        self.assertTrue(guard.decide(True).auto_off)

        for _ in range(50):
            clock.advance(10 * 60)
            later = guard.decide(True)
            self.assertFalse(later.on)
            self.assertFalse(later.auto_off)

    def test_rearms_after_camera_off_then_on(self):
        clock = FakeClock()
        guard = AutoOff(clock)
        guard.decide(True)
        clock.now = LIMIT + 1
        self.assertFalse(guard.decide(True).on)

        self.assertFalse(guard.decide(False).on)
        rearmed = guard.decide(True)
        self.assertTrue(rearmed.on)
        self.assertFalse(rearmed.auto_off)

        started = clock.now
        clock.now = started + LIMIT
        self.assertTrue(guard.decide(True).on)
        clock.now = started + LIMIT + 1
        fired = guard.decide(True)
        self.assertFalse(fired.on)
        self.assertTrue(fired.auto_off)

    def test_any_off_resets_the_timer(self):
        clock = FakeClock()
        guard = AutoOff(clock)
        first_on = clock.now
        self.assertTrue(guard.decide(True).on)

        clock.now = THREE_HOURS_FIFTY_NINE_MINUTES
        self.assertTrue(guard.decide(True).on)
        self.assertFalse(guard.decide(False).on)

        second_on = clock.now
        self.assertTrue(guard.decide(True).on)

        # Past four hours from the first ON, but only a minute into the second.
        clock.now = first_on + LIMIT + 1
        self.assertTrue(guard.decide(True).on)

        clock.now = second_on + LIMIT
        self.assertTrue(guard.decide(True).on)
        clock.now = second_on + LIMIT + 1
        fired = guard.decide(True)
        self.assertFalse(fired.on)
        self.assertTrue(fired.auto_off)

    def test_forward_jump_like_sleep_triggers_auto_off(self):
        clock = FakeClock()
        guard = AutoOff(clock)
        self.assertTrue(guard.decide(True).on)
        clock.advance(5 * 60 * 60)
        fired = guard.decide(True)
        self.assertFalse(fired.on)
        self.assertTrue(fired.auto_off)

    def test_backward_jump_does_not_crash_or_fire(self):
        clock = FakeClock(10_000)
        guard = AutoOff(clock)
        self.assertTrue(guard.decide(True).on)

        clock.now = 100
        jumped = guard.decide(True)
        self.assertTrue(jumped.on)
        self.assertFalse(jumped.auto_off)

        clock.now = 100 + LIMIT
        self.assertTrue(guard.decide(True).on)
        clock.now = 100 + LIMIT + 1
        self.assertFalse(guard.decide(True).on)


class RunForeverAutoOffTests(unittest.TestCase):
    def test_set_light_sequence_follows_the_camera_and_the_four_hour_limit(self):
        script = [
            (0, False),
            (10, True),
            (10 + LIMIT, True),
            (10 + LIMIT + 1, True),
            (10 + LIMIT + 2, True),
            (10 + LIMIT + 3, True),
            (10 + LIMIT + 4, False),
            (10 + LIMIT + 5, True),
            (10 + LIMIT + 5 + LIMIT, True),
            (10 + LIMIT + 5 + LIMIT + 1, True),
        ]
        step = {"i": 0}
        calls = []

        def clock():
            return script[step["i"]][0]

        def camera():
            return script[step["i"]][1]

        def apply_light(ip, port, on):
            self.assertEqual(ip, "192.0.2.5")
            self.assertEqual(port, 9123)
            calls.append(on)
            return True

        def pause(_seconds):
            step["i"] += 1

        with self.assertLogs("elgato-meeting-light", level="INFO") as captured:
            run_forever(
                _Listener(),
                dry_run=False,
                camera_in_use=camera,
                apply_light=apply_light,
                clock=clock,
                sleep=pause,
                max_polls=len(script),
            )

        self.assertEqual(calls, [False, True, False, True, False])
        auto_off_logs = [line for line in captured.output if AUTO_OFF_MESSAGE in line]
        self.assertEqual(auto_off_logs, [
            "INFO:elgato-meeting-light:%s" % (AUTO_OFF_MESSAGE,),
            "INFO:elgato-meeting-light:%s" % (AUTO_OFF_MESSAGE,),
        ])


if __name__ == "__main__":
    unittest.main()
