import { getJSON } from "../api.js";
import { DataTable } from "../components/table.js";
import { DryRunBanner, EventName, LoadView, Summary, ToolLabel, Verdict, When, duration, html } from "../components/ui.js";
import { update } from "../state.js";

export class Activity extends LoadView {
  constructor() {
    super();
    this._more = [];
    this._expanded = {};
  }

  shouldReload(previous, next) {
    return (
      super.shouldReload(previous, next) ||
      previous.q !== next.q ||
      previous.agent !== next.agent ||
      previous.tool !== next.tool ||
      previous.verdict !== next.verdict ||
      previous.tag !== next.tag ||
      previous.type !== next.type ||
      previous.raw !== next.raw
    );
  }

  fetch(app) {
    const params = filters(app);
    params.limit = app.raw ? 100 : 80;
    params.offset = 0;
    return getJSON(app.raw ? "/api/events" : "/api/actions", app, params).then((data) => {
      this._more = [];
      return data;
    });
  }

  async more(data) {
    const app = this.props.app;
    const params = filters(app);
    params.limit = data.limit || 80;
    params.offset = data.items.length + this._more.length;
    const page = await getJSON(app.raw ? "/api/events" : "/api/actions", app, params);
    this._more = this._more.concat(page.items || []);
    this.forceUpdate();
  }

  body(data) {
    const app = this.props.app;
    const rows = (data.items || []).concat(this._more);
    const facets = data.facets || {};
    return html`<${DryRunBanner} mode=${app.mode} />
      <div class="filters">
        ${select("agent", "Agent", app.agent, facets.agent)}
        ${app.raw ? null : select("type", "Action", app.type, facets.action_type)}
        ${select("tool", "Tool", app.tool, facets.tool || facets.tool_name)}
        ${select("verdict", "Verdict", app.verdict, facets.verdict)}
        ${app.raw ? null : select("tag", "Tag", app.tag, ["flagged", "sensitive", "destination", "unsandboxed"])}
        <input aria-label="Search the summary" placeholder="Search the summary" value=${app.q}
          onInput=${(event) => queueQuery(event.target.value)} />
        <button class="btn" type="button" aria-pressed=${app.raw ? "true" : "false"} onClick=${() => update({ raw: !app.raw }, "push")}>
          ${app.raw ? "Showing hook events" : "Showing actions"}
        </button>
      </div>
      ${chips(app)}
      ${app.raw ? this.events(rows) : this.actions(rows)}
      ${rows.length < data.total
        ? html`<button class="btn" type="button" onClick=${() => this.more(data)}>Load more (${data.total - rows.length} left)</button>`
        : null}`;
  }

  componentDidUpdate(previous) {
    super.componentDidUpdate(previous);
  }

  actions(rows) {
    return html`<${DataTable}
      tableId="activity"
      caption="Actions"
      exportName="actions"
      rows=${rows}
      rowKey=${(row) => row.id}
      onRow=${(row) => update({ open: "capture:" + row.id }, "push")}
      meta=${rows.length + " loaded"}
      detail=${(row) => html`<ul class="hook-events">${(row.events || []).map(
        (name, index) => html`<li key=${(row.capture_ids && row.capture_ids[index]) || index}>
          <${EventName} name=${name} />
          <button class="btn mini" type="button" onClick=${(event) => {
            event.stopPropagation();
            update({ open: "capture:" + (row.capture_ids && row.capture_ids[index]) }, "push");
          }}>Open</button>
        </li>`,
      )}</ul>`}
      columns=${[
        { id: "ts", label: "When", sortValue: (row) => row.ts_ms, render: (row) => html`<${When} ts=${row.ts} />` },
        { id: "agent", label: "Agent", sortValue: (row) => row.agent, render: (row) => row.agent },
        { id: "session", label: "Session", sortValue: (row) => row.session || "", render: (row) => html`<button class="btn mini" type="button" onClick=${(event) => { event.stopPropagation(); update({ view: "session", sessionKey: row.session_key, open: "" }, "push"); }}>${(row.session || "–").slice(0, 8)}</button>` },
        { id: "tool", label: "Tool", sortValue: (row) => row.tool || row.action_type, render: (row) => html`<${ToolLabel} name=${row.tool || row.action_type} mcp=${row.action_type === "MCP call"} />` },
        { id: "summary", label: "Summary", truncate: true, sortValue: (row) => row.summary || "", exportValue: (row) => row.summary || "", render: (row) => html`<${Summary} text=${row.summary} />` },
        { id: "verdict", label: "Verdict", sortValue: (row) => row.verdict, render: (row) => html`<${Verdict} verdict=${row.verdict} mode=${this.props.app.mode} />` },
        { id: "tags", label: "Tags", sortValue: (row) => (row.sensitive || []).length + (row.destinations || []).length, render: (row) => tags(row) },
        { id: "duration", label: "Duration", sortValue: (row) => row.duration_ms ?? row.latency_ms ?? -1, render: (row) => duration(row.duration_ms ?? row.latency_ms) },
      ]} />`;
  }

