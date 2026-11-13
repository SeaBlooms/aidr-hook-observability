import { getJSON, saveNoise } from "../api.js";
import { Bars, LoadView, When, duration, html } from "../components/ui.js";
import { load, save } from "../prefs.js";
import { update } from "../state.js";

const EXPLAIN = {
  "?": "Not recorded in the debug log.",
  none: "The hook looked for this and didn't find it.",
};

export class Health extends LoadView {
  constructor() {
    super();
    this._seen = !!load().seenSetup;
    this.state = { ...this.state, setup: null, rules: null, noiseError: "" };
  }

  fetch(app) {
    return Promise.all([getJSON("/api/errors", app), getJSON("/api/summary", app), getJSON("/api/setup", app), getJSON("/api/noise", app)]).then(
      ([errors, summary, setup, noise]) => ({ errors, summary, setup, noise }),
    );
  }

  body(data) {
    const errors = data.errors;
    const status = errors.status || {};
    const real = (errors.groups || []).filter((group) => !group.noise);
    const harmless = (errors.groups || []).filter((group) => group.noise);
    return html`<section class="card health-${status.level || "green"}">
        <h2>Status</h2>
        <div class="stat">${status.level || "green"}</div>
        <p>${status.failures || 0} failed sends · ${status.errors || 0} unexpected errors · ${status.timeouts || 0} timeouts · ${status.tap_errors || 0} tap errors · ${status.noise || 0} known noise, left out of the status</p>
      </section>
      ${this.checklist(data.setup)}
      <div class="stats stats-4">
        ${mini("Hook runs", data.summary.totals.runs)}
        ${mini("Captures", data.summary.totals.captures)}
        ${mini("p50 latency", duration(data.summary.latency.p50))}
        ${mini("POST outcome", Object.keys(data.summary.by_outcome || {})[0] || "–")}
      </div>
      <section class="card">
        <h2>Unexpected errors</h2>
        ${real.length ? errorTable(real) : html`<p class="empty">No unexpected errors in this range</p>`}
      </section>
      <section class="card">
        <h2>Known noise</h2>
        <p class="muted">These patterns are ignored by the health status. The first one is the optional remote-settings file the Noma hook checks for.</p>
        ${harmless.length ? errorTable(harmless, true) : html`<p class="empty">None of the logged errors matched the noise list</p>`}
        ${this.editor(data.noise)}
      </section>
      ${(errors.tap_errors || []).length
        ? html`<section class="card"><h2>Tap errors</h2><table><thead><tr><th>Count</th><th>Error</th><th>Last</th></tr></thead>
            <tbody>${errors.tap_errors.map((row) => html`<tr key=${row.tap_error}><td>${row.n}</td><td>${row.tap_error}</td><td><${When} ts=${row.last} /></td></tr>`)}</tbody></table></section>`
        : null}
      <div class="split">
        ${healthCard("API key source", errors.health.key_source)}
        ${healthCard("Installed config", errors.health.installed_config)}
        ${healthCard("Endpoint", errors.health.url, true)}
        ${healthCard("HTTP client", errors.health.sender)}
        ${healthCard("POST outcome", errors.health.outcome)}
      </div>
      <section class="card"><h2>Hook version</h2>
        ${(errors.versions || []).length
          ? html`<table><thead><tr><th>Agent</th><th>Version</th><th>Captures</th><th>Last</th></tr></thead>
              <tbody>${errors.versions.map((row) => html`<tr key=${row.agent + row.hook_version}><td>${row.agent}</td><td>${row.hook_version || "not recorded"}</td><td>${row.n}</td><td><${When} ts=${row.last} /></td></tr>`)}</tbody></table>`
          : html`<p class="empty">No captures yet</p>`}
      </section>`;
  }

