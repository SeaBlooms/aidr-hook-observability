const KEY = "aidr-obs:prefs";

const DEFAULTS = {
  theme: "auto",
  range: "24h",
  live: false,
  notify: false,
  columns: {},
  pricesOn: false,
  prices: {},
  seenSetup: false,
  lastVisit: 0,
};

export function load() {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || "{}");
    if (!raw || typeof raw !== "object") return { ...DEFAULTS, columns: {}, prices: {} };
    return {
      ...DEFAULTS,
      ...raw,
      columns: raw.columns && typeof raw.columns === "object" ? raw.columns : {},
      prices: raw.prices && typeof raw.prices === "object" ? raw.prices : {},
    };
  } catch {
    return { ...DEFAULTS, columns: {}, prices: {} };
  }
}

export function save(patch) {
  try {
    const next = { ...load(), ...patch };
    localStorage.setItem(KEY, JSON.stringify(next));
    return next;
  } catch {
    return load();
  }
}

export function applyTheme(theme) {
  const root = document.documentElement;
  if (!theme || theme === "auto") root.removeAttribute("data-theme");
  else root.dataset.theme = theme;
}
