/* ==========================================================================
 *  plugin-recorder — Element Web module (ESM), official Module API
 *
 *  Loaded by Element Web via config.json → "modules": ["/modules/recorder/index.js"].
 *  The stock docker image (docker-entrypoint.d/18-load-element-modules.sh)
 *  appends that entry automatically for every directory under /modules.
 *
 *  Integration point: api.extras.addRoomHeaderButtonCallback(). The controls
 *  are rendered by Element's OWN React tree inside the room header, so a
 *  re-render can never remove them — unlike DOM injection, which React 19
 *  wipes out on the first reconcile.
 *
 *  Network: every request goes to a RELATIVE path on the current origin
 *  (window.location.origin + "/recording/api/record/..."). No domain is
 *  hard-coded anywhere. Traefik strips "/recording" → recording controller.
 *
 *  Host contract: window.React (set by Element before importing modules) and
 *  window.mxMatrixClientPeg (access token). No bundler, no JSX, no deps.
 *  moduleApiVersion covers engine 1.12.0 … 2.x (Element Web ≥ 1.12.16).
 * ========================================================================== */

const MODULE_NAME = "plugin-recorder";

// Reverse-DNS key under which optional settings may live in config.json.
// Everything has a working default, so the module runs with no config at all.
const CONFIG_KEY = "io.github.matrix-element-call-recorder";

const DEFAULTS = Object.freeze({
  apiBase: "/recording",          // Traefik prefix → controller (stripped upstream)
  recordingsPath: "/recordings/", // archive link, same origin
  lang: "auto",                   // "ru" | "en" | "auto" (follow Element's UI language)
  showRecordingsLink: true,
  alwaysVisible: true,            // false = only while a MatrixRTC call is active
  pollIntervalMs: 5000,
  statusTimeoutMs: 12000,
  actionTimeoutMs: 90000,
});

const STRINGS = {
  ru: {
    record: "Запись", stop: "Стоп", recordings: "Записи",
    ready: "Готов", checking: "Проверка…", active: "Идёт запись",
    starting: "Запуск…", stopping: "Остановка…", finishing: "Завершение…",
    verifying: "Проверка результата…",
    noToken: "Сессия недействительна. Войдите в Element заново",
    needPower: "Недостаточно прав для управления записью",
    unreachable: "Сервер записи недоступен",
    timeout: "Истекло время ожидания; состояние не подтверждено",
    badResponse: "Некорректный ответ сервера записи",
    failed: "Ошибка записи",
    openRecordings: "Открыть список записей",
  },
  en: {
    record: "Record", stop: "Stop", recordings: "Recordings",
    ready: "Ready", checking: "Checking…", active: "Recording",
    starting: "Starting…", stopping: "Stopping…", finishing: "Finishing…",
    verifying: "Verifying result…",
    noToken: "Session is invalid. Sign in to Element again",
    needPower: "Not enough power to control recording",
    unreachable: "Recording service unreachable",
    timeout: "Timed out; state not confirmed",
    badResponse: "Malformed response from the recording service",
    failed: "Recording error",
    openRecordings: "Open the list of recordings",
  },
};

// ------------------------------------------------------------------ utils

function log(...a) { console.log("%c[" + MODULE_NAME + "]", "color:#2f81f7", ...a); }
function warn(...a) { console.warn("[" + MODULE_NAME + "]", ...a); }

class RecError extends Error {
  constructor(key, detail) {
    super(detail || key);
    this.name = "RecError";
    this.key = key;              // i18n key shown in the badge
    this.detail = detail || "";  // raw server text, appended in parentheses
  }
}

