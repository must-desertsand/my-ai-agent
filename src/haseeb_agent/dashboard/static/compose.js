// The composer, at the top of "Waiting for you": Haseeb types a rough message, Polish asks the agent to tidy it, and the
// draft comes back as a normal card below (and in his DM) with Post, Edit, Shorter and Skip. Nothing is ever posted from
// this box, and the dashboard makes no model call: Polish queues a page command (POST /api/compose), the tick polishes
// the text and sends the draft to his DM, and this module follows that command through actions.js under COMPOSE_KEY,
// so it survives a page change. His text stays in the box until its draft exists (a failed Polish keeps it, with the
// reason), and all text on screen goes in as text.
import { api, ApiError } from "./client.js";
import { COMPOSE_KEY, PAUSED, adopt, inFlight, watch } from "./actions.js";
import { isComposeHeld, isPaused, onJobs } from "./jobs.js";
import { clear, el } from "./dom.js";

export const COMPOSE_MAX = 2000; // the API's limit too (400 "too long")
const SAFETY_MAX = 20000; // not a limit he should ever meet: a paste of a whole document can't freeze the box
const DONE_MS = 8000; // the "Polished" note stays this long, then the line goes quiet
export const DONE_TEXT = "Polished · the draft is below and in your DM";
const GLYPH = { running: "👀", done: "✅", failed: "⚠️", held: "⏳" };
export const HELD_QUEUED = "Waits until today's Next Todo is out";
export const HELD_IDLE = "Polish waits until today's Next Todo is out";
const ERRORS = {
  "empty message": "Write something first.",
  "too long": `That is over the ${COMPOSE_MAX.toLocaleString("en-US")}-character limit. Shorten it first.`,
};
const LEADING_EMOJI = /^(?:\p{Extended_Pictographic}|\ufe0f|\u200d|\s)+/u;

// what outlives the box: his text until the draft exists, and how the last Polish ended
let kept = "";
let outcome = null; // {state: "done" | "failed", text, at}
const views = new Set(); // the composers on screen

const count = (s) => Array.from(s).length; // characters as people count them (the server counts code points too)

function icon(name) { return el("span", { class: "ico i-" + name, attrs: { "aria-hidden": "true" } }); }

/** The command the tick runs: its end clears the text (done) or keeps it with the reason (failed). Registered once, so
 * a Polish that ends while another page is open is still settled. */
watch(COMPOSE_KEY, (u) => {
  if (u.state === "done") {
    kept = "";
    outcome = { state: "done", text: u.result || DONE_TEXT, at: Date.now() };
  } else if (u.state === "failed") {
    outcome = { state: "failed", text: u.result || "Couldn't polish that. Try again.", at: Date.now() };
  }
  for (const v of [...views]) v.changed(u.state);
});

/** A refused or failed request in words for the page: the server's fixed answers, or a plain account of a failure. */
function explain(e) {
  if (!(e instanceof ApiError)) return "Couldn't send that. Try again.";
  if (e.status === 0) return "The dashboard didn't answer. The draft may still arrive below, so look there before trying again.";
  if (e.status === 409 && e.message === "paused") return PAUSED;
  return ERRORS[e.message] || "Couldn't send that. Try again.";
}

