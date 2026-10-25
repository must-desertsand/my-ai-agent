// The API client. The per-session token lives in sessionStorage (D10) and goes out as X-Session-Token on every
// /api request, all of which pass through authFetch. A 401/403 anywhere means the session is gone: the app shows the
// calm "Session expired" page. Updates come over fetch-streamed SSE (D12), so the token rides in a header, never in
// a URL.

const TOKEN_KEY = "ha_token";
export const bus = new EventTarget();

export function emit(name, detail) { bus.dispatchEvent(new CustomEvent(name, { detail })); }
export function on(name, fn) {
  const h = (e) => fn(e.detail);
  bus.addEventListener(name, h);
  return () => bus.removeEventListener(name, h);
}

export function getToken() {
  try { return window.sessionStorage.getItem(TOKEN_KEY); } catch (e) { return null; }
}

function setToken(t) {
  try { window.sessionStorage.setItem(TOKEN_KEY, t); return true; } catch (e) { return false; }
}

let expired = false;
export function isExpired() { return expired; }
function expire() {
  if (expired) return;
  expired = true;
  try { window.sessionStorage.removeItem(TOKEN_KEY); } catch (e) { /* storage blocked */ }
  emit("expired");
}

export class ApiError extends Error {
  constructor(status, message, data = null) { super(message); this.status = status; this.data = data; }
}

/** Login: the redirect lands on /#c=<code>. Strip it at once, then trade it for the token. */
export async function authenticate() {
  const m = /^#c=([0-9a-f]+)$/.exec(window.location.hash);
  if (m) {
    window.history.replaceState(null, "", window.location.pathname + window.location.search + "#/live");
    try {
      const r = await window.fetch("/auth/exchange", {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: m[1] }),
      });
      if (!r.ok) throw new Error("exchange failed");
      const d = await r.json();
      if (typeof d.token !== "string" || !setToken(d.token)) throw new Error("no token");
    } catch (e) {
      expire();
      return false;
    }
  }
  if (!getToken()) { expire(); return false; }
  return true;
}

/** The one door to the API: only /api/ paths, always with this session's token. A 401/403 expires the session;
 * a network failure is ApiError(0); an abort is rethrown as it is. */
async function authFetch(path, init = {}) {
  if (typeof path !== "string" || !path.startsWith("/api/")) throw new ApiError(0, "not an api path");
  const token = getToken();
  if (!token) { expire(); throw new ApiError(401, "session expired"); }
  let r;
  try {
    r = await window.fetch(path, {
      ...init, headers: { ...(init.headers || {}), "X-Session-Token": token },
      credentials: "same-origin", cache: "no-store",
    });
  } catch (e) {
    if (e && e.name === "AbortError") throw e;
    throw new ApiError(0, "dashboard unreachable");
  }
  if (r.status === 401 || r.status === 403) { expire(); throw new ApiError(r.status, "session expired"); }
  return r;
}

/** GET or POST JSON. Throws ApiError with the server's short fixed error string. */
export async function api(path, { method = "GET", body } = {}) {
  const init = { method };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const r = await authFetch(path, init);
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) throw new ApiError(r.status, data && typeof data.error === "string" ? data.error : "request failed", data);
  return data;
}

/** Bytes (a deck PDF or thumbnail, D11): the page shows them through a blob: URL it makes from this Blob. */
export async function apiBlob(path) {
  const r = await authFetch(path);
  if (!r.ok) throw new ApiError(r.status, "not found");
  return r.blob();
}

// ------------------------------------------------------------------ the stream

const BACKOFF_MIN = 1000, BACKOFF_MAX = 30000;
const STALL_MS = 45000; // the server sends a heartbeat after 15 s of quiet: three missed means the stream is dead

/**
 * Read /api/stream until stopped. Emits "event" (a step event) and "command" (a page command state change), plus
 * "stream" with {connected: bool}. Reconnects with backoff from 1 s to 30 s, resuming from the last event id, and
 * after a stall. Returns the stop function.
 */
export function startStream() {
  let lastId = null, backoff = BACKOFF_MIN, stopped = false, ctrl = null, retry = null;

  async function connect() {
    retry = null;
    if (stopped || expired) return;
    const c = new AbortController();
    ctrl = c;
    let watchdog = null;
    const pet = () => { window.clearTimeout(watchdog); watchdog = window.setTimeout(() => c.abort(), STALL_MS); };
    const q = lastId === null ? "" : `?since=${encodeURIComponent(String(lastId))}`;
    let healthy = false;
    try {
      pet();
      const r = await authFetch("/api/stream" + q, { headers: { Accept: "text/event-stream" }, signal: c.signal });
      if (!r.ok || !r.body) throw new Error("stream " + r.status);
      emit("stream", { connected: true });
      healthy = true;
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        pet();
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.search(/\r?\n\r?\n/)) >= 0) {
          const block = buf.slice(0, i);
          buf = buf.slice(i).replace(/^\r?\n\r?\n/, "");
          dispatch(block);
        }
        if (buf.length > 1_000_000) buf = ""; // a runaway block: drop it rather than grow for ever
        backoff = BACKOFF_MIN; // data flowing: the next drop starts again at 1 s
      }
    } catch (e) {
      /* a drop, a stall (the watchdog aborted it), a stop, an expiry or a bad status: reconnect below */
    }
    window.clearTimeout(watchdog);
    emit("stream", { connected: false });
    if (stopped || expired) return;
    const wait = healthy ? BACKOFF_MIN : backoff;
    backoff = Math.min(BACKOFF_MAX, backoff * 2);
    retry = window.setTimeout(connect, wait);
  }

  function dispatch(block) {
    let name = "message", data = "";
    for (const line of block.split(/\r?\n/)) {
      if (!line || line.startsWith(":")) continue; // comments: connected, heartbeat
      const k = line.indexOf(":");
      const field = k < 0 ? line : line.slice(0, k);
      const val = k < 0 ? "" : line.slice(k + 1).replace(/^ /, "");
      if (field === "event") name = val;
      else if (field === "data") data += (data ? "\n" : "") + val;
      else if (field === "id" && /^\d+$/.test(val)) lastId = Number(val);
    }
    if (!data) return;
    let obj;
    try { obj = JSON.parse(data); } catch (e) { return; }
    if (!obj || typeof obj !== "object") return;
    if (name === "event" && Number.isInteger(obj.id)) lastId = Math.max(lastId ?? 0, obj.id);
    if (name === "event" || name === "command") emit(name, obj);
  }

  connect();
  return () => {
    stopped = true;
    if (retry) window.clearTimeout(retry);
    if (ctrl) ctrl.abort();
  };
}
