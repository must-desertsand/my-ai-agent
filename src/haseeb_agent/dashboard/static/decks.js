// The Decks page (Task 10): every deck the agent designed for an insight in the last 14 days, newest first, as a
// grid of first-page thumbnails with the fact-check result, the model, the page count and the time. The thumbnail
// and the PDF are fetched with the session header and shown through blob: URLs, never a plain link (deckview.js,
// which the deck strip on an insight's card shares: D11); a thumbnail's URL is revoked when its tile goes or the page
// unmounts, a PDF's a minute after its tab opened.
// A deck that finished after Haseeb posted its insight (R50) can be attached to the post: Attach goes through
// actions.js (a confirm first, one command per item, disabled until it settles, the server's `pending` after a
// reload). Titles and model names are shown as text only, inside a <bdi>, never in an attribute.
import { api, on } from "./client.js";
import { act, adopt, inFlight, watch } from "./actions.js";
import { clear, dayLabel, el, hhmm, parseTime } from "./dom.js";
import { OPEN_BLOCKED, OPEN_FAILED, fcBadge, fetchDeckThumb, flashNote, modelName, openDeckPdf, thumbImg } from "./deckview.js";

const REFRESH_MS = 60000;
const RETRY_MS = 5000;
const AFTER_SETTLE_MS = 1500; // a settled command changed the deck: look again soon
const EVENT_MS = 2000; // a deck step on the stream: a new deck may be about to land
const CONFIRM_Q = "Attach this deck to your channel post?";
const GLYPH = { running: "👀", done: "✅", failed: "⚠️" };
const VERB = { attach: "Attach", post: "Post", edit: "Revise", shorter: "Shorter", skip: "Skip" };
function icon(name) { return el("span", { class: "ico i-" + name, attrs: { "aria-hidden": "true" } }); }

function isoDay(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || ""));
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
}

function when(d) {
  const at = parseTime(d.at);
  if (at) return `${dayLabel(at)} · ${hhmm(at)}`;
  const day = isoDay(d.day);
  return day ? dayLabel(day) : "";
}

const count = (n) => (Number.isInteger(n) && n >= 0 ? String(n) : null);

