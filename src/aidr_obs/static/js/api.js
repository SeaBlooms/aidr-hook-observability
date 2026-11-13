export function query(app, extra) {
  const params = new URLSearchParams();
  params.set("since", String((app && app.since) || 0));
  if (app && app.until) params.set("until", String(app.until));
  Object.entries(extra || {}).forEach(([key, value]) => {
    if (value != null && value !== "") params.set(key, String(value));
  });
  return params;
}

export async function getJSON(path, app, extra) {
  const response = await fetch(path + "?" + query(app, extra).toString());
  if (!response.ok) {
    let detail = "";
    try {
      detail = (await response.json()).error || "";
    } catch {
      detail = "";
    }
    throw new Error(detail || path + " HTTP " + response.status);
  }
  return response.json();
}

export function ingest() {
  return fetch("/api/ingest", { method: "POST", headers: { "X-Aidr-Obs": "1" } }).then((response) => {
    if (!response.ok) throw new Error("Refresh failed (HTTP " + response.status + ")");
    return response.json();
  });
}

export function saveNoise(rules) {
  return fetch("/api/noise", {
    method: "POST",
    headers: { "X-Aidr-Obs": "1", "Content-Type": "application/json" },
    body: JSON.stringify({ rules }),
  }).then(async (response) => {
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || "Could not save the noise list");
    return body;
  });
}
