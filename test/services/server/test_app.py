"""Bootstrap: the backend runs from the on-disk stores and a private token file."""
import http.client
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from services.server.app import (
    build_server,
    clear_runtime,
    runtime_path,
    write_runtime,
)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="imu-app-"))
        self.web = self.dir / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text(
            "<meta name='imurun-token' content='__IMURUN_TOKEN__'>", encoding="utf-8"
        )

    def test_build_server_starts_with_the_on_disk_stores(self):
        server = build_server(root=self.dir, static_root=self.web)
        server.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
            connection.request(
                "GET",
                "/api/status",
                headers={"Authorization": f"Bearer {server.token}"},
            )
            response = connection.getresponse()
            data = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertTrue(data["ok"])
            self.assertEqual(data["sessions"], 0)
            self.assertFalse(data["model"])
            self.assertEqual(data["runtime"], "provisioning")

            connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
            connection.request("GET", "/")
            response = connection.getresponse()
            body = response.read()
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn(server.token.encode(), body)
        finally:
            server.stop()

    def test_the_runtime_file_holds_the_token_and_is_removed(self):
        server = build_server(root=self.dir, static_root=self.web)
        server.start()
        path = None
        try:
            path = write_runtime(self.dir, server.port, server.token)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["token"], server.token)
            self.assertEqual(data["url"], f"http://127.0.0.1:{server.port}/")
            self.assertGreater(data["pid"], 0)
            self.assertEqual(path, runtime_path(self.dir))
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        finally:
            clear_runtime(self.dir)
            server.stop()
        self.assertFalse(path.exists())

    def test_a_bad_runtime_request_is_refused(self):
        with self.assertRaises(ValueError):
            write_runtime(self.dir, 8000, "")
        with self.assertRaises(ValueError):
            write_runtime(self.dir, 0, "token")
        with self.assertRaises(ValueError):
            write_runtime(self.dir, 70000, "token")


if __name__ == "__main__":
    unittest.main()
