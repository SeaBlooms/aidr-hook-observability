import { getJSON } from "../api.js";
import { LoadView, Verdict, When, html } from "../components/ui.js";
import { Component } from "../html.js";
import { go, update } from "../state.js";

const NODE_TYPES = {
  agent: { label: "Agent", shape: "hexagon", color: "--accent", size: 46 },
  project: { label: "Project", shape: "round-rectangle", color: "--mask", size: 36 },
  session: { label: "Session", shape: "ellipse", color: "--allow", size: 28 },
  tool: { label: "Tool", shape: "diamond", color: "--context", size: 32 },
  destination: { label: "Destination", shape: "ellipse", color: "--other", size: 26 },
  mcp: { label: "MCP server", shape: "round-hexagon", color: "--context", size: 30 },
  sensitive: { label: "Sensitive", shape: "triangle", color: "--ask", size: 32 },
};
const DEST_CLASS = { "public-ip": "--block", "private-ip": "--ask", loopback: "--dry", domain: "--other" };
const SEVERE = ["block", "ask", "mask"];
const FILTERS = {
  all: { label: "All verdicts", keep: null },
  flagged: { label: "Flagged only", keep: ["block", "ask", "mask", "context"] },
  block: { label: "Blocked only", keep: ["block"] },
};

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function layoutKey(focus) {
  return "aidr-obs:graph:" + (focus || "all");
}

function readLayout(focus) {
  try {
    return JSON.parse(localStorage.getItem(layoutKey(focus)) || "null");
  } catch {
    return null;
  }
}

function writeLayout(focus, cy) {
  if (!cy) return;
  const pos = {};
  cy.nodes().forEach((node) => {
    pos[node.id()] = { ...node.position() };
  });
  try {
    localStorage.setItem(layoutKey(focus), JSON.stringify({ pos, zoom: cy.zoom(), pan: cy.pan() }));
  } catch {
    /* private mode */
  }
}

function mergeGraph(base, extra) {
  const nodes = new Set(base.nodes.map((node) => node.id));
  const edges = new Set(base.edges.map((edge) => edge.id));
  extra.nodes.forEach((node) => {
    if (!nodes.has(node.id)) base.nodes.push(node);
  });
  extra.edges.forEach((edge) => {
    if (!edges.has(edge.id)) base.edges.push(edge);
  });
  return base;
}

function elementsOf(nodes, edges, positions, focus) {
  const verdictColor = (verdict) => cssVar(SEVERE.includes(verdict) || verdict === "context" ? "--" + verdict : "--edge");
  return [
    ...nodes.map((node) => {
      const type = NODE_TYPES[node.type] || NODE_TYPES.tool;
      const fill = cssVar(node.type === "destination" ? DEST_CLASS[node.class] || type.color : type.color);
      const severe = SEVERE.includes(node.worst);
      return {
        group: "nodes",
        position: positions[node.id] ? { ...positions[node.id] } : undefined,
        classes: node.id === focus ? "root" : "",
        data: {
          ...node,
          fill,
          shape: type.shape,
          size: type.size + Math.min(24, 4 * Math.log2(1 + node.count)),
          border: severe ? cssVar("--" + node.worst) : cssVar("--panel"),
          bw: severe ? 4 : 1.5,
        },
      };
    }),
    ...edges.map((edge) => ({
      group: "edges",
      data: { ...edge, color: verdictColor(edge.worst), width: 1 + Math.min(6, Math.log2(1 + edge.count)) },
    })),
  ];
}

export class GraphCanvas extends Component {
  constructor() {
    super();
    this.state = { hide: [], verdict: "all", selected: null, query: "", path: null, message: "", expanded: [] };
  }

  componentDidMount() {
    if (this.props.data) this.sync();
    else this.load();
  }

