#!/usr/bin/env python3
"""Tiny Tailscale-gated chat UI for a Grok Bot-style drop pipe.

This process never calls xAI or Cursor APIs. It only reads/writes files
under DATA_DIR so a local agent/watcher can pick up inbox.json and write
reply.json.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8780
PUBLIC_BIND_BLOCKLIST = frozenset({"0.0.0.0", "::", "[::]"})
MAX_BODY = 16_000_000
MAX_TEXT = 32_000
MAX_FILES = 5
MAX_FILE_BYTES = 8_000_000
HISTORY_LIMIT = 400
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).expanduser().resolve()


def ensure_data_dir() -> Path:
    path = data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def uploads_dir() -> Path:
    path = ensure_data_dir() / "uploads"
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


def original_filename(name: str) -> str:
    base = str(name or "").replace("\\", "/").split("/")[-1].strip()
    return base or "file"


def safe_filename(name: str) -> str:
    base = SAFE_NAME_RE.sub("_", original_filename(name)).strip("._")
    return (base or "file")[:180]


def public_file(rec: dict) -> dict:
    stored = Path(rec["path"]).name
    suffix = Path(rec.get("name") or stored).suffix.lower()
    return {
        "name": rec["name"],
        "url": "/api/uploads/" + stored,
        "type": IMAGE_TYPES.get(suffix, ""),
    }


def archive_reply(d: Path, reply: dict) -> None:
    """Append reply.json snapshots to replies.jsonl, skipping duplicate ids."""
    if not isinstance(reply, dict):
        return
    rid = reply.get("id")
    if not isinstance(rid, str) or not rid:
        return
    path = d / "replies.jsonl"
    try:
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("id") == rid:
                    return
    except OSError:
        return
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(reply, separators=(",", ":")) + "\n")
    except OSError:
        return


def load_reply_history(d: Path) -> list[dict]:
    current = read_json(d / "reply.json")
    if current:
        archive_reply(d, current)
    out: list[dict] = []
    seen: set[str] = set()
    path = d / "replies.jsonl"
    try:
        raw = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        raw = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        rid = obj.get("id")
        if not isinstance(rid, str) or not rid or rid in seen:
            continue
        seen.add(rid)
        out.append(obj)
    return out[-HISTORY_LIMIT:]


def fire_on_send_hook(msg: dict) -> None:
    """If GROK_BOT_WEB_ON_SEND is set, run that local command in the background.

    The command receives GROK_BOT_WEB_INBOX_ID. Failures are ignored so a
    broken hook cannot fail the HTTP send. No secrets belong in this tree.
    """
    raw = (os.environ.get("GROK_BOT_WEB_ON_SEND") or "").strip()
    if not raw:
        return
    try:
        argv = shlex.split(raw)
    except ValueError:
        return
    if not argv:
        return
    env = os.environ.copy()
    env["GROK_BOT_WEB_INBOX_ID"] = str(msg.get("id") or "")
    try:
        subprocess.Popen(
            argv,
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], list[dict]]:
    m = re.search(r"boundary=([^;]+)", content_type or "", re.I)
    if not m:
        return {}, []
    boundary = m.group(1).strip().strip('"')
    if not boundary:
        return {}, []
    delim = b"--" + boundary.encode("ascii", "replace")
    fields: dict[str, str] = {}
    files: list[dict] = []
    for raw_part in body.split(delim):
        if not raw_part or raw_part.startswith(b"--"):
            continue
        part = raw_part
        if part.startswith(b"\r\n"):
            part = part[2:]
        elif part.startswith(b"\n"):
            part = part[1:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        elif part.endswith(b"\n"):
            part = part[:-1]
        header_blob, sep, content = part.partition(b"\r\n\r\n")
        if not sep:
            header_blob, sep, content = part.partition(b"\n\n")
        if not sep:
            continue
        headers: dict[str, str] = {}
        for line in header_blob.split(b"\r\n"):
            if b":" not in line:
                continue
            key, val = line.split(b":", 1)
            headers[key.decode("latin1").lower()] = val.decode("latin1").strip()
        disp = headers.get("content-disposition", "")
        name_m = re.search(r'name="([^"]+)"', disp)
        if not name_m:
            continue
        name = name_m.group(1)
        fn_m = re.search(r'filename="([^"]*)"', disp)
        if fn_m is not None:
            files.append(
                {
                    "field": name,
                    "filename": fn_m.group(1),
                    "content": content,
                }
            )
        else:
            fields[name] = content.decode("utf-8", "replace")
    return fields, files


def save_upload(raw_name: str, content: bytes) -> dict:
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("file too large")
    stored = f"{uuid.uuid4().hex[:12]}-{safe_filename(raw_name)}"
    dest = uploads_dir() / stored
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    tmp.write_bytes(content)
    tmp.replace(dest)
    return {
        "name": original_filename(raw_name),
        "path": str(dest.resolve()),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "grok-bot-web/0.2"
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

    def _read_body(self) -> bytes | None:
        length = self.headers.get("Content-Length")
        try:
            n = int(length) if length else 0
        except ValueError:
            return None
        if n < 0 or n > MAX_BODY:
            return None
        return self.rfile.read(n) if n else b""

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
        if path == "/api/history":
            self._handle_history()
            return
        if path.startswith("/api/uploads/"):
            self._handle_upload_get(unquote(path[len("/api/uploads/") :]))
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
        raw = self._read_body()
        if raw is None:
            self._send_json(400, {"error": "body too large or invalid"})
            return
        ctype = (self.headers.get("Content-Type") or "").lower()
        text = ""
        incoming: list[tuple[str, bytes]] = []
        if "multipart/form-data" in ctype:
            fields, files = parse_multipart(self.headers.get("Content-Type") or "", raw)
            text = fields.get("text") or ""
            for item in files:
                incoming.append((item.get("filename") or "file", item.get("content") or b""))
        else:
            if not raw:
                payload: dict = {}
            else:
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send_json(400, {"error": "invalid json"})
                    return
                if not isinstance(data, dict):
                    self._send_json(400, {"error": "invalid json"})
                    return
                payload = data
            got = payload.get("text", "")
            if got is None:
                got = ""
            if not isinstance(got, str):
                self._send_json(400, {"error": "text must be a string"})
                return
            text = got
            files_spec = payload.get("files")
            if files_spec is None:
                files_spec = []
            if not isinstance(files_spec, list):
                self._send_json(400, {"error": "files must be a list"})
                return
            for spec in files_spec:
                if not isinstance(spec, dict):
                    self._send_json(400, {"error": "invalid file"})
                    return
                name = spec.get("name") or "file"
                if not isinstance(name, str):
                    self._send_json(400, {"error": "invalid file"})
                    return
                b64 = spec.get("content_b64")
                if not isinstance(b64, str) or not b64:
                    self._send_json(400, {"error": "file content_b64 required"})
                    return
                try:
                    content = base64.b64decode(b64, validate=False)
                except (ValueError, binascii.Error):
                    self._send_json(400, {"error": "invalid file encoding"})
                    return
                incoming.append((name, content))

        text = text.strip()
        if len(text) > MAX_TEXT:
            self._send_json(400, {"error": "text too long"})
            return
        if not text and not incoming:
            self._send_json(400, {"error": "text or file is required"})
            return
        if len(incoming) > MAX_FILES:
            self._send_json(400, {"error": "too many files"})
            return

        saved: list[dict] = []
        try:
            for name, content in incoming:
                if not content and not name:
                    continue
                saved.append(save_upload(name, content))
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except OSError:
            self._send_json(500, {"error": "could not store file"})
            return

        msg = {
            "id": uuid.uuid4().hex,
            "text": text,
            "ts": time.time(),
            "source": "web",
        }
        if saved:
            msg["files"] = [{"name": f["name"], "path": f["path"]} for f in saved]
        d = ensure_data_dir()
        atomic_write(d / "inbox.json", json.dumps(msg, indent=2) + "\n")
        atomic_write(d / "flag", "1\n")
        fire_on_send_hook(msg)
        out = {"id": msg["id"], "ts": msg["ts"]}
        if saved:
            out["files"] = [public_file(f) for f in saved]
        self._send_json(200, out)

    def _handle_reply(self, parsed) -> None:
        d = data_dir()
        reply = read_json(d / "reply.json")
        if reply:
            archive_reply(d, reply)
        q = parse_qs(parsed.query)
        req_id = (q.get("id") or [""])[0].strip()
        if not reply:
            self._send(204)
            return
        if req_id and reply.get("id") != req_id:
            self._send(204)
            return
        self._send_json(200, reply)

    def _handle_history(self) -> None:
        replies = load_reply_history(data_dir())
        self._send_json(200, {"replies": replies})

    def _handle_upload_get(self, name: str) -> None:
        safe = Path(name).name
        if not safe or safe != name or safe in {".", ".."}:
            self._send_json(404, {"error": "not found"})
            return
        folder = uploads_dir()
        path = (folder / safe).resolve()
        try:
            path.relative_to(folder.resolve())
        except ValueError:
            self._send_json(404, {"error": "not found"})
            return
        if not path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        data = path.read_bytes()
        ctype = IMAGE_TYPES.get(path.suffix.lower(), "application/octet-stream")
        self._send(200, data, ctype)

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
