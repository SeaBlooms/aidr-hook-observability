import { getJSON, ingest } from "../api.js";
import { load, save } from "../prefs.js";
import { fromLocalInput, localInputValue } from "../format.js";
import { Component, html } from "../html.js";
import { RANGES, cycleRange, get, go, refreshTick, setLive, setRange, setTheme, update } from "../state.js";
import { Icon } from "./ui.js";

const GROUPS = [
  ["Review", [
    ["overview", "Overview", "overview"],
    ["activity", "Activity", "activity"],
    ["sessions", "Sessions", "sessions"],
    ["graph", "Graph", "graph"],
  ]],
  ["Risk", [
    ["verdicts", "Verdicts", "verdicts", "flagged"],
    ["sensitive", "Sensitive access", "sensitive", "sensitive"],
    ["destinations", "Destinations", "destinations"],
  ]],
  ["Operations", [
    ["health", "Hook health", "health"],
    ["mcp", "MCP inventory", "mcp"],
    ["usage", "Usage", "usage"],
  ]],
];

const MODE_TITLE = {
  dry_run: "Dry-run: the hook recorded what it would have sent. Nothing was enforced.",
  live: "Live: the hook can enforce Noma's decision.",
  mixed: "This range mixes dry-run and live decisions.",
  unknown: "No captures in this range yet.",
};

const seenAlerts = new Set();
let alertsPrimed = false;

function noteFeed(feed) {
  const hits = (feed || []).filter((row) => row.verdict === "block" || row.verdict === "ask");
  if (!alertsPrimed) {
    hits.forEach((row) => seenAlerts.add(row.id));
    alertsPrimed = true;
    return;
  }
  if (!load().notify || typeof Notification === "undefined" || Notification.permission !== "granted") return;
  hits.forEach((row) => {
    if (!row.id || seenAlerts.has(row.id)) return;
    seenAlerts.add(row.id);
    const body = [row.agent, row.tool || row.action_type, row.summary].filter(Boolean).join(" · ");
    try {
      new Notification("AIDR " + row.verdict, { body });
    } catch {
      /* notifications can be blocked after permission was granted */
    }
  });
}

export class Shell extends Component {
  constructor() {
    super();
    this.state = { overview: null, refreshing: false };
    this._seq = 0;
  }

  componentDidMount() {
    this.load();
  }

  componentDidUpdate(previous) {
    const before = previous.app;
    const after = this.props.app;
    if (before.tick !== after.tick || before.since !== after.since || before.until !== after.until) this.load();
  }

  load() {
    const seq = ++this._seq;
    const visit = load().lastVisit || "";
    getJSON("/api/overview", this.props.app, { visit })
      .then((data) => {
        if (seq !== this._seq) return;
        this.setState({ overview: data });
        noteFeed(data.feed);
        if (get().mode !== data.mode) update({ mode: data.mode }, "none");
      })
      .catch(() => {
        if (seq === this._seq) this.setState({ overview: this.state.overview });
      });
  }

  async refresh() {
    this.setState({ refreshing: true });
    try {
      await ingest();
      refreshTick();
    } finally {
      this.setState({ refreshing: false });
    }
  }

  async enableAlerts() {
    if (typeof Notification === "undefined") return;
    const permission = await Notification.requestPermission();
    save({ notify: permission === "granted" });
    this.forceUpdate();
  }

  badge(kind) {
    const data = this.state.overview;
    if (!data) return 0;
    if (kind === "flagged") return data.flagged || 0;
    if (kind === "sensitive") return data.sensitive_since_visit || 0;
    return 0;
  }

