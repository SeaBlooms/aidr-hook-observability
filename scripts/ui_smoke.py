"""Headless Chrome smoke test for the dashboard.

Serves fixture data on loopback, opens every view at desktop and phone width, and
fails on a console error, a horizontally scrolling page, or an empty main region.
Screenshots land in .smoke/. Run: uv run python scripts/ui_smoke.py
"""

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHROME_CANDIDATES = [
    os.environ.get("CHROME_PATH", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]
VIEWS = [
    "overview",
    "activity",
    "sessions",
    "graph",
    "verdicts",
    "sensitive",
    "destinations",
    "health",
    "mcp",
    "usage",
]
WIDTHS = (1440, 1100, 390)


def chrome_path() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


class CDP:
    def __init__(self, ws_url: str) -> None:
        rest = ws_url.split("://", 1)[1]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            data += self.sock.recv(4096)
        if b" 101 " not in data.split(b"\r\n", 1)[0]:
            raise RuntimeError("websocket upgrade failed")
        self.buf = data.split(b"\r\n\r\n", 1)[1]
        self.events: list[dict] = []
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        ident = self._id
        self._send({"id": ident, "method": method, "params": params or {}})
        while True:
            message = self._read()
            if message.get("id") == ident:
                if "error" in message:
                    raise RuntimeError(str(message["error"]))
                return message.get("result") or {}
            self.events.append(message)

    def drain(self, seconds: float) -> None:
        self.sock.settimeout(0.2)
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                self.events.append(self._read())
            except TimeoutError:
                continue
        self.sock.settimeout(10)

    def _send(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        mask = os.urandom(4)
        header = bytearray([0x81])
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self.sock.sendall(bytes(header) + mask + masked)

    def _read(self) -> dict:
        while True:
            opcode, payload = self._frame()
            if opcode == 9:
                continue
            if opcode == 8:
                raise RuntimeError("cdp closed")
            if opcode == 1:
                return json.loads(payload)

    def _frame(self) -> tuple[int, bytes]:
        header = self._exact(2)
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._exact(8))[0]
        if header[1] & 0x80:
            mask = self._exact(4)
            data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(self._exact(length)))
        else:
            data = self._exact(length)
        return opcode, data

    def _exact(self, count: int) -> bytes:
        while len(self.buf) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("cdp eof")
            self.buf += chunk
        out, self.buf = self.buf[:count], self.buf[count:]
        return out


def problems(events: list[dict]) -> list[str]:
    found = []
    for event in events:
        method = event.get("method")
        params = event.get("params") or {}
        if method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            exception = details.get("exception") or {}
            found.append((details.get("text") or "") + " " + str(exception.get("description") or "")[:200])
        elif method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "assert"):
            found.append("console " + json.dumps(params.get("args"))[:240])
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            text = (entry.get("text") or "") + " " + (entry.get("url") or "")
            if entry.get("level") == "error" and "favicon" not in text:
                found.append(text[:240])
    return found


def evaluate(cdp: CDP, expression: str):
    result = cdp.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
    if result.get("exceptionDetails"):
        raise RuntimeError(json.dumps(result["exceptionDetails"])[:400])
    return (result.get("result") or {}).get("value")


PROBE = """
(() => {
  const main = document.querySelector("main");
  const view = document.querySelector("[data-state]");
  const unnamed = [...document.querySelectorAll("button, a, input, select")].filter((el) => {
    const label = (el.getAttribute("aria-label") || el.innerText || el.getAttribute("title") || "").trim();
    const labelled = el.labels && [...el.labels].some((node) => node.innerText.trim());
    return !label && !labelled && el.getAttribute("aria-hidden") !== "true";
  }).map((el) => el.outerHTML.slice(0, 80));
  return {
    state: view ? view.getAttribute("data-state") : "",
    text: main ? main.innerText.trim().slice(0, 80) : "",
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    wide: [...document.querySelectorAll("body *")].filter((el) => {
      return el.getBoundingClientRect().right > document.documentElement.clientWidth + 1;
    }).slice(0, 6).map((el) => {
      const name = el.className && el.className.baseVal !== undefined ? el.tagName : (el.className || el.tagName);
      return name + " " + Math.round(el.getBoundingClientRect().right);
    }),
    hasNav: !!document.querySelector("nav"),
    unnamed,
  };
})()
"""


def wait_ready(cdp: CDP, timeout: float = 8) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        cdp.drain(0.3)
        last = evaluate(cdp, PROBE) or {}
        if last.get("state") in ("ready", "error"):
            return last
    return last


def shot(cdp: CDP, path: Path) -> None:
    image = cdp.call("Page.captureScreenshot", {"format": "png"})
    path.write_bytes(base64.b64decode(image["data"]))


