import datetime
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


# Import-time directory setup must be isolated from the user's real activity.
_TEST_HOME = tempfile.TemporaryDirectory(prefix="flowtrack-analyze-tests-")
os.environ["FLOWTRACK_HOME"] = _TEST_HOME.name

import analyze  # noqa: E402  (environment must be set first)


START = datetime.datetime(2026, 8, 16, 9, 0, 0)


def entry(
    second,
    *,
    title="A",
    app="chrome",
    duration=30,
    schema=2,
    day=0,
    event="interval",
):
    timestamp = START + datetime.timedelta(days=day, seconds=second)
    return {
        "schema_version": schema,
        "ts": timestamp.isoformat(),
        "dt": timestamp,
        "title": title,
        "app": app,
        "duration": duration,
        "event": event,
        "screenshot": None,
    }


class LegacyNormalizationTests(unittest.TestCase):
    def test_legacy_cumulative_rows_reconstruct_from_timestamp_gaps(self):
        legacy = [
            entry(0, duration=0, schema=1),
            entry(30, duration=30, schema=1),
            entry(60, duration=60, schema=1),
            entry(70, title="B", app="firefox", duration=70, schema=1, event="change"),
        ]
        normalized = analyze.normalize_entries(legacy)
        self.assertEqual(
            [(row["title"], row["duration"], row["event"]) for row in normalized],
            [
                ("A", 30, "interval"),
                ("A", 30, "interval"),
                ("A", 10, "change"),
                ("B", 0, "interval"),
            ],
        )
        self.assertEqual(sum(row["duration"] for row in normalized), 70)

    def test_legacy_idle_gap_is_capped_and_not_a_switch(self):
        normalized = analyze.normalize_entries([
            entry(0, title="A", app="chrome", schema=1),
            entry(0, title="B", app="firefox", schema=1, day=1),
        ])
        self.assertEqual(normalized[0]["duration"], analyze.LEGACY_IDLE_GAP_SECONDS)
        self.assertEqual(normalized[0]["event"], "interval")
        self.assertEqual(analyze.analyze_context_switches(normalized)["total_app_switches"], 0)

    def test_v2_incremental_duration_is_preserved(self):
        normalized = analyze.normalize_entries([entry(0, duration=12.5)])
        self.assertEqual(normalized[0]["duration"], 12.5)
        self.assertNotIn("legacy_normalized", normalized[0])

    def test_preparsed_v2_nonfinite_and_invalid_durations_are_zeroed(self):
        rows = [
            entry(0, duration=float("inf")),
            entry(1, duration=float("nan")),
            entry(2, duration=-1),
            entry(3, duration="invalid"),
        ]
        normalized = analyze.normalize_entries(rows)

        self.assertEqual([row["duration"] for row in normalized], [0.0] * 4)
        focus = analyze.calculate_focus_score(normalized)
        self.assertEqual(focus, {"daily": 0.0, "hourly": {}})


class SwitchAnalysisTests(unittest.TestCase):
    def test_interval_rows_do_not_count_as_rapid_or_context_switches(self):
        intervals = [
            entry(0, title="A", app="app-a", duration=10),
            entry(10, title="B", app="app-b", duration=10),
            entry(20, title="C", app="app-c", duration=10),
        ]
        self.assertEqual(analyze.detect_rapid_switching(intervals), [])
        context = analyze.analyze_context_switches(intervals)
        self.assertEqual(context["total_app_switches"], 0)
        self.assertEqual(context["switches_per_hour"], 0.0)

    def test_six_real_title_changes_trigger_rapid_switching(self):
        switching = [
            entry(
                index * 10,
                title="A" if index % 2 == 0 else "B",
                duration=10,
                event="change" if index < 6 else "interval",
            )
            for index in range(7)
        ]
        rapid = analyze.detect_rapid_switching(switching)
        self.assertEqual(len(rapid), 1)
        self.assertEqual(rapid[0]["switches"], 6)

    def test_overnight_gap_does_not_create_transition_or_dilute_rate(self):
        sessions = [
            entry(0, title="A", app="app-a", duration=30, event="change"),
            entry(30, title="B", app="app-b", duration=30),
            entry(0, title="A", app="app-a", duration=30, day=1, event="change"),
            entry(30, title="B", app="app-b", duration=30, day=1),
        ]
        context = analyze.analyze_context_switches(sessions)
        self.assertEqual(context["total_app_switches"], 2)
        self.assertEqual(context["top_app_pairs"], [{"from": "app-a", "to": "app-b", "count": 2}])
        self.assertEqual(context["switches_per_hour"], 60.0)


