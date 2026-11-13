import { getJSON } from "../api.js";
import { LoadView, When, html, verdictTags } from "../components/ui.js";
import { go, update } from "../state.js";

export class Mcp extends LoadView {
  fetch(app) {
    return getJSON("/api/mcp", app);
  }

  body(data) {
    const mode = this.props.app.mode;
    return html`<p class="banner">The hook log records whether a config file contains MCP servers, not the server names. Names below come from tool calls. A snapshot ratio such as 1 / 2 means one of the two files checked had servers.</p>
      <section class="card"><h2>Servers the agent called</h2>
        ${(data.called || []).length
          ? html`<table><thead><tr><th>Server</th><th>Calls</th><th>Agents</th><th>Verdicts</th><th>Last</th></tr></thead>
              <tbody>${data.called.map(
                (row) => html`<tr key=${row.server}><td><button class="btn link" type="button" onClick=${() => go("graph", { focus: "mcp:" + row.server })}>${row.server}</button></td>
                  <td>${row.count}</td><td>${(row.agents || []).join(", ")}</td><td>${verdictTags(row.verdicts, mode)}</td><td><${When} ts=${row.last} /></td></tr>`,
              )}</tbody></table>`
          : html`<p class="empty">No MCP tool calls in this range</p>`}
      </section>
      <section class="card"><h2>Config sources</h2>
        ${(data.sources || []).length
          ? html`<table><thead><tr><th>Scope</th><th>Kind</th><th>Path</th><th>Servers</th><th>Changes</th><th>Last seen</th></tr></thead>
              <tbody>${data.sources.map(
                (row) => html`<tr key=${row.scope + row.path}><td>${row.scope}</td><td>${row.kind}</td><td>${row.path}</td>
                  <td>${row.has_servers ? "has servers" : "empty"}</td><td>${row.changes}</td><td><${When} ts=${row.last_seen} /></td></tr>`,
              )}</tbody></table>`
          : html`<p class="empty">No MCP discovery in this range</p>`}
      </section>
      <section class="card"><h2>Changes</h2>
        ${(data.changes || []).length
          ? html`<ol class="feed">${data.changes.map(
              (row) => html`<li key=${row.ts + row.path}><button class="feed-row" type="button" onClick=${() => update({ open: "run:" + row.run_id }, "push")}>
                <${When} ts=${row.ts} />
                <span>${row.scope}</span>
                <span>${row.before ? "had servers" : "empty"} → ${row.after ? "has servers" : "empty"}</span>
                <code>${row.path}</code>
              </button></li>`,
            )}</ol>`
          : html`<p class="empty">No source changed between snapshots</p>`}
      </section>
      <section class="card"><h2>Snapshots</h2>
        <p class="muted">Identical consecutive snapshots are one row. "Unchanged since" is how long that inventory stayed the same.</p>
        ${(data.collapsed || []).length
          ? html`<div class="table-wrap"><table><thead><tr><th>When</th><th>Agent</th><th>Project</th><th>Artifacts</th><th>Sources with servers</th><th>Repeats</th></tr></thead>
              <tbody>${data.collapsed.map(
                (row) => html`<tr key=${row.run_id} class="clickable" onClick=${() => update({ open: "run:" + row.run_id }, "push")} tabindex="0"
                  onKeyDown=${(event) => { if (event.key === "Enter") update({ open: "run:" + row.run_id }, "push"); }}>
                  <td><${When} ts=${row.ts} />${row.repeats > 1 ? html`<div class="muted">unchanged since then, last <${When} ts=${row.until} /></div>` : null}</td>
                  <td>${row.agent}</td><td>${row.project_root || ""}</td><td>${row.artifacts}</td>
                  <td title="Sources that contained servers, out of sources checked">${row.with_servers} / ${row.sources}</td>
                  <td>${row.repeats}</td>
                </tr>`,
              )}</tbody></table></div>`
          : html`<p class="empty">No snapshots</p>`}
      </section>`;
  }
}