function readConfig(api) {
  let raw = null;
  try { raw = api.config.get(CONFIG_KEY); } catch (_) { raw = null; }
  const cfg = Object.assign({}, DEFAULTS);
  if (raw && typeof raw === "object") {
    for (const k of Object.keys(DEFAULTS)) {
      if (raw[k] !== undefined && raw[k] !== null && typeof raw[k] === typeof DEFAULTS[k]) cfg[k] = raw[k];
    }
  }
  // Always a same-origin absolute path: "/recording", never a full URL.
  cfg.apiBase = "/" + String(cfg.apiBase).replace(/^[a-z]+:\/\/[^/]+/i, "").replace(/^\/+|\/+$/g, "");
  if (!String(cfg.recordingsPath).startsWith("/")) cfg.recordingsPath = "/" + cfg.recordingsPath;
  return Object.freeze(cfg);
}

function pickLanguage(cfg, api) {
  if (cfg.lang === "ru" || cfg.lang === "en") return cfg.lang;
  let tag = "";
  try { tag = String(api.i18n.language || ""); } catch (_) { /* older engine */ }
  if (!tag) { try { tag = document.documentElement.lang || navigator.language || ""; } catch (_) { /* ignore */ } }
  return /^ru\b/i.test(tag) ? "ru" : "en";
}

/** Matrix access token of the signed-in user. Sent as Bearer so the controller
 *  can ask Synapse who the caller is and check the room power level itself. */
function accessToken() {
  try {
    const t = window.mxMatrixClientPeg?.get?.()?.getAccessToken?.();
    if (t) return t;
  } catch (_) { /* peg not ready yet */ }
  try {
    const v = localStorage.getItem("mx_access_token");
    if (v) return v.replace(/^"|"$/g, "");
  } catch (_) { /* storage blocked */ }
  return "";
}

/** Best-effort "is a call going on in this room" from MatrixRTC membership
 *  state. Unknown → true, so the button is never hidden by mistake. */
const CALL_MEMBER_EVENTS = ["org.matrix.msc3401.call.member", "m.rtc.member", "m.call.member"];
function hasActiveCall(roomId) {
  try {
    const state = window.mxMatrixClientPeg?.get?.()?.getRoom?.(roomId)?.currentState;
    if (!state || typeof state.getStateEvents !== "function") return true;
    for (const type of CALL_MEMBER_EVENTS) {
      for (const ev of state.getStateEvents(type) || []) {
        const c = (ev && ev.getContent && ev.getContent()) || {};
        if (Array.isArray(c.memberships)) { if (c.memberships.length) return true; continue; }
        if (Object.keys(c).length) return true;
      }
    }
  } catch (_) { return true; }
  return false;
}

// ------------------------------------------------------------- API client
//
// Contract (production controller):
//   POST {apiBase}/api/record/start   body {"room": "!id:server"}  → {"status":"started", ...}
//   POST {apiBase}/api/record/stop    body {"room": ...}           → {"status":"stopped", ...}
//   GET  {apiBase}/api/record/status?room=...                      → {"status":"active|inactive|finished|stopped"}
// Every call carries Authorization: Bearer <matrix access token>.
// Errors are JSON {"error": "...", "status": <code>}.
// The 0.1.x overlay controller answers {"state": ...} and accepts "roomId";
// both spellings are sent/accepted so either backend works.

const STATUS_ALIASES = {
  active: "active", recording: "active", started: "active",
  starting: "active", stopping: "active", processing: "active",
  inactive: "inactive", idle: "inactive", unknown: "inactive",
  finished: "finished", published: "finished",
  stopped: "stopped", error: "stopped",
};

function normalizeStatus(data) {
  const raw = String((data && (data.status ?? data.state)) || "").toLowerCase();
  return STATUS_ALIASES[raw] || null;
}

function apiUrl(cfg, action, roomId) {
  // Relative to the current host — works behind any domain and any proxy.
  const u = new URL(cfg.apiBase + "/api/record/" + action, window.location.origin);
  if (action === "status") u.searchParams.set("room", roomId);
  return u.toString();
}

