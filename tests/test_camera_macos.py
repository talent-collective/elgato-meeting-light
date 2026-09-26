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
    light_sequence,
    snapshot_from_queries,
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

    def test_attribute_error_while_loading_is_off_logged_once_and_does_not_raise(self):
        def boom():
            raise AttributeError("CMIOObjectGetPropertyDataSize")

        original_load = camera_macos.load_coremediaio
        original_platform = camera_macos.sys.platform
        camera_macos._error_logged = False
        camera_macos.load_coremediaio = boom
        camera_macos.sys.platform = "darwin"
        try:
            with self.assertLogs("elgato-meeting-light", level="ERROR") as captured:
                first = is_camera_in_use_macos()
                second = is_camera_in_use_macos()
        finally:
            camera_macos.load_coremediaio = original_load
            camera_macos.sys.platform = original_platform
            camera_macos._error_logged = False
        self.assertFalse(first)
        self.assertFalse(second)
        self.assertEqual(len(captured.output), 1)
        self.assertIn("AttributeError", captured.output[0])

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
        self.assertEqual(light_sequence(False), (True, False, False))

    def test_connectivity_check_ends_on_only_when_the_camera_is_on(self):
        self.assertEqual(light_sequence(True), (True, False, True))

    def test_a_failed_camera_read_is_passed_in_as_off(self):
        # Callers treat a CoreMediaIO failure as False before building the sequence.
        self.assertFalse(light_sequence(False)[-1])


class ProbeTests(unittest.TestCase):
    def test_probe_reads_once_per_second_on_stdout_and_does_not_log(self):
        import io

        import main

        calls = []

        def fake():
            calls.append(1)
            return CameraSnapshot(False, (4,), ())

        original = main.take_camera_snapshot
        original_stdout = sys.stdout
        buffer = io.StringIO()
        main.take_camera_snapshot = fake
        sys.stdout = buffer
        try:
            with self.assertNoLogs("elgato-meeting-light", level="INFO"):
                main.run_probe(seconds=3, pause=lambda _seconds: None)
        finally:
            main.take_camera_snapshot = original
            sys.stdout = original_stdout
        self.assertEqual(len(calls), 3)
        output = buffer.getvalue()
        self.assertIn("Probing CoreMediaIO", output)
        self.assertEqual(output.count("camera_in_use=false devices=1 running=none"), 3)

    def test_probe_flag_does_not_open_the_application_log(self):
        import main

        ran = []
        original_probe = main.run_probe
        original_configure = main.configure_logging
        original_argv = sys.argv

        def fake_probe():
            ran.append(True)

        def configure_should_not_run():
            raise AssertionError("configure_logging should not run for --probe")

        main.run_probe = fake_probe
        main.configure_logging = configure_should_not_run
        sys.argv = ["main.py", "--probe"]
        try:
            main.main()
        finally:
            sys.argv = original_argv
            main.run_probe = original_probe
            main.configure_logging = original_configure
        self.assertEqual(ran, [True])


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


