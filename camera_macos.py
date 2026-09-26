"""macOS camera-in-use check via CoreMediaIO.

``VDCAssistant`` and ``cameracaptured`` are persistent daemons on modern macOS
(confirmed on macOS 26.5 / arm64). ``pgrep`` of those processes is true while
the camera is off, so it cannot drive the light.

The green-dot state is ``kCMIODevicePropertyDeviceIsRunningSomewhere``
(four-character code ``gone``) on each video device from
``kCMIOHardwarePropertyDevices`` (``dev#``) on the system object. A failed
call is off. This module never defaults to on.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import (
    POINTER,
    byref,
    c_int32,
    c_uint32,
    c_void_p,
)
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

log = logging.getLogger("elgato-meeting-light")

FRAMEWORK_CANDIDATES = (
    "/System/Library/Frameworks/CoreMediaIO.framework/CoreMediaIO",
    "/System/Library/Frameworks/CoreMediaIO.framework/Versions/A/CoreMediaIO",
)

# CMIOObjectID of the singleton system object (kCMIOObjectSystemObject).
SYSTEM_OBJECT = 1


def fourcc(code: str) -> int:
    """Apple four-character code, big-endian, as a UInt32."""
    raw = code.encode("ascii")
    if len(raw) != 4:
        raise ValueError("fourcc must be 4 bytes, got %r" % (code,))
    return int.from_bytes(raw, "big")


# CMIOHardware.h / CMIOHardwareDevice.h
PROP_DEVICES = fourcc("dev#")  # kCMIOHardwarePropertyDevices
PROP_RUNNING = fourcc("gone")  # kCMIODevicePropertyDeviceIsRunningSomewhere
SCOPE_GLOBAL = fourcc("glob")  # kCMIOObjectPropertyScopeGlobal
ELEMENT_MAIN = 0  # kCMIOObjectPropertyElementMain


class CMIOObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", c_uint32),
        ("mScope", c_uint32),
        ("mElement", c_uint32),
    ]


@dataclass(frozen=True)
class CameraSnapshot:
    """One poll of every CoreMediaIO video device.

    ``in_use`` is true only when at least one device reported running.
    ``error`` is set when a query failed. Failure never sets ``in_use``.
    """

    in_use: bool
    device_ids: Tuple[int, ...]
    running_ids: Tuple[int, ...]
    error: str = ""


def snapshot_from_queries(
    list_devices: Callable[[], Sequence[int]],
    device_is_running: Callable[[int], bool],
) -> CameraSnapshot:
    """Fold device-list and per-device queries into a snapshot.

    ``list_devices`` or ``device_is_running`` may raise. A raised list query
    yields camera off. A raised per-device query skips that device and is
    recorded on ``error``. Any successful ``True`` still means the camera is on.
    """
    try:
        device_ids = tuple(int(device_id) for device_id in list_devices())
    except Exception as exc:
        return CameraSnapshot(False, (), (), "device list failed: %s" % (exc,))

    running: List[int] = []
    errors: List[str] = []
    for device_id in device_ids:
        try:
            if device_is_running(device_id):
                running.append(device_id)
        except Exception as exc:
            errors.append("device %s: %s" % (device_id, exc))
    return CameraSnapshot(
        bool(running),
        device_ids,
        tuple(running),
        "; ".join(errors),
    )


def format_probe_line(snapshot: CameraSnapshot) -> str:
    if snapshot.error and not snapshot.device_ids:
        return "camera_in_use=false error=%s" % (snapshot.error,)
    running = ",".join(str(device_id) for device_id in snapshot.running_ids) or "none"
    line = "camera_in_use=%s devices=%d running=%s" % (
        "true" if snapshot.in_use else "false",
        len(snapshot.device_ids),
        running,
    )
    if snapshot.error:
        line += " error=%s" % (snapshot.error,)
    return line


def test_light_sequence(camera_in_use: bool) -> Tuple[bool, bool, bool]:
    """Connectivity blink, then the real camera state.

    The last value is what the light is left at. It is on only when
    ``camera_in_use`` is true.
    """
    return (True, False, bool(camera_in_use))


def _address(selector: int) -> CMIOObjectPropertyAddress:
    return CMIOObjectPropertyAddress(selector, SCOPE_GLOBAL, ELEMENT_MAIN)


def _bind(lib: ctypes.CDLL) -> None:
    lib.CMIOObjectGetPropertyDataSize.argtypes = [
        c_uint32,
        POINTER(CMIOObjectPropertyAddress),
        c_uint32,
        c_void_p,
        POINTER(c_uint32),
    ]
    lib.CMIOObjectGetPropertyDataSize.restype = c_int32
    lib.CMIOObjectGetPropertyData.argtypes = [
        c_uint32,
        POINTER(CMIOObjectPropertyAddress),
        c_uint32,
        c_void_p,
        c_uint32,
        POINTER(c_uint32),
        c_void_p,
    ]
    lib.CMIOObjectGetPropertyData.restype = c_int32


_library: Optional[ctypes.CDLL] = None


def load_coremediaio() -> ctypes.CDLL:
    """Load CoreMediaIO. Raises OSError if the framework is not on this machine."""
    global _library
    if _library is not None:
        return _library
    last_error: Optional[BaseException] = None
    for path in FRAMEWORK_CANDIDATES:
        try:
            lib = ctypes.CDLL(path)
        except OSError as exc:
            last_error = exc
            continue
        _bind(lib)
        _library = lib
        return lib
    raise OSError("CoreMediaIO.framework not found (%s)" % (last_error,))


def _status_error(what: str, status: int) -> OSError:
    return OSError("%s failed (OSStatus %s)" % (what, status))


def list_device_ids(lib: ctypes.CDLL) -> List[int]:
    address = _address(PROP_DEVICES)
    size = c_uint32(0)
    status = lib.CMIOObjectGetPropertyDataSize(
        c_uint32(SYSTEM_OBJECT),
        byref(address),
        c_uint32(0),
        None,
        byref(size),
    )
    if status != 0:
        raise _status_error("CoreMediaIO device list size", status)
    if size.value == 0:
        return []
    if size.value % ctypes.sizeof(c_uint32) != 0:
        raise OSError("CoreMediaIO device list size %s is not a multiple of 4" % (size.value,))
    count = size.value // ctypes.sizeof(c_uint32)
    devices = (c_uint32 * count)()
    used = c_uint32(0)
    status = lib.CMIOObjectGetPropertyData(
        c_uint32(SYSTEM_OBJECT),
        byref(address),
        c_uint32(0),
        None,
        c_uint32(size.value),
        byref(used),
        ctypes.cast(devices, c_void_p),
    )
    if status != 0:
        raise _status_error("CoreMediaIO device list", status)
    reported = used.value // ctypes.sizeof(c_uint32)
    return [int(devices[index]) for index in range(min(count, reported))]


def device_is_running(lib: ctypes.CDLL, device_id: int) -> bool:
    address = _address(PROP_RUNNING)
    value = c_uint32(0)
    used = c_uint32(0)
    status = lib.CMIOObjectGetPropertyData(
        c_uint32(device_id),
        byref(address),
        c_uint32(0),
        None,
        c_uint32(ctypes.sizeof(value)),
        byref(used),
        ctypes.cast(byref(value), c_void_p),
    )
    if status != 0:
        raise _status_error(
            "kCMIODevicePropertyDeviceIsRunningSomewhere for device %s" % (device_id,),
            status,
        )
    if used.value != ctypes.sizeof(value):
        raise OSError(
            "device %s running flag was %s bytes" % (device_id, used.value)
        )
    return value.value != 0


def take_camera_snapshot() -> CameraSnapshot:
    """Query CoreMediaIO. On any failure the snapshot says the camera is off."""
    if sys.platform != "darwin":
        return CameraSnapshot(
            False, (), (), "CoreMediaIO is only available on macOS"
        )
    try:
        lib = load_coremediaio()
    except OSError as exc:
        return CameraSnapshot(False, (), (), str(exc))
    return snapshot_from_queries(
        lambda: list_device_ids(lib),
        lambda device_id: device_is_running(lib, device_id),
    )


_error_logged = False


def is_camera_in_use_macos() -> bool:
    """Return whether any video device is running. Errors log once and mean off."""
    global _error_logged
    snapshot = take_camera_snapshot()
    if snapshot.error:
        if not _error_logged:
            log.error("CoreMediaIO camera check failed: %s", snapshot.error)
            _error_logged = True
    else:
        _error_logged = False
    return snapshot.in_use