async function callApi(cfg, action, roomId) {
  const token = accessToken();
  if (!token) throw new RecError("noToken");

  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(),
    action === "status" ? cfg.statusTimeoutMs : cfg.actionTimeoutMs);
  const init = {
    method: action === "status" ? "GET" : "POST",
    headers: { Authorization: "Bearer " + token, Accept: "application/json" },
    cache: "no-store",
    credentials: "same-origin",
    signal: ctl.signal,
  };
  if (action !== "status") {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify({ room: roomId, roomId: roomId });
  }

  let res;
  try {
    res = await fetch(apiUrl(cfg, action, roomId), init);
  } catch (e) {
    throw new RecError(e && e.name === "AbortError" ? "timeout" : "unreachable");
  } finally {
    clearTimeout(timer);
  }

  if (res.status === 401) throw new RecError("noToken");
  if (res.status === 403) throw new RecError("needPower");

  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch (_) { throw new RecError("badResponse", "HTTP " + res.status); }
  if (!data || typeof data !== "object") throw new RecError("badResponse");
  if (!res.ok || data.error) throw new RecError("failed", String(data.error || ("HTTP " + res.status)));
  return data;
}

// ---------------------------------------------------------- state store
//
// One store per room id. The header callback is invoked on every RoomView
// render, so the component must be pure: all state lives here and React
// subscribes through useSyncExternalStore. Polling runs only while at least
// one component for that room is mounted.

class RoomStore {
  constructor(cfg, roomId) {
    this.cfg = cfg;
    this.roomId = roomId;
    this.listeners = new Set();
    this.snapshot = { status: null, busy: false, message: "checking", error: false, detail: "" };
    this.revision = 0;
    this.checking = false;
    this.timer = null;
  }

  subscribe(fn) {
    this.listeners.add(fn);
    if (this.listeners.size === 1) this.startPolling();
    return () => {
      this.listeners.delete(fn);
      if (this.listeners.size === 0) this.stopPolling();
    };
  }

  getSnapshot() { return this.snapshot; }

  set(patch) {
    this.snapshot = Object.assign({}, this.snapshot, patch);
    for (const fn of this.listeners) { try { fn(); } catch (e) { warn("listener failed", e); } }
  }

  startPolling() {
    this.stopPolling();
    void this.sync();
    this.timer = setInterval(() => { void this.sync(); }, this.cfg.pollIntervalMs);
  }

  stopPolling() {
    if (this.timer) { clearInterval(this.timer); this.timer = null; }
  }

  async sync() {
    if (this.snapshot.busy || this.checking) return;
    const rev = this.revision;
    this.checking = true;
    try {
      const data = await callApi(this.cfg, "status", this.roomId);
      if (rev !== this.revision || this.snapshot.busy) return;
      const status = normalizeStatus(data);
      if (!status) throw new RecError("badResponse", "status=" + String(data.status ?? data.state));
      this.set({ status, message: status === "active" ? "active" : "ready", error: false, detail: "" });
    } catch (e) {
      if (rev !== this.revision || this.snapshot.busy) return;
      this.set({ status: null, message: e.key || "failed", error: true, detail: e.detail || "" });
      if (!(e instanceof RecError)) warn("status failed", e);
    } finally {
      this.checking = false;
    }
  }

  canStart() { return !this.snapshot.busy && ["inactive", "finished", "stopped"].includes(this.snapshot.status); }
  canStop() { return !this.snapshot.busy && this.snapshot.status === "active"; }

  async act(action) {
    if (action === "start" && !this.canStart()) return;
    if (action === "stop" && !this.canStop()) return;
    this.revision++;
    this.set({ busy: true, message: action === "start" ? "starting" : "stopping", error: false, detail: "" });
    try {
      await callApi(this.cfg, action, this.roomId);
      this.set({ busy: false, status: null, message: "verifying", error: false });
    } catch (e) {
      this.set({ busy: false, status: null, message: e.key || "failed", error: true, detail: e.detail || "" });
      if (!(e instanceof RecError)) warn(action + " failed", e);
    } finally {
      setTimeout(() => { void this.sync(); }, 1000);
    }
  }
}
// ---------------------------------------------------------------- styles
//
// One <style> in <head>. <head> is outside Element's React root, so this is
// the only place where touching the DOM directly is safe. Colours come from
// Compound design tokens (--cpd-*) so the controls follow light/dark themes.

