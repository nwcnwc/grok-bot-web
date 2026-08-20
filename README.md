# grok-bot-web

A tiny, Tailscale-gated web chat UI for a Grok Bot-style teammate.

Official Grok Bot is desktop and iOS only. This is a first public web UI aimed at that product: one column, a message list, and a send box. Others can contribute.

This project is **not affiliated with xAI or Cursor**. It does **not** call xAI or Cursor APIs. The browser talks only to this local server. The server only writes and reads files so your own Grok Bot / agent can answer.

The page keeps the transcript in `localStorage` for this origin until you hit Clear. Clear wipes only that local transcript. It does not touch the agent's conversation. Replies that land while the tab is closed are stored in `DATA_DIR/replies.jsonl` and merged back in on reload (and while the page is open).

## How it works

```
phone / laptop on your tailnet
        |
        |  HTTP (Tailscale only)
        v
   server.py  -->  DATA_DIR/inbox.json + flag=1
                        |   (+ DATA_DIR/uploads/ when a file is attached)
                        v
              your Grok Bot / agent watcher
                        |
                        v
                   DATA_DIR/reply.json
                   DATA_DIR/replies.jsonl
                        |
                        v
              GET /api/reply  and  GET /api/history  -->  chat UI
```

1. The web UI `POST`s text and optional files to `/api/send`.
2. The server writes `DATA_DIR/inbox.json` as `{id, text, ts, source: "web", files?}` and sets `DATA_DIR/flag` to `1`. Attached files land under `DATA_DIR/uploads/` and are listed in `inbox.json` with `path` + `name`.
3. Your Grok Bot / agent watches the flag (same idea as `watch.sh`), reads the inbox (and any upload paths), and writes a reply.
4. The UI polls `GET /api/reply` / `GET /api/reply?id=<inbox-id>` and `GET /api/history` so a reply is picked up even if the tab was closed when it landed.

Use `reply.py` if you want a one-liner that writes `reply.json`, appends `replies.jsonl`, and clears the flag.

## Run (localhost)

Python 3.10+ (stdlib only). No pip packages.

```bash
python3 server.py
```

Then open http://127.0.0.1:8780

Defaults:

| setting | default | override |
| --- | --- | --- |
| bind host | `127.0.0.1` | `GROK_BOT_WEB_HOST` or `TAILSCALE_IP` |
| port | `8780` | `GROK_BOT_WEB_PORT` or `PORT` |
| data dir | `./data` | `DATA_DIR` |
| on-send hook | unset | `GROK_BOT_WEB_ON_SEND` |

**Do not bind `0.0.0.0` (or `::`) to the public internet. Tailscale only.** The server refuses those bind addresses.

If `GROK_BOT_WEB_ON_SEND` is set, `/api/send` runs that local command in the background after writing `inbox.json` and `flag`. The command gets `GROK_BOT_WEB_INBOX_ID`. Use it to wake a watcher on the same machine. Keep tokens out of this repo.

## Run behind Tailscale

Keep this UI off the public internet. Bind only a Tailscale address (or localhost and reach it through Tailscale Serve / a tailnet-only path you already trust).

```bash
export TAILSCALE_IP="$(tailscale ip -4)"
python3 server.py
```

Or:

```bash
export GROK_BOT_WEB_HOST="$(tailscale ip -4)"
python3 server.py
```

Open `http://<that-tailscale-ip>:8780` from another device on the same tailnet. Do not port-forward this process to the public internet. Do not put `0.0.0.0` in `GROK_BOT_WEB_HOST`.

## Protocol

Compatible with a file drop pipe.

### `POST /api/send`

JSON body: `{ "text": "hello" }`

Or `multipart/form-data` with a `text` field and one or more `files`. Text-only JSON still works. Files-only send is allowed.

Writes `DATA_DIR/inbox.json`:

```json
{
  "id": "…",
  "text": "hello",
  "ts": 1710000000.0,
  "source": "web",
  "files": [
    {
      "name": "notes.txt",
      "path": "/abs/data/uploads/ab12cd34ef56-notes.txt"
    }
  ]
}
```