class FocusAndClassificationTests(unittest.TestCase):
    def test_adjacent_segments_merge_into_deep_work_block(self):
        segments = [entry(second, duration=30) for second in range(0, 30 * 60, 30)]
        blocks = analyze.merge_activity_blocks(segments)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["duration"], 30 * 60)
        focus = analyze.calculate_focus_score(segments)
        self.assertEqual(focus["daily"], 100.0)

    def test_non_contiguous_same_window_is_not_merged(self):
        blocks = analyze.merge_activity_blocks([
            entry(0, duration=30),
            entry(120, duration=30),
        ])
        self.assertEqual(len(blocks), 2)

    def test_browser_aliases_cover_macos_and_windows_names(self):
        aliases = {
            "Google Chrome": "chrome",
            "Safari": "safari",
            "msedge": "microsoft-edge",
            "chrome.exe": "chrome",
        }
        for raw, expected in aliases.items():
            with self.subTest(raw=raw):
                self.assertEqual(analyze.normalize_app_name(raw), expected)

        browser_rows = [
            entry(0, title="First", app="Google Chrome"),
            entry(30, title="Second", app="Google Chrome"),
        ]
        with patch.object(analyze, "RABBIT_HOLE_THRESHOLD", 2):
            self.assertEqual(len(analyze.detect_rabbit_holes(browser_rows)), 1)

    def test_visual_bait_counts_visit_and_actual_switches_not_intervals(self):
        rows = [
            entry(0, title="YouTube", app="chrome"),
            entry(30, title="YouTube", app="chrome", event="change"),
            entry(60, title="Editor", app="code"),
        ]
        result = analyze.detect_visual_bait(rows)["YouTube"]
        self.assertEqual(result["visits"], 1)
        self.assertEqual(result["max_post_trigger_switches"], 1)

    def test_afternoon_only_activity_is_not_labeled_as_fatigue(self):
        rows = [entry(3 * 60 * 60, duration=30)]
        fatigue = analyze.detect_fatigue_pattern(rows)
        self.assertFalse(fatigue["fatigue_detected"])


class LoadingReportingAndPrivacyTests(unittest.TestCase):
    def test_malformed_json_and_non_object_rows_are_skipped(self):
        today = datetime.date.today()
        path = analyze.LOG_DIR / f"{today.isoformat()}.jsonl"
        good = {
            "schema_version": 2,
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "title": "A",
            "app": "app",
            "event": "interval",
            "duration": 5,
            "screenshot": None,
        }
        path.write_text("not json\n[]\n" + __import__("json").dumps(good) + "\n", encoding="utf-8")
        loaded = analyze.load_entries(1)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["title"], "A")

    def test_report_uses_requested_days_and_platform_neutral_prompt(self):
        rows = [entry(0)]
        report = analyze.generate_text_report(
            rows,
            [],
            [],
            analyze.detect_fatigue_pattern(rows),
            {},
            analyze.analyze_context_switches(rows),
            analyze.calculate_focus_score(rows),
            days=1,
        )
        prompt = analyze.build_ai_prompt(report, days=1)
        self.assertIn("last 1 day", report)
        self.assertIn("past 1 day", prompt)
        self.assertNotIn("Ubuntu", prompt)

    def test_day_argument_is_bounded(self):
        self.assertEqual(analyze._days_value("7"), 7)
        for invalid in ("0", "366"):
            with self.subTest(invalid=invalid), self.assertRaises(Exception):
                analyze._days_value(invalid)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions only")
    def test_report_directory_and_output_are_private(self):
        self.assertEqual(stat.S_IMODE(analyze.REPORT_DIR.stat().st_mode), 0o700)
        path = analyze.REPORT_DIR / "private.txt"
        analyze._write_private_text(path, "private")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
