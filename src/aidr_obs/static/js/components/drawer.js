import { getJSON } from "../api.js";
import { duration, toolName } from "../format.js";
import { Component, html } from "../html.js";
import { go, update } from "../state.js";
import { EventName, JsonBlock, KV, ToolLabel, Verdict, When } from "./ui.js";

function parseOpen(value) {
  if (!value) return null;
  const split = value.indexOf(":");
  if (split < 1) return null;
  return { kind: value.slice(0, split), id: value.slice(split + 1) };
}

function focusable(root) {
  return [...root.querySelectorAll("a, button, input, select, textarea, [tabindex]")].filter(
    (node) => !node.disabled && node.tabIndex !== -1,
  );
}

export class Drawer extends Component {
  constructor() {
    super();
    this.state = { data: null, error: null, loading: false };
    this._seq = 0;
    this.onKey = this.onKey.bind(this);
  }

  componentDidMount() {
    document.addEventListener("keydown", this.onKey);
    this.load(this.props.app.open);
  }

  componentWillUnmount() {
    document.removeEventListener("keydown", this.onKey);
  }

  componentDidUpdate(previous) {
    if (previous.app.open !== this.props.app.open) this.load(this.props.app.open);
  }

  load(open) {
    const ref = parseOpen(open);
    if (!ref || !ref.id) {
      this.setState({ data: null, error: null, loading: false });
      return;
    }
    const seq = ++this._seq;
    this._return = document.activeElement;
    this.setState({ loading: true, error: null });
    const params = ref.kind === "run" ? { run_id: ref.id } : { capture_id: ref.id };
    getJSON("/api/event", { since: 0 }, params)
      .then((data) => {
        if (seq !== this._seq) return;
        this.setState({ data, loading: false, error: null });
        const close = this.panel && this.panel.querySelector("[data-close]");
        if (close) close.focus();
      })
      .catch((err) => {
        if (seq === this._seq) this.setState({ loading: false, error: err.message || String(err) });
      });
  }

  close() {
    update({ open: "" }, "push");
    if (this._return && this._return.focus) this._return.focus();
  }

  onKey(event) {
    if (!this.props.app.open) return;
    if (event.key === "Escape") {
      event.preventDefault();
      this.close();
      return;
    }
    if (event.key !== "Tab" || !this.panel) return;
    const nodes = focusable(this.panel);
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  render() {
    const open = !!parseOpen(this.props.app.open);
    if (!open) return null;
    const mode = this.props.mode;
    return html`<div class="scrim" onClick=${() => this.close()}></div>
      <aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title"
        ref=${(node) => {
          this.panel = node;
        }}>
        ${this.state.loading && !this.state.data ? html`<p>Loading…</p>` : null}
        ${this.state.error ? html`<p role="alert">${this.state.error}</p>` : null}
        ${this.state.data ? this.detail(this.state.data, mode) : null}
        <button class="btn" type="button" data-close="1" onClick=${() => this.close()}>Close</button>
      </aside>`;
  }

  detail(data, mode) {
    const capture = data.capture;
    const run = data.run;
    const title = (capture && capture.event) || (run && run.event) || "Event";
    const sessionKey = capture && capture.session_id ? (capture.agent || "?") + "|" + capture.session_id : "";
    const tool = toolName(capture && capture.tool_name);
    const graphId = tool.name ? "tool:" + tool.name : "";
    return html`<div class="drawer-head">
        <h2 id="drawer-title"><${EventName} name=${title} /></h2>
        <div class="row-actions">
          ${sessionKey
            ? html`<button class="btn" type="button" onClick=${() => go("session", { sessionKey })}>Session</button>`
            : null}
          ${graphId
            ? html`<button class="btn" type="button" onClick=${() => go("graph", { focus: graphId })}>Graph</button>`
            : null}
          ${run
            ? html`<button class="btn" type="button" onClick=${() => update({ open: "run:" + run.run_id }, "replace")}>Debug log</button>`
            : null}
        </div>
      </div>
      ${capture
        ? html`<p><${Verdict} verdict=${capture.verdict} mode=${mode} /> ${capture.reason || ""}</p>
            <${KV} rows=${[
              ["When", html`<${When} ts=${capture.ts} />`],
              ["Tool", html`<${ToolLabel} name=${capture.tool_name} />`],
              ["Session", capture.session_id],
              ["Directory", capture.cwd],
              ["Hook time", duration(capture.latency_ms)],
              ["Exit", capture.exit_code],
              ["Timed out", capture.timed_out ? "yes" : ""],
              ["Hook version", capture.hook_version],
              ["Dry-run", capture.dry_run ? "yes" : ""],
              ["Tap error", capture.tap_error],
            ]} />
            <details open>
              <summary>Hook output ${capture.dry_run ? "(dry-run payload, not a decision)" : ""}</summary>
              ${capture.stdout_text
                ? html`<${JsonBlock} value=${capture.stdout_text} label="Hook output" />`
                : html`<p class="muted">Empty: no decision, so the agent kept its default.</p>`}
            </details>
            <details>
              <summary>Event payload</summary>
              <${JsonBlock} value=${capture.stdin_json} label="Payload" />
            </details>`
        : html`<p class="muted">No tap capture for this run.</p>`}
      ${run
        ? html`<details open>
            <summary>Debug log</summary>
            <${KV} rows=${[
              ["Run", run.run_id],
              ["Agent", run.agent],
              ["Outcome", run.outcome],
              ["HTTP", run.http_status],
              ["Endpoint", run.url],
              ["Key source", run.key_source],
              ["Config", run.installed_config],
              ["Sender", run.sender],
              ["Errors", run.error_count],
            ]} />
            <pre class="log">${(data.lines || []).map(
              (line) => html`<span class=${"lvl-" + line.level} key=${line.ts + line.message}>${line.ts} ${line.level} [${line.location}] ${line.message}${"\n"}</span>`,
            )}</pre>
          </details>`
        : null}`;
  }
}
