import { getJSON } from "../api.js";
import { LoadView, Summary, ToolLabel, Verdict, When, duration, html, verdictTags } from "../components/ui.js";
import { compact } from "../format.js";
import { go, update } from "../state.js";
import { GraphCanvas } from "./graph.js";

export class SessionPage extends LoadView {
  shouldReload(previous, next) {
    return super.shouldReload(previous, next) || previous.sessionKey !== next.sessionKey;
  }

  fetch(app) {
    return Promise.all([
      getJSON("/api/session", { since: 0 }, { key: app.sessionKey }),
      getJSON("/api/sessions", app),
    ]).then(([detail, list]) => ({ detail, list }));
  }

  body(data) {
    const detail = data.detail || { turns: [] };
    const items = (data.list && data.list.items) || [];
    const index = items.findIndex((row) => row.key === this.props.app.sessionKey);
    const current = index >= 0 ? items[index] : null;
    const counts = countTurns(detail.turns);
    const mode = this.props.app.mode;
    return html`<header class="session-head">
        <div>
          <h2>${this.props.app.sessionKey}</h2>
          <p class="muted">${current ? current.agent : ""} ${current && current.projects ? current.projects.join(", ") : ""} ${current && current.models ? current.models.join(", ") : ""}</p>
        </div>
        <div class="row-actions">
          <button class="btn" type="button" disabled=${index <= 0} onClick=${() => go("session", { sessionKey: items[index - 1].key })}>Previous</button>
          <button class="btn" type="button" disabled=${index < 0 || index >= items.length - 1} onClick=${() => go("session", { sessionKey: items[index + 1].key })}>Next</button>
          <button class="btn" type="button" onClick=${() => go("graph", { focus: "session:" + this.props.app.sessionKey })}>Graph</button>
        </div>
      </header>
      <div class="stats stats-4">
        ${stat("Prompts", counts.prompts)}
        ${stat("Tool calls", counts.tools)}
        ${stat("Span", current ? duration(current.duration_ms) : "–")}
        <div class="card"><h2>Verdicts</h2><div class="stat-row">${verdictTags(counts.verdicts, mode)}</div></div>
      </div>
      <section class="card">
        <h2>Timeline</h2>
        ${(detail.turns || []).map(
          (turn, index) => html`<article class="turn" key=${turn.turn || index}>
            <div class="muted"><${When} ts=${turn.ts} /></div>
            ${turn.prompt
              ? html`<div class="who">Prompt <${Verdict} verdict=${turn.prompt.verdict} mode=${mode} /></div>
                  <div class="clip">${turn.prompt.text || "(empty)"}</div>`
              : null}
            ${(turn.tools || []).map(
              (tool) => html`<button class="tool" type="button" key=${tool.capture_id} onClick=${() => update({ open: "capture:" + tool.capture_id }, "push")}>
                <${Verdict} verdict=${tool.verdict} mode=${mode} />
                <${ToolLabel} name=${tool.tool} />
                <span class="muted"><${When} ts=${tool.ts} /> ${tool.duration_ms != null ? "· " + duration(tool.duration_ms) : ""}</span>
                ${(tool.sensitive || []).map((label) => html`<span class="tag warn" key=${label}>${label}</span>`)}
                ${(tool.destinations || []).map((host) => html`<span class="tag" key=${host}>${host}</span>`)}
                <div><${Summary} text=${tool.summary} limit=${180} /></div>
              </button>`,
            )}
            ${turn.response
              ? html`<div class="who">Response <${Verdict} verdict=${turn.response.verdict} mode=${mode} />
                  ${turn.tokens && turn.tokens.output_tokens != null
                    ? html`<span class="muted">${compact(turn.tokens.input_tokens)} in / ${compact(turn.tokens.output_tokens)} out</span>`
                    : null}
                </div><div class="clip">${turn.response.text || "(no text captured)"}</div>`
              : null}
          </article>`,
        )}
        ${(detail.turns || []).length ? null : html`<p class="empty">No events in this session</p>`}
      </section>
      <section class="card">
        <h2>Session graph</h2>
        <${GraphCanvas} app=${this.props.app} focus=${"session:" + this.props.app.sessionKey} compact=${true} />
      </section>`;
  }
}

function countTurns(turns) {
  const verdicts = {};
  let prompts = 0;
  let tools = 0;
  (turns || []).forEach((turn) => {
    if (turn.prompt) {
      prompts += 1;
      bump(verdicts, turn.prompt.verdict);
    }
    (turn.tools || []).forEach((tool) => {
      tools += 1;
      bump(verdicts, tool.verdict);
    });
    if (turn.response) bump(verdicts, turn.response.verdict);
  });
  return { prompts, tools, verdicts };
}

function bump(map, verdict) {
  if (!verdict) return;
  map[verdict] = (map[verdict] || 0) + 1;
}

function stat(label, value) {
  return html`<div class="card"><h2>${label}</h2><div class="stat">${value}</div></div>`;
}
