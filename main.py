#!/usr/bin/env python3
"""
Elgato Key Light meeting controller.

Turns the Key Light on when the camera is actually in use and off when it is
not. On macOS that decision comes from the unified log (the same signals as
the green camera dot), not from whether VDCAssistant or AppleCameraAssistant
is running — those processes stay alive while the camera is off.
"""

import argparse
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from camera_state import (
    LOG_BIN,
    LOG_PREDICATE,
    CameraStateMachine,
    format_state,
    format_transition,
    light_should_be_on,
    parse_camera_log_line,
    replay_log_lines,
)

POLL_INTERVAL = 2  # seconds between camera checks
ELGATO_SERVICE_TYPE = "_elg._tcp.local."
ELGATO_PORT_DEFAULT = 9123
DISCOVERY_WAIT = 15  # seconds

log = logging.getLogger("elgato-meeting-light")

_network_hint_logged = False
_log_access_hint_logged = False


def default_log_path() -> Path:
    """App log. Launchd stdout/stderr go to sibling *.launchd.*.log files."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "elgato-meeting-light.log"
    return Path(__file__).resolve().parent / "elgato-light.log"


def emit(message: str) -> None:
    """Log ``message``. Also print it when stdout is not a terminal.

    A terminal already receives logger output. Launchd and ``curl | bash``
    do not, and those callers need the sync result on stdout.
    """
    log.info(message)
    if not sys.stdout.isatty():
        print(message, flush=True)


def configure_logging() -> Path:
    path = default_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(path, encoding="utf-8"),
    ]
    # Under launchd, stdout is the launchd log file. Logging there as well
    # would duplicate every line. A terminal still gets a copy.
    if sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )
    return path


def _log_access_hint() -> None:
    global _log_access_hint_logged
    if _log_access_hint_logged:
        return
    _log_access_hint_logged = True
    log.warning(
        "Cannot read the macOS unified log. Grant Full Disk Access to this "
        "Python (%s) in System Settings → Privacy & Security → Full Disk "
        "Access, then run the install command again. Do not grant Camera "
        "access — this program never opens the camera, and a camera grant "
        "is not required.",
        sys.executable,
    )


def _note_log_stderr(text: str) -> None:
    cleaned = (text or "").strip()
    if not cleaned:
        return
    lowered = cleaned.lower()
    if (
        "not permitted" in lowered
        or "permission denied" in lowered
        or "full disk" in lowered
    ):
        log.warning("System log error: %s", cleaned.splitlines()[-1])
        _log_access_hint()
        return
    log.warning("System log error: %s", cleaned.splitlines()[-1])


def seed_from_system_log() -> CameraStateMachine:
    """Replay recent camera log lines. No events means the camera is off."""
    command = [
        LOG_BIN,
        "show",
        "--last",
        "24h",
        "--style",
        "compact",
        "--info",
        "--predicate",
        LOG_PREDICATE,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=45,
        )
    except FileNotFoundError:
        log.warning("%s is missing; assuming the camera is off", LOG_BIN)
        return CameraStateMachine()
    except subprocess.TimeoutExpired:
        log.warning("Reading recent camera logs timed out; assuming the camera is off")
        return CameraStateMachine()
    except OSError as exc:
        log.warning("Could not read camera logs (%s); assuming the camera is off", exc)
        return CameraStateMachine()

    if completed.returncode != 0:
        _note_log_stderr(completed.stderr)
        if not completed.stdout:
            return CameraStateMachine()
    elif completed.stderr:
        _note_log_stderr(completed.stderr)

    machine, _transitions = replay_log_lines(completed.stdout.splitlines())
    return machine


class MacCameraWatch:
    """Seed camera state from `log show`, then follow `log stream`."""

    def __init__(self, follow: bool = True) -> None:
        self._lock = threading.Lock()
        self._machine = seed_from_system_log()
        log.info(format_state(self._machine))
        if not follow:
            return
        log.info(
            "Watching camera events with %s (Control Center frame publisher, "
            "sensor attributions, and legacy stream/power messages). "
            "VDCAssistant / AppleCameraAssistant process checks are not used.",
            LOG_BIN,
        )
        self._thread = threading.Thread(
            target=self._stream_forever, name="camera-log", daemon=True
        )
        self._thread.start()

    def light_on(self) -> bool:
        with self._lock:
            return light_should_be_on(self._machine)

    def describe(self) -> str:
        with self._lock:
            return format_state(self._machine)

    def _apply_line(self, line: str) -> None:
        event = parse_camera_log_line(line)
        if event is None:
            if "<private>" in line and "changed to" in line:
                log.warning(
                    "Camera log line was redacted (<private>); state left unchanged"
                )
                _log_access_hint()
            return
        with self._lock:
            transition = self._machine.apply(event)
        if transition is not None:
            log.info(format_transition(transition))

    def _stream_forever(self) -> None:
        while True:
            try:
                proc = subprocess.Popen(
                    [
                        LOG_BIN,
                        "stream",
                        "--style",
                        "compact",
                        "--info",
                        "--predicate",
                        LOG_PREDICATE,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
            except (OSError, FileNotFoundError) as exc:
                log.warning("Could not start camera log stream (%s)", exc)
                time.sleep(5)
                continue

            stderr_thread = threading.Thread(
                target=self._drain_stderr, args=(proc,), daemon=True
            )
            stderr_thread.start()
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    self._apply_line(line)
            except Exception as exc:
                log.warning("Camera log stream dropped (%s)", exc)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()

            log.warning("Camera log stream stopped; reconnecting in 2s")
            time.sleep(2)
            self._reseed()

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        if proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                _note_log_stderr(line)
        except Exception:
            return

    def _reseed(self) -> None:
        fresh = seed_from_system_log()
        with self._lock:
            previous = light_should_be_on(self._machine)
            self._machine = fresh
            current = light_should_be_on(self._machine)
            detail = format_state(self._machine)
        if previous != current:
            prev_word = "on" if previous else "off"
            cur_word = "on" if current else "off"
            log.info(
                "Camera state changed: %s -> %s (re-seeded after the log stream stopped; %s)",
                prev_word,
                cur_word,
                detail,
            )
        else:
            log.info("Camera log re-seeded; %s", detail)


class WindowsCameraWatch:
    def __init__(self) -> None:
        self._last: Optional[bool] = None

    def light_on(self) -> bool:
        return _is_camera_in_use_windows()

    def describe(self) -> str:
        on = self.light_on()
        state = "on" if on else "off"
        return f"Camera state: {state} (Windows webcam privacy registry)"

    def note_poll(self) -> None:
        """Log registry transitions. macOS logs those from the stream thread."""
        current = self.light_on()
        if self._last is None:
            log.info(self.describe())
            self._last = current
            return
        if current != self._last:
            previous = "on" if self._last else "off"
            now = "on" if current else "off"
            log.info(
                "Camera state changed: %s -> %s (Windows webcam privacy registry)",
                previous,
                now,
            )
            self._last = current


def make_camera_watch(follow: bool = True):
    if sys.platform == "darwin":
        return MacCameraWatch(follow=follow)
    if sys.platform == "win32":
        return WindowsCameraWatch()
    raise NotImplementedError(f"Unsupported platform: {sys.platform}")


# ---------------------------------------------------------------------------
# Camera detection — Windows
# ---------------------------------------------------------------------------

def _is_camera_in_use_windows() -> bool:
    """Check Windows registry for an active camera session.

    Windows sets LastUsedTimeStop=0 while a camera session is live — the same
    signal that drives the system privacy indicator dot.
    """
    import winreg

    registry_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"

    def scan_subkeys(parent_key) -> bool:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(parent_key, i)
                i += 1
            except OSError:
                break
            try:
                with winreg.OpenKey(parent_key, name) as sk:
                    start, _ = winreg.QueryValueEx(sk, "LastUsedTimeStart")
                    try:
                        stop, _ = winreg.QueryValueEx(sk, "LastUsedTimeStop")
                    except FileNotFoundError:
                        stop = 0
                    if start > 0 and stop == 0:
                        return True
            except (FileNotFoundError, OSError):
                pass
        return False

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_path) as base:
            if scan_subkeys(base):
                return True
            try:
                with winreg.OpenKey(base, "NonPackaged") as np:
                    return scan_subkeys(np)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass
    return False


# ---------------------------------------------------------------------------
# Elgato Key Light
# ---------------------------------------------------------------------------

def _ip_from_addresses(addresses) -> Optional[str]:
    """Prefer IPv4. A link-local IPv6 address is often unreachable for the PUT."""
    ipv4 = []
    ipv6 = []
    for addr in addresses or []:
        if len(addr) == 4:
            ipv4.append(socket.inet_ntoa(addr))
        elif len(addr) == 16:
            ipv6.append(socket.inet_ntop(socket.AF_INET6, addr))
    if ipv4:
        return ipv4[0]
    if ipv6:
        return ipv6[0]
    return None


try:
    from zeroconf import ServiceListener as _ServiceListener
except ImportError:  # `python main.py --sample-log` has no network dependency
    _ServiceListener = object  # type: ignore[misc,assignment]


class KeyLightListener(_ServiceListener):
    """mDNS listener that tracks the Key Light's address."""

    def __init__(self):
        self.ip: Optional[str] = None
        self.port: int = ELGATO_PORT_DEFAULT

    def add_service(self, zc, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if not info:
            return
        ip = _ip_from_addresses(info.addresses)
        if not ip:
            return
        self.ip = ip
        self.port = info.port or ELGATO_PORT_DEFAULT
        log.info("Key Light discovered: %s:%s  (%s)", self.ip, self.port, name)

    def remove_service(self, zc, type_: str, name: str) -> None:
        log.warning("Key Light went offline: %s", name)
        self.ip = None

    def update_service(self, zc, type_: str, name: str) -> None:
        self.add_service(zc, type_, name)


def set_light(ip: str, port: int, on: bool) -> bool:
    """Toggle the Key Light without changing brightness or temperature."""
    import requests

    global _network_hint_logged
    try:
        response = requests.put(
            f"http://{ip}:{port}/elgato/lights",
            json={"numberOfLights": 1, "lights": [{"on": 1 if on else 0}]},
            timeout=3,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.error("Key Light unreachable at %s:%s: %s", ip, port, exc)
        if not _network_hint_logged:
            _network_hint_logged = True
            log.error(
                "On macOS, allow Local Network access for %s if prompted. "
                "Do not grant Camera access; it is not used.",
                sys.executable,
            )
        return False


def _start_discovery():
    from zeroconf import ServiceBrowser, Zeroconf

    zc = Zeroconf()
    listener = KeyLightListener()
    ServiceBrowser(zc, ELGATO_SERVICE_TYPE, listener)
    return zc, listener


def _wait_for_light(listener: KeyLightListener, seconds: float = DISCOVERY_WAIT) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and listener.ip is None:
        time.sleep(0.5)


def run_sample_log(path: Path) -> None:
    if not path.is_file():
        raise SystemExit(f"Sample log not found: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    machine, transitions = replay_log_lines(lines)
    if not transitions:
        emit(format_state(machine))
    for transition in transitions:
        emit(format_transition(transition))
    emit(format_state(machine))
    emit(
        "light_should_be_on="
        + ("true" if light_should_be_on(machine) else "false")
    )


def run_sync(listener: KeyLightListener, watch, dry_run: bool) -> None:
    """Set the light once to the camera's current state. Never blinks it on."""
    _wait_for_light(listener)
    description = watch.describe()
    camera_on = watch.light_on()
    light_word = "ON" if camera_on else "OFF"
    # MacCameraWatch already logged the seeded state. Repeat it on stdout
    # for the installer, and log it here for Windows (no seed log).
    if not isinstance(watch, MacCameraWatch):
        log.info(description)
    if not sys.stdout.isatty():
        print(description, flush=True)
    elif not isinstance(watch, MacCameraWatch):
        print(description, flush=True)
    if listener.ip is None:
        message = "Key Light not found. Not changing the light."
        log.warning(message)
        print(message, flush=True)
        return
    if dry_run:
        message = (
            f"Dry run: would turn light {light_word} at {listener.ip}:{listener.port}"
        )
        log.info(message)
        print(message, flush=True)
        return
    if set_light(listener.ip, listener.port, on=camera_on):
        message = f"Light {light_word} to match the camera"
        log.info(message)
        print(message, flush=True)
    else:
        print(f"Could not set the light {light_word}. See the log.", flush=True)


def run_forever(listener: KeyLightListener, watch, dry_run: bool) -> None:
    actual_on: Optional[bool] = None
    waiting_reported: Optional[bool] = None
    try:
        while True:
            if isinstance(watch, WindowsCameraWatch):
                watch.note_poll()
            desired_on = watch.light_on()
            if listener.ip is None:
                if desired_on != waiting_reported:
                    log.info(
                        "Camera %s — waiting for the Key Light before applying it",
                        "on" if desired_on else "off",
                    )
                    waiting_reported = desired_on
                time.sleep(5)
                continue

            if desired_on != actual_on:
                light_word = "ON" if desired_on else "OFF"
                if dry_run:
                    log.info("Dry run: would turn light %s (%s)", light_word, watch.describe())
                    actual_on = desired_on
                elif set_light(listener.ip, listener.port, on=desired_on):
                    log.info("Light %s (%s)", light_word, watch.describe())
                    actual_on = desired_on
                else:
                    actual_on = None  # retry next cycle
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("Shutting down")
        if listener.ip and actual_on and not dry_run:
            set_light(listener.ip, listener.port, on=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Turn an Elgato Key Light on only while the camera is in use."
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Discover the light, set it to match the camera, and exit. Does not blink the light.",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Same as --test: one-shot sync to the real camera state.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log camera state and the light action, without sending commands to the light.",
    )
    parser.add_argument(
        "--sample-log",
        type=Path,
        help="Replay a camera log file and print state transitions, then exit. Does not touch the light.",
    )
    parser.add_argument(
        "--brightness",
        type=int,
        default=None,
        help="Ignored. Brightness and color temperature are never changed.",
    )
    args = parser.parse_args()

    log_path = configure_logging()
    if args.sample_log:
        log.info("Replaying camera log %s", args.sample_log)
        run_sample_log(args.sample_log)
        return

    log.info("Elgato meeting light starting (pid %s)", os.getpid())
    log.info("Log file: %s", log_path)
    if sys.platform not in ("darwin", "win32"):
        raise SystemExit(
            "Live camera detection supports macOS and Windows. "
            "Replay a capture with --sample-log tests/fixtures/macos-camera-sample.txt"
        )

    try:
        zc, listener = _start_discovery()
    except ImportError as exc:
        raise SystemExit(
            f"Missing dependency ({exc}). Re-run setup.sh so the virtualenv is installed."
        ) from exc

    try:
        # One-shot sync does not need a live `log stream`; `log show` is enough
        # and avoids two stream processes during install.
        watch = make_camera_watch(follow=not (args.test or args.sync))
        if args.test or args.sync:
            run_sync(listener, watch, dry_run=args.dry_run)
            return
        if args.dry_run:
            log.info(
                "Dry run: camera transitions will be logged and the light will not be changed"
            )
            run_forever(listener, watch, dry_run=True)
            return
        run_forever(listener, watch, dry_run=False)
    finally:
        zc.close()


if __name__ == "__main__":
    main()
