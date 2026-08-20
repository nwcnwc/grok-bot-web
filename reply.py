#!/usr/bin/env python3
"""Write data/reply.json for a pending inbox message.

Usage:
  python3 reply.py "hello from the agent"
  python3 reply.py --id <inbox-id> --text "hello"

Reads DATA_DIR/inbox.json unless --id is given. Clears flag to 0 after writing.
Also appends the reply to DATA_DIR/replies.jsonl so a closed tab can pick it up.
This script does not call xAI or Cursor APIs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).expanduser().resolve()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def archive_reply(d: Path, reply: dict) -> None:
    rid = reply.get("id") if isinstance(reply, dict) else None
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


def load_inbox_id() -> str:
    path = data_dir() / "inbox.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"cannot read inbox.json: {exc}")
    msg_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(msg_id, str) or not msg_id:
        sys.exit("inbox.json has no id")
    return msg_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Write reply.json for grok-bot-web")
    parser.add_argument("message", nargs="?", help="reply text")
    parser.add_argument("--id", dest="msg_id", help="inbox id to match (default: inbox.json)")
    parser.add_argument("--text", dest="text", help="reply text (alternative to positional)")
    args = parser.parse_args()
    text = args.text if args.text is not None else args.message
    if not text:
        parser.error("reply text is required")
    msg_id = args.msg_id or load_inbox_id()
    reply = {"id": msg_id, "text": text, "ts": time.time()}
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    atomic_write(d / "reply.json", json.dumps(reply, indent=2) + "\n")
    archive_reply(d, reply)
    atomic_write(d / "flag", "0\n")
    print(json.dumps({"wrote": str(d / "reply.json"), "id": msg_id}, separators=(",", ":")))


if __name__ == "__main__":
    main()
