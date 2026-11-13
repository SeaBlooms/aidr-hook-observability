import { getJSON } from "../api.js";
import { StackedChart } from "../components/chart.js";
import { DryRunBanner, LoadView, Summary, When, html } from "../components/ui.js";
import { shortId } from "../format.js";
import { go, update } from "../state.js";

const LEVEL = { green: "Quiet", amber: "Check health", red: "Needs attention" };

export class Overview extends LoadView {
  fetch(app) {
    return getJSON("/api/overview", app);
  }

  body(data) {
    const dest = data.destinations || {};
    const destTotal = (dest.public_ip || 0) + (dest.domain || 0) + (dest.private_ip || 0);
    return html`<${DryRunBanner} mode=${data.mode} />
      <section class="stats" aria-label="Needs attention">
        ${card("Flagged", data.flagged, "block, ask, mask, context", "a-flag", () => go("activity", { tag: "flagged" }))}
        ${card("Sensitive", data.sensitive, "credential and key access", "a-sens", () => go("sensitive"))}
        ${card(dest.in_window ? "New destinations" : "Destinations", destTotal,
          (dest.public_ip || 0) + " public IP · " + (dest.domain || 0) + " domain · " + (dest.private_ip || 0) + " private",
          "a-dest", () => go("destinations"))}
        ${card("Unsandboxed", (data.unsandboxed || 0) + (data.bypass || 0),
          (data.unsandboxed || 0) + " outside the sandbox · " + (data.bypass || 0) + " bypass",
          "a-sand", () => go("sensitive"))}
        ${card("Hook health", LEVEL[data.health.level] || data.health.level, healthDetail(data.health), "a-" + data.health.level, () => go("health"))}
      </section>
      <section class="card">
        <h2>Recent actions</h2>
        ${data.feed && data.feed.length
          ? html`<ol class="feed" aria-live="polite" aria-relevant="additions">
              ${data.feed.map(
                (row) => html`<li key=${row.id}><button type="button" class="feed-row" onClick=${() => update({ open: "capture:" + row.id }, "push")}>
                  <span>${row.agent}</span>
                  <span>${row.action_type}</span>
                  <${Summary} text=${row.summary} limit=${72} />
                  <span class="muted">${shortId(row.session)}</span>
                  <${When} ts=${row.ts} />
                </button></li>`,
              )}
            </ol>`
          : html`<p class="empty">No actions in this range. Install the tap to capture them.</p>`}
      </section>
      <section class="card">
        <h2>Activity by action</h2>
        <${StackedChart} series=${data.series} bucketMs=${data.bucket_ms} label="Actions over time"
          onPick=${(pick) =>
            go("activity", {
              type: pick.type || "",
              range: "custom",
              from: String(pick.from),
              to: String(pick.to),
            })} />
        <p class="muted">Drag across the chart, or click a bar, to open that slice in Activity.</p>
      </section>`;
  }
}

function card(title, stat, detail, cls, onClick) {
  return html`<button type="button" class=${"card attention " + cls} onClick=${onClick}>
    <h2>${title}</h2>
    <div class="stat">${stat}</div>
    <p>${detail}</p>
  </button>`;
}

function healthDetail(health) {
  if (!health) return "";
  const parts = [];
  if (health.failures) parts.push(health.failures + " failed");
  if (health.errors) parts.push(health.errors + " errors");
  if (health.tap_errors) parts.push(health.tap_errors + " tap errors");
  if (health.timeouts) parts.push(health.timeouts + " timeouts");
  if (health.noise) parts.push(health.noise + " known noise");
  return parts.length ? parts.join(" · ") : "No failures in this range";
}