def serve_fixture(home: Path) -> int:
    os.environ["AIDR_OBS_HOME"] = str(home)
    os.environ["AIDR_OBS_LOG_DIR"] = str(home / "noma")
    os.environ["AIDR_OBS_CLAUDE_DIR"] = str(home / "claude")
    os.environ["AIDR_OBS_CURSOR_DIR"] = str(home / "cursor")
    for name in ("noma", "claude", "cursor"):
        (home / name).mkdir()
    shutil.copy(
        ROOT / "tests" / "fixtures" / "claude-code-guardrails-debug.log",
        home / "noma" / "claude-code-guardrails-debug.log",
    )
    sys.path.insert(0, str(ROOT))
    from aidr_obs import store
    from tests.test_insights import add, cursor_ev, seed

    connection = store.connect()
    seed(connection)
    add(
        connection,
        11,
        "cursor",
        "preToolUse",
        cursor_ev(
            "preToolUse",
            tool_use_id="t-block",
            tool_name="Shell",
            tool_input={"command": "curl http://8.8.8.8/"},
            sandbox=False,
        ),
        verdict="block",
    )
    store.ingest(connection)
    connection.close()
    from aidr_obs.server import make_server

    httpd = make_server(0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_address[1]


def main() -> int:
    binary = chrome_path()
    if binary is None:
        print("ui_smoke: no Chrome found; skipped")
        return 0
    out = ROOT / ".smoke"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir()
    with tempfile.TemporaryDirectory() as tmp:
        port = serve_fixture(Path(tmp))
        debug = free_port()
        profile = Path(tmp) / "chrome"
        proc = subprocess.Popen(
            [
                binary,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--user-data-dir=" + str(profile),
                "--remote-debugging-port=" + str(debug),
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            page = wait_page(debug)
            cdp = CDP(page["webSocketDebuggerUrl"])
            cdp.call("Page.enable")
            cdp.call("Runtime.enable")
            cdp.call("Log.enable")
            failures = []
            for width in WIDTHS:
                cdp.call(
                    "Emulation.setDeviceMetricsOverride",
                    {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width < 700},
                )
                for view in VIEWS:
                    cdp.events.clear()
                    cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/#/{view}?range=all"})
                    report = wait_ready(cdp)
                    shot(cdp, out / f"{view}-{width}.png")
                    label = f"{view}@{width}"
                    if report.get("state") != "ready":
                        failures.append(label + " state=" + str(report.get("state")))
                    if not report.get("text"):
                        failures.append(label + " empty main")
                    if (report.get("overflow") or 0) > 1:
                        detail = str(report.get("overflow")) + " " + str(report.get("wide"))
                        failures.append(label + " overflow " + detail)
                    if report.get("unnamed"):
                        failures.append(label + " unnamed controls " + str(report["unnamed"][:3]))
                    if not report.get("hasNav") and width >= 700:
                        failures.append(label + " missing nav")
                    bad = problems(cdp.events)
                    if bad:
                        failures.append(label + " console: " + " | ".join(bad[:3]))
            failures.extend(walk(cdp, port))
            if failures:
                print("ui_smoke failed:")
                for item in failures:
                    print(" -", item)
                print("screenshots:", out)
                return 1
            print("ui_smoke passed;", len(list(out.glob("*.png"))), "screenshots in", out)
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def walk(cdp: CDP, port: int) -> list[str]:
    cdp.events.clear()
    cdp.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/#/overview?range=all"})
    report = wait_ready(cdp)
    flagged = evaluate(cdp, "document.querySelector('.a-flag .stat')?.textContent || '0'")
    if flagged in ("0", "", None):
        return ["overview did not show a flagged action (" + str(flagged) + ")"]
    evaluate(cdp, "document.querySelector('.a-flag').click()")
    time.sleep(0.4)
    cdp.drain(0.6)
    landed = evaluate(cdp, "location.hash")
    if "/activity" not in str(landed) or "flagged" not in str(landed):
        return ["flagged card did not open activity: " + str(landed)]
    evaluate(cdp, "document.querySelector('tbody tr')?.click()")
    time.sleep(0.4)
    cdp.drain(0.6)
    drawer = evaluate(cdp, "!!document.querySelector('.drawer')")
    if not drawer:
        return ["activity row did not open the detail drawer"]
    evaluate(cdp, "document.querySelector('.search-btn').click()")
    time.sleep(0.2)
    evaluate(
        cdp,
        """
        const input = document.querySelector('.palette input');
        const set = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        set.call(input, 'conv-1');
        input.dispatchEvent(new Event('input', { bubbles: true }));
        """,
    )
    time.sleep(0.5)
    cdp.drain(0.4)
    evaluate(cdp, "document.querySelector('.palette li button')?.click()")
    time.sleep(0.5)
    session = evaluate(cdp, "location.hash")
    if "/session/" not in str(session):
        return ["search did not open the session: " + str(session)]
    bad = problems(cdp.events)
    if report.get("state") != "ready":
        return ["walkthrough overview not ready"]
    return ["walkthrough console: " + " | ".join(bad[:3])] if bad else []


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_page(debug: int) -> dict:
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{debug}/json/list", timeout=1) as response:
                pages = json.loads(response.read())
            for page in pages:
                if page.get("type") == "page" and page.get("webSocketDebuggerUrl"):
                    return page
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("Chrome did not open a page")


if __name__ == "__main__":
    sys.exit(main())