  checklist(setup) {
    if (!setup) return null;
    const seen = this._seen == null ? false : this._seen;
    const show = setup.show || !seen;
    if (!show) return html`<p class="muted"><button class="btn mini" type="button" onClick=${() => { this._seen = false; save({ seenSetup: false }); this.forceUpdate(); }}>Setup checklist</button></p>`;
    return html`<section class="card"><h2>Setup</h2><ul class="checks">
      ${(setup.checks || []).map(
        (item) => html`<li key=${item.id} class=${item.ok ? "ok" : "bad"}><strong>${item.label}</strong> ${item.detail}${item.age_ms != null ? " · " + duration(item.age_ms) + " ago" : ""}</li>`,
      )}
    </ul>
    ${setup.show ? null : html`<button class="btn" type="button" onClick=${() => { save({ seenSetup: true }); this._seen = true; this.forceUpdate(); }}>Hide checklist</button>`}
    </section>`;
  }

  editor(noise) {
    const rules = this.state.rules || (noise && noise.rules) || [];
    return html`<form class="noise-form" onSubmit=${(event) => this.addRule(event, rules)}>
      <h3>Noise list ${noise && noise.source === "user" ? "(edited on this machine)" : "(built-in)"}</h3>
      <ul>
        ${rules.map(
          (rule) => html`<li key=${rule.id}><code>${rule.contains || rule.pattern}</code> ${rule.explanation}
            <button class="btn mini" type="button" onClick=${() => this.save(rules.filter((item) => item.id !== rule.id))}>Remove</button></li>`,
        )}
      </ul>
      <label>Id <input name="id" required /></label>
      <label>Signature contains <input name="pattern" placeholder="read_json open/read <path>: FileNotFoundError" /></label>
      <label>Raw message contains <input name="contains" placeholder="remote-settings.json" /></label>
      <label>Explanation <input name="explanation" required /></label>
      <button class="btn" type="submit">Add pattern</button>
      ${this.state.noiseError ? html`<p role="alert">${this.state.noiseError}</p>` : null}
    </form>`;
  }

  addRule(event, rules) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = Object.fromEntries(new FormData(form).entries());
    this.save(rules.concat(data));
    form.reset();
  }

  save(rules) {
    saveNoise(rules)
      .then((body) => {
        this.setState({ rules: body.rules, noiseError: "" });
        update({ tick: this.props.app.tick + 1 }, "none");
      })
      .catch((err) => this.setState({ noiseError: err.message || String(err) }));
  }
}

function errorTable(groups, quiet) {
  return html`<div class="table-wrap"><table>
    <thead><tr><th>Count</th><th>Pattern</th><th>First</th><th>Last</th></tr></thead>
    <tbody>${groups.map(
      (group) => html`<tr key=${group.signature + (group.explanation || "")} class=${quiet ? "noise" : ""}>
        <td>${group.count}</td>
        <td><code>${group.signature}</code>
          <div class="muted">${(group.locations || []).join(", ")} · ${(group.agents || []).join(", ")}
            ${group.explanation ? " · " + group.explanation : ""}</div></td>
        <td><${When} ts=${group.first} /></td>
        <td><${When} ts=${group.last} /></td>
      </tr>`,
    )}</tbody>
  </table></div>`;
}

function healthCard(title, rows, endpoint) {
  const items = rows || [];
  const onlyPlaceholder = items.length > 0 && items.every((row) => row.value === "?" || row.value === "none");
  return html`<section class="card"><h2>${title}</h2>
    ${onlyPlaceholder
      ? html`<p class="muted">${EXPLAIN[items[0].value] || "No value was recorded."} ${items[0].n} runs.</p>`
      : html`<${Bars} rows=${items.map((row) => {
          const shown = explainValue(row.value, endpoint);
          return [shown.label, row.n, shown.title];
        })} />`}
  </section>`;
}

function explainValue(value, endpoint) {
  const text = String(value ?? "");
  if (value === "?" || value === "none") {
    const label = (EXPLAIN[value] || value) + " (" + value + ")";
    return { label, title: label };
  }
  if (endpoint && text.length > 42) return { label: text.slice(0, 42) + "…", title: text };
  return { label: text, title: text };
}

function mini(label, value) {
  return html`<div class="card"><h2>${label}</h2><div class="stat">${value}</div></div>`;
}