  events(rows) {
    return html`<${DataTable}
      tableId="events"
      caption="Hook events"
      exportName="events"
      rows=${rows}
      rowKey=${(row) => (row.capture_id || "") + (row.run_id || "")}
      onRow=${(row) => update({ open: row.capture_id ? "capture:" + row.capture_id : "run:" + row.run_id }, "push")}
      columns=${[
        { id: "ts", label: "When", sortValue: (row) => row.ts_ms, render: (row) => html`<${When} ts=${row.ts} />` },
        { id: "agent", label: "Agent", sortValue: (row) => row.agent },
        { id: "event", label: "Event", sortValue: (row) => row.event, render: (row) => html`<${EventName} name=${row.event} />` },
        { id: "tool", label: "Tool", sortValue: (row) => row.tool_name || "", render: (row) => html`<${ToolLabel} name=${row.tool_name} />` },
        { id: "outcome", label: "Outcome", sortValue: (row) => row.outcome || "", render: (row) => row.outcome || "–" },
        { id: "verdict", label: "Verdict", sortValue: (row) => row.verdict || "", render: (row) => html`<${Verdict} verdict=${row.verdict} mode=${this.props.app.mode} />` },
        { id: "latency", label: "Latency", sortValue: (row) => row.latency_ms ?? -1, render: (row) => duration(row.latency_ms) },
      ]} />`;
  }

}

function filters(app) {
  return {
    agent: app.agent,
    q: app.q,
    tool: app.tool,
    verdict: app.verdict,
    tag: app.tag,
    type: app.type,
  };
}

function select(key, label, value, options) {
  const list = options || [];
  return html`<select aria-label=${label} value=${value || ""} onChange=${(event) => update({ [key]: event.target.value }, "push")}>
    <option value="">${label}: any</option>
    ${list.map((item) => html`<option key=${item} value=${item}>${item}</option>`)}
  </select>`;
}

function chips(app) {
  const items = [
    ["agent", app.agent],
    ["type", app.type],
    ["tool", app.tool],
    ["verdict", app.verdict],
    ["tag", app.tag],
    ["q", app.q],
  ].filter((item) => item[1]);
  if (!items.length) return null;
  return html`<div class="chips">${items.map(
    ([key, value]) => html`<button class="chip" type="button" key=${key} onClick=${() => update({ [key]: "" }, "push")}>${key}: ${value} ×</button>`,
  )}</div>`;
}

function tags(row) {
  const bits = [];
  (row.sensitive || []).forEach((label) => bits.push(html`<span class="tag warn" key=${label}>${label}</span>`));
  (row.destinations || []).forEach((host) => bits.push(html`<span class="tag" key=${host}>${host}</span>`));
  if (row.unsandboxed) bits.push(html`<span class="tag warn" key="sand">unsandboxed</span>`);
  if (row.bypass) bits.push(html`<span class="tag warn" key="bypass">bypass</span>`);
  return bits.length ? bits : html`<span class="muted">–</span>`;
}

let queryTimer = null;
function queueQuery(value) {
  clearTimeout(queryTimer);
  queryTimer = setTimeout(() => update({ q: value }, "replace"), 250);
}
