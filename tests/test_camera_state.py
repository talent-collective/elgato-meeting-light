"""Unit tests for macOS camera-log parsing and the light decision."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from camera_state import (  # noqa: E402
    LOG_BIN,
    LOG_PREDICATE,
    CameraStateMachine,
    format_transition,
    light_should_be_on,
    parse_camera_log_line,
    replay_log_lines,
)

FIXTURE = Path(__file__).parent / "fixtures" / "macos-camera-sample.txt"

# Verbatim macOS 26.5 lines (Zoom on, empty-dictionary release, Photo Booth).
TAHOE_ON = (
    "2026-06-03 08:54:27.633 Df ControlCenter[1169:2621] "
    "[com.apple.controlcenter:captureFrameReceiver] Frame publisher "
    'cameras changed to [us.zoom.xos: ["0x1000002e1a4c01"]]'
)
TAHOE_OFF = (
    "2026-06-15 09:00:01.000 Df ControlCenter[808:1afb] "
    "[com.apple.controlcenter:captureFrameReceiver] Frame publisher "
    "cameras changed to [:]"
)
TAHOE_PHOTOBOOTH = (
    "Frame publisher cameras changed to "
    '[com.apple.PhotoBooth: ["EBB0ACC7-C7DB-49F1-B1FD-C77F4E234E8C"]]'
)


class ParseTests(unittest.TestCase):
    def test_log_binary_is_absolute(self):
        self.assertTrue(LOG_BIN.startswith("/"))
        self.assertTrue(LOG_BIN.endswith("/log"))

    def test_predicate_subscribes_to_current_and_legacy_signals(self):
        self.assertIn("Frame publisher cameras changed to", LOG_PREDICATE)
        self.assertIn("Active activity attributions changed to", LOG_PREDICATE)
        self.assertIn("kCameraStreamStart", LOG_PREDICATE)
        self.assertIn("power_on_hardware", LOG_PREDICATE)

    def test_stream_banner_is_not_a_camera_event(self):
        banner = (
            'Filtering the log data using "eventMessage CONTAINS '
            '"kCameraStreamStart" OR eventMessage CONTAINS '
            '"Frame publisher cameras changed to""'
        )
        echoed = 'eventMessage CONTAINS "kCameraStreamStart"'
        machine, transitions = replay_log_lines(
            [banner, echoed, "Timestamp               Ty Process[PID:TID]"]
        )
        self.assertFalse(machine.known)
        self.assertFalse(light_should_be_on(machine))
        self.assertEqual(transitions, [])

    def test_tahoe_zoom_on_and_empty_dictionary_off(self):
        on = parse_camera_log_line(TAHOE_ON)
        self.assertIsNotNone(on)
        assert on is not None
        self.assertTrue(on.on)
        self.assertEqual(on.kind, "aggregate")
        self.assertIn("us.zoom.xos", on.detail)

        off = parse_camera_log_line(TAHOE_OFF)
        self.assertIsNotNone(off)
        assert off is not None
        self.assertFalse(off.on)
        self.assertIn("none", off.detail)

    def test_tahoe_photobooth_dictionary_is_on(self):
        event = parse_camera_log_line(TAHOE_PHOTOBOOTH)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertTrue(event.on)
        self.assertIn("com.apple.PhotoBooth", event.detail)

    def test_empty_collections(self):
        for line in (
            "Frame publisher cameras changed to []",
            "Frame publisher cameras changed to [ ]",
            "Frame publisher cameras changed to [:]",
            "Frame publisher cameras changed to [ : ]",
            "Cameras changed to []",
        ):
            event = parse_camera_log_line(line)
            self.assertIsNotNone(event, line)
            assert event is not None
            self.assertFalse(event.on, line)

    def test_redacted_snapshot_does_not_clobber_state(self):
        machine, _ = replay_log_lines(
            [TAHOE_ON, "Frame publisher cameras changed to <private>"]
        )
        self.assertTrue(machine.in_use)

    def test_attribution_camera_mic_and_both(self):
        camera = parse_camera_log_line(
            'Active activity attributions changed to ["cam:com.apple.PhotoBooth"]'
        )
        mic = parse_camera_log_line(
            'Active activity attributions changed to ["mic:us.zoom.xos"]'
        )
        both = parse_camera_log_line(
            'Active activity attributions changed to ["mic:us.zoom.xos", "cam:us.zoom.xos"]'
        )
        empty = parse_camera_log_line(
            "Active activity attributions changed to []"
        )
        self.assertTrue(camera and camera.on and "PhotoBooth" in camera.detail)
        self.assertTrue(mic and not mic.on and "microphone only" in mic.detail)
        self.assertTrue(both and both.on)
        self.assertTrue(empty and not empty.on)

    def test_legacy_and_sonoma_edge_lines(self):
        samples = {
            "VDCAssistant: Post event kCameraStreamStart": True,
            "VDCAssistant: Post event kCameraStreamStop": False,
            "CMIOHardware.cpp:741:CMIODeviceStartStream (389 390)": True,
            "CMIOHardware.cpp:802:CMIODeviceStopStream (389 390)": False,
            "CMIOExtensionPropertyClientStreamingFromDALDevice 1 client": True,
            "CMIOExtensionPropertyClientStreamingFromDALDevice 0 client": False,
            "appleh13camerad PowerOnCamera": True,
            "appleh13camerad PowerOffCamera": False,
            "kernel: AppleH13CamIn::power_on_hardware": True,
            "kernel: AppleH13CamIn::power_off_hardware": False,
            '"VDCAssistant_Power_State" = On;': True,
            '"VDCAssistant_Power_State" = Off;': False,
        }
        for line, expected in samples.items():
            event = parse_camera_log_line(line)
            self.assertIsNotNone(event, line)
            assert event is not None
            self.assertEqual(event.on, expected, line)
            self.assertEqual(event.kind, "edge", line)


class StateMachineTests(unittest.TestCase):
    def test_unknown_does_not_turn_the_light_on(self):
        machine = CameraStateMachine()
        self.assertFalse(machine.known)
        self.assertFalse(light_should_be_on(machine))

    def test_hardware_only_sonoma_tracks_power(self):
        machine, transitions = replay_log_lines(
            [
                "kernel: AppleH13CamIn::power_on_hardware",
                "kernel: AppleH13CamIn::power_off_hardware",
            ]
        )
        self.assertEqual([t.current for t in transitions], [True, False])
        self.assertFalse(light_should_be_on(machine))

    def test_legacy_stream_last_event_wins(self):
        machine, _ = replay_log_lines(
            [
                "Post event kCameraStreamStart",
                "Post event kCameraStreamStop",
            ]
        )
        self.assertFalse(machine.in_use)

    def test_release_is_not_undone_by_a_later_power_on_echo(self):
        machine, transitions = replay_log_lines(
            [
                TAHOE_ON,
                TAHOE_OFF,
                "kernel: AppleH13CamIn::power_on_hardware",
                "Post event kCameraStreamStart",
                "appleh13camerad PowerOnCamera",
            ]
        )
        self.assertFalse(machine.in_use)
        self.assertTrue(machine.aggregate_seen)
        self.assertEqual([t.current for t in transitions], [True, False])

    def test_edges_still_work_before_any_aggregate(self):
        machine, _ = replay_log_lines(
            [
                "kernel: AppleH13CamIn::power_on_hardware",
                "Post event kCameraStreamStop",
            ]
        )
        self.assertFalse(machine.in_use)
        self.assertFalse(machine.aggregate_seen)

    def test_mic_only_leaves_the_light_off(self):
        machine, _ = replay_log_lines(
            [
                'Active activity attributions changed to ["mic:us.zoom.xos"]',
            ]
        )
        self.assertTrue(machine.known)
        self.assertFalse(light_should_be_on(machine))

    def test_fixture_ends_off_and_sees_zoom_then_release(self):
        lines = FIXTURE.read_text(encoding="utf-8").splitlines()
        machine, transitions = replay_log_lines(lines)
        self.assertGreaterEqual(len(transitions), 2)
        self.assertFalse(transitions[0].previous)
        self.assertTrue(transitions[0].current)
        self.assertIn("us.zoom.xos", transitions[0].detail)
        self.assertFalse(light_should_be_on(machine))
        self.assertTrue(any(not t.current for t in transitions))
        rendered = [format_transition(t) for t in transitions]
        self.assertTrue(rendered[0].startswith("Camera state changed: off -> on"))
        self.assertTrue(any(line.startswith("Camera state changed: on -> off") for line in rendered))


class SampleLogCliTests(unittest.TestCase):
    def test_sample_log_ends_with_the_light_off(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [
                sys.executable,
                str(root / "main.py"),
                "--sample-log",
                str(FIXTURE),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Camera state changed: off -> on", completed.stdout)
        self.assertIn("us.zoom.xos", completed.stdout)
        self.assertIn("Camera state changed: on -> off", completed.stdout)
        self.assertIn("light_should_be_on=false", completed.stdout)
        # The log-stream banner in the fixture names kCameraStreamStart.
        # That banner must not be the event that turns the light on.
        first_on = completed.stdout.split("Camera state changed: off -> on", 1)[1]
        self.assertIn("us.zoom.xos", first_on.splitlines()[0])


if __name__ == "__main__":
    unittest.main()
