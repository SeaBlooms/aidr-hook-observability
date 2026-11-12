"""Parse Noma guardrails debug logs into lines and per-invocation runs.

Line format (set by the plugin's common/debug.py):

    2026-06-29 14:23:01,123 ERROR pid=1234 [hook.py:60] message

Every hook invocation is its own process, so the lines of one pid that sit close
together in time make up one run. The log never holds payloads or verdicts; those
come from the capture tap.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) ([A-Z]+) pid=(\d+) \[([^\]]*)\] (.*)$")
LOG_NAME_RE = re.compile(r"^(?:(.+)-)?guardrails-debug\.log(?:\.(\d+))?$")
# Longer than one hook can live (25s manifest timeout), shorter than typical pid reuse.
RUN_GAP_MS = 30_000

_EVENT_RE = re.compile(r"^event (\S+?)(?:;| from )")
_STDIN_RE = re.compile(r"^read (\d+) bytes from stdin")
_MCP_RE = re.compile(r"^MCP inventory: (\d+) artifact")
_DISCOVERY_RE = re.compile(r"^discovery: ([^/ ]+)/(\S+) (.+) -> (has servers|empty)$")
_POST_RE = re.compile(r"^POST (\d+) bytes to (\S+) via (\S+)$")
_DRYRUN_RE = re.compile(r"^DRY-RUN would POST to (\S+) \((\d+) bytes\)")
_POST_OK_RE = re.compile(r"^POST ok (\S+) \(HTTP (\d+)\)")


@dataclass
class LogLine:
    ts: str  # ISO, local time as written by the hook
    ts_ms: int
    level: str
    pid: int
    where: str
    message: str
    raw: str


@dataclass
class Run:
    agent: str
    pid: int
    start_ms: int
    end_ms: int
    start_ts: str
    lines: list = field(default_factory=list)
    event: str | None = None
    stdin_bytes: int | None = None
    key_source: str | None = None
    installed_config: str | None = None
    mcp_artifacts: int | None = None
    discovery: list = field(default_factory=list)
    project_root: str | None = None
    outcome: str = "unknown"
    http_status: int | None = None
    url: str | None = None
    post_bytes: int | None = None
    sender: str | None = None
    retries: int = 0
    fallbacks: int = 0
    error_count: int = 0
    hook_file: str | None = None

    @property
    def run_id(self) -> str:
        return self.agent + ":" + str(self.pid) + ":" + str(self.start_ms)

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def agent_for_log(name: str) -> str | None:
    """'claude-code-guardrails-debug.log.1' -> 'claude-code'; None if not a Noma log."""
    m = LOG_NAME_RE.match(name)
    if not m:
        return None
    return m.group(1) or "unknown"


def discover_log_files(log_dir: Path) -> list[tuple[Path, str]]:
    """Every Noma debug log in log_dir, rotated ones first so lines arrive oldest-first."""
    found = []
    if not log_dir.is_dir():
        return found
    for path in log_dir.iterdir():
        agent = agent_for_log(path.name)
        if agent is None or not path.is_file():
            continue
        m = LOG_NAME_RE.match(path.name)
        rotation = int(m.group(2)) if m and m.group(2) else 0
        found.append((rotation, path, agent))
    found.sort(key=lambda t: (t[2], -t[0]))
    return [(path, agent) for _, path, agent in found]


def _to_ms(ts: str) -> int:
    return int(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S,%f").timestamp() * 1000)


def parse_lines(text: str) -> list[LogLine]:
    """Parse log text. A line that doesn't match the format continues the previous message."""
    out: list[LogLine] = []
    for raw in text.splitlines():
        m = LINE_RE.match(raw)
        if m:
            ts, level, pid, where, message = m.groups()
            out.append(
                LogLine(
                    ts=ts.replace(" ", "T").replace(",", "."),
                    ts_ms=_to_ms(ts),
                    level=level,
                    pid=int(pid),
                    where=where,
                    message=message,
                    raw=raw,
                )
            )
        elif out and raw.strip():
            out[-1].message += "\n" + raw
            out[-1].raw += "\n" + raw
    return out


