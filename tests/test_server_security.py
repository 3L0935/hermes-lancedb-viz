import json
import http.client
import importlib.util
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lancedb_http_security_server", ROOT / "server" / "server.py"
)
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class ServerSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.static_dir = self.root / "static"
        self.static_dir.mkdir()
        (self.static_dir / "index.html").write_text("safe index")
        (self.root / "sentinel.txt").write_text("audit-sentinel")
        self.previous_static_dir = server.STATIC_DIR
        server.STATIC_DIR = self.static_dir
        self.httpd = server.HTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        server.STATIC_DIR = self.previous_static_dir
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.httpd.server_port, timeout=3
        )
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), payload

    def test_static_parent_traversal_cannot_read_sentinel(self):
        status, _headers, body = self.request("GET", "/static/../sentinel.txt")

        self.assertEqual(404, status)
        self.assertNotIn(b"audit-sentinel", body)

    def test_static_symlink_cannot_escape_static_directory(self):
        (self.static_dir / "sentinel-link.txt").symlink_to(
            self.root / "sentinel.txt"
        )

        status, _headers, body = self.request(
            "GET", "/static/sentinel-link.txt"
        )

        self.assertEqual(404, status)
        self.assertNotIn(b"audit-sentinel", body)

    def test_regular_static_file_remains_available_without_cors(self):
        status, headers, body = self.request("GET", "/static/index.html")

        self.assertEqual(200, status)
        self.assertEqual(b"safe index", body)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_post_rejects_non_local_host(self):
        status, _headers, body = self.request(
            "POST",
            "/api/not-found",
            body=b"{}",
            headers={"Host": "attacker.example", "Content-Type": "application/json"},
        )

        self.assertEqual(403, status)
        self.assertIn(b"Local same-origin request required", body)

    def test_post_rejects_cross_origin_request(self):
        status, _headers, body = self.request(
            "POST",
            "/api/not-found",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://attacker.example",
            },
        )

        self.assertEqual(403, status)
        self.assertIn(b"Local same-origin request required", body)

    def test_post_accepts_same_origin_local_json(self):
        authority = f"127.0.0.1:{self.httpd.server_port}"
        status, headers, body = self.request(
            "POST",
            "/api/not-found",
            body=b"{}",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Origin": f"http://{authority}",
            },
        )

        self.assertEqual(404, status)
        self.assertEqual({"error": "Not found"}, json.loads(body))
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_post_rejects_non_json_body(self):
        status, _headers, body = self.request(
            "POST",
            "/api/not-found",
            body=b"memory_id=aaaaaaaa-aaa",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        self.assertEqual(415, status)
        self.assertIn(b"Content-Type must be application/json", body)

    def test_post_rejects_body_over_size_limit_without_reading_it(self):
        status, _headers, body = self.request(
            "POST",
            "/api/not-found",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(server.MAX_JSON_BODY_BYTES + 1),
            },
        )

        self.assertEqual(413, status)
        self.assertIn(b"JSON body exceeds 2 MiB limit", body)

    def test_post_rejects_malformed_or_non_object_json(self):
        malformed_status, _headers, _body = self.request(
            "POST",
            "/api/not-found",
            body=b"{",
            headers={"Content-Type": "application/json"},
        )
        array_status, _headers, _body = self.request(
            "POST",
            "/api/not-found",
            body=b"[]",
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(400, malformed_status)
        self.assertEqual(400, array_status)


class ContainerBindingTests(unittest.TestCase):
    def test_container_ports_are_published_on_ipv4_loopback_only(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        docker_run = (ROOT / "scripts" / "docker-run.sh").read_text()

        self.assertIn('"127.0.0.1:7777:7777"', compose)
        self.assertIn('-p "127.0.0.1:${PORT}:7777"', docker_run)
        self.assertNotIn('- "7777:7777"', compose)
        self.assertNotIn('-p "${PORT}:7777"', docker_run)


if __name__ == "__main__":
    unittest.main()
