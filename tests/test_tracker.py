import datetime
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


# Import-time directory setup must be isolated from the user's real activity.
_TEST_HOME = tempfile.TemporaryDirectory(prefix="flowtrack-tracker-tests-")
os.environ["FLOWTRACK_HOME"] = _TEST_HOME.name

import tracker  # noqa: E402  (environment must be set first)


class RecorderHarness:
    def __init__(self, *, excluded=()):
        self.rows = []
        self.shots = []

        def writer(title, app, event, screenshot, duration, **metadata):
            self.rows.append({
                "title": title,
                "app": app,
                "event": event,
                "screenshot": screenshot,
                "duration": duration,
                **metadata,
            })

        def screenshotter():
            name = f"shot-{len(self.shots)}.jpg"
            self.shots.append(name)
            return name

        self.recorder = tracker.SegmentRecorder(
            writer=writer,
            screenshotter=screenshotter,
            interval=30,
            exclude_patterns=excluded,
            storage_capper=lambda: None,
        )
        self.wall_start = datetime.datetime(2026, 8, 16, 9, 0, 0)

    def observe(self, title, app, second):
        self.recorder.observe(
            title,
            app,
            now=second,
            wall_now=self.wall_start + datetime.timedelta(seconds=second),
        )


class SegmentRecorderTests(unittest.TestCase):
    def test_stable_window_segments_are_incremental_and_flush(self):
        harness = RecorderHarness()
        harness.observe("A", "app-a", 0)
        harness.observe("A", "app-a", 30)
        harness.observe("A", "app-a", 60)
        harness.recorder.flush(now=75)

        self.assertEqual([row["duration"] for row in harness.rows], [30, 30, 15])
        self.assertEqual(sum(row["duration"] for row in harness.rows), 75)
        self.assertEqual({row["title"] for row in harness.rows}, {"A"})
        self.assertEqual([row["screenshot"] for row in harness.rows], harness.shots)
        self.assertTrue(all(row["schema_version"] == 2 for row in harness.rows))

    def test_change_duration_belongs_to_window_that_ended(self):
        harness = RecorderHarness()
        harness.observe("A", "app-a", 0)
        harness.observe("B", "app-b", 10)
        harness.observe("B", "app-b", 40)
        harness.recorder.flush(now=55)

        self.assertEqual(
            [(row["title"], row["app"], row["duration"]) for row in harness.rows],
            [("A", "app-a", 10), ("B", "app-b", 30), ("B", "app-b", 15)],
        )
        self.assertEqual(harness.rows[0]["event"], "change")
        self.assertEqual(sum(row["duration"] for row in harness.rows), 55)

    def test_app_change_with_same_title_is_a_change(self):
        harness = RecorderHarness()
        harness.observe("Shared title", "app-a", 0)
        harness.observe("Shared title", "app-b", 5)
        harness.recorder.flush(now=10)
        self.assertEqual([row["app"] for row in harness.rows], ["app-a", "app-b"])
        self.assertEqual(harness.rows[0]["event"], "change")

    def test_transient_unknown_does_not_create_switch_or_screenshot(self):
        harness = RecorderHarness()
        harness.observe("A", "app-a", 0)
        harness.observe("Unknown", "unknown", 1)
        harness.observe("A", "app-a", 2)
        harness.recorder.flush(now=3)
        self.assertEqual(len(harness.rows), 1)
        self.assertEqual(harness.rows[0]["duration"], 3)
        self.assertEqual(harness.shots, ["shot-0.jpg"])

    def test_sustained_unknown_excludes_probe_downtime(self):
        harness = RecorderHarness()
        harness.observe("A", "app-a", 0)
        harness.observe("Unknown", "unknown", 1)
        harness.observe("Unknown", "unknown", 2)
        harness.observe("Unknown", "unknown", 3)
        harness.observe("A", "app-a", 4)
        harness.recorder.flush(now=5)
        self.assertEqual([row["duration"] for row in harness.rows], [1, 1])
        self.assertNotIn("Unknown", {row["title"] for row in harness.rows})

    def test_flush_stops_at_first_unresolved_unknown(self):
        harness = RecorderHarness()
        harness.observe("A", "app-a", 0)
        harness.observe("Unknown", "unknown", 1)
        harness.observe("Unknown", "unknown", 2)
        harness.recorder.flush(now=3)

        self.assertEqual([row["duration"] for row in harness.rows], [1])
        self.assertNotIn("Unknown", {row["title"] for row in harness.rows})

    def test_excluded_windows_are_neither_logged_nor_screenshotted(self):
        harness = RecorderHarness(excluded=("vault",))
        harness.observe("A", "app-a", 0)
        harness.observe("My Vault", "secrets", 10)
        harness.observe("My Vault", "secrets", 15)
        harness.observe("B", "app-b", 20)
        harness.recorder.flush(now=25)
        self.assertEqual([(row["title"], row["duration"]) for row in harness.rows], [("A", 10), ("B", 5)])
        self.assertEqual([row["event"] for row in harness.rows], ["interval", "interval"])
        self.assertEqual(harness.shots, ["shot-0.jpg", "shot-1.jpg"])

    def test_screenshot_failure_still_logs_segment(self):
        rows = []
        recorder = tracker.SegmentRecorder(
            writer=lambda *args, **kwargs: rows.append((args, kwargs)),
            screenshotter=lambda: None,
            storage_capper=lambda: None,
        )
        wall = datetime.datetime(2026, 8, 16, 9)
        recorder.observe("A", "app-a", now=0, wall_now=wall)
        recorder.flush(now=5)
        self.assertEqual(rows[0][0][3], None)
        self.assertEqual(rows[0][0][4], 5)

    def test_long_poll_gap_is_not_counted_as_active_time(self):
        harness = RecorderHarness()
        harness.observe("A", "app-a", 0)
        harness.observe("A", "app-a", 1)
        harness.observe("A", "app-a", 600)
        harness.recorder.flush(now=610)

        self.assertEqual([row["duration"] for row in harness.rows], [2, 10])
        self.assertEqual(sum(row["duration"] for row in harness.rows), 12)


