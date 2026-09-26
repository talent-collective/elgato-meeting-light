"""CoreMediaIO camera detection, with the framework call mocked."""

from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import camera_macos  # noqa: E402
from camera_macos import (  # noqa: E402
    PROP_DEVICES,
    PROP_RUNNING,
    SCOPE_GLOBAL,
    CameraSnapshot,
    format_probe_line,
    fourcc,
    is_camera_in_use_macos,
    snapshot_from_queries,
    test_light_sequence,
)


class FourCCTests(unittest.TestCase):
    def test_property_codes_match_the_headers(self):
        self.assertEqual(fourcc("gone"), 0x676F6E65)
        self.assertEqual(PROP_RUNNING, fourcc("gone"))
        self.assertEqual(fourcc("dev#"), 0x64657623)
        self.assertEqual(PROP_DEVICES, fourcc("dev#"))
        self.assertEqual(fourcc("glob"), 0x676C6F62)
        self.assertEqual(SCOPE_GLOBAL, fourcc("glob"))


class SnapshotTests(unittest.TestCase):
    def test_no_devices_is_off(self):
        snap = snapshot_from_queries(lambda: [], lambda device_id: True)
        self.assertFalse(snap.in_use)
        self.assertEqual(snap.device_ids, ())
        self.assertEqual(snap.error, "")

    def test_all_idle_is_off(self):
        snap = snapshot_from_queries(lambda: [7, 8], lambda device_id: False)
        self.assertFalse(snap.in_use)
        self.assertEqual(snap.running_ids, ())

    def test_any_running_device_is_on(self):
        snap = snapshot_from_queries(
            lambda: [7, 8, 9],
            lambda device_id: device_id == 8,
        )
        self.assertTrue(snap.in_use)
        self.assertEqual(snap.running_ids, (8,))

    def test_device_list_failure_is_off_and_recorded(self):
        def boom():
            raise OSError("OSStatus -50")

        snap = snapshot_from_queries(boom, lambda device_id: True)
        self.assertFalse(snap.in_use)
        self.assertIn("device list failed", snap.error)
        self.assertIn("-50", snap.error)

    def test_one_device_failure_does_not_force_on(self):
        def read(device_id):
            if device_id == 1:
                raise OSError("OSStatus 560947818")
            return False

        snap = snapshot_from_queries(lambda: [1, 2], read)
        self.assertFalse(snap.in_use)
        self.assertIn("device 1", snap.error)

    def test_a_running_device_still_counts_if_another_query_fails(self):
        def read(device_id):
            if device_id == 1:
                raise OSError("bad device")
            return True

        snap = snapshot_from_queries(lambda: [1, 2], read)
        self.assertTrue(snap.in_use)
        self.assertEqual(snap.running_ids, (2,))
        self.assertIn("device 1", snap.error)

    def test_macos_wrapper_logs_and_returns_false_when_the_reader_fails(self):
        def broken_snapshot():
            return CameraSnapshot(False, (), (), "CoreMediaIO.framework not found")

        original = camera_macos.take_camera_snapshot
        camera_macos._error_logged = False
        camera_macos.take_camera_snapshot = broken_snapshot
        try:
            with self.assertLogs("elgato-meeting-light", level="ERROR") as captured:
                first = is_camera_in_use_macos()
                second = is_camera_in_use_macos()
        finally:
            camera_macos.take_camera_snapshot = original
            camera_macos._error_logged = False
        self.assertFalse(first)
        self.assertFalse(second)
        errors = [line for line in captured.output if "CoreMediaIO" in line]
        self.assertEqual(len(errors), 1)

    def test_probe_line_for_idle_busy_and_failure(self):
        idle = format_probe_line(CameraSnapshot(False, (4, 5), ()))
        busy = format_probe_line(CameraSnapshot(True, (4, 5), (5,)))
        failed = format_probe_line(
            CameraSnapshot(False, (), (), "device list failed: OSStatus -50")
        )
        self.assertEqual(idle, "camera_in_use=false devices=2 running=none")
        self.assertEqual(busy, "camera_in_use=true devices=2 running=5")
        self.assertTrue(failed.startswith("camera_in_use=false error="))


class LightSequenceTests(unittest.TestCase):
    def test_connectivity_check_ends_off_when_the_camera_is_off(self):
        self.assertEqual(test_light_sequence(False), (True, False, False))

    def test_connectivity_check_ends_on_only_when_the_camera_is_on(self):
        self.assertEqual(test_light_sequence(True), (True, False, True))

    def test_a_failed_camera_read_is_passed_in_as_off(self):
        # Callers treat a CoreMediaIO failure as False before building the sequence.
        self.assertFalse(test_light_sequence(False)[-1])


class ProbeTests(unittest.TestCase):
    def test_probe_reads_once_per_second(self):
        import main

        calls = []

        def fake():
            calls.append(1)
            return CameraSnapshot(False, (4,), ())

        original = main.take_camera_snapshot
        main.take_camera_snapshot = fake
        try:
            with self.assertLogs("elgato-meeting-light", level="INFO") as captured:
                main.run_probe(seconds=3, pause=lambda _seconds: None)
        finally:
            main.take_camera_snapshot = original
        self.assertEqual(len(calls), 3)
        states = [line for line in captured.output if "camera_in_use=false" in line]
        self.assertEqual(len(states), 3)


class LogHandlerTests(unittest.TestCase):
    def test_launchd_does_not_get_a_stream_handler(self):
        from main import build_log_handlers

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "elgato-light.log"
            quiet = build_log_handlers(path, stdout_is_tty=False)
            noisy = build_log_handlers(path, stdout_is_tty=True)
            for handler in quiet + noisy:
                handler.close()
        self.assertEqual(len(quiet), 1)
        self.assertIsInstance(quiet[0], logging.FileHandler)
        # FileHandler subclasses StreamHandler. The second handler is the one
        # that writes to the terminal. Launchd (not a TTY) must not get it,
        # or those lines land in the same file as the FileHandler.
        self.assertEqual(len(noisy), 2)
        self.assertIs(type(noisy[1]), logging.StreamHandler)


if __name__ == "__main__":
    unittest.main()