def group_runs(agent: str, lines: list[LogLine]) -> list[Run]:
    """Group one agent's lines into runs: same pid, no gap longer than RUN_GAP_MS."""
    by_pid: dict[int, list[LogLine]] = {}
    for line in lines:
        by_pid.setdefault(line.pid, []).append(line)
    runs: list[Run] = []
    for pid, pid_lines in by_pid.items():
        pid_lines.sort(key=lambda ln: ln.ts_ms)
        current: Run | None = None
        for line in pid_lines:
            if current is None or line.ts_ms - current.end_ms > RUN_GAP_MS:
                current = Run(agent=agent, pid=pid, start_ms=line.ts_ms, end_ms=line.ts_ms, start_ts=line.ts)
                runs.append(current)
            current.end_ms = line.ts_ms
            current.lines.append(line)
    for run in runs:
        classify(run)
    runs.sort(key=lambda r: r.start_ms)
    return runs


def _key_source(msg: str) -> str | None:
    if msg.startswith("API key resolved from NOMA_API_KEY"):
        return "env"
    if msg.startswith("endpoint token resolved"):
        return "endpoint_token"
    if msg.startswith("credential: MDM certificate") or msg.startswith("MDM ingestion certificate"):
        return "mdm_certificate"
    if msg == "credential store returned a key":
        return "credential_store"
    return None


def classify(run: Run) -> None:
    """Fill a run's summary fields from its lines."""
    outcome = None
    posted = False
    for line in run.lines:
        msg = line.message
        if line.level == "ERROR":
            run.error_count += 1
        if run.hook_file is None and line.where.split(":")[0].endswith("hook.py"):
            run.hook_file = line.where.split(":")[0]

        source = _key_source(msg)
        if source and run.key_source is None:
            run.key_source = source
        if msg.startswith("no installed config at"):
            run.installed_config = "none"
        elif msg.startswith("installed config ") and " applied " in msg:
            run.installed_config = "applied"
        elif msg.startswith("installed config ") and " ignored" in msg and run.installed_config is None:
            run.installed_config = "ignored"

        if m := _EVENT_RE.match(msg):
            run.event = m.group(1)
        if m := _STDIN_RE.match(msg):
            run.stdin_bytes = int(m.group(1))
        if m := _MCP_RE.match(msg):
            run.mcp_artifacts = int(m.group(1))
        if m := _DISCOVERY_RE.match(msg):
            run.discovery.append(
                {
                    "scope": m.group(1),
                    "kind": m.group(2),
                    "path": m.group(3),
                    "has_servers": m.group(4) == "has servers",
                }
            )
        if msg.startswith("discovery: project root "):
            run.project_root = msg[len("discovery: project root ") :].split(" (cwd ")[0]

        if "nothing to send" in msg:
            if "no API key" in msg or "no ingestion key" in msg:
                outcome = "no_key"
                run.key_source = run.key_source or "none"
            else:
                outcome = "nothing_sent"
        elif msg.endswith("from Cursor; skipped"):
            outcome = "skipped"
        elif m := _DRYRUN_RE.match(msg):
            outcome = "dry_run"
            run.url, run.post_bytes = m.group(1), int(m.group(2))
        elif m := _POST_RE.match(msg):
            posted = True
            run.post_bytes, run.url, run.sender = int(m.group(1)), m.group(2), m.group(3)
        elif m := _POST_OK_RE.match(msg):
            outcome = "ok"
            run.http_status = int(m.group(2))
        elif msg.startswith("POST failed "):
            outcome = "failed"
        elif msg.startswith("POST to ") and "no time left" in msg:
            outcome = "budget_exhausted"
        elif msg.startswith("POST attempt via ") and "(retrying)" in msg:
            run.retries += 1
        elif msg.startswith("POST via ") and "never sent" in msg:
            run.fallbacks += 1
    if outcome is None:
        outcome = "incomplete" if posted else "unknown"
    run.outcome = outcome


_SIG_SUBS = [
    (re.compile(r"'[^']*'"), "'…'"),
    (re.compile(r"(?<!\w)(?:[A-Za-z]:)?[/\\][^\s:'\"]*[/\\][^\s:'\"]+"), "<path>"),
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"\b[0-9a-f]{12,}\b"), "<hex>"),
    (re.compile(r"\d+"), "N"),
]


def error_signature(message: str) -> str:
    """A message with paths, URLs and numbers folded out, for grouping similar errors."""
    sig = message.split("\n", 1)[0]
    for pattern, repl in _SIG_SUBS:
        sig = pattern.sub(repl, sig)
    return sig