  componentDidUpdate(previous) {
    const focus = this.focus();
    const prevFocus = previous.focus != null ? previous.focus : (previous.app && previous.app.focus) || "";
    const dataChanged = previous.data !== this.props.data || previous.app.theme !== this.props.app.theme || prevFocus !== focus;
    if (!this.props.data && (prevFocus !== focus || previous.app.tick !== this.props.app.tick || previous.app.since !== this.props.app.since)) {
      this.load();
      return;
    }
    if (dataChanged && this.props.data) {
      this.sync();
      return;
    }
    const before = previous.state || {};
    if (before.query !== this.state.query || before.verdict !== this.state.verdict || (before.hide || []).join() !== this.state.hide.join() || before.selected !== this.state.selected) {
      this.applyFilters();
    }
  }

  load() {
    const seq = (this._seq = (this._seq || 0) + 1);
    getJSON("/api/graph", this.props.app, { focus: this.focus(), max_nodes: this.props.compact ? 80 : 300 }).then((data) => {
      if (seq !== this._seq) return;
      this._data = data;
      this.forceUpdate();
      this.sync();
    }).catch((err) => {
      if (seq === this._seq) this.setState({ message: err.message || String(err) });
    });
  }

  componentWillUnmount() {
    this.destroy();
  }

  focus() {
    return this.props.focus != null ? this.props.focus : this.props.app.focus || "";
  }

  destroy() {
    if (this.cy) {
      writeLayout(this.focus(), this.cy);
      this.cy.destroy();
      this.cy = null;
    }
  }

  graphData() {
    return this.props.data || this._data;
  }

  sync() {
    const data = this.graphData();
    const cytoscape = globalThis.cytoscape;
    if (!this.node || !data || !data.nodes || !data.nodes.length || typeof cytoscape !== "function") return;
    const saved = readLayout(this.focus()) || {};
    const positions = saved.pos || {};
    const known = data.nodes.every((node) => positions[node.id]);
    if (this.cy) this.cy.destroy();
    this.cy = cytoscape({
      container: this.node,
      elements: elementsOf(data.nodes, data.edges, positions, this.focus()),
      minZoom: 0.1,
      maxZoom: 4,
      style: graphStyle(),
      layout: known ? { name: "preset" } : this.layout(false),
    });
    if (known && saved.zoom) {
      this.cy.zoom(saved.zoom);
      this.cy.pan(saved.pan);
    }
    this.cy.on("tap", "node", (event) => this.select(event.target.id()));
    this.cy.on("tap", (event) => {
      if (event.target === this.cy) this.select(null);
    });
    this.cy.on("dbltap", "node", (event) => this.expand(event.target.id()));
    this.cy.on("mouseover", "node", (event) => {
      this.cy.elements().addClass("faded");
      event.target.closedNeighborhood().removeClass("faded");
    });
    this.cy.on("mouseout", "node", () => this.applyFilters());
    this.cy.on("dragfree", "node", () => writeLayout(this.focus(), this.cy));
    this.applyFilters();
  }

  layout(randomize) {
    const name = "cose";
    return {
      name,
      animate: false,
      randomize,
      nodeRepulsion: () => 9000,
      idealEdgeLength: () => 70,
      nodeOverlap: 12,
      gravity: 0.6,
      numIter: 1200,
      padding: 24,
    };
  }

  applyFilters() {
    const cy = this.cy;
    if (!cy) return;
    const keep = FILTERS[this.state.verdict].keep;
    const hidden = new Set(this.state.hide);
    const query = this.state.query.trim().toLowerCase();
    cy.batch(() => {
      cy.elements().removeClass("hidden faded dim");
      if (keep) cy.edges().forEach((edge) => {
        if (!keep.includes(edge.data("worst"))) edge.addClass("hidden");
      });
      cy.nodes().forEach((node) => {
        if (node.id() === this.focus()) return;
        if (hidden.has(node.data("type"))) node.addClass("hidden");
        if (keep && node.connectedEdges().every((edge) => edge.hasClass("hidden"))) node.addClass("hidden");
        if (query && !String(node.data("label") || "").toLowerCase().includes(query)) node.addClass("dim");
      });
      cy.edges().forEach((edge) => {
        if (edge.connectedNodes(".hidden").nonempty()) edge.addClass("hidden");
      });
    });
    if (this.state.selected) {
      const node = cy.getElementById(this.state.selected);
      if (node.empty() || node.hasClass("hidden")) this.setState({ selected: null });
      else {
        cy.elements().not(".hidden").difference(node.closedNeighborhood()).addClass("faded");
        node.removeClass("faded").addClass("focus");
      }
    }
  }