`files` is omitted when nothing was attached. Each stored file is under `DATA_DIR/uploads/`. Original filename is sanitized; `path` is the absolute path on disk so a local agent can read it.

Also writes `DATA_DIR/flag` containing `1`.

Response: `{ "id": "…", "ts": …, "files": [ … ] }`

Limits: 32k text, 5 files, 8 MiB per file, 16 MiB request body.

### `GET /api/reply` and `GET /api/reply?id=…`

Reads `DATA_DIR/reply.json` once and archives it into `DATA_DIR/replies.jsonl`.

- `200` and the JSON object when the file exists (and `id` matches, if given)
- `204` when the file is missing or the requested `id` does not match

Expected `reply.json`:

```json
{
  "id": "same-as-inbox",
  "text": "hello back",
  "ts": 1710000000.1
}
```

### `GET /api/history`

`{ "replies": [ … ] }` — archived replies, oldest first. Used to merge missed replies into a restored transcript.

### `GET /api/uploads/<stored-name>`

Serves one file from `DATA_DIR/uploads/` so the transcript can show images. Names are basename-only.

### `GET /api/health`

`{ "ok": true, "service": "grok-bot-web" }`

## Agent helpers

Your watcher should: see `flag == 1`, read `inbox.json`, do the work, write `reply.json` with the same `id`, then set `flag` to `0`. Prefer `reply.py` so the reply is also appended to `replies.jsonl`.

```bash
# exits 0 only when data/flag is 1
./watch.sh

# write a reply for the current inbox id
python3 reply.py "hello from the agent"
```

`reply.py` also accepts `--id` and `--text`.

## Windows Grok Bot (broken webhook card)

Clone and run this tree on the Grok Bot machine — the Linux computer the agent uses — not inside Program Files.

Python 3.10+, stdlib only. Bind Tailscale IPv4 or `127.0.0.1`. Never `0.0.0.0`. Port `8780`.

```bash
export TAILSCALE_IP="$(tailscale ip -4)"
python3 server.py
```

On current Windows Grok Bot, opening a webhook routine card crashes with `TypeError: Cannot read properties of undefined (reading 'platform')`. That UI never got a webhook URL from Cursor. Do not try to copy one from the card.

Wake does not need that card. It is local, the same path as Test run:

1. Make a webhook routine (name it e.g. `grok-bot-web inbound`). The prompt should read `DATA_DIR/inbox.json` (`id`, `text`, `ts`, `source`). If the flag is `0`, or that `id` already has an answer in `reply.json`, stay silent. Otherwise treat the line as a normal chat message and write back with `python3 reply.py --id <inbox-id> --text "<reply>"`. The page polls for about 120s.
2. Point `GROK_BOT_WEB_ON_SEND` at a local script that POSTs `http://127.0.0.1:1340/api/runAgentAutomationNow` with `{id: <this agent id>, automationId: <that routine id>}` and the local gateway bearer from `gateway.json`. Do not print the token. Do not commit secrets.
3. Send one line from `http://<tailscale-ip>:8780`. If a reply lands, wake works even while the Windows card still crashes.

An asar patch can hide the card crash. It is optional and **not** required for wake. Do not add an asar to this repo.

## License

MIT. Copyright (c) 2026 grok-bot-web contributors. See [LICENSE](LICENSE).

## Contribute

Fork, make the change, open a pull request. We'll review it.

1. Keep the server stdlib-only and the UI one column / mobile-friendly.
2. Do not add xAI or Cursor API calls. This UI is a drop pipe, not a hosted model client.
3. Do not bind `0.0.0.0` by default. Do not commit `data/`, `.env`, or personal identifiers (names, emails, phones, chat handles, workspace names, Tailscale IPs).

Small, reviewable patches are welcome: protocol hardening, accessibility, a clearer mobile layout, or a better watcher example.
