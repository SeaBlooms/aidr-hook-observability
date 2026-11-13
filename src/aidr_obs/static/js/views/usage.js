import { getJSON } from "../api.js";
import { StackedChart } from "../components/chart.js";
import { DataTable } from "../components/table.js";
import { EventName, LoadView, duration, html } from "../components/ui.js";
import { compact } from "../format.js";
import { load, save } from "../prefs.js";

export class Usage extends LoadView {
  constructor() {
    super();
    const prefs = load();
    this.state = { ...this.state, split: true, pricesOn: !!prefs.pricesOn, prices: prefs.prices || {} };
  }

  fetch(app) {
    return getJSON("/api/usage", app);
  }

  setPrice(model, field, value) {
    const prices = { ...this.state.prices, [model]: { ...(this.state.prices[model] || {}), [field]: value } };
    this.setState({ prices });
    save({ prices, pricesOn: true });
  }

  body(data) {
    const input = (data.token_series || []).map((row) => ({ t: Date.parse(row.t + ":00"), counts: { input_tokens: row.input_tokens || 0 } }));
    const output = (data.token_series || []).map((row) => ({ t: Date.parse(row.t + ":00"), counts: { output_tokens: row.output_tokens || 0 } }));
    const both = (data.token_series || []).map((row) => ({
      t: Date.parse(row.t + ":00"),
      counts: { input_tokens: row.input_tokens || 0, output_tokens: row.output_tokens || 0 },
    }));
    return html`<section class="card">
        <h2>Tokens by model</h2>
        <p class="muted">Cursor reports token counts. Claude Code hooks don't. Cost estimates stay off until you turn them on and type prices.</p>
        <label class="check"><input type="checkbox" checked=${this.state.pricesOn} onChange=${(event) => { this.setState({ pricesOn: event.target.checked }); save({ pricesOn: event.target.checked }); }} /> Estimate cost</label>
        <${DataTable}
          tableId="tokens"
          caption="Tokens by model"
          exportName="tokens"
          rows=${data.tokens_by_model || []}
          rowKey=${(row) => row.model}
          empty="No token data yet"
          columns=${[
            { id: "model", label: "Model", sortValue: (row) => row.model },
            { id: "responses", label: "Responses", sortValue: (row) => row.responses },
            { id: "input_tokens", label: "Input", sortValue: (row) => row.input_tokens, render: (row) => compact(row.input_tokens) },
            { id: "output_tokens", label: "Output", sortValue: (row) => row.output_tokens, render: (row) => compact(row.output_tokens) },
            { id: "cache_read_tokens", label: "Cache read", sortValue: (row) => row.cache_read_tokens, render: (row) => compact(row.cache_read_tokens) },
            { id: "cache_write_tokens", label: "Cache write", sortValue: (row) => row.cache_write_tokens, render: (row) => compact(row.cache_write_tokens) },
            ...(this.state.pricesOn
              ? [{ id: "cost", label: "Est. cost", sortValue: (row) => costOf(row, this.state.prices), render: (row) => priceCell(row, this.state.prices, (field, value) => this.setPrice(row.model, field, value)) }]
              : []),
          ]} />
        ${data.redacted_token_responses
          ? html`<p class="muted">${data.redacted_token_responses} earlier responses had token counts masked and aren't counted.</p>`
          : null}
        <div class="row-actions">
          <button class="btn" type="button" aria-pressed=${this.state.split ? "true" : "false"} onClick=${() => this.setState({ split: !this.state.split })}>
            ${this.state.split ? "Separate scales" : "One scale"}
          </button>
        </div>
        ${this.state.split
          ? html`<h3>Input tokens</h3><${StackedChart} series=${input} bucketMs=${3600000} label="Input tokens per hour" />
              <h3>Output tokens</h3><${StackedChart} series=${output} bucketMs=${3600000} label="Output tokens per hour" />`
          : html`<${StackedChart} series=${both} bucketMs=${3600000} label="Tokens per hour" />`}
      </section>
      <section class="card"><h2>Projects</h2>
        <${DataTable} tableId="projects" caption="Projects" exportName="projects" rows=${data.projects || []} rowKey=${(row) => row.project}
          columns=${[
            { id: "name", label: "Project", sortValue: (row) => row.name, render: (row) => html`<strong>${row.name}</strong><div class="muted">${row.project}</div>` },
            { id: "agents", label: "Agents", sortValue: (row) => (row.agents || []).join(","), render: (row) => (row.agents || []).join(", ") },
            { id: "sessions", label: "Sessions", sortValue: (row) => row.sessions },
            { id: "prompts", label: "Prompts", sortValue: (row) => row.prompts },
            { id: "tool_calls", label: "Tool calls", sortValue: (row) => row.tool_calls },
            { id: "flagged", label: "Flagged", sortValue: (row) => row.flagged || 0 },
            { id: "sensitive", label: "Sensitive", sortValue: (row) => row.sensitive || 0 },
          ]} />
      </section>
      <div class="split">
        <section class="card"><h2>Tool run time</h2>
          <${DataTable} tableId="tool-perf" caption="Tool run time" exportName="tool-perf" rows=${data.tool_perf || []} rowKey=${(row) => row.agent + row.tool}
            empty="No post-tool events yet"
            columns=${perfColumns(false)} />
        </section>
        <section class="card"><h2>Hook latency</h2>
          <${DataTable} tableId="hook-perf" caption="Hook latency" exportName="hook-perf" rows=${data.hook_perf || []} rowKey=${(row) => row.agent + row.event}
            empty="No captures yet"
            columns=${perfColumns(true)} />
        </section>
      </div>`;
  }
}

function perfColumns(hook) {
  return [
    { id: "agent", label: "Agent", sortValue: (row) => row.agent },
    hook
      ? { id: "event", label: "Event", sortValue: (row) => row.event, render: (row) => html`<${EventName} name=${row.event} />` }
      : { id: "tool", label: "Tool", sortValue: (row) => row.tool },
    { id: "count", label: "Runs", sortValue: (row) => row.count },
    { id: "p50", label: "p50", sortValue: (row) => row.p50, render: (row) => duration(row.p50) },
    { id: "p95", label: "p95", sortValue: (row) => row.p95, render: (row) => duration(row.p95) },
    { id: "max", label: "Max", sortValue: (row) => row.max, render: (row) => duration(row.max) },
  ];
}

function costOf(row, prices) {
  const price = prices[row.model] || {};
  const input = Number(price.in) || 0;
  const output = Number(price.out) || 0;
  return ((row.input_tokens || 0) * input + (row.output_tokens || 0) * output) / 1e6;
}

function priceCell(row, prices, onChange) {
  const price = prices[row.model] || {};
  const estimate = costOf(row, prices);
  return html`<span>$${estimate ? estimate.toFixed(2) : "0.00"}</span>
    <label class="price">in/M <input inputmode="decimal" value=${price.in || ""} onClick=${(event) => event.stopPropagation()} onInput=${(event) => onChange("in", event.target.value)} /></label>
    <label class="price">out/M <input inputmode="decimal" value=${price.out || ""} onClick=${(event) => event.stopPropagation()} onInput=${(event) => onChange("out", event.target.value)} /></label>`;
}