  select(id) {
    this.setState({ selected: id, path: null, message: "" });
  }

  async expand(id) {
    const extra = await getJSON("/api/graph", this.props.app, { focus: id });
    if (!this.cy || !this.props.data) return;
    mergeGraph(this.props.data, extra);
    const node = this.cy.getElementById(id);
    const center = node.nonempty() ? node.position() : { x: 0, y: 0 };
    const fresh = extra.nodes.filter((item) => this.cy.getElementById(item.id).empty());
    const edges = extra.edges.filter((item) => this.cy.getElementById(item.id).empty());
    const positions = {};
    fresh.forEach((item, index) => {
      const angle = (2 * Math.PI * index) / Math.max(1, fresh.length);
      const radius = 90 + 8 * fresh.length;
      positions[item.id] = { x: center.x + radius * Math.cos(angle), y: center.y + radius * Math.sin(angle) };
    });
    this.cy.add(elementsOf(fresh, edges, positions, this.focus()));
    this.setState({
      expanded: this.state.expanded.includes(id) ? this.state.expanded : this.state.expanded.concat(id),
      message: fresh.length || edges.length ? "Added " + fresh.length + " nodes." : "Nothing new around this node.",
    });
    this.applyFilters();
  }

  trace(id) {
    const cy = this.cy;
    if (!cy) return;
    const previous = new Map();
    const seen = new Set([id]);
    const queue = [cy.getElementById(id)];
    while (queue.length) {
      const node = queue.shift();
      if (node.data("type") === "agent") break;
      node.incomers("node").forEach((source) => {
        if (seen.has(source.id())) return;
        seen.add(source.id());
        if (!previous.has(node.id())) previous.set(node.id(), source.id());
        queue.push(source);
      });
    }
    const labels = [];
    let cursor = id;
    const guard = new Set();
    while (cursor && !guard.has(cursor)) {
      guard.add(cursor);
      labels.push(cy.getElementById(cursor).data("label"));
      cursor = previous.get(cursor);
    }
    this.setState({ path: labels.reverse() });
  }

  render() {
    const data = this.graphData();
    const missing = typeof globalThis.cytoscape !== "function";
    return html`<div class=${this.props.compact ? "graph-compact" : "graph-wrap"}>
      ${this.props.compact
        ? null
        : html`<div class="filters">
            <input aria-label="Search the graph" placeholder="Find a node" value=${this.state.query}
              onInput=${(event) => this.setState({ query: event.target.value })} />
            <select aria-label="Verdict filter" value=${this.state.verdict} onChange=${(event) => this.setState({ verdict: event.target.value })}>
              ${Object.entries(FILTERS).map(([id, item]) => html`<option value=${id} key=${id}>${item.label}</option>`)}
            </select>
            <button class="btn" type="button" onClick=${() => this.cy && this.cy.layout(this.layout(true)).run()}>Re-run layout</button>
            <button class="btn" type="button" onClick=${() => this.cy && this.cy.fit(undefined, 24)}>Fit</button>
          </div>
          <div class="filters">
            ${Object.entries(NODE_TYPES).map(([id, type]) => html`<button type="button" key=${id} class=${"chip" + (this.state.hide.includes(id) ? " off" : "")}
              onClick=${() => {
                const hide = this.state.hide.includes(id) ? this.state.hide.filter((item) => item !== id) : this.state.hide.concat(id);
                this.setState({ hide });
              }}><i class="gtype" style=${{ background: "var(" + type.color + ")" }}></i>${type.label}</button>`)}
          </div>`}
      ${missing ? html`<p class="empty">Graph library failed to load.</p>` : null}
      ${data && data.focus && data.focus.found
        ? html`<p class="banner">Focused on <strong>${data.focus.label}</strong>, ${data.focus.actions} actions.
            <button class="btn mini" type="button" onClick=${() => go("graph", { focus: "" })}>Show everything</button></p>`
        : null}
      ${data && data.truncated ? html`<p class="banner">Showing the busiest and most severe entities. Focus on one to see the rest.</p>` : null}
      <div class="cy" ref=${(node) => { this.node = node; }}></div>
      ${this.props.compact ? null : html`<aside class="graph-side">${this.side()}</aside>`}
    </div>`;
  }