const STYLE_ID = MODULE_NAME + "-style";
const CSS = `
.rp-wrap{display:inline-flex;align-items:center;gap:6px;margin-inline-end:8px;
  padding:4px 8px;border-radius:8px;
  background:var(--cpd-color-bg-subtle-secondary,rgba(128,128,128,.12));
  font:500 13px/1 var(--cpd-font-family-sans,Inter,system-ui,sans-serif);
  color:var(--cpd-color-text-primary,inherit)}
.rp-link{color:inherit;opacity:.8;text-decoration:none;padding:4px 6px;border-radius:6px}
.rp-link:hover{opacity:1;background:var(--cpd-color-bg-subtle-primary,rgba(128,128,128,.2))}
.rp-btn{display:inline-flex;align-items:center;gap:6px;height:28px;padding:0 10px;
  border-radius:6px;border:1px solid var(--cpd-color-border-interactive-secondary,rgba(128,128,128,.4));
  background:var(--cpd-color-bg-canvas-default,#fff);color:inherit;cursor:pointer;font:inherit}
.rp-btn:hover:not([disabled]){background:var(--cpd-color-bg-subtle-primary,rgba(128,128,128,.2))}
.rp-btn[disabled]{opacity:.45;cursor:default}
.rp-btn.rp-start .rp-dot{width:9px;height:9px;border-radius:50%;
  background:var(--cpd-color-icon-critical-primary,#d51928)}
.rp-btn.rp-stop .rp-dot{width:9px;height:9px;border-radius:2px;background:currentColor}
.rp-badge{min-width:0;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  padding:0 4px;color:var(--cpd-color-text-success-primary,#0f7a3d)}
.rp-badge.rp-active{color:var(--cpd-color-text-critical-primary,#d51928);font-weight:600}
.rp-badge.rp-error{color:var(--cpd-color-text-critical-primary,#d51928)}
@media (max-width:900px){.rp-badge,.rp-label{display:none}}
`;

function ensureStyles() {
  try {
    if (document.getElementById(STYLE_ID)) return;
    const s = document.createElement("style");
    s.id = STYLE_ID;
    s.textContent = CSS;
    document.head.appendChild(s);
  } catch (e) { warn("cannot inject styles", e); }
}

// ------------------------------------------------------------- component
//
// Plain React.createElement on the host's React (window.React). No JSX, no
// own React copy — the Module API contract. The component is pure: every bit
// of state comes from RoomStore through useSyncExternalStore (React 18+),
// with a useState/useEffect fallback for older engines.

function useStoreSnapshot(React, store) {
  if (typeof React.useSyncExternalStore === "function") {
    const subscribe = React.useCallback((fn) => store.subscribe(fn), [store]);
    const get = () => store.getSnapshot();
    return React.useSyncExternalStore(subscribe, get, get);
  }
  const [snap, setSnap] = React.useState(store.getSnapshot());
  React.useEffect(() => store.subscribe(() => setSnap(store.getSnapshot())), [store]);
  return snap;
}

