import { getJSON } from "../api.js";
import { Bars, LoadView, Summary, Verdict, When, html, verdictTags } from "../components/ui.js";
import { go, update } from "../state.js";

export class Destinations extends LoadView {
  fetch(app) {
    return getJSON("/api/destinations", app);
  }

  body(data) {
    const mode = this.props.app.mode;
    return html`<p class="banner">Destinations are parsed from what the agent asked to do: URLs, hosts in shell commands, and MCP server names. The hooks don't see network traffic, so these are intended targets, not resolved IPs.</p>
      <section class="card"><h2>By class</h2><${Bars} rows=${data.by_class} /></section>
      <section class="card"><h2>Destinations</h2>
        ${(data.items || []).length
          ? html`<div class="table-wrap"><table>
              <caption class="sr-only">Destinations, most severe verdict first</caption>
              <thead><tr><th>Target</th><th>Kind</th><th>Class</th><th>Actions</th><th>Verdicts</th><th>Agents</th><th>Last</th></tr></thead>
              <tbody>
                ${(data.items || []).map((row) => html`<tr key=${row.kind + row.target}>
                  <td><button class="btn link" type="button" onClick=${() => go("graph", { focus: (row.kind === "mcp" ? "mcp:" : "dest:") + row.target })}>${row.target}</button></td>
                  <td>${row.kind}</td>
                  <td><span class=${"c-" + row.class}>${row.class}</span></td>
                  <td>${row.count}</td>
                  <td>${verdictTags(row.verdicts, mode)}</td>
                  <td>${(row.agents || []).join(", ")}</td>
                  <td><${When} ts=${row.last} /></td>
                </tr>
                <tr key=${row.kind + row.target + "-ex"}><td colspan="7">
                  ${(row.examples || []).map(
                    (example) => html`<button class="tool" type="button" key=${example.capture_id} onClick=${() => update({ open: "capture:" + example.capture_id }, "push")}>
                      <${Verdict} verdict=${example.verdict} mode=${mode} />
                      <span class="muted"><${When} ts=${example.ts} /> ${example.tool || ""}</span>
                      <${Summary} text=${example.summary} limit=${160} />
                    </button>`,
                  )}
                </td></tr>`)}
              </tbody>
            </table></div>`
          : html`<p class="empty">No destinations in this range</p>`}
      </section>`;
  }
}
