// Acting on an item: POST /api/action, then follow the command until it settles, through the stream's `command`
// events with /api/command/{id} polling as the fallback. One module for every page that acts on an item (the inbox
// now, Task 10's Attach next). The in-flight map lives at module scope, so a command outlives a page change; after a
// reload the server's /api/inbox `pending` hands it back through adopt().
import { api, ApiError, on } from "./client.js";

const POLL_MS = 2500;
export const NETWORK_FAILED = "may have been queued — check the status";
export const PAUSED = "Paused: resume the agent first";
/** The composer's command has no item yet (the draft is made when the tick runs it), so it is followed under this
 * fixed key: at most one at a time per page, and a real item id can never be this word. */
export const COMPOSE_KEY = "compose";

const inflight = new Map(); // item_id → {itemId, verb, cid, state}: at most one command per item
const listeners = new Map(); // item_id → Set of fn({itemId, verb, cid, state, result, network})
const settled = new Set(); // ids of commands that already ended here: a stale `pending` can't bring one back

function tell(itemId, update) {
  for (const fn of [...(listeners.get(itemId) || [])]) {
    try { fn(update); } catch (e) { /* one listener never breaks the others */ }
  }
}

function set(entry, state, result = null, extra = {}) {
  entry.state = state;
  if (state === "done" || state === "failed") {
    if (inflight.get(entry.itemId) === entry) inflight.delete(entry.itemId);
    if (entry.cid !== null) {
      settled.add(entry.cid);
      if (settled.size > 200) settled.delete(settled.values().next().value);
    }
  }
  tell(entry.itemId, { itemId: entry.itemId, verb: entry.verb, cid: entry.cid, state, result, ...extra });
}

on("command", (cmd) => {
  for (const e of [...inflight.values()]) {
    if (e.cid === cmd.id && e.state !== cmd.state) set(e, cmd.state, typeof cmd.result === "string" ? cmd.result : null);
  }
});
on("expired", () => inflight.clear());

/** The item's command in flight, as {verb, cid, state}, or null. */
export function inFlight(itemId) {
  const e = inflight.get(itemId);
  return e ? { verb: e.verb, cid: e.cid, state: e.state } : null;
}

/** Follow an item's command updates. Returns the function that stops following. */
export function watch(itemId, fn) {
  if (!listeners.has(itemId)) listeners.set(itemId, new Set());
  listeners.get(itemId).add(fn);
  return () => {
    const s = listeners.get(itemId);
    if (s) { s.delete(fn); if (!s.size) listeners.delete(itemId); }
  };
}

/** A command the server reports as queued or running (after a reload, or sent from another tab): follow it too. */
export function adopt(itemId, pending) {
  if (!pending || !Number.isInteger(pending.command_id) || settled.has(pending.command_id)) return;
  const e = inflight.get(itemId);
  if (e && (e.cid === null || e.cid === pending.command_id)) { // ours already, or our POST is still on its way
    if (e.cid !== null && pending.state === "running" && e.state === "queued") set(e, "running");
    return;
  }
  const entry = { itemId, verb: pending.verb, cid: pending.command_id, state: pending.state === "running" ? "running" : "queued" };
  inflight.set(itemId, entry);
  tell(itemId, { itemId, verb: entry.verb, cid: entry.cid, state: entry.state, result: null });
  poll(entry);
}

/** Act on an item. Returns false, doing nothing, while the item already has a command in flight: the double-click
 * guard, which holds across page changes. */
export function act(itemId, verb, { arg, shownHash } = {}) {
  if (inflight.has(itemId)) return false;
  const entry = { itemId, verb, cid: null, state: "sending" };
  inflight.set(itemId, entry);
  set(entry, "sending");
  send(entry, arg, shownHash);
  return true;
}

async function send(entry, arg, shownHash) {
  const body = { item_id: entry.itemId, verb: entry.verb, shown_hash: shownHash };
  if (arg !== undefined) body.arg = arg;
  let res;
  try {
    res = await api("/api/action", { method: "POST", body });
  } catch (e) {
    const busy = e instanceof ApiError && e.status === 409 && e.data && Number.isInteger(e.data.command_id);
    if (busy && inflight.get(entry.itemId) === entry) {
      // another tab already has a command in flight for this item (the server keeps one per item): follow that one
      entry.cid = e.data.command_id;
      if (typeof e.data.verb === "string") entry.verb = e.data.verb;
      set(entry, e.data.state === "running" ? "running" : "queued");
      poll(entry);
      return;
    }
    const network = e instanceof ApiError && e.status === 0; // the request may or may not have reached the server
    const paused = e instanceof ApiError && e.status === 409 && e.message === "paused"; // the agent is paused
    set(entry, "failed", network ? NETWORK_FAILED : paused ? PAUSED : (e && e.message) || "could not queue", { network });
    return;
  }
  if (inflight.get(entry.itemId) !== entry) return; // the session expired meanwhile
  if (!res || !Number.isInteger(res.command_id)) { set(entry, "failed", "could not queue"); return; }
  entry.cid = res.command_id;
  set(entry, "queued");
  poll(entry);
}

async function poll(entry) {
  while (inflight.get(entry.itemId) === entry) {
    await new Promise((r) => window.setTimeout(r, POLL_MS));
    if (inflight.get(entry.itemId) !== entry) return;
    let cmd;
    try {
      cmd = await api(`/api/command/${entry.cid}`);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) { set(entry, "failed", "command lost, reload"); return; }
      continue; // unreachable or expired: the loop ends once the entry is gone
    }
    if (inflight.get(entry.itemId) === entry && cmd && cmd.id === entry.cid && cmd.state !== entry.state) {
      set(entry, cmd.state, typeof cmd.result === "string" ? cmd.result : null);
    }
  }
}
