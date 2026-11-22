# aidr-obs and Jev on vanishai

Two containers on the home server (`vanishai` below, the SSH alias), next to its native Ollama:

- `aidr-obs`: the dashboard in remote mode (`AIDR_OBS_REMOTE=1`). It never reads the Mac. The Mac's shipper (`aidr-obs ship`) pushes new capture and Noma log lines to `POST /api/push` with a bearer token and sends a heartbeat with `tap status`, which feeds the Setup and Health views.
- `jev`: the Jev review service from `jevai-dev-lab`, with both backends (AI Gateway and the host's Ollama at `host.docker.internal:11434`).

Jev is published on the server's `127.0.0.1` only. The dashboard defaults to the same, and the Mac shipper reaches it through an SSH tunnel. To serve the UI on the LAN, set `AIDR_OBS_BIND=0.0.0.0` and `AIDR_OBS_ALLOWED_HOSTS` to the name the browser uses. The tap on the Mac is unchanged and stays offline: it only appends to `~/.aidr-obs/capture.jsonl`.

```
Mac: tap -> capture.jsonl, ~/.noma/*.log -> aidr-obs ship -> ssh -L 8770 -> aidr-obs
LAN browser -> http://<allowed-host>:8770 -> aidr-obs ──http://jev:8790──> jev ──> Ollama / AI Gateway
```

## One-time setup on the server

1. **Ollama.** It runs natively as the `ollama` systemd service and must listen on `0.0.0.0:11434` so containers reach it through `host.docker.internal`:

   ```bash
   systemctl cat ollama | grep OLLAMA_HOST
   ollama list   # qwen3:8b
   ```

2. **Folder and files.** From an aidr-hook-observability checkout on the Mac:

   ```bash
   ssh vanishai 'mkdir -p ~/aidr-stack'
   scp deploy/docker-compose.yml deploy/backup.sh vanishai:aidr-stack/
   scp deploy/server.env.example vanishai:aidr-stack/.env
   ssh vanishai 'chmod 600 ~/aidr-stack/.env && chmod +x ~/aidr-stack/backup.sh'
   ```

   Fill in `.env` on the server: `AIDR_OBS_PUSH_TOKEN` and `JEV_API_TOKEN` (new values from `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`), and `AI_GATEWAY_API_KEY` if you use the gateway backend. Compose passes each container only the variables it needs, so the gateway key never reaches the dashboard.

3. **Pull access.** If the GHCR packages are private, log in once with a classic token that has only `read:packages`:

   ```bash
   echo <token> | docker login ghcr.io -u SeaBlooms --password-stdin
   ```

   Images are published from `main` (`:latest`, `:sha-…`) and `v*` tags. Before the first merge you can build them on the server instead: clone both repos, run `docker build -t aidr-obs:local .` and `docker build -t jev:local .`, and set `AIDR_OBS_IMAGE=aidr-obs:local` and `JEV_IMAGE=jev:local` in `.env`.

4. **Start.**

   ```bash
   cd ~/aidr-stack && docker compose pull && docker compose up -d
   docker compose ps                        # both healthy within a minute
   curl -fsS http://127.0.0.1:8770/api/setup
   curl -fsS http://127.0.0.1:8790/health   # backends: gateway, ollama
   ```

## Moving existing history in

On the Mac, stop the local `aidr-obs serve`, then copy the database (safe while readers are open):

```bash
sqlite3 ~/.aidr-obs/aidr.db ".backup /tmp/aidr.db"
scp /tmp/aidr.db vanishai:/tmp/
```

On the server:

```bash
cd ~/aidr-stack
docker compose stop aidr-obs
docker compose cp /tmp/aidr.db aidr-obs:/data/aidr.db
docker compose run --rm --no-deps --user root --entrypoint sh aidr-obs -c \
  'rm -f /data/aidr.db-wal /data/aidr.db-shm && chown 10001 /data/aidr.db && chmod 600 /data/aidr.db'
docker compose start aidr-obs
rm /tmp/aidr.db
```

Then install the shipper with `--from-end` (below) so lines already in the database are not pushed again. Ingest also skips duplicate captures by id, so a repeat is harmless, only slower.

## On the Mac

1. **SSH alias.** `~/.ssh/config` needs a `Host vanishai` entry with key auth that works without a prompt (the tunnel runs unattended).

2. **Tunnel.** Copy `deploy/mac/tunnel.plist.example` to `~/Library/LaunchAgents/com.seablooms.aidr-obs.tunnel.plist` and load it:

   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.seablooms.aidr-obs.tunnel.plist
   curl -fsS http://127.0.0.1:8770/health
   ```

3. **Shipper.** Copy `deploy/mac/ship.env.example` to `~/.aidr-obs/ship.env`, `chmod 600` it, and set `AIDR_OBS_PUSH_TOKEN` to the server's value. From the repo checkout (the LaunchAgent runs this checkout's `.venv` Python, so keep it in place):

   ```bash
   uv run aidr-obs ship once                # one pass, prints what it shipped
   uv run aidr-obs ship install --from-end  # LaunchAgent com.seablooms.aidr-obs.ship, every 15 s
   uv run aidr-obs ship status
   tail ~/.aidr-obs/ship.log
   ```

   Plain http is accepted only to loopback, so the shipper refuses to send captures across the LAN without the tunnel. If the tunnel or server is down, it retries; offsets are kept per file in `~/.aidr-obs/ship_state.json`, and the server answers a mismatched offset with `409 {expected}` so nothing is sent twice or skipped.

4. **Jev on the Mac (optional).** A local `jev serve` is no longer needed. If you keep one, point its `OLLAMA_BASE_URL` at the server's Ollama (`http://<server>:11434/v1`).

## Another Mac

The dashboard stays on this server. Another Mac only needs the tap, the tunnel, and the shipper. It does not run the dashboard or Jev.

1. Clone this repo and leave the checkout where it is. `uv sync`, then `uv run aidr-obs tap install` (Noma hooks already installed, with `NOMA_DEBUG=1`).
2. Add an SSH alias `vanishai` that logs in with a key and no prompt. Copy `deploy/mac/tunnel.plist.example` to `~/Library/LaunchAgents/` and bootstrap it, the same way as the first Mac. `curl -fsS http://127.0.0.1:8770/health` should print `{"ok":true}`.
3. Copy `~/.aidr-obs/ship.env` from a Mac that already ships, or start from `deploy/mac/ship.env.example`. The push token is the server's `AIDR_OBS_PUSH_TOKEN`. `chmod 600` the file. Leave `AIDR_OBS_SHIP_DEVICE` unset unless this Mac's hostname matches another shipper; the server uses that name to keep each Mac's byte offset separate.
4. From the checkout: `uv run aidr-obs ship install`. Omit `--from-end` so this Mac's existing capture and debug logs are pushed once. Use `--from-end` only when those lines are already in the server database and you do not want them sent again.

Opening the UI on that Mac needs no install: the LAN URL and the shared password. The shipper still uses the tunnel, not the LAN URL.

## Updating

```bash
cd ~/aidr-stack && docker compose pull && docker compose up -d
```

Pin images with `AIDR_OBS_IMAGE=ghcr.io/seablooms/aidr-obs:sha-…` and `JEV_IMAGE=…` in `.env`.

## Backups and pruning

`backup.sh` writes a consistent copy of `aidr.db` to `/data/backups/` inside the volume and keeps 14 days. Cron on the server:

```cron
15 3 * * * $HOME/aidr-stack/backup.sh
30 3 * * * cd $HOME/aidr-stack && docker compose exec -T aidr-obs aidr-obs prune --days 90
```

Copy backups off the box with `docker compose cp aidr-obs:/data/backups ./backups`.

## Security

- Jev listens on the server's loopback only. The dashboard does too, unless `AIDR_OBS_BIND=0.0.0.0`. There is no TLS and the UI has no login, so a LAN bind lets anyone on the LAN read captured prompts. Do not forward the port to the internet.
- The dashboard checks the `Host` header (`127.0.0.1:8770`, `localhost:8770`, or a name in `AIDR_OBS_ALLOWED_HOSTS`). Set `AIDR_OBS_UI_PASSWORD` whenever that allowlist is non-empty: the server will not start without it, and the browser must sign in. The cookie is `HttpOnly` and `SameSite=Strict`. It is not marked `Secure`, because the LAN URL is plain HTTP. Every push needs `X-Aidr-Obs: 1` and the bearer token; without `AIDR_OBS_PUSH_TOKEN` the push routes answer 403. The shipper still uses the tunnel, because it refuses plain http off loopback.
- The dashboard calls Jev only on loopback or on names in `AIDR_OBS_JEV_HOSTS` (here `jev`, the compose service); IP literals there are ignored.
- Both containers run as UID 10001. `.env` holds the only secrets: keep it `chmod 600` and out of git.
