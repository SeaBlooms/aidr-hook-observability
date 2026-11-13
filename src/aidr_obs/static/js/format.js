const EVENT_LABELS = {
  UserPromptSubmit: "Prompt",
  beforeSubmitPrompt: "Prompt",
  preToolUse: "Pre-tool",
  PreToolUse: "Pre-tool",
  postToolUse: "Post-tool",
  PostToolUse: "Post-tool",
  beforeShellExecution: "Shell",
  afterShellExecution: "Shell",
  beforeReadFile: "Read file",
  afterFileEdit: "File edit",
  Stop: "Response",
  afterAgentResponse: "Response",
  afterAgentThought: "Response",
};

const ZONE = (() => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
})();

export function eventLabel(name) {
  if (!name) return "Event";
  return EVENT_LABELS[name] || name;
}

export function toolName(name) {
  if (!name) return { name: "", mcp: false };
  if (String(name).startsWith("MCP:")) return { name: String(name).slice(4), mcp: true };
  return { name: String(name), mcp: false };
}

export function parseTime(ts) {
  if (ts == null || ts === "") return null;
  if (typeof ts === "number") return ts;
  const parsed = Date.parse(ts);
  return Number.isFinite(parsed) ? parsed : null;
}

export function relative(ts) {
  const ms = parseTime(ts);
  if (ms == null) return "–";
  const delta = Date.now() - ms;
  const abs = Math.abs(delta);
  const future = delta < 0;
  const say = (n, unit) => (future ? "in " : "") + n + " " + unit + (n === 1 ? "" : "s") + (future ? "" : " ago");
  if (abs < 45_000) return future ? "in a moment" : "just now";
  if (abs < 3_600_000) return say(Math.round(abs / 60_000), "min");
  if (abs < 86_400_000) return say(Math.round(abs / 3_600_000), "hour");
  if (abs < 14 * 86_400_000) return say(Math.round(abs / 86_400_000), "day");
  return absolute(ts);
}

export function absolute(ts) {
  const ms = parseTime(ts);
  if (ms == null) return "";
  const formatted = new Date(ms).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  return ZONE ? formatted + " " + ZONE : formatted;
}

export function iso(ts) {
  const ms = parseTime(ts);
  return ms == null ? "" : new Date(ms).toISOString();
}

export function compact(value) {
  if (value == null || value === "") return "–";
  const n = Number(value);
  if (!Number.isFinite(n)) return "–";
  const sign = n < 0 ? "−" : "";
  const abs = Math.abs(n);
  const units = [
    [1e12, "T"],
    [1e9, "B"],
    [1e6, "M"],
    [1e3, "k"],
  ];
  for (const [size, suffix] of units) {
    if (abs >= size) {
      const scaled = abs / size;
      const digits = scaled >= 10 ? 1 : 1;
      return sign + trim(scaled.toFixed(digits)) + suffix;
    }
  }
  return sign + String(Math.round(abs));
}

function trim(text) {
  return text.replace(/\.0$/, "");
}

export function duration(ms) {
  if (ms == null || ms === "") return "–";
  const n = Number(ms);
  if (!Number.isFinite(n)) return "–";
  if (n < 1000) return Math.round(n) + " ms";
  if (n < 10_000) return (n / 1000).toFixed(2) + " s";
  if (n < 60_000) return (n / 1000).toFixed(1) + " s";
  const minutes = Math.floor(n / 60_000);
  const seconds = Math.round((n % 60_000) / 1000);
  return minutes + "m " + seconds + "s";
}

export function clip(text, max) {
  const value = text == null ? "" : String(text);
  if (value.length <= max) return value;
  return value.slice(0, max - 1) + "…";
}

export function shortId(value) {
  const text = value == null ? "" : String(value);
  if (!text || text === "?") return "–";
  return text.length > 8 ? text.slice(0, 4) + "…" : text;
}

export function localInputValue(ms) {
  const n = Number(ms);
  if (!n) return "";
  const date = new Date(n);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (part) => String(part).padStart(2, "0");
  return (
    date.getFullYear() +
    "-" +
    pad(date.getMonth() + 1) +
    "-" +
    pad(date.getDate()) +
    "T" +
    pad(date.getHours()) +
    ":" +
    pad(date.getMinutes())
  );
}

export function fromLocalInput(value) {
  if (!value) return 0;
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : 0;
}

const REDACTED = "***REDACTED***";

export function presentJson(value) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return (text || "").split(REDACTED).join("[redacted]");
}

export function hasRedaction(value) {
  const text = typeof value === "string" ? value : JSON.stringify(value || "");
  return text.includes(REDACTED) || text.includes("[redacted]");
}
