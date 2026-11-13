import { getJSON } from "../api.js";
import { absolute, clip, duration, eventLabel, hasRedaction, iso, presentJson, relative, toolName } from "../format.js";
import { Component, html } from "../html.js";

export function When({ ts }) {
  if (!ts) return html`<span class="muted">–</span>`;
  return html`<time datetime=${iso(ts)} title=${absolute(ts)}>${relative(ts)}</time>`;
}

export function Verdict({ verdict, mode }) {
  if (!verdict) return html`<span class="muted">–</span>`;
  if (mode === "dry_run" && verdict === "dry_run") return null;
  return html`<span class="pill v-${verdict}">${verdict}</span>`;
}

export function verdictTags(counts, mode) {
  const entries = Object.entries(counts || {}).filter(([name]) => !(mode === "dry_run" && name === "dry_run"));
  if (!entries.length) return html`<span class="muted">–</span>`;
  return entries
    .sort((a, b) => b[1] - a[1])
    .map(([name, n]) => html`<span class="pill v-${name}" key=${name}>${name} ${n}</span>`);
}

export function ToolLabel({ name, mcp }) {
  const info = toolName(name);
  const showMcp = mcp || info.mcp;
  const label = info.name || name || "";
  if (!label) return html`<span class="muted">–</span>`;
  return html`<span class="tool-name">${showMcp ? html`<span class="badge">MCP</span>` : null}${label}</span>`;
}

export function EventName({ name }) {
  return html`<span title=${name || ""}>${eventLabel(name)}</span>`;
}

export function DryRunBanner({ mode }) {
  if (mode !== "dry_run") return null;
  return html`<div class="banner" role="status">
    Dry-run: decisions are recorded, not enforced. Verdicts show what was sent, not what Noma decided.
  </div>`;
}

export function Empty({ children }) {
  return html`<div class="empty">${children || "Nothing in this range"}</div>`;
}

export function Skeletons() {
  return html`<div class="stats" aria-hidden="true">
    ${[0, 1, 2, 3].map((n) => html`<div class="card skeleton" key=${n}><div class="sk-line"></div><div class="sk-stat"></div></div>`)}
  </div>
  <p class="sr-only">Loading</p>`;
}

export function ErrorBox({ message, onRetry }) {
  return html`<div class="card error-card" role="alert">
    <p>${message}</p>
    <button class="btn" type="button" onClick=${onRetry}>Retry</button>
  </div>`;
}

export class LoadView extends Component {
  constructor() {
    super();
    this.state = { data: null, error: null, loading: true };
    this._seq = 0;
  }

  componentDidMount() {
    this.reload();
  }

  componentDidUpdate(previous) {
    const before = previous && previous.app;
    if (before && this.shouldReload(before, this.props.app)) this.reload();
  }

  shouldReload(previous, next) {
    return previous.tick !== next.tick || previous.since !== next.since || previous.until !== next.until;
  }

  reload() {
    const seq = ++this._seq;
    this.setState({ loading: true });
    Promise.resolve()
      .then(() => this.fetch(this.props.app))
      .then((data) => {
        if (seq === this._seq) this.setState({ data, loading: false, error: null });
      })
      .catch((err) => {
        if (seq === this._seq) this.setState({ loading: false, error: err.message || String(err) });
      });
  }

  fetch(app) {
    return getJSON(this.props.path, app, this.props.params);
  }

  body() {
    return null;
  }

  render() {
    if (!this.state.data && this.state.loading) {
      return html`<div class="view" data-state="loading"><${Skeletons} /></div>`;
    }
    if (this.state.error && !this.state.data) {
      return html`<div class="view" data-state="error"><${ErrorBox} message=${this.state.error} onRetry=${() => this.reload()} /></div>`;
    }
    return html`<div class="view ${this.state.loading ? "refreshing" : ""}" data-state=${this.state.loading ? "refresh" : "ready"}>
      ${this.state.error
        ? html`<div class="banner warn" role="status">Couldn't refresh. <button class="btn" type="button" onClick=${() => this.reload()}>Retry</button></div>`
        : null}
      ${this.body(this.state.data)}
    </div>`;
  }
}

export function Bars({ rows, onPick }) {
  const entries = Array.isArray(rows) ? rows : Object.entries(rows || {});
  if (!entries.length) return html`<${Empty} />`;
  const max = Math.max(...entries.map((entry) => Number(entry[1]) || 0), 1);
  return html`<div class="bars">
    ${entries.map(([label, n, title]) => {
      const count = Number(n) || 0;
      const inner = html`<span class="lbl" title=${String(title || label)}>${label}</span>
        <span class="track"><span class="fill" style=${{ width: (100 * count) / max + "%" }}></span></span>
        <span class="n">${count}</span>`;
      return onPick
        ? html`<button class="hbar" type="button" key=${String(label)} onClick=${() => onPick(label)}>${inner}</button>`
        : html`<div class="hbar" key=${String(label)}>${inner}</div>`;
    })}
  </div>`;
}

export function Icon({ name }) {
  return html`<svg class="ico" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">${iconShape(name)}</svg>`;
}

