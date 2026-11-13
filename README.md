# aidr-hook-observability

A local-only observability dashboard for AI-agent guardrail (AIDR) hooks in Claude Code
and Cursor. It currently works with the [Noma](https://noma.security) guardrails hooks. It reads the hooks' debug logs and records what each hook
received and what it decided, then shows it all on `http://127.0.0.1:8770/`.

Nothing leaves the machine: the dashboard binds to loopback, loads no CDN assets,
and the capture tap makes no network calls. The Noma plugin code is never modified.

This is an independent project. It is not affiliated with, endorsed by or supported by
Noma Security. "Noma" refers only to the guardrails plugins this tool observes
([Noma-Security/noma-marketplace](https://github.com/Noma-Security/noma-marketplace)).

## Status

- Works with Claude Code and Cursor. The capture tap sits in front of the installed Noma hooks
  and records each event locally; with `NOMA_DRYRUN=1` nothing is sent to Noma.
- Sidebar views: overview, activity, sessions, graph, verdicts, sensitive access, destinations,
  hook health, MCP inventory, and usage. Activity is one row per agent action; the hook events
  behind it expand on the row.
- Destinations are hosts and MCP servers parsed from what the agent asked to do (URLs, shell
  commands, MCP names). The hooks do not see network traffic or DNS, so there are no resolved IPs.
- Token counts on Cursor responses are kept; string secrets under similar key names are still
  redacted. Captures written before that fix may show masked counts and are left out of totals.
- The HTTP server does not reload code. After updating the checkout, stop it and run
  `aidr-obs serve` again.
- Tested on macOS with Python 3.12+, Claude Code and Cursor.

## How it works

- **Debug logs.** With `NOMA_DEBUG=1`, each Noma hook writes `~/.noma/<agent>-guardrails-debug.log`.
  Grouping lines by process id rebuilds one *run* per hook invocation: event, key source,
  MCP discovery, and the POST outcome (ok, dry-run, failed, no key, ...).
- **Capture tap.** The log never holds payloads or verdicts, so `src/aidr_obs/tap.py` sits in
  front of the Noma hook. It runs the hook unchanged as a child process, relays its stdout,
  stderr and exit code, then appends one redacted record to `~/.aidr-obs/capture.jsonl`.
- **Store and UI.** `aidr-obs ingest` loads both into `~/.aidr-obs/aidr.db` (SQLite,
  incremental, rotation-safe). `aidr-obs serve` is a loopback dashboard: an attention overview,
  one row per agent action, sessions, verdicts, destinations, sensitive access, hook health,
  MCP inventory, usage, and the entity graph. Live mode watches the log and capture files and
  pushes new rows over `/api/stream`; paused is the default. Known log noise (the optional
  `remote-settings.json` miss, and anything you add in `~/.aidr-obs/known_noise.json`) stays out
  of the health status.
- **Insights from captures** (`src/aidr_obs/insights.py`). Hook events are grouped into agent
  actions by `tool_use_id`; one Cursor shell command fires preToolUse, beforeShellExecution and
  postToolUse. The dashboard derives four more tabs from those actions:
  - *Destinations*: hosts from URLs, shell commands (curl, wget, git, ssh, ...) and MCP server
    names, classed as domain, public IP, private IP, loopback or MCP. These are the targets the
    agent asked for. The hooks don't see network traffic or DNS.
  - *Sessions*: one row per Claude Code session or Cursor conversation, and a timeline of
    prompt, tool calls (with verdict, run time, sensitive and destination tags) and response.
  - *Sensitive access*: reads, edits and commands touching SSH keys, `.env` files, cloud and
    registry credentials, keychains and similar files, plus Cursor shell commands run outside
    the sandbox and Claude Code permission modes.
  - *Usage & performance*: Cursor token counts per model (Claude Code hooks carry none), tool
    run time reported by the agent, hook latency per event, and activity per project.
- **Entity graph** (`src/aidr_obs/graph.py`, Graph tab). The same actions as a navigable graph:
  agent → project → session → tool → destination, MCP server or sensitive category. Nodes and edges
  carry action counts and their worst verdict, which sets the edge colour, and blocked or flagged
  entities are kept first when the graph is capped (`/api/graph?max_nodes=`, default 300). Click a
  node to see its connections, the sessions that reached it and recent actions. Tools are shared
  across sessions, so a destination node lists which sessions reached it. Rendered with Cytoscape.js,
  vendored in `static/vendor/` (see its README for the version and checksum).
  - *Focus* (`/api/graph?focus=<node id>`, e.g. `dest:github.com`, `session:<agent>|<id>`,
    `sensitive:env file`): only the actions that involve that node, so focusing on a host shows the
    sessions and tools that led to it rather than everything that shares a tool. Counts stay global;
    `in_focus` says how many involve the focus. The URL `#/graph?focus=<id>` links straight to it, and
    Destinations, Sessions, Sensitive access and the session page link to the graph.
  - *Expand*: double-click a node, or use Expand in the side panel, to add every entity its
    actions touch. Expansions survive auto-refresh until the focus changes.
  - *Filters*: node-type chips and a verdict filter (all, flagged, blocked). The verdict filter keeps
    whole paths, since every link an action creates carries that action's verdict.

## Dashboard

The sidebar is grouped: Review (Overview, Activity, Sessions, Graph), Risk (Verdicts, Sensitive
access, Destinations), Operations (Hook health, MCP inventory, Usage). Under 1100px it collapses
to icons; under 700px it becomes a menu.

| View | Shows |
| --- | --- |
| Overview | what needs a look (flagged, sensitive, destinations, unsandboxed, hook health), the latest actions, and a chart by action type |
| Activity | one row per agent action (command, path, prompt). Expand a row for the hook events behind it, or switch to raw events. Filters and a detail drawer (payload, hook stdout, log lines) |
| Sessions | per-session counts; open one for the prompt → tools → response timeline |
| Graph | the entity graph described above |
| Verdicts | decisions that did something (block, ask, mask, context) and verdicts per tool |
| Sensitive access | credential and key files touched, unsandboxed Cursor shell commands, permission modes |
| Destinations | hosts and MCP servers the agent asked to reach |
| Hook health | failures, timeouts, a setup checklist, and the noise list |
| MCP inventory | whether each source has servers (`n / m`), the servers the agent actually called, and identical snapshots collapsed to "unchanged since" |
| Usage | Cursor tokens per model, optional per-model prices you type in (off by default, kept in this browser), tool run time, hook latency, activity per project |

The time range (1 hour, 24 hours, 7 days, 30 days, all time, since your last visit, or a custom
from/to) is in the URL, `#/<view>?range=24h`, so a view survives reload and the Back button.
Refresh ingests new log lines and captures. Live does that when the files change. Search (`/` or
Ctrl/⌘ K) jumps to a session, action, host, file or tool. In dry-run, verdict pills that only say
`dry_run` are hidden: the banner says decisions were recorded, not enforced.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+. No third-party Python packages:
the dashboard and the tap use only the standard library. The hook entries run the tap with
`uv run --no-project`, so it starts without the project environment. Tested on macOS with
Claude Code and Cursor.

```bash
uv sync
uv run aidr-obs tap install          # both agents; --agent claude-code|cursor for one
uv run aidr-obs tap status
uv run aidr-obs serve                # ingests, then serves http://127.0.0.1:8770/
```

`tap install --cursor-hook PATH` points the Cursor tap at a specific Noma `hook.py`.
`serve --port N` picks another port, and `serve --no-ingest` skips the startup ingest.

What `tap install` changes:

| Agent | File | Change |
| --- | --- | --- |
| Claude Code | `~/.claude/settings.json` | adds 5 tap hooks, disables `guardrails@noma` so hooks don't run twice |
| Cursor | `~/.cursor/hooks.json` | adds 8 tap hooks (the events the Noma Cursor plugin registers) |
| both | `~/.aidr-obs/config.json` | Cursor's hook path and env |

- Before writing anything, each exact hook command is run once through `/bin/sh` with a sample
  event. It must exit 0 with no output, or nothing is installed for that agent.
- Every entry is one command string. Cursor also runs the hooks in `~/.claude/settings.json`
  and ignores `args`, so a split command would run a bare `uv` there and block every tool call.
- Claude Code runs the installed plugin's `hook.py` from `~/.claude/plugins/cache/noma/`.
  For Cursor, the hook is found in this order: `--cursor-hook PATH`, the hook a previous
  install used, then the newest Noma plugin under `~/.cursor/plugins/`. If none is found,
  install stops and asks for `--cursor-hook`. To run the hook straight from a
  noma-marketplace checkout, pass `--cursor-hook <checkout>/cursor-guardrails/scripts/hook.py`.
  The tap sets `PYTHONDONTWRITEBYTECODE=1` so the checkout stays clean.
- Cursor hooks don't see Claude Code's `settings.json` env, so `NOMA_DEBUG`, `NOMA_DRYRUN` and
  `NOMA_API_URL` are copied into the Cursor config. `NOMA_API_KEY` is copied only in dry-run,
  where it never leaves the machine. For live mode, store the key in the macOS Keychain
  (`security add-generic-password -s noma-guardrails -a "$USER" -w`).
- Existing hooks in both files are kept. Re-running `install` doesn't duplicate entries.

## Tap behavior

- **Fail open.** If the hook is missing, crashes or runs past 23 seconds, the tap prints nothing
  and exits 0, which leaves the agent's default behavior. A capture failure never changes what was
  relayed.
- **Dry-run.** With `NOMA_DRYRUN` set, the hook prints the payload it would have sent instead of a
  decision. The tap records it but doesn't relay it, so the agent never reads a payload as a
  verdict. Set `"relay_dry_run_stdout": true` in `~/.aidr-obs/config.json` to relay it anyway.
- **Cursor events in the Claude Code tap.** Cursor stamps the Claude-format events it sends with
  `cursor_version`. The Noma Claude hook skips those, so the claude-code tap passes them straight
  through and records nothing; the cursor tap reports them.
- **Redaction.** Captured stdin and stdout are masked by `redact.py` (tokens, keys, JWTs, PEM
  blocks, `name=secret` pairs, URL passwords) and long fields are truncated. Under key names like
  `token` or `api_key` only string values are masked, and digit-only strings under `*_tokens`
  keys are kept, so Cursor's `input_tokens`/`output_tokens` counts survive. `capture.jsonl`,
  `aidr.db` and backups are mode 0600 in a 0700 directory. They still hold prompts and tool
  output, so treat them as sensitive.
- **Verdicts** are classified from either decision format: Claude Code
  (`permissionDecision`, `decision: block`, `updatedInput`, ...) or Cursor
  (`permission: deny|ask|allow`, `continue: false`, `updated_*`).

## Uninstall and recovery

```bash
uv run aidr-obs tap uninstall        # --agent claude-code|cursor for one
```

This removes only the tap's entries and re-enables the Claude plugin if it was enabled before.
If nothing else changed since install, it puts the original file bytes back. A
`~/.cursor/hooks.json` the tap created is deleted.

If a hook ever blocks every tool call, run the uninstall from a plain terminal. Without the
dashboard, restore the backup in `~/.aidr-obs/backups/` (named in
`~/.aidr-obs/tap-state.json`) over `~/.claude/settings.json`, and delete `~/.cursor/hooks.json`.

## Other commands

```bash
uv run aidr-obs ingest               # load new log lines and captures
uv run aidr-obs prune --days 14      # drop older captures and stored log lines (never touches ~/.noma)
```

`prune` rewrites `capture.jsonl`; a record appended by a running hook at that exact moment can
be lost.

## HTTP API

The UI is a single static page over a JSON API on the same loopback port. Every `GET` takes
`since=<epoch ms>` (0 for all time) and an optional `until=<epoch ms>`.

| Endpoint | Returns |
| --- | --- |
| `/api/overview` | attention counts, the latest actions, and the action-type chart |
| `/api/actions` | one row per agent action (`offset`, filters, `q`) |
| `/api/search` | sessions, actions, hosts, files, tools and MCP servers for a query |
| `/api/summary` | hook-run counts and a time series |
| `/api/verdicts` | verdict breakdowns |
| `/api/events` | paged runs and captures (`offset`, filters, `q` search) |
| `/api/event` | one run and/or capture (`run_id`, `capture_id`) with its log lines |
| `/api/errors` | errors, health, and which groups match the noise list |
| `/api/noise` | the noise rules (the built-in list, or `~/.aidr-obs/known_noise.json` once you edit it) |
| `/api/setup` | tap, debug-log and capture checks for the setup checklist |
| `/api/mcp` | MCP inventory, collapsed when nothing changed |
| `/api/destinations`, `/api/sensitive`, `/api/sessions`, `/api/usage` | the insights views |
| `/api/session` | one session's timeline (`key=<agent>\|<session id>`) |
| `/api/graph` | entity graph (`focus`, `max_nodes`) |
| `/api/stream` | server-sent events: a hello, then ingest counts when the log or capture file changes |
| `POST /api/ingest` | load new data; needs header `X-Aidr-Obs: 1` |
| `POST /api/noise` | replace the noise rules (`{"rules":[...]}`); same header |

Requests whose `Host` isn't `127.0.0.1:<port>` or `localhost:<port>` are refused, which blocks
DNS rebinding. Responses send `Content-Security-Policy: script-src 'self'` (no inline scripts).
Styles may be inline, because bar widths are set on the element.

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run python scripts/ui_smoke.py   # headless Chrome; skips itself when Chrome is absent
```

Paths are overridable for tests: `AIDR_OBS_HOME`, `AIDR_OBS_LOG_DIR`, `AIDR_OBS_CLAUDE_DIR`,
`AIDR_OBS_CURSOR_DIR`, `AIDR_OBS_HOOK`, `AIDR_OBS_CHILD_TIMEOUT`.

## License

MIT, see [LICENSE](LICENSE). The bundled Cytoscape.js, Preact and htm are MIT too; see
[`src/aidr_obs/static/vendor/README.md`](src/aidr_obs/static/vendor/README.md).
