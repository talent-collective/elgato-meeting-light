#!/usr/bin/env python3
"""
Elgato Key Light meeting controller.

Turns the Key Light on when the camera is actually in use and off when it is
not. On macOS the camera check is CoreMediaIO's
kCMIODevicePropertyDeviceIsRunningSomewhere, not pgrep of VDCAssistant or
AppleCameraAssistant — those processes stay running while the camera is off.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time
import warnings
from pathlib import Path
from typing import List, Optional

# CLT Python on macOS links against LibreSSL. urllib3 2 warns about that on
# import; requirements.txt pins urllib3<2, and this hides a leftover warning.
warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL")

from camera_macos import (
    format_probe_line,
    is_camera_in_use_macos,
    take_camera_snapshot,
    test_light_sequence,
)

POLL_INTERVAL = 2  # seconds between camera checks
ELGATO_SERVICE_TYPE = "_elg._tcp.local."
ELGATO_PORT_DEFAULT = 9123
DISCOVERY_WAIT = 15  # seconds
PROBE_SECONDS = 20

log = logging.getLogger("elgato-meeting-light")

_network_hint_logged = False


def default_log_path() -> Path:
    """Application log. Launchd stdout/stderr go to sibling launchd logs."""
    return Path(__file__).resolve().parent / "elgato-light.log"


def build_log_handlers(log_path: Path, stdout_is_tty: bool) -> List[logging.Handler]:
    """File log always. Stdout only when the process is attached to a terminal.

    launchd redirects stdout and stderr into files. A StreamHandler in that
    case writes every line a second time into the launchd log, and if that
    path is the same file as the FileHandler the lines are duplicated.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: List[logging.Handler] = [
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    if stdout_is_tty:
        handlers.append(logging.StreamHandler(sys.stdout))
    return handlers


def configure_logging() -> Path:
    path = default_log_path()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=build_log_handlers(path, sys.stdout.isatty()),
        force=True,
    )
    return path


def is_camera_in_use() -> bool:
    if sys.platform == "win32":
        return _is_camera_in_use_windows()
    if sys.platform == "darwin":
        return is_camera_in_use_macos()
    log.error(
        "Camera detection is not implemented on %s; treating the camera as off",
        sys.platform,
    )
    return False


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
except ImportError:
    _ServiceListener = object  # type: ignore[misc,assignment]


class KeyLightListener(_ServiceListener):
    """mDNS listener that tracks the Key Light's address."""

    def __init__(self) -> None:
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
            "http://%s:%s/elgato/lights" % (ip, port),
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
                "Camera access is not used.",
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


def run_probe(seconds: int = PROBE_SECONDS, pause=time.sleep) -> None:
    """Print the live CoreMediaIO camera state once a second."""
    log.info(
        "Probing CoreMediaIO kCMIODevicePropertyDeviceIsRunningSomewhere "
        "for %s seconds",
        seconds,
    )
    for index in range(seconds):
        log.info(format_probe_line(take_camera_snapshot()))
        if index + 1 < seconds:
            pause(1)


def run_test(listener: KeyLightListener) -> None:
    """Blink the light on then off, then leave it matched to the camera.

    The blink is only a connectivity check. The last command is the real
    camera state, so a camera that is off cannot be left with the light on.
    """
    _wait_for_light(listener)
    if listener.ip is None:
        log.error("Key Light not found. Make sure Elgato Control Center is running.")
        print("Key Light not found. Not changing the light.", flush=True)
        return

    log.info("Key Light at %s:%s", listener.ip, listener.port)
    try:
        log.info("Connectivity check: turning light ON")
        set_light(listener.ip, listener.port, on=True)
        time.sleep(2)
        log.info("Connectivity check: turning light OFF")
        set_light(listener.ip, listener.port, on=False)
    finally:
        # Always run, including after an interrupt mid-blink.
        camera_on = is_camera_in_use()
        final_on = test_light_sequence(camera_on)[-1]
        log.info("Camera currently in use: %s", camera_on)
        if listener.ip and set_light(listener.ip, listener.port, on=final_on):
            message = "Light %s to match the camera" % ("ON" if final_on else "OFF")
            log.info(message)
            print(message, flush=True)
        else:
            log.error(
                "Could not set the light to the camera state (%s)",
                "ON" if final_on else "OFF",
            )


def run_forever(listener: KeyLightListener, dry_run: bool) -> None:
    actual_on: Optional[bool] = None
    reported: Optional[bool] = None
    try:
        while True:
            desired_on = is_camera_in_use()
            if desired_on != reported:
                if reported is None:
                    log.info("Initial camera state: %s", "on" if desired_on else "off")
                else:
                    log.info(
                        "Camera state changed: %s -> %s",
                        "on" if reported else "off",
                        "on" if desired_on else "off",
                    )
                reported = desired_on

            if listener.ip is None:
                time.sleep(5)
                continue

            if desired_on != actual_on:
                light_word = "ON" if desired_on else "OFF"
                if dry_run:
                    log.info("Dry run: would turn light %s", light_word)
                    actual_on = desired_on
                elif set_light(listener.ip, listener.port, on=desired_on):
                    log.info("Light %s", light_word)
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
        help="Find the light, blink it on then off, then leave it matched to the camera.",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Print the live CoreMediaIO camera state once a second for 20 seconds.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log camera state and the light action without sending commands to the light.",
    )
    parser.add_argument(
        "--brightness",
        type=int,
        default=None,
        help="Ignored. Brightness and color temperature are never changed.",
    )
    args = parser.parse_args()

    log_path = configure_logging()
    if args.probe:
        run_probe()
        return

    log.info("Elgato meeting light starting (pid %s)", os.getpid())
    log.info("Log file: %s", log_path)
    if sys.platform == "darwin":
        log.info(
            "Camera detection: CoreMediaIO kCMIODevicePropertyDeviceIsRunningSomewhere"
        )
    elif sys.platform == "win32":
        log.info("Camera detection: Windows webcam privacy registry")
    else:
        raise SystemExit(
            "Live camera detection supports macOS and Windows. "
            "On a Mac, check it with: python3 main.py --probe"
        )

    try:
        zc, listener = _start_discovery()
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency (%s). Re-run setup.sh so the virtualenv is installed."
            % (exc,)
        ) from exc

    try:
        if args.test:
            run_test(listener)
            return
        if args.dry_run:
            log.info("Dry run: the light will not be changed")
            run_forever(listener, dry_run=True)
            return
        run_forever(listener, dry_run=False)
    finally:
        zc.close()


if __name__ == "__main__":
    main()