class PythonwStdoutTests(unittest.TestCase):
    def _restore_root_logging(self, handlers, level):
        root = logging.getLogger()
        for handler in root.handlers[:]:
            handler.close()
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)

    def test_configure_logging_when_stdout_is_none(self):
        import main

        root = logging.getLogger()
        saved_handlers = root.handlers[:]
        saved_level = root.level
        for handler in saved_handlers:
            root.removeHandler(handler)
        original_stdout = sys.stdout
        original_log_path = main.default_log_path
        sys.stdout = None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                log_path = Path(tmp) / "elgato-light.log"
                main.default_log_path = lambda: log_path
                path = main.configure_logging()
                self.assertEqual(path, log_path)
                self.assertTrue(log_path.is_file())
                stream_handlers = [
                    handler
                    for handler in root.handlers
                    if type(handler) is logging.StreamHandler
                ]
                self.assertEqual(stream_handlers, [])
                self.assertTrue(
                    any(isinstance(handler, logging.FileHandler) for handler in root.handlers)
                )
        finally:
            sys.stdout = original_stdout
            main.default_log_path = original_log_path
            self._restore_root_logging(saved_handlers, saved_level)

    def test_run_test_when_stdout_is_none(self):
        import main

        listener = type("Listener", (), {"ip": None, "port": 9123})()
        original_stdout = sys.stdout
        original_wait = main._wait_for_light
        original_camera = main.is_camera_in_use
        original_set = main.set_light
        original_sleep = main.time.sleep
        sys.stdout = None
        main._wait_for_light = lambda *args, **kwargs: None
        main.is_camera_in_use = lambda: False
        main.set_light = lambda *args, **kwargs: True
        main.time.sleep = lambda *args, **kwargs: None
        try:
            with self.assertLogs("elgato-meeting-light", level="INFO"):
                main.run_test(listener)
                listener.ip = "192.0.2.5"
                main.run_test(listener)
        finally:
            sys.stdout = original_stdout
            main._wait_for_light = original_wait
            main.is_camera_in_use = original_camera
            main.set_light = original_set
            main.time.sleep = original_sleep

    def test_camera_match_line_is_printed_once_on_a_tty(self):
        import io

        import main

        class Tty(io.StringIO):
            def isatty(self):
                return True

        tty = Tty()
        listener = type("Listener", (), {"ip": "192.0.2.5", "port": 9123})()
        root = logging.getLogger()
        saved_handlers = root.handlers[:]
        saved_level = root.level
        for handler in saved_handlers:
            root.removeHandler(handler)
        original_stdout = sys.stdout
        original_log_path = main.default_log_path
        original_camera = main.is_camera_in_use
        original_set = main.set_light
        original_sleep = main.time.sleep
        original_wait = main._wait_for_light
        sys.stdout = tty
        main.is_camera_in_use = lambda: False
        main.set_light = lambda *args, **kwargs: True
        main.time.sleep = lambda *args, **kwargs: None
        main._wait_for_light = lambda *args, **kwargs: None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                main.default_log_path = lambda: Path(tmp) / "elgato-light.log"
                main.configure_logging()
                main.run_test(listener)
        finally:
            sys.stdout = original_stdout
            main.default_log_path = original_log_path
            main.is_camera_in_use = original_camera
            main.set_light = original_set
            main.time.sleep = original_sleep
            main._wait_for_light = original_wait
            self._restore_root_logging(saved_handlers, saved_level)
        self.assertEqual(tty.getvalue().count("Light OFF to match the camera"), 1)

    def test_camera_match_line_still_prints_when_stdout_is_not_a_tty(self):
        import io

        import main

        buffer = io.StringIO()
        listener = type("Listener", (), {"ip": "192.0.2.5", "port": 9123})()
        root = logging.getLogger()
        saved_handlers = root.handlers[:]
        saved_level = root.level
        for handler in saved_handlers:
            root.removeHandler(handler)
        original_stdout = sys.stdout
        original_log_path = main.default_log_path
        original_camera = main.is_camera_in_use
        original_set = main.set_light
        original_sleep = main.time.sleep
        original_wait = main._wait_for_light
        sys.stdout = buffer
        main.is_camera_in_use = lambda: False
        main.set_light = lambda *args, **kwargs: True
        main.time.sleep = lambda *args, **kwargs: None
        main._wait_for_light = lambda *args, **kwargs: None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                main.default_log_path = lambda: Path(tmp) / "elgato-light.log"
                main.configure_logging()
                main.run_test(listener)
        finally:
            sys.stdout = original_stdout
            main.default_log_path = original_log_path
            main.is_camera_in_use = original_camera
            main.set_light = original_set
            main.time.sleep = original_sleep
            main._wait_for_light = original_wait
            self._restore_root_logging(saved_handlers, saved_level)
        self.assertEqual(buffer.getvalue().count("Light OFF to match the camera"), 1)

    def test_brightness_flag_is_hidden_and_still_accepted(self):
        import subprocess

        script = Path(__file__).resolve().parents[1] / "main.py"
        help_run = subprocess.run(
            [sys.executable, str(script), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(help_run.returncode, 0)
        self.assertNotIn("brightness", help_run.stdout.lower())
        accepted = subprocess.run(
            [sys.executable, str(script), "--brightness", "40", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)


if __name__ == "__main__":
    unittest.main()