function secs(n) {
  if (typeof n !== "number" || !Number.isFinite(n) || n < 0) return null;
  const s = Math.round(n);
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} m ${String(s % 60).padStart(2, "0")} s`;
}

function button(label, ico, cls, fn) {
  return el("button", { class: ["btn", cls].filter(Boolean).join(" "), attrs: { type: "button" }, on: { click: fn } },
    ico ? icon(ico) : null, el("span", { text: label }));
}

const stat = (label, value) => el("div", {}, el("dt", { text: label }),
  el("dd", { class: value === null ? "none" : "", text: value === null ? "—" : value }));

export function mountDecks(view) {
  const tiles = new Map(); // item_id → tile state
  const timers = [];
  const offs = [];
  let dead = false, loaded = false, soonT = null, soonAt = 0, retryT = null;

  const tallies = el("div", { class: "tallies" });
  const head = el("header", { class: "pg-head" },
    el("div", { class: "pg-title" }, el("h1", { text: "Decks" }),
      el("p", { text: "Every deck the agent designed for your insights in the last two weeks, with its fact check." })),
    tallies);
  const grid = el("div", { class: "deck-grid" });
  const root = el("div", { class: "decks" }, head,
    el("div", { class: "sk", attrs: { "aria-hidden": "true" } }, el("i"), el("i"), el("i")));
  view.appendChild(root);

  function loadSoon(ms) {
    const at = Date.now() + ms;
    if (soonT && soonAt <= at) return;
    window.clearTimeout(soonT);
    soonAt = at;
    soonT = window.setTimeout(() => { soonT = null; load(); }, ms);
  }

  async function load() {
    window.clearTimeout(retryT);
    retryT = null;
    let list;
    try { list = await api("/api/decks"); } catch (e) {
      if (dead) return;
      if (!loaded) {
        clear(root, head, el("div", { class: "load-error", attrs: { role: "status" } }, icon("warn"),
          el("span", { text: "Couldn't load your decks. Trying again…" })));
        root.dataset.ready = "1";
      }
      retryT = window.setTimeout(load, RETRY_MS);
      return;
    }
    if (dead || !Array.isArray(list)) return;
    const first = !loaded;
    loaded = true;
    const order = [];
    const keep = new Set();
    for (const d of list) {
      if (!d || typeof d.item_id !== "string" || keep.has(d.item_id)) continue;
      keep.add(d.item_id);
      if (d.pending) adopt(d.item_id, d.pending);
      const old = tiles.get(d.item_id);
      let t = old;
      if (old && old.shape === shapeOf(d)) { // the same deck: only its attach state moves on, in place
        old.d = { ...d, attachable: d.attachable && !old.attached };
        sync(old);
      } else {
        t = buildTile(d, old, first ? order.length : 0);
        if (old) dropTile(old, t.node);
        tiles.set(d.item_id, t);
        queueThumb(t);
      }
      order.push(t);
    }
    for (const [id, t] of tiles) {
      if (keep.has(id)) continue;
      tiles.delete(id);
      dropTile(t, null);
    }
    paint(order);
    root.dataset.ready = "1";
  }

  function paint(order) {
    const due = order.filter((t) => t.d.attachable).length;
    clear(tallies,
      el("span", { class: "tally" }, icon("palette"), el("b", { class: "num", text: String(order.length) }),
        order.length === 1 ? " deck" : " decks"),
      due ? el("span", { class: "tally due" }, icon("clip"), el("b", { class: "num", text: String(due) }), " to attach") : null);
    if (!order.length) {
      if (!root.querySelector(".decks-empty")) {
        clear(root, head, el("section", { class: "card decks-empty rise" },
          el("div", { class: "pg-orb", attrs: { "aria-hidden": "true" } }, icon("palette")),
          el("p", { class: "empty-t", text: "No decks yet" }),
          el("p", { class: "empty-s", text: "When the agent designs a deck for one of your insights, its first page "
            + "lands here with the fact-check result, ready to open." })));
      }
      return;
    }
    if (grid.parentNode !== root) clear(root, head, grid);
    const want = order.map((t) => t.node);
    const kids = grid.children;
    if (want.length !== kids.length || want.some((n, i) => kids[i] !== n)) grid.replaceChildren(...want);
  }

  // ---------------------------------------------------------------- a tile

  function shapeOf(d) { // what the tile shows that never changes in place: a change rebuilds it
    return JSON.stringify([d.title, d.model, d.factcheck, d.pages, d.images, d.seconds, d.available, d.day, d.at]);
  }

  function buildTile(d, prev, idx) {
    const id = d.item_id;
    const safe = id.replace(/[^A-Za-z0-9_-]/g, "_");
    const t = { id, d, shape: shapeOf(d), confirming: false, opening: false, attached: !!(prev && prev.attached),
      notice: prev ? prev.notice : null, url: null, actionsKey: "", noteT: null };
    t.ph = el("span", { class: "deck-ph loading", attrs: { "aria-hidden": "true" } },
      el("span", { class: "slide" }, el("i"), el("i"), el("i")));
    const pages = count(d.pages);
    t.thumb = el("button", {
      class: "deck-thumb",
      attrs: { type: "button", "aria-labelledby": `deck-o-${safe} deck-t-${safe}`, disabled: d.available ? null : true },
      on: { click: () => openPdf(t) },
    }, t.ph,
    el("span", { class: "deck-open", attrs: { id: `deck-o-${safe}` } }, icon("ext"), d.available ? "Open PDF" : "PDF gone"));
    t.note = el("div", { class: "deck-note", attrs: { role: "alert", hidden: true } });
    t.actions = el("div", { class: "actions" });
    t.statusLine = el("div", { class: "cstatus", attrs: { role: "status", "aria-live": "polite" } });
    t.node = el("article", {
      class: "card deck rise", data: { item: id }, attrs: { "aria-labelledby": `deck-t-${safe}`, tabindex: "-1" },
      vars: { "--d": `${Math.min(idx, 8) * 60}ms` },
    },
    t.thumb,
    el("div", { class: "deck-b" },
      el("div", { class: "deck-top" },
        fcBadge(d),
        el("span", { class: "deck-when num", text: when(d) })),
      el("h3", { class: "deck-t", attrs: { id: `deck-t-${safe}` } }, el("bdi", { text: d.title || "Untitled insight" })),
      el("div", { class: "deck-model" }, icon("spark"), el("bdi", { text: modelName(d.model) })),
      el("dl", { class: "deck-stats" }, stat("Pages", pages), stat("Images", count(d.images)), stat("Design", secs(d.seconds))),
      t.note, t.actions, t.statusLine));
    // the entrance plays once: a later re-order moves the node, and a finished animation must not replay
    t.node.addEventListener("animationend", (e) => { if (e.target === t.node) t.node.classList.remove("rise"); });
    if (!d.available) noPreview(t, "The PDF is no longer on disk");
    t.unwatch = watch(id, (u) => onCommand(t, u));
    sync(t);
    return t;
  }

  function dropTile(t, replacement) {
    t.unwatch();
    window.clearTimeout(t.noteT);
    if (t.url) URL.revokeObjectURL(t.url);
    t.url = null;
    if (replacement) t.node.replaceWith(replacement); else t.node.remove();
  }

  function sync(t) {
    paintStatus(t);
    if (!t.confirming) paintActions(t);
  }

  function paintActions(t) {
    const cmd = inFlight(t.id);
    const key = `${!!cmd}|${!!t.d.attachable}|${!!t.d.available}|${t.opening}`;
    if (key === t.actionsKey) return; // unchanged: the buttons (and the focus on one) stay as they are
    t.actionsKey = key;
    const had = t.actions.contains(document.activeElement);
    t.actions.classList.remove("confirm");
    const open = button(t.opening ? "Opening…" : "Open PDF", "ext", "", () => openPdf(t));
    open.disabled = !t.d.available || t.opening;
    const btns = [open];
    if (t.d.attachable) {
      const a = button("Attach", "clip", "primary", () => {
        if (inFlight(t.id) || !t.d.attachable) return;
        t.confirming = true;
        paintConfirm(t);
      });
      a.disabled = !!cmd;
      btns.push(a);
    }
    clear(t.actions, ...btns);
    if (had) t.node.focus({ preventScroll: true }); // the focused button was replaced: keep him on the deck
  }

  function paintConfirm(t) {
    t.actionsKey = "";
    const finish = (go) => {
      t.confirming = false;
      if (go) attach(t);
      paintActions(t);
      t.node.focus({ preventScroll: true }); // back to the deck, never lost to <body>
    };
    const yes = button("Confirm", "clip", "primary", () => finish(true));
    const no = button("Cancel", null, "ghost", () => finish(false));
    for (const b of [yes, no]) b.addEventListener("keydown", (e) => { if (e.key === "Escape") finish(false); });
    clear(t.actions, el("span", { class: "confirm-q", text: CONFIRM_Q }), no, yes);
    t.actions.classList.add("confirm");
    window.setTimeout(() => { if (no.isConnected) no.focus(); }, 0); // Enter lands on Cancel
  }

  function attach(t) {
    if (inFlight(t.id) || !t.d.attachable) return; // the double-click guard (actions.js and the server's too)
    t.notice = null;
    act(t.id, "attach");
  }

  function onCommand(t, u) {
    if (u.state === "done" || u.state === "failed") {
      t.notice = { state: u.state, text: u.result || (u.state === "done" ? "Done" : "Failed") };
      if (u.state === "done" && u.verb === "attach") { // attached: never offered again, even before the reload
        t.attached = true;
        t.d = { ...t.d, attachable: false };
      }
      loadSoon(AFTER_SETTLE_MS);
    }
    sync(t);
  }

  function paintStatus(t) {
    const cmd = inFlight(t.id);
    if (cmd) {
      const label = VERB[cmd.verb] || "Command";
      const text = cmd.state === "sending" ? `${label}: sending…`
        : cmd.state === "running" ? `${label}: working on it…` : `${label}: queued for the next tick…`;
      fill(t.statusLine, cmd.state === "sending" || cmd.state === "running" ? cmd.state : "queued", text);
    } else if (t.notice) {
      fill(t.statusLine, t.notice.state, t.notice.text);
    } else if (t.statusLine.dataset.key !== "") {
      t.statusLine.dataset.key = "";
      clear(t.statusLine);
      t.statusLine.className = "cstatus";
    }
    t.node.classList.toggle("busy", !!cmd);
    t.node.setAttribute("aria-busy", String(!!cmd));
    t.node.classList.toggle("settled", !cmd && !!t.notice && t.notice.state === "done");
  }

  /** The state glyph, then the runner's words as text (its own leading emoji gives way to the glyph). */
  function fill(line, state, text) {
    const key = `${state}|${text}`;
    if (line.dataset.key === key) return; // the same words again: an aria-live region must not repeat them
    line.dataset.key = key;
    const glyph = GLYPH[state] || "";
    let words = String(text);
    if (glyph) words = words.replace(/^(?:\p{Extended_Pictographic}|️|‍|\s)+/u, "") || String(text);
    clear(line,
      state === "queued" || state === "sending" ? el("span", { class: "spinner sm", attrs: { "aria-hidden": "true" } }) : null,
      glyph ? el("span", { class: "glyph", text: glyph, attrs: { "aria-hidden": "true" } }) : null,
      el("bdi", { text: words }));
    line.className = "cstatus on s-" + state;
  }

  function note(t, text) { t.noteT = flashNote(t.note, text, t.noteT); }

  // ---------------------------------------------------------------- thumbnails and the PDF

  function queueThumb(t) { // deckview takes them two at a time, in this order
    if (t.d.available) loadThumb(t);
  }

  async function loadThumb(t) {
    const url = await fetchDeckThumb(t.id, () => !dead && tiles.get(t.id) === t); // a tile rebuilt or gone: no request
    if (dead || tiles.get(t.id) !== t) return;
    if (!url) { noPreview(t, "No preview"); return; }
    t.url = url;
    const img = thumbImg(url, "deck-img", () => t.ph.remove(), () => noPreview(t, "No preview"));
    t.thumb.insertBefore(img, t.thumb.firstChild);
  }

  function noPreview(t, words) {
    t.ph.classList.remove("loading");
    clear(t.ph, el("span", { class: "slide" }, el("i"), el("i"), el("i")), el("span", { text: words }));
  }

  async function openPdf(t) {
    if (t.opening || !t.d.available) return;
    t.opening = true;
    t.node.classList.add("opening");
    if (!t.confirming) paintActions(t);
    const result = await openDeckPdf(t.id, () => !dead);
    t.opening = false;
    t.node.classList.remove("opening");
    if (dead) return;
    if (!t.confirming) paintActions(t);
    if (result === "failed") note(t, OPEN_FAILED);
    else if (result === "blocked") note(t, OPEN_BLOCKED);
  }

  // ---------------------------------------------------------------- wiring

  offs.push(on("event", (e) => { if (e && e.job === "decks") loadSoon(EVENT_MS); }));
  timers.push(window.setInterval(load, REFRESH_MS));
  load();
  return () => {
    dead = true;
    timers.forEach((x) => window.clearInterval(x));
    offs.forEach((f) => f());
    window.clearTimeout(soonT);
    window.clearTimeout(retryT);
    for (const t of tiles.values()) dropTile(t, null);
    tiles.clear();
  };
}
