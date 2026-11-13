import { getJSON } from "../api.js";
import { StackedChart } from "../components/chart.js";
import { DataTable } from "../components/table.js";
import { DryRunBanner, EventName, LoadView, ToolLabel, Verdict, When, html } from "../components/ui.js";
import { clip } from "../format.js";
import { update } from "../state.js";

export class Verdicts extends LoadView {
  constructor() {
    super();
    this.state = { ...this.state, tool: "" };
  }

  fetch(app) {
    return getJSON("/api/verdicts", app);
  }

  body(data) {
    const mode = this.props.app.mode;
    const tools = data.tools || [];
    const selected = this.state.tool || (tools[0] && tools[0].tool) || "";
    const series = ((data.tool_series || []).find((item) => item.tool === selected) || {}).points || [];
    const chartSeries = series.map((point) => ({ t: point.t, counts: point.verdicts || {} }));
    return html`<${DryRunBanner} mode=${mode} />
      <section class="card">
        <h2>Decisions that changed behavior</h2>
        <${DataTable}
          tableId="verdicts"
          caption="Flagged decisions"
          exportName="verdicts"
          rows=${data.items || []}
          rowKey=${(row) => row.id}
          onRow=${(row) => update({ open: "capture:" + row.id }, "push")}
          empty="No block, ask, mask or context verdicts in this range"
          columns=${[
            { id: "ts", label: "When", sortValue: (row) => row.ts_ms, render: (row) => html`<${When} ts=${row.ts} />` },
            { id: "verdict", label: "Verdict", sortValue: (row) => row.verdict, render: (row) => html`<${Verdict} verdict=${row.verdict} mode=${mode} />` },
            { id: "event", label: "Event", sortValue: (row) => row.event, render: (row) => html`<${EventName} name=${row.event} />` },
            { id: "tool", label: "Tool", sortValue: (row) => row.tool_name || "", render: (row) => html`<${ToolLabel} name=${row.tool_name} />` },
            { id: "reason", label: "Reason", truncate: true, sortValue: (row) => row.reason || "", exportValue: (row) => row.reason || "", render: (row) => clip(row.reason || "", 180) },
          ]} />
      </section>
      <section class="card">
        <h2>Verdicts by tool</h2>
        <${DataTable}
          tableId="verdict-tools"
          caption="Verdicts by tool"
          exportName="verdicts-by-tool"
          rows=${tools}
          rowKey=${(row) => row.tool}
          onRow=${(row) => this.setState({ tool: row.tool })}
          empty="No tool calls in this range"
          columns=${[
            { id: "tool", label: "Tool", sortValue: (row) => row.tool, render: (row) => html`<${ToolLabel} name=${row.tool} mcp=${row.mcp} />` },
            { id: "n", label: "Calls", sortValue: (row) => row.n },
            { id: "mix", label: "Mix", sortValue: (row) => row.n, render: (row) => mix(row.verdicts, mode) },
          ]} />
        ${tools.length
          ? html`<label class="range">Tool
              <select aria-label="Tool over time" value=${selected} onChange=${(event) => this.setState({ tool: event.target.value })}>
                ${tools.map((row) => html`<option key=${row.tool} value=${row.tool}>${row.tool}</option>`)}
              </select>
            </label>
            <${StackedChart} series=${chartSeries} bucketMs=${data.bucket_ms} label=${"Verdicts for " + selected} />`
          : null}
      </section>
      <section class="card">
        <h2>Prompts and responses</h2>
        <${DataTable}
          tableId="conversations"
          caption="Prompts and responses"
          exportName="prompts-responses"
          rows=${data.conversations || []}
          rowKey=${(row) => row.kind + row.event}
          empty="No prompts or responses in this range"
          columns=${[
            { id: "kind", label: "Kind", sortValue: (row) => row.kind },
            { id: "event", label: "Event", sortValue: (row) => row.event, render: (row) => html`<${EventName} name=${row.event} />` },
            { id: "n", label: "Count", sortValue: (row) => row.n },
            { id: "mix", label: "Mix", sortValue: (row) => row.n, render: (row) => mix(row.verdicts, mode) },
          ]} />
      </section>`;
  }
}

function mix(counts, mode) {
  return Object.entries(counts || {})
    .filter(([name]) => !(mode === "dry_run" && name === "dry_run"))
    .sort((a, b) => b[1] - a[1])
    .map(([name, n]) => html`<span class="pill v-${name}" key=${name}>${name} ${n}</span>`);
}