class PlatformProbeTests(unittest.TestCase):
    def test_windows_uses_ctypes_without_starting_powershell(self):
        title = "Résumé – 文書"
        executable = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

        def write_title(window, buffer, capacity):  # noqa: ARG001
            buffer.value = title
            return len(title)

        def write_process_id(window, process_id):  # noqa: ARG001
            process_id._obj.value = 4321
            return 1

        def write_executable(process, flags, buffer, length):  # noqa: ARG001
            buffer.value = executable
            length._obj.value = len(executable)
            return 1

        close_handle = Mock(return_value=1)
        user32 = types.SimpleNamespace(
            GetForegroundWindow=Mock(return_value=101),
            GetWindowTextLengthW=Mock(return_value=len(title)),
            GetWindowTextW=Mock(side_effect=write_title),
            GetWindowThreadProcessId=Mock(side_effect=write_process_id),
        )
        kernel32 = types.SimpleNamespace(
            OpenProcess=Mock(return_value=202),
            QueryFullProcessImageNameW=Mock(side_effect=write_executable),
            CloseHandle=close_handle,
        )

        def load_library(name, **kwargs):  # noqa: ARG001
            return user32 if name == "user32" else kernel32

        subprocess_run = Mock()
        with (
            patch.object(tracker.platform, "system", return_value="Windows"),
            patch.object(tracker.ctypes, "WinDLL", side_effect=load_library, create=True),
            patch.object(tracker, "_run", subprocess_run),
        ):
            self.assertEqual(tracker.get_active_window_info(), (title, "chrome"))

        subprocess_run.assert_not_called()
        kernel32.OpenProcess.assert_called_once_with(0x1000, False, 4321)
        close_handle.assert_called_once_with(202)

    def test_windows_powershell_is_safe_fallback_only(self):
        run = Mock(return_value="Window title\x1fchrome")
        with (
            patch.object(tracker.platform, "system", return_value="Windows"),
            patch.object(tracker, "_get_windows_foreground_ctypes", return_value=None),
            patch.object(tracker, "_run", run),
        ):
            self.assertEqual(tracker.get_active_window_info(), ("Window title", "chrome"))

        command = run.call_args.args[0]
        self.assertEqual(command[0], "powershell.exe")
        self.assertIn("-NonInteractive", command)
        script = command[-1]
        self.assertIn("$processId", script)
        self.assertNotIn("$pid=", script.casefold())

    def test_windows_ctypes_failure_is_non_fatal(self):
        with patch.object(tracker.ctypes, "WinDLL", side_effect=OSError("unavailable"), create=True):
            self.assertIsNone(tracker._get_windows_foreground_ctypes())

    def test_macos_uses_one_structured_osascript_call(self):
        run = Mock(return_value="Document\x1fSafari")
        with patch.object(tracker.platform, "system", return_value="Darwin"), patch.object(tracker, "_run", run):
            self.assertEqual(tracker.get_active_window_info(), ("Document", "Safari"))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], "osascript")

    def test_scrot_fallback_is_linux_only(self):
        run = Mock()
        with patch.object(tracker, "HAS_MSS", False), patch.object(tracker.platform, "system", return_value="Darwin"), patch.object(tracker, "_run", run):
            self.assertIsNone(tracker.take_screenshot())
        run.assert_not_called()

    def test_mss_lowercase_factory_is_used(self):
        factory = Mock()
        context = Mock()
        context.__enter__ = Mock(return_value=context)
        context.__exit__ = Mock(return_value=False)
        context.monitors = [{}, {"width": 1, "height": 1}]
        context.grab.return_value = types.SimpleNamespace(size=(1, 1), bgra=b"\0\0\0\0")
        factory.return_value = context

        image = Mock()
        image.width = 1
        image.height = 1
        image.resize.return_value = image
        image.convert.return_value = image
        fake_image_module = Mock(LANCZOS=1)
        fake_image_module.frombytes.return_value = image
        fake_mss_module = types.SimpleNamespace(mss=factory)
        with patch.dict(sys.modules, {"mss": fake_mss_module}), patch.object(tracker, "HAS_MSS", True), patch.object(tracker, "HAS_PIL", True), patch.object(tracker, "Image", fake_image_module), patch.object(tracker.platform, "system", return_value="Windows"):
            self.assertIsNotNone(tracker.take_screenshot())
        factory.assert_called_once_with()


class PrivacyTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX permissions only")
    def test_data_directories_and_new_log_are_private(self):
        for directory in (tracker.BASE_DIR, tracker.LOG_DIR, tracker.SCREENSHOTS_DIR):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        timestamp = datetime.datetime(2026, 8, 17, 1, 2, 3)
        tracker.append_log_entry("A", "app", "interval", None, 1, timestamp=timestamp)
        path = tracker.LOG_DIR / "2026-08-17.jsonl"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
