from __future__ import annotations

import datetime
import hashlib
import hmac
import http.client
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


_IMPORT_DATA = tempfile.TemporaryDirectory()
os.environ["FLOWTRACK_HOME"] = _IMPORT_DATA.name
os.environ["FLOWTRACK_LAUNCHER"] = str(Path(_IMPORT_DATA.name) / "flowtrack-dashboard-launch.html")

import dashboard  # noqa: E402


class DashboardHelpersTests(unittest.TestCase):
    def test_local_host_and_origin_require_loopback_and_exact_port(self) -> None:
        self.assertTrue(dashboard._local_host_and_port("127.0.0.1:7070", 7070))
        self.assertTrue(dashboard._local_host_and_port("LOCALHOST:7070", 7070))
        self.assertTrue(dashboard._local_origin("http://localhost:7070", 7070))
        self.assertFalse(dashboard._local_host_and_port("example.test:7070", 7070))
        self.assertFalse(dashboard._local_host_and_port("localhost:8080", 7070))
        self.assertFalse(dashboard._local_origin("https://evil.example", 7070))

    def test_export_rejects_reverse_and_excessive_date_ranges(self) -> None:
        self.assertIn("error", dashboard.export_logs_by_date("2025-01-02", "2025-01-01"))
        self.assertIn("error", dashboard.export_logs_by_date("2000-01-01", "2025-01-01"))

    def test_analysis_auto_model_is_provider_specific_and_key_is_not_command_arg(self) -> None:
        completed = mock.Mock(returncode=0, stdout="ok", stderr="")

        class ImmediateThread:
            def __init__(self, target, daemon=False, args=()):
                self.target = target
                self.args = args

            def start(self):
                self.target(*self.args)

        dashboard._running = False
        with (
            mock.patch.object(dashboard.threading, "Thread", ImmediateThread),
            mock.patch.object(dashboard.subprocess, "run", return_value=completed) as run,
        ):
            self.assertTrue(dashboard._start_analysis(True, "ollama", "", "secret-value"))

        command = run.call_args.args[0]
        self.assertEqual(command[-4:], ["--provider", "ollama", "--model", "llama3"])
        self.assertNotIn("secret-value", command)

    def test_html_escapes_attribute_quotes_and_has_no_automatic_font_request(self) -> None:
        self.assertIn(".replace(/\"/g, '&quot;')", dashboard.HTML)
        self.assertIn(".replace(/'/g, '&#39;')", dashboard.HTML)
        self.assertNotIn("fonts.googleapis.com", dashboard.HTML)
        self.assertNotIn("cameraToggle", dashboard.HTML)

    def test_live_focus_merges_interval_segments(self) -> None:
        start = datetime.datetime(2026, 8, 16, 9)
        entries = [
            {
                "schema_version": 2,
                "ts": (start + datetime.timedelta(seconds=second)).isoformat(),
                "title": "Deep work",
                "app": "editor",
                "event": "interval",
                "duration": 30,
            }
            for second in range(0, 30 * 60, 30)
        ]
        self.assertEqual(dashboard._quick_focus(entries), "100.0")

    @unittest.skipUnless(os.name == "posix", "POSIX permissions only")
    def test_dashboard_token_is_private(self) -> None:
        token_path = Path(_IMPORT_DATA.name) / "dashboard-token"
        launcher_path = Path(_IMPORT_DATA.name) / "flowtrack-dashboard-launch.html"
        self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(launcher_path.stat().st_mode), 0o600)
        self.assertIn("/api/launcher-proof", launcher_path.read_text(encoding="utf-8"))


class DashboardHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        base = Path(cls.temp_dir.name)
        dashboard.BASE_DIR = base
        dashboard.LOG_DIR = base / "logs"
        dashboard.SCREENSHOTS_DIR = base / "screenshots"
        dashboard.REPORTS_DIR = base / "reports"
        for directory in (dashboard.LOG_DIR, dashboard.SCREENSHOTS_DIR, dashboard.REPORTS_DIR):
            directory.mkdir(parents=True)

        cls.server = dashboard.ThreadedServer(("127.0.0.1", 0), dashboard.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        for directory in (dashboard.LOG_DIR, dashboard.SCREENSHOTS_DIR, dashboard.REPORTS_DIR):
            for child in directory.iterdir():
                child.unlink()

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        request_headers = dict(headers or {})
        request_headers.setdefault(
            dashboard.DASHBOARD_TOKEN_HEADER, dashboard.DASHBOARD_TOKEN
        )
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        response_body = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        status = response.status
        connection.close()
        return status, response_headers, response_body

    def json_post(
        self,
        path: str,
        payload: object,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], dict]:
        headers = {"Content-Type": "application/json"}
        headers.update(extra_headers or {})
        status, response_headers, body = self.request(
            "POST", path, json.dumps(payload).encode("utf-8"), headers
        )
        return status, response_headers, json.loads(body)

    def test_root_rejects_dns_rebinding_host(self) -> None:
        status, _, body = self.request("GET", "/", headers={"Host": f"evil.example:{self.port}"})
        self.assertEqual(status, 403)
        self.assertFalse(json.loads(body)["ok"])

    def test_root_requires_token_and_serves_authenticated_page(self) -> None:
        status, _, body = self.request(
            "GET", "/", headers={dashboard.DASHBOARD_TOKEN_HEADER: ""}
        )
        self.assertEqual(status, 401)
        self.assertFalse(json.loads(body)["ok"])

        status, _, body = self.request(
            "GET",
            f"/?token={dashboard.DASHBOARD_TOKEN}",
            headers={dashboard.DASHBOARD_TOKEN_HEADER: ""},
        )
        self.assertEqual(status, 200)
        self.assertIn(
            f'const FLOWTRACK_TOKEN = "{dashboard.DASHBOARD_TOKEN}";',
            body.decode("utf-8"),
        )

    def test_launcher_can_verify_server_without_sending_bearer_token(self) -> None:
        nonce = "launcher-test-nonce-1234567890"
        status, headers, body = self.request(
            "GET",
            f"/api/launcher-proof?nonce={nonce}",
            headers={dashboard.DASHBOARD_TOKEN_HEADER: ""},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["access-control-allow-origin"], "null")
        expected = hmac.new(
            dashboard.DASHBOARD_TOKEN.encode("ascii"),
            nonce.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(json.loads(body)["proof"], expected)

    def test_root_has_browser_security_headers(self) -> None:
        status, headers, _ = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertEqual(headers["x-frame-options"], "DENY")

    def test_cross_origin_log_sync_is_rejected_before_upload(self) -> None:
        with mock.patch.object(dashboard, "sync_json_to_cloud") as sync:
            status, _, data = self.json_post(
                "/api/sync-json",
                {"provider": "webhook", "target": "https://evil.example/collect"},
                {"Origin": "https://evil.example"},
            )
        self.assertEqual(status, 403)
        self.assertFalse(data["ok"])
        sync.assert_not_called()

    def test_non_json_and_non_object_posts_are_rejected(self) -> None:
        status, _, _ = self.request(
            "POST", "/api/service", b'{"action":"start"}', {"Content-Type": "text/plain"}
        )
        self.assertEqual(status, 415)
        status, _, data = self.json_post("/api/service", ["start"])
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    def test_oversized_and_invalid_service_requests_are_rejected(self) -> None:
        status, _, _ = self.request(
            "POST",
            "/api/service",
            b"{}",
            {
                "Content-Type": "application/json",
                "Content-Length": str(dashboard.MAX_REQUEST_BYTES + 1),
            },
        )
        self.assertEqual(status, 413)
        status, _, data = self.json_post("/api/service", {"action": "launch-everything"})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    def test_invalid_log_limit_returns_json_400(self) -> None:
        status, _, body = self.request("GET", "/api/logs?limit=abc")
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])

    def test_png_screenshots_are_listed_and_served(self) -> None:
        filename = "2026-08-16_12-00-00.png"
        (dashboard.SCREENSHOTS_DIR / filename).write_bytes(b"png-data")
        (dashboard.SCREENSHOTS_DIR / "bad'onclick=alert.jpg").write_bytes(b"not-listed")
        status, _, body = self.request("GET", "/api/screenshots")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [filename])
        status, headers, body = self.request("GET", f"/screenshots/{filename}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "image/png")
        self.assertEqual(body, b"png-data")

    def test_custom_download_is_scoped_and_bad_dates_are_http_400(self) -> None:
        first = '{"day":"first"}\n'
        second = '{"day":"second"}\n'
        (dashboard.LOG_DIR / "2026-08-15.jsonl").write_text(first, encoding="utf-8")
        (dashboard.LOG_DIR / "2026-08-16.jsonl").write_text(second, encoding="utf-8")

        status, headers, body = self.request(
            "POST",
            "/api/backup-download",
            json.dumps(
                {"backup_type": "custom", "start_date": "2026-08-15", "end_date": "2026-08-15"}
            ).encode("utf-8"),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/x-ndjson; charset=utf-8")
        self.assertEqual(body.decode("utf-8"), first)

        status, _, data = self.json_post(
            "/api/backup-download",
            {"backup_type": "custom", "start_date": "not-a-date", "end_date": "2026-08-15"},
        )
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    def test_custom_upload_passes_only_selected_logs(self) -> None:
        selected_name = "2026-08-15.jsonl"
        (dashboard.LOG_DIR / selected_name).write_text("{}\n", encoding="utf-8")
        (dashboard.LOG_DIR / "2026-08-16.jsonl").write_text('{"other":true}\n', encoding="utf-8")
        with mock.patch.object(
            dashboard,
            "sync_json_to_cloud",
            return_value={"ok": True, "message": "uploaded"},
        ) as sync:
            status, _, data = self.json_post(
                "/api/backup-upload",
                {
                    "backup_type": "custom",
                    "start_date": "2026-08-15",
                    "end_date": "2026-08-15",
                    "provider": "gist",
                    "credential": "token",
                },
            )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(sync.call_args.kwargs["logs"], {selected_name: "{}\n"})


if __name__ == "__main__":
    unittest.main()