/** Mount the composer in `host`. Returns the function that unmounts it. */
export function mountComposer(host) {
  let sending = false;
  let doneTimer = null;
  const ta = el("textarea", { class: "editor composer-text", attrs: { id: "compose-text", rows: "4",
    maxlength: String(SAFETY_MAX), spellcheck: "true", "aria-describedby": "compose-hint compose-count",
    placeholder: "Type it roughly, e.g. standup moved to 3pm, please update your notes before then" } });
  ta.value = kept; // .value is never parsed
  const counter = el("span", { class: "counter num", attrs: { id: "compose-count" } });
  const over = el("span", { class: "counter-over num" });
  const status = el("div", { class: "cstatus", attrs: { role: "status", "aria-live": "polite" } });
  const ico = icon("spark");
  const spin = el("span", { class: "spinner sm", attrs: { "aria-hidden": "true" } });
  const label = el("span", { text: "Polish" });
  const btn = el("button", { class: "btn primary composer-btn", attrs: { type: "button" } }, ico, spin, label);

  function phase() {
    if (sending) return "sending";
    const c = inFlight(COMPOSE_KEY);
    return c ? (c.state === "running" ? "running" : "queued") : "idle";
  }

  /** The line under the box: progress while it works, the last result, or why Polish is off. */
  function line() {
    const p = phase();
    if (p === "sending") return ["sending", "Polish: sending…"];
    if (p === "queued") return ["queued", isComposeHeld() ? HELD_QUEUED : "Polish: queued for the next tick…"];
    if (p === "running") return ["running", "Polish: working on it…"];
    if (outcome && (outcome.state === "failed" || Date.now() - outcome.at < DONE_MS)) return [outcome.state, outcome.text];
    if (isPaused() && ta.value.trim()) return ["failed", PAUSED];
    if (isComposeHeld()) return ["held", HELD_IDLE]; // the huddle: a Polish is queued and waits its turn
    return null;
  }

  function fill(state, text) {
    const key = `${state}|${text}`;
    if (status.dataset.key === key) return; // the same words again: an aria-live region must not repeat them
    status.dataset.key = key;
    const glyph = GLYPH[state] || "";
    const t = glyph ? String(text).replace(LEADING_EMOJI, "") || String(text) : String(text);
    clear(status,
      state === "queued" || state === "sending" ? el("span", { class: "spinner sm", attrs: { "aria-hidden": "true" } }) : null,
      glyph ? el("span", { class: "glyph", text: glyph, attrs: { "aria-hidden": "true" } }) : null,
      el("bdi", { text: t }));
    status.className = "cstatus on s-" + state;
  }

  function paint() {
    const p = phase();
    const busy = p !== "idle";
    const n = count(ta.value);
    const tooLong = n > COMPOSE_MAX;
    const empty = !ta.value.trim();
    const paused = isPaused();
    counter.textContent = `${n} / ${COMPOSE_MAX}`;
    counter.classList.toggle("full", n >= COMPOSE_MAX && !tooLong);
    counter.classList.toggle("over", tooLong);
    over.textContent = tooLong ? `${n - COMPOSE_MAX} too many` : "";
    ta.readOnly = busy; // not disabled: his text stays selectable while the agent works on it
    ta.setAttribute("aria-invalid", String(tooLong));
    ta.setAttribute("aria-busy", String(busy));
    btn.disabled = busy || paused || empty || tooLong;
    label.textContent = busy ? "Polishing…" : "Polish";
    spin.hidden = !busy;
    ico.hidden = busy;
    btn.setAttribute("aria-busy", String(busy));
    const why = busy ? "" : paused ? PAUSED : empty ? "Write something first" : tooLong ? "Over the character limit" : "";
    if (why) btn.setAttribute("title", why); else btn.removeAttribute("title");
    const l = line();
    if (l) {
      fill(l[0], l[1]);
    } else if (status.dataset.key !== "") {
      status.dataset.key = "";
      clear(status);
      status.className = "cstatus";
    }
  }

  async function polish() {
    if (btn.disabled || sending || inFlight(COMPOSE_KEY)) return; // the double-click guard
    const text = ta.value;
    kept = text;
    outcome = null;
    sending = true;
    paint();
    let res = null, err = null;
    try {
      res = await api("/api/compose", { method: "POST", body: { text } });
    } catch (e) {
      err = e;
    }
    sending = false;
    if (res && Number.isInteger(res.command_id)) {
      adopt(COMPOSE_KEY, { command_id: res.command_id, verb: "compose", state: "queued" });
    } else if (err instanceof ApiError && err.status === 409 && err.data && Number.isInteger(err.data.command_id)) {
      // this very text is already being polished (another tab, or a click that was not seen): follow that one
      adopt(COMPOSE_KEY, { command_id: err.data.command_id, verb: "compose", state: err.data.state });
    } else {
      outcome = { state: "failed", text: explain(err), at: Date.now() };
    }
    paint();
  }

  function changed(state) {
    if (state === "done") {
      ta.value = "";
      window.clearTimeout(doneTimer);
      doneTimer = window.setTimeout(paint, DONE_MS + 100); // the note goes quiet by itself
    } else if (state === "failed") {
      const a = document.activeElement;
      if (!a || a === document.body || host.contains(a)) ta.focus({ preventScroll: true }); // back to fix it and retry
    }
    paint();
  }

  ta.addEventListener("input", () => {
    kept = ta.value;
    if (outcome && outcome.state === "failed") outcome = null; // he has read it and is changing the text
    paint();
  });
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); btn.click(); }
  });
  btn.addEventListener("click", polish);

  const view = { changed };
  views.add(view);
  const offJobs = onJobs(paint); // Pause or Resume: the button follows
  clear(host,
    el("h3", { class: "composer-h", text: "Write a message" }),
    el("p", { class: "composer-hint", attrs: { id: "compose-hint" },
      text: "Type it roughly. The agent tidies it up and sends you a draft, here and in your DM. Nothing is posted until you press Post." }),
    el("label", { class: "sr-only", attrs: { for: "compose-text" }, text: "Your message, as you would write it quickly" }),
    ta,
    el("div", { class: "composer-row" }, counter, over, el("span", { class: "grow" }), btn),
    status);
  paint();
  return () => {
    views.delete(view);
    offJobs();
    window.clearTimeout(doneTimer);
  };
}