function iconShape(name) {
  if (name === "overview") {
    return html`<path fill="currentColor" d="M4 4h7v7H4V4zm9 0h7v7h-7V4zM4 13h7v7H4v-7zm9 0h7v7h-7v-7z"/>`;
  }
  if (name === "activity") {
    return html`<path fill="currentColor" d="M4 6h16v2H4V6zm0 5h16v2H4v-2zm0 5h10v2H4v-2z"/>`;
  }
  if (name === "sessions") {
    return html`<path fill="currentColor" d="M12 3a9 9 0 100 18 9 9 0 000-18zm1 5v4.2l3.2 1.9-.9 1.5L11 13V8h2z"/>`;
  }
  if (name === "graph") {
    return html`<path fill="currentColor" d="M8 6a3 3 0 11-2.8 4H5l6 8 6-8h-.2A3 3 0 1116 6a3 3 0 01-1.2 2.4L12 11 9.2 8.4A3 3 0 018 6z"/>`;
  }
  if (name === "verdicts") {
    return html`<path fill="currentColor" d="M12 2l8 3v6c0 5-3.4 8.4-8 10-4.6-1.6-8-5-8-10V5l8-3zm-1 13l5-5-1.4-1.4L11 12.2 9.4 10.6 8 12l3 3z"/>`;
  }
  if (name === "sensitive") {
    return html`<path fill="currentColor" d="M7 10V8a5 5 0 0110 0v2h1v10H6V10h1zm2 0h6V8a3 3 0 00-6 0v2z"/>`;
  }
  if (name === "destinations") {
    return html`<path fill="currentColor" d="M12 2a10 10 0 100 20 10 10 0 000-20zm-1 2.1A8 8 0 004.1 11H8c.2-2.6.8-4.8 1.6-6.4.4-.2.8-.3 1.4-.5zM10 13H4.1A8 8 0 0011 19.9V13zm2 6.9A8 8 0 0019.9 13H14v6.9zM14 11h5.9A8 8 0 0013 4.1V11z"/>`;
  }
  if (name === "health") {
    return html`<path fill="currentColor" d="M3 12h4l2-5 4 10 2-5h6v2h-4l-2 5-4-10-2 5H3v-2z"/>`;
  }
  if (name === "mcp") {
    return html`<path fill="currentColor" d="M3 5h8v6H3V5zm10 0h8v6h-8V5zM3 13h8v6H3v-6zm10 0h8v6h-8v-6z"/>`;
  }
  return html`<path fill="currentColor" d="M4 19V5h2v12h12v2H4zm3-3l4-5 3 3 5-7 1.6 1.2-6.4 9-3.2-3.2L8.4 17 7 16z"/>`;
}

export function KV({ rows }) {
  const items = (rows || []).filter((row) => row[1] != null && row[1] !== "");
  if (!items.length) return null;
  return html`<dl class="kv">
    ${items.map(([key, value]) => html`<dt key=${key}>${key}</dt><dd>${value}</dd>`)}
  </dl>`;
}

export class CopyButton extends Component {
  constructor() {
    super();
    this.state = { done: false };
  }

  copy() {
    const text = this.props.text == null ? "" : String(this.props.text);
    const done = () => {
      this.setState({ done: true });
      clearTimeout(this._timer);
      this._timer = setTimeout(() => this.setState({ done: false }), 1200);
    };
    const fallback = () => {
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand("copy");
      } catch {
        /* clipboard can be unavailable */
      }
      area.remove();
      done();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(fallback);
    } else fallback();
  }

  render() {
    return html`<button class="btn mini" type="button" onClick=${() => this.copy()}>${this.state.done ? "Copied" : "Copy"}</button>`;
  }
}

export function JsonBlock({ value, label }) {
  const text = presentJson(value);
  const parts = highlight(text);
  return html`<div class="json-wrap">
    <div class="json-bar"><span>${label || "JSON"}</span><${CopyButton} text=${text} /></div>
    ${hasRedaction(value) ? html`<p class="muted redacted-note">[redacted] marks a value masked before it was stored.</p>` : null}
    <pre class="json">${parts}</pre>
  </div>`;
}

function highlight(text) {
  const pattern = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b|\[redacted\]/g;
  const parts = [];
  let last = 0;
  let match = pattern.exec(text);
  let index = 0;
  while (match) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    const token = match[0];
    let cls = "tok-num";
    if (match[1] && match[2]) cls = "tok-key";
    else if (match[1]) cls = token.includes("[redacted]") ? "tok-redacted" : "tok-str";
    else if (match[3]) cls = "tok-lit";
    else if (token === "[redacted]") cls = "tok-redacted";
    parts.push(html`<span class=${cls} key=${index}>${token}</span>`);
    last = match.index + token.length;
    index += 1;
    match = pattern.exec(text);
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

export function Summary({ text, limit }) {
  const full = text == null ? "" : String(text);
  if (!full) return html`<span class="muted">–</span>`;
  return html`<code class="summary" title=${full}>${clip(full, limit || 96)}</code>`;
}

export { clip, duration, html };
