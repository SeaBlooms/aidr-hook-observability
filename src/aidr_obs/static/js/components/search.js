import { getJSON } from "../api.js";
import { Component, html } from "../html.js";
import { go, update } from "../state.js";

const LABELS = {
  session: "Sessions",
  action: "Actions",
  destination: "Destinations",
  file: "Files",
  tool: "Tools",
  mcp: "MCP servers",
};

function flat(groups) {
  const rows = [];
  (groups || []).forEach((group) => {
    group.items.forEach((item) => rows.push({ ...item, type: group.type }));
  });
  return rows;
}

export class Palette extends Component {
  constructor() {
    super();
    this.state = { q: "", groups: [], active: 0, error: null };
    this._timer = null;
    this.onKey = this.onKey.bind(this);
  }

  componentDidMount() {
    document.addEventListener("keydown", this.onKey);
  }

  componentWillUnmount() {
    document.removeEventListener("keydown", this.onKey);
    clearTimeout(this._timer);
  }

  componentDidUpdate(previous) {
    if (!previous.app.palette && this.props.app.palette && this.input) this.input.focus();
  }

  close() {
    update({ palette: false }, "none");
  }

  search(q) {
    clearTimeout(this._timer);
    this.setState({ q, active: 0 });
    if (q.trim().length < 2) {
      this.setState({ groups: [], error: null });
      return;
    }
    this._timer = setTimeout(() => {
      getJSON("/api/search", this.props.app, { q })
        .then((data) => {
          if (q === this.state.q) this.setState({ groups: data.groups || [], error: null });
        })
        .catch((err) => this.setState({ error: err.message || String(err) }));
    }, 180);
  }

  activate(item) {
    this.close();
    if (!item) return;
    if (item.type === "session") go("session", { sessionKey: item.id });
    else if (item.type === "action") go("activity", { open: "capture:" + item.id });
    else if (item.type === "destination" || item.type === "mcp") go("graph", { focus: item.id });
    else if (item.type === "file") go("activity", { q: item.id });
    else if (item.type === "tool") go("activity", { tool: item.id });
  }

  onKey(event) {
    if ((event.key === "k" && (event.metaKey || event.ctrlKey)) || (event.key === "/" && !typing(event))) {
      event.preventDefault();
      update({ palette: true, help: false }, "none");
      return;
    }
    if (!this.props.app.palette) return;
    const rows = flat(this.state.groups);
    if (event.key === "Escape") {
      event.preventDefault();
      this.close();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      const active = rows.length ? (this.state.active + step + rows.length) % rows.length : 0;
      this.setState({ active });
    } else if (event.key === "Enter") {
      event.preventDefault();
      this.activate(rows[this.state.active]);
    }
  }

  render() {
    if (!this.props.app.palette) return null;
    const rows = flat(this.state.groups);
    let cursor = 0;
    return html`<div class="scrim" onClick=${() => this.close()}></div>
      <div class="palette" role="dialog" aria-modal="true" aria-label="Search">
        <input ref=${(node) => {
          this.input = node;
        }}
          aria-label="Search sessions, hosts, files, tools and commands"
          placeholder="Session, host, path, tool, command…"
          value=${this.state.q}
          onInput=${(event) => this.search(event.target.value)} />
        ${this.state.error ? html`<p role="alert">${this.state.error}</p>` : null}
        <div class="palette-results">
          ${(this.state.groups || []).map((group) => html`<section key=${group.type}>
            <h3>${LABELS[group.type] || group.type}</h3>
            <ul>
              ${group.items.map((item) => {
                const index = cursor;
                cursor += 1;
                return html`<li key=${item.id}><button type="button" class=${index === this.state.active ? "active" : ""}
                  onMouseEnter=${() => this.setState({ active: index })}
                  onClick=${() => this.activate({ ...item, type: group.type })}>
                  <strong>${item.label}</strong>
                  <span class="muted">${item.detail || ""}</span>
                </button></li>`;
              })}
            </ul>
          </section>`)}
          ${this.state.q.trim().length >= 2 && !rows.length && !this.state.error ? html`<p class="muted">No matches</p>` : null}
        </div>
      </div>`;
  }
}

function typing(event) {
  const tag = event.target && event.target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (event.target && event.target.isContentEditable);
}

export { typing };