function RecorderControls(props) {
  const React = window.React;
  const h = React.createElement;
  const { store, cfg, api } = props;
  const snap = useStoreSnapshot(React, store);

  const lang = pickLanguage(cfg, api);
  const t = (k) => (STRINGS[lang] || STRINGS.en)[k] || k;

  const canStart = !snap.busy && ["inactive", "finished", "stopped"].includes(snap.status);
  const canStop = !snap.busy && snap.status === "active";
  const badgeText = t(snap.message);
  const badgeTitle = snap.detail ? badgeText + " (" + snap.detail + ")" : badgeText;

  const onStart = (ev) => { ev.preventDefault(); ev.stopPropagation(); void store.act("start"); };
  const onStop = (ev) => { ev.preventDefault(); ev.stopPropagation(); void store.act("stop"); };

  const children = [];

  if (cfg.showRecordingsLink) {
    children.push(h("a", {
      key: "link",
      className: "rp-link",
      href: new URL(cfg.recordingsPath, window.location.origin).toString(),
      target: "_blank",
      rel: "noopener noreferrer",
      title: t("openRecordings"),
      onClick: (ev) => ev.stopPropagation(),
    }, t("recordings")));
  }

  children.push(h("button", {
    key: "start",
    type: "button",
    className: "rp-btn rp-start",
    disabled: !canStart,
    onClick: onStart,
    title: t("record"),
    "aria-label": t("record"),
  }, h("span", { className: "rp-dot", "aria-hidden": "true" }),
     h("span", { className: "rp-label" }, t("record"))));

  children.push(h("button", {
    key: "stop",
    type: "button",
    className: "rp-btn rp-stop",
    disabled: !canStop,
    onClick: onStop,
    title: t("stop"),
    "aria-label": t("stop"),
  }, h("span", { className: "rp-dot", "aria-hidden": "true" }),
     h("span", { className: "rp-label" }, t("stop"))));

  children.push(h("span", {
    key: "badge",
    className: "rp-badge" + (snap.error ? " rp-error" : "") + (snap.status === "active" ? " rp-active" : ""),
    title: badgeTitle,
    role: "status",
    "aria-live": "polite",
  }, badgeText));

  return h("div", {
    className: "rp-wrap",
    "data-plugin": MODULE_NAME,
    "data-room": store.roomId,
    onClick: (ev) => ev.stopPropagation(),
  }, children);
}

// ---------------------------------------------------------------- module
//
// Default export = ModuleFactory: static moduleApiVersion (semver range checked
// by Element's ModuleLoader against its engine version), constructor(api),
// async load(). load() must never throw: a throwing module makes Element show
// the "misconfigured" screen for every user.

class RecorderModule {
  static moduleApiVersion = ">=1.12.0 <3.0.0";

  constructor(api) {
    this.api = api;
    this.cfg = null;
    this.stores = new Map();   // roomId -> RoomStore
  }

  storeFor(roomId) {
    let s = this.stores.get(roomId);
    if (!s) {
      s = new RoomStore(this.cfg, roomId);
      this.stores.set(roomId, s);
    }
    return s;
  }

  isGuest() {
    try { return this.api.profile?.value?.isGuest === true; } catch (_) { return false; }
  }

  renderHeaderButtons(roomId) {
    try {
      if (!roomId || typeof roomId !== "string" || !window.React) return undefined;
      if (this.isGuest()) return undefined;
      if (!this.cfg.alwaysVisible && !hasActiveCall(roomId)) return undefined;
      ensureStyles();
      return window.React.createElement(RecorderControls, {
        key: MODULE_NAME + ":" + roomId,   // RoomView collects callbacks into an array
        store: this.storeFor(roomId),
        cfg: this.cfg,
        api: this.api,
      });
    } catch (e) {
      warn("header render failed", e);
      return undefined;
    }
  }

  async load() {
    try {
      this.cfg = readConfig(this.api);

      const extras = this.api.extras;
      if (!extras || typeof extras.addRoomHeaderButtonCallback !== "function") {
        warn("extras.addRoomHeaderButtonCallback is not available in this Element Web; module disabled");
        return;
      }

      extras.addRoomHeaderButtonCallback((roomId) => this.renderHeaderButtons(roomId));

      // Diagnostics handle for the browser console; never used by the code itself.
      try {
        window.__pluginRecorder = Object.freeze({
          version: "2.0.0",
          origin: window.location.origin,
          apiBase: this.cfg.apiBase,
          stores: this.stores,
        });
      } catch (_) { /* ignore */ }

      log("loaded", { origin: window.location.origin, apiBase: this.cfg.apiBase,
                      recordings: this.cfg.recordingsPath, lang: this.cfg.lang,
                      alwaysVisible: this.cfg.alwaysVisible });
    } catch (e) {
      // Fail soft: the recorder is unavailable, Element keeps working.
      warn("load failed; module disabled", e);
    }
  }
}

export default RecorderModule;