  render({ app, children }) {
    const data = this.state.overview;
    const mode = (data && data.mode) || "unknown";
    const modeLabel = mode === "dry_run" ? "Dry-run" : mode === "live" ? "Live" : mode === "mixed" ? "Mixed" : "No data";
    return html`<div class="app">
      <a class="skip" href="#main">Skip to content</a>
      <header class="topbar">
        <button class="btn icon-btn menu-btn" type="button" aria-label="Open menu" aria-expanded=${app.navOpen ? "true" : "false"}
          onClick=${() => update({ navOpen: !app.navOpen }, "none")}>Menu</button>
        <div class="brand">AIDR <span>Hook Observability</span></div>
        <button class="search-btn" type="button" onClick=${() => update({ palette: true }, "none")}>
          <span>Search</span><kbd>/</kbd>
        </button>
        <label class="range">
          <span class="sr-only">Time range</span>
          <select value=${app.range} onChange=${(event) => setRange(event.target.value)}>
            ${RANGES.map((item) => html`<option value=${item.id} key=${item.id}>${item.label}</option>`)}
          </select>
        </label>
        ${app.range === "custom"
          ? html`<label class="range">From <input type="datetime-local" value=${localInputValue(app.from)}
              onChange=${(event) => setRange("custom", { from: String(fromLocalInput(event.target.value)), to: app.to })} /></label>
            <label class="range">To <input type="datetime-local" value=${localInputValue(app.to)}
              onChange=${(event) => setRange("custom", { from: app.from, to: String(fromLocalInput(event.target.value)) })} /></label>`
          : null}
        <button class="btn" type="button" aria-pressed=${app.live ? "true" : "false"} onClick=${() => setLive(!app.live)}>${app.live ? "Live" : "Paused"}</button>
        <span class="mode mode-${mode}" title=${MODE_TITLE[mode] || ""}>${modeLabel}</span>
        <button class="btn" type="button" onClick=${() => this.refresh()} disabled=${this.state.refreshing}>Refresh</button>
        <button class="btn icon-btn" type="button" aria-label="Theme" title="Theme"
          onClick=${() => setTheme(app.theme === "auto" ? "light" : app.theme === "light" ? "dark" : "auto")}>${app.theme === "light" ? "Light" : app.theme === "dark" ? "Dark" : "Auto"}</button>
      </header>
      <nav class="sidebar ${app.navOpen ? "open" : ""}" aria-label="Sections">
        ${GROUPS.map(
          ([title, items]) => html`<div class="nav-group" key=${title}>
            <h2>${title}</h2>
            ${items.map(([id, label, icon, badge]) => {
              const count = badge ? this.badge(badge) : 0;
              return html`<button type="button" key=${id} class=${app.view === id ? "active" : ""}
                aria-current=${app.view === id ? "page" : null}
                title=${label}
                onClick=${() => go(id)}>
                <${Icon} name=${icon} />
                <span class="nav-label">${label}</span>
                ${count ? html`<span class="nav-badge">${count}</span>` : null}
              </button>`;
            })}
          </div>`,
        )}
        <button class="btn alerts" type="button" onClick=${() => this.enableAlerts()}>${load().notify ? "Alerts on" : "Alert me"}</button>
      </nav>
      ${app.navOpen ? html`<div class="scrim nav-scrim" onClick=${() => update({ navOpen: false }, "none")}></div>` : null}
      <main id="main" tabindex="-1">${children}</main>
      <div class="sr-only" aria-live="polite">${app.live ? "Live updates on" : "Updates paused"}</div>
    </div>`;
  }
}

export function Help({ app }) {
  if (!app.help) return null;
  return html`<div class="scrim" onClick=${() => update({ help: false }, "none")}></div>
    <div class="palette help" role="dialog" aria-modal="true" aria-labelledby="help-title">
      <h2 id="help-title">Shortcuts</h2>
      <dl class="kv">
        <dt>/</dt><dd>Search</dd>
        <dt>Ctrl/⌘ K</dt><dd>Search</dd>
        <dt>g then o</dt><dd>Overview</dd>
        <dt>g then a</dt><dd>Activity</dd>
        <dt>g then s</dt><dd>Sessions</dd>
        <dt>g then g</dt><dd>Graph</dd>
        <dt>g then v</dt><dd>Verdicts</dd>
        <dt>g then e</dt><dd>Sensitive access</dd>
        <dt>g then d</dt><dd>Destinations</dd>
        <dt>g then h</dt><dd>Hook health</dd>
        <dt>g then m</dt><dd>MCP inventory</dd>
        <dt>g then u</dt><dd>Usage</dd>
        <dt>[ and ]</dt><dd>Previous or next time range</dd>
        <dt>?</dt><dd>This list</dd>
        <dt>Esc</dt><dd>Close</dd>
      </dl>
      <button class="btn" type="button" onClick=${() => update({ help: false }, "none")}>Close</button>
    </div>`;
}

export function installKeys() {
  let chord = "";
  let chordAt = 0;
  const map = {
    o: "overview",
    a: "activity",
    s: "sessions",
    g: "graph",
    v: "verdicts",
    h: "health",
    d: "destinations",
    u: "usage",
    m: "mcp",
    e: "sensitive",
  };
  document.addEventListener("keydown", (event) => {
    const tag = event.target && event.target.tagName;
    const typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    if (event.key === "?" && !typing) {
      event.preventDefault();
      update({ help: !get().help, palette: false }, "none");
      return;
    }
    if (typing) return;
    if (event.key === "[" || event.key === "]") {
      event.preventDefault();
      cycleRange(event.key === "]" ? 1 : -1);
      return;
    }
    if (event.key === "g" && !event.metaKey && !event.ctrlKey && chord !== "g") {
      chord = "g";
      chordAt = Date.now();
      return;
    }
    if (chord === "g" && Date.now() - chordAt < 1200 && map[event.key]) {
      event.preventDefault();
      go(map[event.key]);
    }
    chord = "";
  });
}
