import { getJSON } from "../api.js";
import { DataTable } from "../components/table.js";
import { LoadView, When, duration, html, verdictTags } from "../components/ui.js";
import { go } from "../state.js";

export class Sessions extends LoadView {
  fetch(app) {
    return getJSON("/api/sessions", app);
  }

  body(data) {
    const mode = this.props.app.mode;
    return html`<${DataTable}
      tableId="sessions"
      caption="Sessions"
      exportName="sessions"
      rows=${data.items || []}
      rowKey=${(row) => row.key}
      onRow=${(row) => go("session", { sessionKey: row.key })}
      empty="No sessions in this range"
      columns=${[
        { id: "last", label: "Last", sortValue: (row) => row.last_ms, render: (row) => html`<${When} ts=${row.last} />` },
        { id: "agent", label: "Agent", sortValue: (row) => row.agent },
        { id: "session", label: "Session", sortValue: (row) => row.session || "", render: (row) => (row.session || "?").slice(0, 8) },
        { id: "project", label: "Project", sortValue: (row) => (row.projects || []).join(","), render: (row) => (row.projects || []).map((path) => path.split("/").pop()).join(", ") || "–" },
        { id: "model", label: "Model", sortValue: (row) => (row.models || []).join(","), render: (row) => (row.models || []).join(", ") || "–" },
        { id: "prompts", label: "Prompts", sortValue: (row) => row.prompts, render: (row) => row.prompts },
        { id: "tools", label: "Tools", sortValue: (row) => row.tool_calls, render: (row) => row.tool_calls },
        { id: "verdicts", label: "Verdicts", sortValue: (row) => Object.values(row.verdicts || {}).reduce((a, b) => a + b, 0), render: (row) => verdictTags(row.verdicts, mode) },
        { id: "sensitive", label: "Sensitive", sortValue: (row) => row.sensitive || 0 },
        { id: "destinations", label: "Destinations", sortValue: (row) => row.destinations || 0 },
        { id: "span", label: "Span", sortValue: (row) => row.duration_ms || 0, render: (row) => duration(row.duration_ms) },
      ]} />`;
  }
}
