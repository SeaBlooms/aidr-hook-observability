import { applyTheme, load, save } from "./prefs.js";

export const RANGES = [
  { id: "1h", label: "Last hour", ms: 3_600_000 },
  { id: "24h", label: "Last 24 hours", ms: 86_400_000 },
  { id: "7d", label: "Last 7 days", ms: 604_800_000 },
  { id: "30d", label: "Last 30 days", ms: 2_592_000_000 },
  { id: "all", label: "All time", ms: 0 },
  { id: "visit", label: "Since last visit" },
  { id: "custom", label: "Custom" },
];

const PRESETS = ["1h", "24h", "7d", "30d", "all"];

const VIEWS = new Set([
  "overview",
  "activity",
  "sessions",
  "graph",
  "verdicts",
  "sensitive",
  "destinations",
  "health",
  "mcp",
  "usage",
]);

const subs = new Set();
let state = null;
let source = null;

function boundsOf(partial) {
  const now = Date.now();
  const range = partial.range || "24h";
  if (range === "all") return { since: 0, until: 0 };
  if (range === "visit") return { since: Number(load().lastVisit) || 0, until: 0 };
  if (range === "custom") return { since: Number(partial.from) || 0, until: Number(partial.to) || 0 };
  const spec = RANGES.find((item) => item.id === range);
  const span = spec && spec.ms ? spec.ms : 86_400_000;
  return { since: now - span, until: 0 };
}

export function parseHash(hash) {
  const raw = (hash || "").replace(/^#/, "");
  const splitAt = raw.indexOf("?");
  const path = splitAt === -1 ? raw : raw.slice(0, splitAt);
  const params = new URLSearchParams(splitAt === -1 ? "" : raw.slice(splitAt + 1));
  const parts = path.split("/").filter(Boolean);
  let view = "overview";
  let sessionKey = "";
  if (parts[0] === "session" && parts.length > 1) {
    view = "session";
    sessionKey = decodeURIComponent(parts.slice(1).join("/"));
  } else if (parts[0] && VIEWS.has(parts[0])) {
    view = parts[0];
  }
  const prefs = load();
  return {
    view,
    sessionKey,
    range: params.get("range") || prefs.range || "24h",
    from: params.get("from") || "",
    to: params.get("to") || "",
    agent: params.get("agent") || "",
    q: params.get("q") || "",
    tool: params.get("tool") || "",
    verdict: params.get("verdict") || "",
    tag: params.get("tag") || "",
    type: params.get("type") || "",
    focus: params.get("focus") || "",
    open: params.get("open") || "",
    raw: params.get("raw") === "1",
    bucket: params.get("bucket") || "",
  };
}

function buildHash(current) {
  const path =
    current.view === "session" ? "/session/" + encodeURIComponent(current.sessionKey || "") : "/" + current.view;
  const params = new URLSearchParams();
  params.set("range", current.range || "24h");
  if (current.range === "custom") {
    if (current.from) params.set("from", current.from);
    if (current.to) params.set("to", current.to);
  }
  const keep = ["agent", "q", "tool", "verdict", "tag", "type", "focus", "open", "bucket"];
  keep.forEach((key) => {
    if (current[key]) params.set(key, current[key]);
  });
  if (current.raw) params.set("raw", "1");
  return "#" + path + "?" + params.toString();
}

function materialize(partial) {
  const window = boundsOf(partial);
  return {
    view: partial.view || "overview",
    sessionKey: partial.sessionKey || "",
    range: partial.range || "24h",
    from: partial.from || "",
    to: partial.to || "",
    agent: partial.agent || "",
    q: partial.q || "",
    tool: partial.tool || "",
    verdict: partial.verdict || "",
    tag: partial.tag || "",
    type: partial.type || "",
    focus: partial.focus || "",
    open: partial.open || "",
    raw: !!partial.raw,
    bucket: partial.bucket || "",
    since: window.since,
    until: window.until,
    tick: partial.tick || 0,
    live: !!partial.live,
    mode: partial.mode || "unknown",
    palette: !!partial.palette,
    help: !!partial.help,
    navOpen: !!partial.navOpen,
    theme: partial.theme || "auto",
  };
}

function notify() {
  subs.forEach((fn) => fn(state));
}

export function get() {
  return state;
}

export function subscribe(fn) {
  subs.add(fn);
  return () => subs.delete(fn);
}

export function update(patch, mode = "push") {
  if (patch.range && PRESETS.includes(patch.range)) save({ range: patch.range });
  if (patch.range === "visit") save({ range: "visit" });
  state = materialize({ ...state, ...patch });
  const hash = buildHash(state);
  if (mode === "push" && location.hash !== hash) history.pushState({ aidr: 1 }, "", hash);
  else if (mode === "replace" && location.hash !== hash) history.replaceState({ aidr: 1 }, "", hash);
  notify();
}

export function go(view, extra = {}) {
  update(
    {
      view,
      sessionKey: extra.sessionKey || "",
      agent: extra.agent || "",
      q: extra.q || "",
      tool: extra.tool || "",
      verdict: extra.verdict || "",
      tag: extra.tag || "",
      type: extra.type || "",
      focus: extra.focus || "",
      open: extra.open || "",
      raw: !!extra.raw,
      bucket: extra.bucket || "",
      palette: false,
      help: false,
      navOpen: false,
      range: extra.range || state.range,
      from: extra.from != null ? extra.from : extra.range ? "" : state.from,
      to: extra.to != null ? extra.to : extra.range ? "" : state.to,
    },
    "push",
  );
}

export function setRange(range, extra = {}) {
  update({ range, from: extra.from || "", to: extra.to || "", bucket: "" }, "push");
}

export function cycleRange(step) {
  const current = PRESETS.includes(state.range) ? state.range : "24h";
  const index = PRESETS.indexOf(current);
  const next = PRESETS[(index + step + PRESETS.length) % PRESETS.length];
  setRange(next);
}

export function refreshTick() {
  const window = boundsOf(state);
  state = { ...state, ...window, tick: state.tick + 1 };
  notify();
}

function stopStream() {
  if (source) {
    source.close();
    source = null;
  }
}

function startStream() {
  stopStream();
  source = new EventSource("/api/stream");
  source.addEventListener("ingest", () => refreshTick());
}

export function setLive(on) {
  save({ live: !!on });
  if (on) startStream();
  else stopStream();
  update({ live: !!on }, "none");
}

export function setTheme(theme) {
  save({ theme });
  applyTheme(theme);
  update({ theme }, "none");
}

function onPop() {
  const parsed = parseHash(location.hash);
  state = materialize({ ...state, ...parsed, palette: false, help: false, navOpen: false });
  notify();
}

export function boot() {
  const prefs = load();
  const parsed = parseHash(location.hash || "#/overview");
  state = materialize({ ...parsed, live: !!prefs.live, theme: prefs.theme || "auto", tick: 1 });
  const hash = buildHash(state);
  if (location.hash !== hash) history.replaceState({ aidr: 1 }, "", hash);
  applyTheme(state.theme);
  window.addEventListener("popstate", onPop);
  window.addEventListener("pagehide", () => save({ lastVisit: Date.now() }));
  if (state.live) startStream();
}
