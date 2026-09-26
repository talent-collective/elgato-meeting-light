"""Turn the light off after it has been on for more than four hours.

The timer starts when the commanded light state changes from off (or unknown)
to on. It is cleared by any camera-off reading. After it fires, the light
stays off until the camera has been off at least once and then on again.

Elapsed time uses wall-clock ``time.time``, not ``time.monotonic`` and not a
poll count. On macOS the monotonic clock pauses during system sleep, so a
light left on across sleep would never reach four hours. A backward step
(NTP, a timezone change, or a manual clock change) resets the start to now.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

# One place. Strictly greater than this, not equal, turns the light off.
AUTO_OFF_AFTER_SECONDS = 4 * 60 * 60

AUTO_OFF_MESSAGE = (
    "Light on for over 4h continuously; auto-off. "
    "Will turn on again after the camera goes off and back on."
)


@dataclass(frozen=True)
class LightCommand:
    """Desired light state for one poll.

    ``auto_off`` is true only on the poll that crosses the limit, so the
    caller can log that once.
    """

    on: bool
    auto_off: bool


class AutoOff:
    """Pure on-duration guard. Pass a clock callable; nothing here sleeps."""

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        limit_seconds: float = AUTO_OFF_AFTER_SECONDS,
    ) -> None:
        self._clock = clock
        self._limit_seconds = limit_seconds
        self._on_since: Optional[float] = None
        self._latched_off = False

    def decide(self, camera_on: bool) -> LightCommand:
        if not camera_on:
            self._on_since = None
            self._latched_off = False
            return LightCommand(on=False, auto_off=False)

        if self._latched_off:
            return LightCommand(on=False, auto_off=False)

        now = self._clock()
        if self._on_since is None:
            self._on_since = now
            return LightCommand(on=True, auto_off=False)

        elapsed = now - self._on_since
        if elapsed < 0:
            self._on_since = now
            return LightCommand(on=True, auto_off=False)

        if elapsed > self._limit_seconds:
            self._latched_off = True
            self._on_since = None
            return LightCommand(on=False, auto_off=True)

        return LightCommand(on=True, auto_off=False)
