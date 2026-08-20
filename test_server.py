#!/usr/bin/env python3
"""Stdlib tests for grok-bot-web send/poll/upload/history. No pip deps."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402


def _http(method: str, url: str, data: bytes | None = None, headers: dict | None = None):
    req = Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urlopen(req, timeout=5) as resp:
            body = resp.read()
            return resp.status, body, dict(resp.headers)
    except HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data = Path(cls.tmp.name)
        os.environ["DATA_DIR"] = str(cls.data)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def test_health(self) -> None:
        status, body, _ = _http("GET", self.base + "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_json_send_and_reply(self) -> None:
        status, body, _ = _http(
            "POST",
            self.base + "/api/send",
            json.dumps({"text": "hello pipe"}).encode(),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["id"])
        inbox = json.loads((self.data / "inbox.json").read_text())
        self.assertEqual(inbox["text"], "hello pipe")
        self.assertEqual(inbox["source"], "web")
        self.assertNotIn("files", inbox)
        self.assertEqual((self.data / "flag").read_text().strip(), "1")
        reply = {"id": data["id"], "text": "ack hello", "ts": time.time()}
        (self.data / "reply.json").write_text(json.dumps(reply) + "\n", encoding="utf-8")
        status, body, _ = _http("GET", self.base + "/api/reply?id=" + data["id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["text"], "ack hello")
        status, _, _ = _http("GET", self.base + "/api/reply?id=not-the-id")
        self.assertEqual(status, 204)

    def test_multipart_file_in_inbox(self) -> None:
        boundary = "----testboundary9"
        payload = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="text"\r\n\r\n'
            "see attached\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="note.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
            "file-bytes-here\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        status, body, _ = _http(
            "POST",
            self.base + "/api/send",
            payload,
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        self.assertEqual(status, 200, body)
        resp = json.loads(body)
        inbox = json.loads((self.data / "inbox.json").read_text())
        self.assertEqual(inbox["text"], "see attached")
        self.assertEqual(inbox["id"], resp["id"])
        self.assertEqual(len(inbox["files"]), 1)
        rec = inbox["files"][0]
        self.assertEqual(rec["name"], "note.txt")
        self.assertIn("path", rec)
        path = Path(rec["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), b"file-bytes-here")
        self.assertEqual(path.parent, (self.data / "uploads").resolve())
        self.assertEqual(resp["files"][0]["name"], "note.txt")
        self.assertTrue(resp["files"][0]["url"].startswith("/api/uploads/"))
        status, got, _ = _http("GET", self.base + resp["files"][0]["url"])
        self.assertEqual(status, 200)
        self.assertEqual(got, b"file-bytes-here")

    def test_file_only_and_traversal_name(self) -> None:
        boundary = "----evilbound"
        payload = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="../../etc/passwd"\r\n'
            "Content-Type: text/plain\r\n\r\n"
            "nope\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        status, body, _ = _http(
            "POST",
            self.base + "/api/send",
            payload,
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        self.assertEqual(status, 200, body)
        inbox = json.loads((self.data / "inbox.json").read_text())
        rec = inbox["files"][0]
        self.assertEqual(rec["name"], "passwd")
        path = Path(rec["path"])
        self.assertEqual(path.parent, (self.data / "uploads").resolve())
        self.assertTrue(path.name.endswith("-passwd"))
        self.assertEqual(path.read_bytes(), b"nope")
        status, _, _ = _http("GET", self.base + "/api/uploads/../server.py")
        self.assertEqual(status, 404)

    def test_upload_too_large(self) -> None:
        n = server.MAX_BODY + 10
        status, body, _ = _http(
            "POST",
            self.base + "/api/send",
            b"x" * 8,
            {
                "Content-Type": "multipart/form-data; boundary=z",
                "Content-Length": str(n),
            },
        )
        self.assertEqual(status, 400)
        self.assertIn(b"too large", body)

    def test_file_bytes_cap(self) -> None:
        with self.assertRaises(ValueError):
            server.save_upload("big.bin", b"x" * (server.MAX_FILE_BYTES + 1))

    def test_history_includes_reply_landed_while_away(self) -> None:
        """Reload / closed-tab path: reply.json is merged via /api/history."""
        status, body, _ = _http(
            "POST",
            self.base + "/api/send",
            json.dumps({"text": "away now"}).encode(),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        msg_id = json.loads(body)["id"]
        reply = {"id": msg_id, "text": "landed while tab closed", "ts": time.time()}
        (self.data / "reply.json").write_text(json.dumps(reply) + "\n", encoding="utf-8")
        status, body, _ = _http("GET", self.base + "/api/history")
        self.assertEqual(status, 200)
        replies = json.loads(body)["replies"]
        ids = [r.get("id") for r in replies]
        self.assertIn(msg_id, ids)
        found = next(r for r in replies if r["id"] == msg_id)
        self.assertEqual(found["text"], "landed while tab closed")
        status, body, _ = _http("GET", self.base + "/api/reply")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], msg_id)


    def test_history_and_latest_reply(self) -> None:
        status, body, _ = _http(
            "POST",
            self.base + "/api/send",
            json.dumps({"text": "first"}).encode(),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        first = json.loads(body)["id"]
        status, body, _ = _http(
            "POST",
            self.base + "/api/send",
            json.dumps({"text": "second"}).encode(),
            {"Content-Type": "application/json"},
        )
        second = json.loads(body)["id"]
        env = os.environ.copy()
        env["DATA_DIR"] = str(self.data)
        subprocess.check_call(
            [sys.executable, str(ROOT / "reply.py"), "--id", first, "--text", "away-one"],
            env=env,
        )
        subprocess.check_call(
            [sys.executable, str(ROOT / "reply.py"), "--id", second, "--text", "away-two"],
            env=env,
        )
        status, body, _ = _http("GET", self.base + "/api/reply?id=" + first)
        self.assertEqual(status, 204)
        status, body, _ = _http("GET", self.base + "/api/reply")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], second)
        status, body, _ = _http("GET", self.base + "/api/history")
        self.assertEqual(status, 200)
        ids = [r["id"] for r in json.loads(body)["replies"]]
        self.assertIn(first, ids)
        self.assertIn(second, ids)

    def test_refuse_public_bind(self) -> None:
        env = os.environ.copy()
        env["GROK_BOT_WEB_HOST"] = "0.0.0.0"
        env["DATA_DIR"] = str(self.data)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "server.py")],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("0.0.0.0", proc.stderr)
        self.assertNotIn("listening", proc.stdout)


class FilenameTests(unittest.TestCase):
    def test_safe_filename(self) -> None:
        self.assertEqual(server.safe_filename("../../x.png"), "x.png")
        self.assertEqual(server.safe_filename(r"C:\\tmp\\y.bin"), "y.bin")
        self.assertEqual(server.safe_filename(".."), "file")
        self.assertEqual(server.safe_filename(""), "file")


class UITests(unittest.TestCase):
    def test_page_has_file_persist_clear(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('type="file"', html)
        self.assertIn("localStorage.setItem", html)
        self.assertIn("localStorage.getItem", html)
        self.assertIn('id="clear"', html)
        self.assertIn("STORE_KEY", html)
        self.assertIn("clearedAt", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
