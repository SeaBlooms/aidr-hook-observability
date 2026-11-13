import { Component, html } from "../html.js";

const PALETTE = ["var(--c1)", "var(--c2)", "var(--c3)", "var(--c4)", "var(--c5)", "var(--c6)", "var(--c7)", "var(--c8)"];

const NAMED = {
  Prompt: "var(--c1)",
  Shell: "var(--c2)",
  "Read file": "var(--c3)",
  "File edit": "var(--c4)",
  "MCP call": "var(--c5)",
  Tool: "var(--c6)",
  Response: "var(--c7)",
  block: "var(--block)",
  ask: "var(--ask)",
  mask: "var(--mask)",
  context: "var(--context)",
  allow: "var(--allow)",
  dry_run: "var(--dry)",
  input_tokens: "var(--c1)",
  output_tokens: "var(--c5)",
};

function colorOf(name, index) {
  return NAMED[name] || PALETTE[index % PALETTE.length];
}

function tickLabel(ts, bucketMs) {
  const date = new Date(ts);
  if (bucketMs >= 86_400_000) return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export class StackedChart extends Component {
  constructor() {
    super();
    this.state = { drag: null };
  }

  keys() {
    return [...new Set((this.props.series || []).flatMap((bucket) => Object.keys(bucket.counts || {})))];
  }

  indexAt(event) {
    const svg = this.node;
    const series = this.props.series || [];
    if (!svg || !series.length) return 0;
    const rect = svg.getBoundingClientRect();
    const viewW = 1000;
    const pad = 52;
    const x = ((event.clientX - rect.left) / rect.width) * viewW;
    const inner = viewW - pad - 8;
    const index = Math.floor(((x - pad) / inner) * series.length);
    return Math.max(0, Math.min(series.length - 1, index));
  }

  finish(event) {
    const drag = this.state.drag;
    this.setState({ drag: null });
    if (!drag || !this.props.onPick) return;
    const series = this.props.series || [];
    const end = this.indexAt(event);
    const from = Math.min(drag.start, end);
    const to = Math.max(drag.start, end);
    const bucket = Number(this.props.bucketMs) || 0;
    const type = from === to ? drag.type || "" : "";
    this.props.onPick({
      from: series[from].t,
      to: series[to].t + bucket,
      type,
    });
  }

  render() {
    const series = this.props.series || [];
    const keys = this.keys();
    if (!series.length || !keys.length) return html`<div class="empty">${this.props.empty || "No activity in this range"}</div>`;
    const width = 1000;
    const height = 200;
    const padLeft = 52;
    const padBottom = 28;
    const max = Math.max(1, ...series.map((bucket) => Object.values(bucket.counts || {}).reduce((sum, n) => sum + n, 0)));
    const barW = (width - padLeft - 8) / series.length;
    const plotH = height - padBottom - 12;
    const rects = [];
    series.forEach((bucket, index) => {
      let y = 12 + plotH;
      keys.forEach((key) => {
        const value = bucket.counts[key] || 0;
        if (!value) return;
        const h = (plotH * value) / max;
        y -= h;
        rects.push({
          key,
          index,
          x: padLeft + index * barW + 1,
          y,
          w: Math.max(1, barW - 2),
          h,
          title: tickLabel(bucket.t, this.props.bucketMs) + " " + key + ": " + value,
        });
      });
    });
    const ticks = [0, Math.floor(series.length / 2), series.length - 1].filter((value, index, all) => all.indexOf(value) === index);
    const drag = this.state.drag;
    const summary = keys.map((key) => key + " " + series.reduce((sum, bucket) => sum + (bucket.counts[key] || 0), 0)).join(", ");
    return html`<div class="chart">
      <svg viewBox=${"0 0 " + width + " " + height} role="img" aria-label=${this.props.label || "Activity chart"}
        ref=${(node) => {
          this.node = node;
        }}
        onPointerDown=${(event) => {
          if (!this.props.onPick) return;
          const target = event.target.closest ? event.target.closest("[data-key]") : null;
          const start = this.indexAt(event);
          this.setState({ drag: { start, end: start, type: target ? target.dataset.key : "" } });
          event.currentTarget.setPointerCapture(event.pointerId);
        }}
        onPointerMove=${(event) => {
          const drag = this.state.drag;
          if (!drag) return;
          const end = this.indexAt(event);
          if (end !== drag.end) this.setState({ drag: { ...drag, end } });
        }}
        onPointerUp=${(event) => this.finish(event)}>
        <text x="4" y="16">${max}</text>
        <line x1=${padLeft} x2=${width - 4} y1=${12 + plotH} y2=${12 + plotH} stroke="var(--border)" />
        ${rects.map(
          (rect) => html`<rect key=${rect.key + rect.index} data-key=${rect.key} x=${rect.x} y=${rect.y} width=${rect.w} height=${Math.max(rect.h, 0.5)}
            fill=${colorOf(rect.key, keys.indexOf(rect.key))}>
            <title>${rect.title}</title>
          </rect>`,
        )}
        ${drag
          ? html`<rect x=${padLeft + Math.min(drag.start, drag.end ?? drag.start) * barW} y="12" width=${(Math.abs((drag.end ?? drag.start) - drag.start) + 1) * barW} height=${plotH} fill="var(--accent)" opacity="0.15" />`
          : null}
        ${ticks.map((index) => html`<text key=${index} x=${padLeft + index * barW} y=${height - 8} text-anchor=${index === 0 ? "start" : index === series.length - 1 ? "end" : "middle"}>
          ${tickLabel(series[index].t, this.props.bucketMs)}
        </text>`)}
      </svg>
      <div class="legend">
        ${keys.map((key, index) => html`<span key=${key}><i style=${{ background: colorOf(key, index) }}></i>${key}</span>`)}
      </div>
      <div class="sr-only"><table><caption>${this.props.label || "Activity"}</caption>
        <thead><tr><th>Time</th>${keys.map((key) => html`<th key=${key}>${key}</th>`)}</tr></thead>
        <tbody>
          ${series.map(
            (bucket) => html`<tr key=${bucket.t}><td>${tickLabel(bucket.t, this.props.bucketMs)}</td>${keys.map((key) => html`<td key=${key}>${bucket.counts[key] || 0}</td>`)}</tr>`,
          )}
        </tbody>
      </table></div>
      <p class="sr-only">${summary}</p>
    </div>`;
  }
}
