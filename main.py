#!/usr/bin/env python3
"""
Elgato Key Light meeting controller.
Monitors camera usage and toggles the Key Light when a video call starts or ends.
Supports Windows and macOS.
"""

import argparse
import logging
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import requests
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

LOG_FILE = Path(__file__).parent / "elgato-light.log"
POLL_INTERVAL = 2  # seconds between camera checks
ELGATO_SERVICE_TYPE = "_elg._tcp.local."
ELGATO_PORT_DEFAULT = 9123

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Camera detection
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


class _MacCameraMonitor:
    """Track macOS camera usage via the Control Center privacy indicator.

    macOS publishes the exact signal that drives the green camera dot to the
    unified log: ControlCenter logs "Active activity attributions changed to
    [...]" whenever a sensor (camera/mic) turns on or off. A camera entry looks
    like `cam:<bundle-id>`; mic entries are `mic:<bundle-id>`. The list is empty
    (`[]`) when nothing is active.

    This is authoritative — unlike the old VDCAssistant-process check, which
    stays alive for minutes after the camera is released and produced false
    positives (light stuck on). We seed the current state once with `log show`,
    then keep it current by tailing `log stream` on a daemon thread.
    """

    LOG = "/usr/bin/log"
    PREDICATE = (
        'subsystem == "com.apple.controlcenter" AND '
        'category == "sensor-indicators" AND '
        'eventMessage BEGINSWITH "Active activity attributions changed to "'
    )
    _MARKER = "changed to "

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_use = self._seed_state()
        t = threading.Thread(target=self._stream_forever, daemon=True)
        t.start()

    @classmethod
    def _line_has_camera(cls, line: str) -> Optional[bool]:
        """True/False if `line` is an attribution-change event, else None."""
        idx = line.find(cls._MARKER)
        if idx == -1:
            return None
        return "cam:" in line[idx + len(cls._MARKER):]

    def _seed_state(self) -> bool:
        """Determine the current state from the most recent recent log event."""
        try:
            out = subprocess.run(
                [self.LOG, "show", "--last", "30m", "--style", "compact",
                 "--predicate", self.PREDICATE],
                capture_output=True, text=True, timeout=20,
            ).stdout
        except Exception as exc:
            log.warning(f"Camera state seed failed ({exc}); assuming off")
            return False
        state = False  # no event in the window => idle
        for line in out.splitlines():
            parsed = self._line_has_camera(line)
            if parsed is not None:
                state = parsed  # last transition wins
        return state

    def _stream_forever(self) -> None:
        """Tail the log, updating state on each transition. Reconnects if it dies."""
        while True:
            try:
                proc = subprocess.Popen(
                    [self.LOG, "stream", "--style", "compact",
                     "--predicate", self.PREDICATE],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, bufsize=1,
                )
                for line in proc.stdout:
                    parsed = self._line_has_camera(line)
                    if parsed is not None:
                        with self._lock:
                            self._in_use = parsed
            except Exception as exc:
                log.warning(f"Camera log stream dropped ({exc}); reconnecting")
            # Stream exited: re-seed (we may have missed a transition) and retry.
            with self._lock:
                self._in_use = self._seed_state()
            time.sleep(2)

    def is_in_use(self) -> bool:
        with self._lock:
            return self._in_use


_mac_monitor: Optional[_MacCameraMonitor] = None


def is_camera_in_use() -> bool:
    if sys.platform == "win32":
        return _is_camera_in_use_windows()
    elif sys.platform == "darwin":
        global _mac_monitor
        if _mac_monitor is None:
            _mac_monitor = _MacCameraMonitor()
        return _mac_monitor.is_in_use()
    else:
        raise NotImplementedError(f"Unsupported platform: {sys.platform}")


# ---------------------------------------------------------------------------
# Elgato Key Light
# ---------------------------------------------------------------------------

class KeyLightListener(ServiceListener):
    """mDNS listener that tracks the Key Light's address."""

    def __init__(self):
        self.ip: Optional[str] = None
        self.port: int = ELGATO_PORT_DEFAULT

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info and info.addresses:
            addr = info.addresses[0]
            self.ip = socket.inet_ntoa(addr) if len(addr) == 4 else socket.inet_ntop(socket.AF_INET6, addr)
            self.port = info.port or ELGATO_PORT_DEFAULT
            log.info(f"Key Light discovered: {self.ip}:{self.port}  ({name})")

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        log.warning(f"Key Light went offline: {name}")
        self.ip = None

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.add_service(zc, type_, name)


def set_light(ip: str, port: int, on: bool) -> bool:
    """Toggle the Key Light without changing brightness or temperature. Returns True on success."""
    try:
        r = requests.put(
            f"http://{ip}:{port}/elgato/lights",
            json={"numberOfLights": 1, "lights": [{"on": 1 if on else 0}]},
            timeout=3,
        )
        r.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.error(f"Key Light unreachable at {ip}:{port}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_test(listener: KeyLightListener) -> None:
    log.info("--- Test mode ---")
    log.info("Waiting up to 15s for Key Light...")
    deadline = time.time() + 15
    while time.time() < deadline and listener.ip is None:
        time.sleep(0.5)

    if listener.ip is None:
        log.error("FAIL: Key Light not found. Make sure Elgato Control Center is running.")
        return

    log.info(f"OK: Key Light at {listener.ip}:{listener.port}")
    log.info("Turning light ON...")
    set_light(listener.ip, listener.port, on=True)
    time.sleep(2)
    log.info("Turning light OFF...")
    set_light(listener.ip, listener.port, on=False)
    log.info(f"Camera currently in use: {is_camera_in_use()}")
    log.info("--- Test complete ---")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Toggle Elgato Key Light when your camera activates."
    )
    parser.add_argument("--test", action="store_true", help="Discover light, toggle it, and exit")
    args = parser.parse_args()

    log.info("Elgato meeting light starting")

    zc = Zeroconf()
    listener = KeyLightListener()
    ServiceBrowser(zc, ELGATO_SERVICE_TYPE, listener)

    if args.test:
        try:
            run_test(listener)
        finally:
            zc.close()
        return

    log.info("Discovering Key Light (up to 15s)...")
    deadline = time.time() + 15
    while time.time() < deadline and listener.ip is None:
        time.sleep(0.5)

    if listener.ip is None:
        log.warning("Key Light not found at startup — will keep watching for it")

    actual_on: Optional[bool] = None

    try:
        while True:
            if listener.ip is None:
                time.sleep(5)
                continue

            desired_on = is_camera_in_use()

            if desired_on != actual_on:
                if set_light(listener.ip, listener.port, on=desired_on):
                    log.info(f"Camera {'activated' if desired_on else 'deactivated'} — light {'ON' if desired_on else 'OFF'}")
                    actual_on = desired_on
                else:
                    actual_on = None  # retry next cycle

            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("Shutting down")
        if listener.ip and actual_on:
            set_light(listener.ip, listener.port, on=False)
    finally:
        zc.close()


if __name__ == "__main__":
    main()
