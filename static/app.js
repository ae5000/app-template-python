// app.js — Alpine store + channel manager + applyPatch
// window.__CONFIG__ is SSR'd by base.html before this script runs.

const CONFIG = {
  rootPath: window.__CONFIG__?.rootPath ?? "",
  apiBase:  window.__CONFIG__?.apiBase  ?? "/api",
  wsBase:   window.__CONFIG__?.wsBase   ?? `ws://${location.host}`,
};

// ── Patch interpreter ────────────────────────────────────────────────────────
// Applies a single patch message to the Alpine store.
// Path syntax: dot-notation string, e.g. "items", "ui.progress", "messages.s1"

function resolvePath(obj, path) {
  const parts = path.split(".");
  let target = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    if (target[parts[i]] === undefined) target[parts[i]] = {};
    target = target[parts[i]];
  }
  return { parent: target, key: parts[parts.length - 1] };
}

function applyPatch(store, patch) {
  const { op, path, id, value, key, by = 1 } = patch;
  if (!op || !path) return;
  const { parent, key: field } = resolvePath(store, path);
  switch (op) {
    case "set":
      parent[field] = value;
      break;
    case "add":
      if (Array.isArray(parent[field])) parent[field] = [...parent[field], value];
      else if (parent[field] && typeof parent[field] === "object") parent[field] = { ...parent[field], ...value };
      else parent[field] = [value];
      break;
    case "update": {
      const k = key || "id";
      if (Array.isArray(parent[field]))
        parent[field] = parent[field].map(item => item[k] === id ? { ...item, ...value } : item);
      break;
    }
    case "remove": {
      const k = key || "id";
      if (Array.isArray(parent[field]))
        parent[field] = parent[field].filter(item => item[k] !== id);
      break;
    }
    case "merge":
      if (parent[field] && typeof parent[field] === "object" && !Array.isArray(parent[field]))
        parent[field] = { ...parent[field], ...value };
      break;
    case "inc":
      parent[field] = (typeof parent[field] === "number" ? parent[field] : 0) + by;
      break;
    case "prepend":
      if (Array.isArray(parent[field])) parent[field] = [value, ...parent[field]];
      break;
    case "append-log":
      if (Array.isArray(parent[field])) parent[field] = [...parent[field], value].slice(-500);
      break;
  }
}

// ── Channel manager ──────────────────────────────────────────────────────────

async function createChannel() {
  try {
    const res = await fetch(`${CONFIG.rootPath}/ws/channel`, { method: "POST" });
    if (!res.ok) return null;
    const { channel_id } = await res.json();
    return channel_id;
  } catch {
    return null;
  }
}

function openChannelWS(channelId, store) {
  const ws = new WebSocket(`${CONFIG.wsBase}/ws/channel/${channelId}`);
  ws.onopen  = () => { store.channel.wsStatus = "connected"; };
  ws.onclose = () => { store.channel.wsStatus = "disconnected"; };
  ws.onerror = () => { store.channel.wsStatus = "error"; };
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === "batch") msg.patches.forEach(p => applyPatch(store, p));
      else applyPatch(store, msg);
    } catch { /* ignore malformed messages */ }
  };
  return ws;
}

// ── Alpine form component ─────────────────────────────────────────────────────
// Used by the create_form macro. action is relative to apiBase (e.g. "/items").

document.addEventListener("alpine:init", () => {
  Alpine.data("platformForm", ({ action, fields }) => ({
    form: Object.fromEntries(fields.map(f => [f, ""])),
    submitting: false,
    error: null,
    async submit() {
      this.submitting = true;
      this.error = null;
      try {
        const headers = { "Content-Type": "application/json" };
        const channelId = Alpine.store("app").channel.id;
        if (channelId) headers["X-Channel-Id"] = channelId;
        const r = await fetch(`${CONFIG.apiBase}${action}`, {
          method: "POST",
          headers,
          body: JSON.stringify(this.form),
        });
        if (r.ok) {
          this.form = Object.fromEntries(fields.map(f => [f, ""]));
        } else {
          this.error = `Error ${r.status}`;
        }
      } catch (e) {
        this.error = "Network error";
      } finally {
        this.submitting = false;
      }
    },
  }));
});

// ── Store init ───────────────────────────────────────────────────────────────

window.initAppStore = function () {
  document.addEventListener("alpine:init", () => {
    // Build initial store shape synchronously — must complete before Alpine walks DOM.
    const store = {
      ui:      { navItem: null, navCollapsed: false, progress: null },
      user:    window.__USER__ || { id: null, name: "—", initials: "?", email: "" },
      items:   [],
      channel: { id: null, wsStatus: "disconnected" },
    };

    Alpine.store("app", store);

    // Load data + open WS channel asynchronously (store is already registered above).
    Promise.all([
      fetch(`${CONFIG.apiBase}/items`).then(r => r.ok ? r.json() : []).catch(() => []),
      createChannel(),
    ]).then(([items, channelId]) => {
      applyPatch(store, { op: "set", path: "items", value: items });
      if (channelId) {
        store.channel.id = channelId;
        openChannelWS(channelId, store);
      }
    });
  });
};
