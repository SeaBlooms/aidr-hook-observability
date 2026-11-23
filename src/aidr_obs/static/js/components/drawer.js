import { askJev, getJSON } from "../api.js";
import { duration, toolName } from "../format.js";
import { Component, html } from "../html.js";
import { go, update } from "../state.js";
import { EventName, JsonBlock, KV, ToolLabel, Verdict, When, deviceName } from "./ui.js";

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
    this.state = { data: null, error: null, loading: false, jevBusy: false, jevError: "" };
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
    this.setState({ loading: true, error: null, jevBusy: false, jevError: "" });
    const params = ref.kind === "run" ? { run_id: ref.id } : { capture_id: ref.id };
    getJSON("/api/event", { since: 0 }, params)
      .then((data) => {
        if (seq !== this._seq) return;
        this.setState({ data, loading: false, error: null, jevError: "" });
        const close = this.panel && this.panel.querySelector("[data-close]");
        if (close) close.focus();
      })
      .catch((err) => {
        if (seq === this._seq) this.setState({ loading: false, error: err.message || String(err) });
      });
  }

  requestJev(backends) {
    const capture = this.state.data && this.state.data.capture;
    if (!capture || this.state.jevBusy) return;
    const have = new Set((this.state.data.jev_reviews || []).map((review) => review.backend));
    const seq = this._seq;
    this.setState({ jevBusy: true, jevError: "" });
    Promise.allSettled(backends.map((backend) => askJev(capture.id, have.has(backend), backend))).then((results) => {
      if (seq !== this._seq || !this.state.data) return;
      const byBackend = new Map((this.state.data.jev_reviews || []).map((review) => [review.backend, review]));
      const errors = [];
      results.forEach((result, i) => {
        if (result.status === "fulfilled") byBackend.set(result.value.backend, result.value);
        else errors.push(backends[i] + ": " + (result.reason.message || String(result.reason)));
      });
      const reviews = [...byBackend.values()].sort((a, b) => a.backend.localeCompare(b.backend));
      const latest = reviews.reduce((best, review) => (!best || review.asked_at > best.asked_at ? review : best), null);
      const data = { ...this.state.data, jev_reviews: reviews, jev_review: latest };
      this.setState({ data, jevBusy: false, jevError: errors.join("\n") });
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
    const sessionKey = capture && capture.session_id
      ? (capture.agent || "?") + "|" + capture.session_id + (capture.device ? "|" + capture.device : "")
      : "";
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
            ${this.jevBox(data)}
            <${KV} rows=${[
              ["When", html`<${When} ts=${capture.ts} />`],
              ["Device", deviceName(capture.device)],
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
              ["Device", deviceName(run.device)],
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

  jevBox(data) {
    const reviews = data.jev_reviews || [];
    const configured = !!(data.jev && data.jev.configured);
    const backends = (data.jev && data.jev.backends) || ["gateway", "ollama"];
    const busy = this.state.jevBusy;
    const have = new Set(reviews.map((review) => review.backend));
    const stances = new Set(reviews.map((review) => review.stance));
    let body;
    if (!configured) {
      body = html`<p class="muted">Add <code>JEV_API_TOKEN</code> to <code>aidr-hook-observability/.env</code>
        (same value as <code>jevai-dev-lab/.env</code>), restart <code>aidr-obs serve</code>, and run
        <code>uv run jev serve</code>.</p>`;
    } else {
      body = html`${reviews.length
          ? html`${reviews.map((review) => {
                const persona = review.persona_sha256 ? " · persona " + review.persona_sha256.slice(0, 8) : "";
                return html`<div class="jev-review" key=${review.backend}>
                  <p><span class=${"pill v-" + review.stance}>${review.stance}</span>
                    <strong>${review.backend}</strong>
                    <span class="muted"> ${review.model || ""}${persona}</span></p>
                  <p>${review.note}</p>
                </div>`;
              })}
              ${reviews.length > 1 && stances.size > 1
                ? html`<p class="jev-disagree">The backends disagree on this verdict.</p>`
                : null}
              <p class="muted">Second opinion only — does not change the hook decision.</p>`
          : html`<p class="muted">Ask the local Jev sandbox for a second opinion on this redacted capture.
              Replies are cached here per backend; they do not change the hook decision.</p>`}
        <div class="row-actions">
          ${backends.map(
            (backend) => html`<button class="btn" type="button" key=${backend} disabled=${busy}
              onClick=${() => this.requestJev([backend])}>
              ${"Ask " + backend + (have.has(backend) ? " again" : "")}
            </button>`,
          )}
          <button class="btn" type="button" disabled=${busy} onClick=${() => this.requestJev(backends)}>
            ${busy ? "Asking…" : "Ask both"}
          </button>
        </div>`;
    }
    return html`<section class="jev-box">
      <h3>Jev review</h3>
      ${body}
      ${this.state.jevError ? html`<p class="jev-err" role="alert">${this.state.jevError}</p>` : null}
    </section>`;
  }
}
