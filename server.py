#!/usr/bin/env python3
"""Tiny Tailscale-gated chat UI for a Grok Bot-style drop pipe.

This process never calls xAI or Cursor APIs. It only reads/writes files
under DATA_DIR so a local agent/watcher can pick up inbox.json and write
reply.json.
"""

from __future__ import annotations

import json
import os
import posixpath
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8780
PUBLIC_BIND_BLOCKLIST = frozenset({"0.0.0.0", "::", "[::]"})


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).expanduser().resolve()


def ensure_data_dir() -> Path:
    path = data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def resolve_host() -> str:
    host = (
        os.environ.get("GROK_BOT_WEB_HOST")
        or os.environ.get("TAILSCALE_IP")
        or DEFAULT_HOST
    ).strip()
    if host in PUBLIC_BIND_BLOCKLIST:
        print(
            "Refusing to bind "
            f"{host}. Do not expose this UI on the public internet.\n"
            "Bind 127.0.0.1 (default) or a Tailscale address via "
            "GROK_BOT_WEB_HOST / TAILSCALE_IP.",
            file=sys.stderr,
        )
        sys.exit(2)
    return host


def resolve_port() -> int:
    raw = os.environ.get("GROK_BOT_WEB_PORT") or os.environ.get("PORT") or str(DEFAULT_PORT)
    try:
        port = int(raw)
    except ValueError:
        print(f"Invalid port: {raw!r}", file=sys.stderr)
        sys.exit(2)
    if not (1 <= port <= 65535):
        print(f"Port out of range: {port}", file=sys.stderr)
        sys.exit(2)
    return port


class Handler(BaseHTTPRequestHandler):
    server_version = "grok-bot-web/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes = b"", content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        self._send(status, raw, "application/json; charset=utf-8")

    def _read_json_body(self) -> dict | None:
        length = self.headers.get("Content-Length")
        try:
            n = int(length) if length else 0
        except ValueError:
            return None
        if n < 0 or n > 1_000_000:
            return None
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            self._handle_health()
            return
        if path == "/api/reply":
            self._handle_reply(parsed)
            return
        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/") :])
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/send":
            self._handle_send()
            return
        self._send_json(404, {"error": "not found"})

    def _handle_health(self) -> None:
        self._send_json(200, {"ok": True, "service": "grok-bot-web"})

    def _handle_send(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            self._send_json(400, {"error": "invalid json"})
            return
        text = payload.get("text")
        if not isinstance(text, str):
            self._send_json(400, {"error": "text must be a string"})
            return
        text = text.strip()
        if not text:
            self._send_json(400, {"error": "text is required"})
            return
        if len(text) > 32_000:
            self._send_json(400, {"error": "text too long"})
            return
        msg = {
            "id": uuid.uuid4().hex,
            "text": text,
            "ts": time.time(),
            "source": "web",
        }
        d = ensure_data_dir()
        atomic_write(d / "inbox.json", json.dumps(msg, indent=2) + "\n")
        atomic_write(d / "flag", "1\n")
        self._send_json(200, {"id": msg["id"], "ts": msg["ts"]})

    def _handle_reply(self, parsed) -> None:
        q = parse_qs(parsed.query)
        req_id = (q.get("id") or [""])[0].strip()
        if not req_id:
            self._send_json(400, {"error": "id is required"})
            return
        reply = read_json(data_dir() / "reply.json")
        if not reply or reply.get("id") != req_id:
            self._send(204)
            return
        self._send_json(200, reply)

    def _serve_static(self, rel: str) -> None:
        name = posixpath.normpath(rel).lstrip("/")
        if name in (".", "") or name.startswith("..") or "/" in name:
            self._send_json(404, {"error": "not found"})
            return
        path = (STATIC_DIR / name).resolve()
        try:
            path.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._send_json(404, {"error": "not found"})
            return
        if not path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        data = path.read_bytes()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".json": "application/json; charset=utf-8",
        }.get(path.suffix, "application/octet-stream")
        self._send(200, data, ctype)


def main() -> None:
    host = resolve_host()
    port = resolve_port()
    ensure_data_dir()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"grok-bot-web listening on http://{host}:{port}")
    print(f"DATA_DIR={data_dir()}")
    print("Tailscale only. Do not bind 0.0.0.0 to the public internet.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
