"""Parse macOS camera-on/off signals and turn them into a light decision.

The meeting light used to call the camera "on" whenever the ``VDCAssistant``
or ``AppleCameraAssistant`` process existed. On Apple Silicon those CoreMediaIO
helper processes stay running while the green camera dot is off, and they do
not exit when an app closes the camera. The check was therefore stuck on, the
light came on at launch, and later camera toggles did nothing.

macOS does not keep one stable log line for this:

* Legacy (through early macOS 12): ``Post event kCameraStreamStart`` /
  ``kCameraStreamStop`` from VDCAssistant.
* Monterey / Ventura external cameras: ``VDCAssistant_Power_State = On/Off``.
* Ventura / Sonoma built-in cameras: ``PowerOnCamera`` / ``PowerOffCamera`` and
  ``AppleH13CamIn::power_on_hardware`` / ``power_off_hardware``.
* Sonoma and later, Control Center green-dot attributions:
  ``Active activity attributions changed to ["cam:<bundle>"]``.
  A ``mic:`` entry alone is a microphone, not a camera.
* Sequoia 15 and Tahoe 26 (macOS 26), Control Center aggregate, including the
  empty-dictionary release form captured on macOS 26.5::

    Frame publisher cameras changed to [us.zoom.xos: ["0x1000002e1a4c01"]]
    Frame publisher cameras changed to [:]

Aggregate lines are a full snapshot of who holds the camera. Once one has been
seen, older edge events (hardware power, stream start/stop) are ignored so a
lingering power-on message cannot force the light back on after release.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

# Absolute path: launchd's PATH is only /usr/bin:/bin:/usr/sbin:/sbin, and a
# relative "log" could be shadowed. /usr/bin/log is the system binary.
LOG_BIN = "/usr/bin/log"

# Joined with OR. "Cameras changed to" is intentionally broader than the
# macOS 26 "Frame publisher cameras changed to" line so the Sequoia wording
# is included; the parser treats both as one aggregate snapshot.
LOG_PREDICATE = " OR ".join(
    [
        'eventMessage CONTAINS "Frame publisher cameras changed to"',
        'eventMessage CONTAINS "Cameras changed to"',
        'eventMessage CONTAINS "Active activity attributions changed to"',
        'eventMessage CONTAINS "kCameraStreamStart"',
        'eventMessage CONTAINS "kCameraStreamStop"',
        'eventMessage CONTAINS "CMIODeviceStartStream"',
        'eventMessage CONTAINS "CMIODeviceStopStream"',
        'eventMessage CONTAINS "CMIOExtensionPropertyClientStreamingFromDALDevice"',
        'eventMessage CONTAINS "PowerOnCamera"',
        'eventMessage CONTAINS "PowerOffCamera"',
        'eventMessage CONTAINS "power_on_hardware"',
        'eventMessage CONTAINS "power_off_hardware"',
        'eventMessage CONTAINS "VDCAssistant_Power_State"',
    ]
)

# Sensor ids are the bundle id after "cam:" or "mic:" up to a comma, quote, or bracket.
_ATTRIBUTION_MARKER = "Active activity attributions changed to "
_CAMERAS_CHANGED_MARKERS = ("cameras changed to ", "Cameras changed to ")
_CAM_ID = re.compile(r"cam:([^,\"\'\]\s)]+)")
_MIC_ID = re.compile(r"mic:([^,\"\'\]\s)]+)")
_CLIENT_ID = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.+-]*)\s*:")
_POWER_STATE = re.compile(r'VDCAssistant_Power_State"?\s*=\s*(On|Off)\b')
_DAL_STREAMING = re.compile(
    r"CMIOExtensionPropertyClientStreamingFromDALDevice\s+([01])\b"
)
_EMPTY_COLLECTION = re.compile(r"(?:\[\s*\]|\(\s*\))")


@dataclass(frozen=True)
class CameraEvent:
    """One parsed log line.

    ``kind`` is ``aggregate`` for a full snapshot (Control Center's camera set
    or sensor attributions) or ``edge`` for a single start/stop.
    """

    kind: str
    on: bool
    detail: str


@dataclass(frozen=True)
class Transition:
    previous: bool
    current: bool
    detail: str


@dataclass
class CameraStateMachine:
    """Fold camera log events into "should the light be on?".

    Starts unknown. Unknown is not "on": callers must not light the lamp
    until a real camera-on event has been observed.
    """

    in_use: bool = False
    known: bool = False
    detail: str = "no camera signal yet"
    aggregate_seen: bool = False

    def apply(self, event: CameraEvent) -> Optional[Transition]:
        if event.kind == "edge" and self.aggregate_seen:
            return None

        previous = self.in_use
        previous_detail = self.detail
        if event.kind == "aggregate":
            self.aggregate_seen = True
        elif event.kind != "edge":
            return None

        self.known = True
        self.in_use = event.on
        self.detail = event.detail
        if self.in_use == previous and self.detail == previous_detail:
            return None
        return Transition(previous, self.in_use, self.detail)


def light_should_be_on(machine: CameraStateMachine) -> bool:
    """Return whether the lamp should be lit.

    An unknown detector (no log line yet, log command failed, empty history)
    reports off. That is what stops install and launch from forcing the light on.
    """

    if not machine.known:
        return False
    return machine.in_use


def format_transition(transition: Transition) -> str:
    previous = "on" if transition.previous else "off"
    current = "on" if transition.current else "off"
    if transition.previous == transition.current:
        return f"Camera state: {current} ({transition.detail})"
    return f"Camera state changed: {previous} -> {current} ({transition.detail})"


def format_state(machine: CameraStateMachine) -> str:
    if not machine.known:
        return (
            "Camera state: off (no recent camera event; assuming off so the "
            "light is not forced on)"
        )
    current = "on" if machine.in_use else "off"
    return f"Camera state: {current} ({machine.detail})"


def parse_camera_log_line(line: str) -> Optional[CameraEvent]:
    """Return a camera event, or None if ``line`` is not a state signal.

    ``log stream`` echoes its predicate on a ``Filtering the log data`` header
    line. That header contains every marker we search for (including
    ``kCameraStreamStart``). It must not be treated as a camera turning on.
    """

    # `log stream` prints its predicate back, and that banner contains every
    # marker we match (including kCameraStreamStart). A real camera event
    # never contains the predicate syntax.
    if (
        not line
        or "Filtering the log data" in line
        or "eventMessage CONTAINS" in line
        or line.startswith("Timestamp ")
    ):
        return None

    if _ATTRIBUTION_MARKER in line:
        return _parse_attribution(line.split(_ATTRIBUTION_MARKER, 1)[1])

    for marker in _CAMERAS_CHANGED_MARKERS:
        if marker in line:
            return _parse_camera_set(line.split(marker, 1)[1])

    if "kCameraStreamStart" in line:
        return CameraEvent("edge", True, "kCameraStreamStart")
    if "kCameraStreamStop" in line:
        return CameraEvent("edge", False, "kCameraStreamStop")

    if "CMIODeviceStartStream" in line:
        return CameraEvent("edge", True, "CMIODeviceStartStream")
    if "CMIODeviceStopStream" in line:
        return CameraEvent("edge", False, "CMIODeviceStopStream")

    dal = _DAL_STREAMING.search(line)
    if dal:
        on = dal.group(1) == "1"
        return CameraEvent(
            "edge",
            on,
            "ContinuityCapture streaming " + ("on" if on else "off"),
        )

    if "PowerOnCamera" in line or "power_on_hardware" in line:
        return CameraEvent("edge", True, "camera hardware power on")
    if "PowerOffCamera" in line or "power_off_hardware" in line:
        return CameraEvent("edge", False, "camera hardware power off")

    power = _POWER_STATE.search(line)
    if power:
        on = power.group(1) == "On"
        return CameraEvent(
            "edge",
            on,
            "UVC camera power " + ("on" if on else "off"),
        )

    return None


def replay_log_lines(
    lines: Iterable[str],
) -> tuple[CameraStateMachine, list[Transition]]:
    machine = CameraStateMachine()
    transitions: list[Transition] = []
    for line in lines:
        event = parse_camera_log_line(line)
        if event is None:
            continue
        transition = machine.apply(event)
        if transition is not None:
            transitions.append(transition)
    return machine, transitions


def _parse_attribution(payload: str) -> Optional[CameraEvent]:
    """Control Center sensor-indicator snapshot. ``cam:`` is the camera; ``mic:`` is not."""

    if "<private>" in payload and "cam:" not in payload and "mic:" not in payload:
        return None

    cameras = _CAM_ID.findall(payload)
    mics = _MIC_ID.findall(payload)
    if cameras:
        return CameraEvent("aggregate", True, "camera attribution: " + ", ".join(cameras))
    if _EMPTY_COLLECTION.search(payload):
        return CameraEvent("aggregate", False, "no camera attribution")
    if mics:
        return CameraEvent(
            "aggregate",
            False,
            "microphone only (" + ", ".join(mics) + "); camera off",
        )
    # Unrecognized payload (new format, redaction). Leave the previous state.
    return None


def _parse_camera_set(payload: str) -> Optional[CameraEvent]:
    """Control Center's published set of in-use cameras.

    Empty array ``[]`` (earlier macOS) and empty dictionary ``[:]`` (macOS 26)
    both mean every camera was released. A missing bracket usually means the
    payload was redacted to ``<private>``; that is not evidence either way.
    """

    open_idx = payload.find("[")
    if open_idx < 0:
        return None
    inner = payload[open_idx + 1 :].lstrip()
    # `[:]` is an empty dictionary. A non-empty `[bundle.id: [...]]` does not
    # start with the colon, so this strip does not touch it.
    if inner.startswith(":"):
        inner = inner[1:].lstrip()
    if inner.startswith("]"):
        return CameraEvent("aggregate", False, "Frame publisher: none")
    names = _CLIENT_ID.findall(inner)
    detail = "Frame publisher: " + (", ".join(names) if names else "camera")
    return CameraEvent("aggregate", True, detail)
