# grok-bot-web

A tiny, Tailscale-gated web chat UI for a Grok Bot-style teammate.

Official Grok Bot is desktop and iOS only. This is a first public web UI aimed at that product: one column, a message list, and a send box. Others can contribute.

This project is **not affiliated with xAI or Cursor**. It does **not** call xAI or Cursor APIs. The browser talks only to this local server. The server only writes and reads files so your own Grok Bot / agent can answer.

## How it works

```
phone / laptop on your tailnet
        |
        |  HTTP (Tailscale only)
        v
   server.py  -->  DATA_DIR/inbox.json + flag=1
                        |
                        v
              your Grok Bot / agent watcher
                        |
                        v
                   DATA_DIR/reply.json
                        |
                        v
              GET /api/reply?id=...  -->  chat UI
```

1. The web UI `POST`s `{ "text": "..." }` to `/api/send`.
2. The server writes `DATA_DIR/inbox.json` as `{id, text, ts, source: "web"}` and sets `DATA_DIR/flag` to `1`.
3. Your Grok Bot / agent watches the flag (same idea as `watch.sh`), reads the inbox, and writes a reply.
4. The UI polls `GET /api/reply?id=<inbox-id>` until `reply.json` has that `id` (HTTP 200) or the server has nothing yet (HTTP 204).

Use `reply.py` if you want a one-liner that writes `reply.json` and clears the flag.

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

**Do not bind `0.0.0.0` (or `::`) to the public internet. Tailscale only.** The server refuses those bind addresses.

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

Body: `{ "text": "hello" }`

Writes `DATA_DIR/inbox.json`:

```json
{
  "id": "…",
  "text": "hello",
  "ts": 1710000000.0,
  "source": "web"
}
```

and `DATA_DIR/flag` containing `1`.

Response: `{ "id": "…", "ts": … }`

### `GET /api/reply?id=…`

Reads `DATA_DIR/reply.json` once.

- `200` and the JSON object when `id` matches
- `204` when the file is missing or the id does not match

Expected `reply.json`:

```json
{
  "id": "same-as-inbox",
  "text": "hello back",
  "ts": 1710000000.1
}
```

### `GET /api/health`

`{ "ok": true, "service": "grok-bot-web" }`

## Agent helpers

Your watcher should: see `flag == 1`, read `inbox.json`, do the work, write `reply.json` with the same `id`, then set `flag` to `0`.

```bash
# exits 0 only when data/flag is 1
./watch.sh

# write a reply for the current inbox id
python3 reply.py "hello from the agent"
```

`reply.py` also accepts `--id` and `--text`.

## License

MIT. Copyright (c) 2026 grok-bot-web contributors. See [LICENSE](LICENSE).

## Contribute

Fork, make the change, open a pull request. We'll review it.

1. Keep the server stdlib-only and the UI one column / mobile-friendly.
2. Do not add xAI or Cursor API calls. This UI is a drop pipe, not a hosted model client.
3. Do not bind `0.0.0.0` by default. Do not commit `data/`, `.env`, or personal identifiers (names, emails, phones, chat handles, workspace names, Tailscale IPs).

Small, reviewable patches are welcome: protocol hardening, accessibility, a clearer mobile layout, or a better watcher example.