  side() {
    const cy = this.cy;
    const node = cy && this.state.selected ? cy.getElementById(this.state.selected) : null;
    if (!node || node.empty()) return html`<h2>Graph</h2><p class="muted">Click a node for its links. Double-click to expand it. Hover highlights its neighbours.</p>`;
    const data = node.data();
    const examples = data.examples || [];
    return html`<h2>${(NODE_TYPES[data.type] || {}).label || data.type}</h2>
      <div class="node-title">${data.label}</div>
      <p>${data.count} actions <${Verdict} verdict=${data.worst} mode=${this.props.app.mode} /></p>
      <div class="row-actions">
        ${data.id !== this.focus() ? html`<button class="btn" type="button" onClick=${() => go("graph", { focus: data.id })}>Focus</button>` : null}
        <button class="btn" type="button" onClick=${() => this.trace(data.id)}>Path to here</button>
        ${data.type === "session" && data.key ? html`<button class="btn" type="button" onClick=${() => go("session", { sessionKey: data.key })}>Session</button>` : null}
      </div>
      ${this.state.path ? html`<p class="path">${this.state.path.join(" → ")}</p>` : null}
      ${this.state.message ? html`<p class="muted">${this.state.message}</p>` : null}
      <h3>Recent actions</h3>
      ${examples.length
        ? examples.map(
            (item) => html`<button class="tool" type="button" key=${item.capture_id} onClick=${() => update({ open: "capture:" + item.capture_id }, "push")}>
              <${Verdict} verdict=${item.verdict} mode=${this.props.app.mode} /> ${item.tool || "?"}
              <span class="muted"><${When} ts=${item.ts} /></span>
              <div><code>${item.summary}</code></div>
            </button>`,
          )
        : html`<p class="muted">None stored on this node</p>`}`;
  }
}

function graphStyle() {
  return [
    { selector: "node", style: {
      "background-color": "data(fill)", shape: "data(shape)", width: "data(size)", height: "data(size)",
      "border-color": "data(border)", "border-width": "data(bw)", label: "data(label)", color: cssVar("--text"),
      "font-size": 10, "text-valign": "bottom", "text-margin-y": 4, "text-wrap": "ellipsis", "text-max-width": 140,
      "text-outline-color": cssVar("--bg"), "text-outline-width": 2,
    } },
    { selector: "edge", style: {
      width: "data(width)", "line-color": "data(color)", "target-arrow-color": "data(color)",
      "target-arrow-shape": "triangle", "arrow-scale": 0.8, "curve-style": "bezier", opacity: 0.85,
    } },
    { selector: "node.root, node.focus", style: { "underlay-color": cssVar("--accent"), "underlay-opacity": 0.2, "underlay-padding": 8 } },
    { selector: ".faded, .dim", style: { opacity: 0.12 } },
    { selector: ".hidden", style: { display: "none" } },
  ];
}

export class GraphPage extends LoadView {
  shouldReload(previous, next) {
    return super.shouldReload(previous, next) || previous.focus !== next.focus;
  }

  fetch(app) {
    return getJSON("/api/graph", app, { focus: app.focus || "", max_nodes: 300 });
  }

  body(data) {
    return html`<${GraphCanvas} data=${data} app=${this.props.app} />`;
  }
}
