import { getJSON } from "../api.js";
import { DataTable } from "../components/table.js";
import { Bars, LoadView, Verdict, When, html } from "../components/ui.js";
import { go, update } from "../state.js";

export class Sensitive extends LoadView {
  fetch(app) {
    return getJSON("/api/sensitive", app);
  }

  body(data) {
    const mode = this.props.app.mode;
    return html`<div class="split">
        <section class="card"><h2>Sensitive targets</h2>
          <${Bars} rows=${data.by_label} onPick=${(label) => go("graph", { focus: "sensitive:" + label })} />
        </section>
        <section class="card"><h2>Agent autonomy</h2>
          <div class="stat">${data.unsandboxed_count}<small>Cursor commands outside the sandbox</small></div>
          ${(data.permission_modes || []).length
            ? html`<table><thead><tr><th>Agent</th><th>Permission mode</th><th>Events</th></tr></thead>
                <tbody>${data.permission_modes.map(
                  (row) => html`<tr key=${row.agent + row.mode}><td>${row.agent}</td><td class=${row.mode === "bypassPermissions" ? "v-block" : ""}>${row.mode}</td><td>${row.events}</td></tr>`,
                )}</tbody></table>`
            : null}
        </section>
      </div>
      <section class="card">
        <h2>Reads, edits and commands</h2>
        <${DataTable}
          tableId="sensitive"
          caption="Sensitive access"
          exportName="sensitive"
          rows=${data.hits || []}
          rowKey=${(row) => row.capture_id + (row.labels || []).join(",")}
          onRow=${(row) => update({ open: "capture:" + row.capture_id }, "push")}
          empty="No sensitive access in this range"
          columns=${[
            { id: "ts", label: "When", sortValue: (row) => Date.parse(row.ts) || 0, render: (row) => html`<${When} ts=${row.ts} />` },
            { id: "agent", label: "Agent", sortValue: (row) => row.agent },
            { id: "tool", label: "Tool", sortValue: (row) => row.tool || "" },
            { id: "access", label: "Access", sortValue: (row) => row.access },
            { id: "labels", label: "Matched", sortValue: (row) => (row.labels || []).join(","), render: (row) => (row.labels || []).map((label) => html`<button class="tag warn" type="button" key=${label} onClick=${(event) => { event.stopPropagation(); go("graph", { focus: "sensitive:" + label }); }}>${label}</button>`) },
            { id: "verdict", label: "Verdict", sortValue: (row) => row.verdict, render: (row) => html`<${Verdict} verdict=${row.verdict} mode=${mode} />` },
            { id: "summary", label: "Target", truncate: true, sortValue: (row) => row.summary || "", exportValue: (row) => row.summary || "", render: (row) => html`<code title=${row.summary || ""}>${row.summary || ""}</code>` },
          ]} />
      </section>
      <section class="card">
        <h2>Unsandboxed Cursor shell commands</h2>
        <${DataTable}
          tableId="unsandboxed"
          caption="Unsandboxed commands"
          exportName="unsandboxed"
          rows=${data.unsandboxed || []}
          rowKey=${(row) => row.capture_id}
          onRow=${(row) => update({ open: "capture:" + row.capture_id }, "push")}
          empty="None in this range"
          columns=${[
            { id: "ts", label: "When", sortValue: (row) => Date.parse(row.ts) || 0, render: (row) => html`<${When} ts=${row.ts} />` },
            { id: "verdict", label: "Verdict", sortValue: (row) => row.verdict, render: (row) => html`<${Verdict} verdict=${row.verdict} mode=${mode} />` },
            { id: "summary", label: "Command", truncate: true, sortValue: (row) => row.summary || "", exportValue: (row) => row.summary || "", render: (row) => html`<code title=${row.summary || ""}>${row.summary || ""}</code>` },
          ]} />
      </section>`;
  }
}
