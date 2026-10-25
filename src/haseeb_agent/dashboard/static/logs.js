// The Logs page (Task 10): every line of the agent's log files (the tick's and the insights', with their error logs)
// as the API parses them (time, level, source, text), newest first and grouped by day, in monospace. The lines are
// text-free by rule, but they are shown as untrusted text all the same: through textContent, inside a <bdi>.
// The filter chips and the search (250 ms after the last keystroke) go to the server. A step event on the stream,
// or a quiet look every 15 s, fetches the newest lines, which slide in on top; "Load older" pages back with before=.
// At most MAX_ROWS rows stay in the DOM: new lines push the oldest out at the bottom, and paging back drops the
// newest at the top, after which the page holds still ("Back to newest") until he asks for the newest again.
import { api, on } from "./client.js";
import { clear, dayLabel, el, hhmmss, parseTime } from "./dom.js";

const FILTERS = [["all", "All", "layers"], ["slack", "Slack", "hash"], ["models", "Models", "spark"],
  ["decks", "Decks", "palette"], ["problems", "Problems", "warn"]];
const SEARCH_MS = 250;
const Q_MAX = 100; // the API's cap on a query
const PAGE = 200; // the API answers at most this many lines: fewer means nothing older is left
const MAX_ROWS = 300;
const EVENT_MS = 600; // a burst of step events: one fetch
const REFRESH_MS = 15000;
const RETRY_MS = 5000;
const SLACK_JOBS = ["process_dm", "process_page", "deliver_pending", "run_todo", "run_feedback"]; // as the API's filter
const MODEL_SOURCES = ["litellm", "judge", "claude_cli"];

function icon(name) { return el("span", { class: "ico i-" + name, attrs: { "aria-hidden": "true" } }); }

const pad = (n) => String(n).padStart(2, "0");
const dayOf = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const keyOf = (r) => `${r.at}|${r.level}|${r.source}|${r.text}`;

/** "bad" (an error), "warn" (a warning, or a line that says it failed: the API's Problems filter) or "info". */
function levelOf(r) {
  if (r.level === "ERROR" || r.level === "CRITICAL") return "bad";
  if (r.level === "WARNING" || ` ${String(r.text).toLowerCase()} `.includes(" fail ")) return "warn";
  return "info";
}

/** The source's colour, by the same groups as the API's filters. */
function sourceKind(src) {
  const s = String(src).toLowerCase();
  if (s.includes("deck")) return "k-deck";
  if (MODEL_SOURCES.includes(s)) return "k-model";
  if (s.includes("slack") || SLACK_JOBS.includes(s)) return "k-slack";
  if (s === "tick" || s === "insights") return "k-tick";
  return "";
}

function dayTitle(day) {
  const now = new Date();
  const [y, m, d] = day.split("-").map(Number);
  const date = new Date(y, m - 1, d);
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (day === dayOf(now)) return ["Today", dayLabel(date)];
  if (day === dayOf(yesterday)) return ["Yesterday", dayLabel(date)];
  return [dayLabel(date), ""];
}

export function mountLogs(view) {
  const s = { filter: "all", q: "", browsing: false, exhausted: false, fresh: 0 };
  let rows = []; // what's on screen, newest first: {key, t, day, node}
  const keys = new Set(); // the keys on screen
  const seen = new Set(); // every key shown or counted since the last full load (for "N new lines")
  let topT = -Infinity; // the newest time shown or counted since then
  let dead = false, loaded = false, busy = false, seq = 0, searchT = null, soonT = null, retryT = null;
  let connected = true;
  const timers = [];
  const offs = [];

  // ---- the frame, built once: using a control only changes its state, so the focus stays on it
  const input = el("input", { attrs: { type: "search", placeholder: "Search the log… “slack”, “deck”, “failed”",
    "aria-label": "Search the log", maxlength: String(Q_MAX), autocomplete: "off", spellcheck: "false",
    enterkeyhint: "search" } });
  const xBtn = el("button", { class: "search-x", attrs: { type: "button", "aria-label": "Clear the search", hidden: true } },
    icon("x"));
  const searchBox = el("div", { class: "lsearch", attrs: { role: "search" } }, icon("search"), input, xBtn,
    el("kbd", { class: "kbd", text: "/", attrs: { "aria-hidden": "true" } }));
  searchBox.addEventListener("click", (e) => { if (e.target === searchBox) input.focus(); });
  const head = el("header", { class: "pg-head" },
    el("div", { class: "pg-title" }, el("h1", { text: "Logs" }),
      el("p", { text: "Every tick, model call and Slack read the agent logged, newest first." })),
    searchBox);
  const chips = new Map();
  const chipBox = el("div", { class: "lchips", attrs: { role: "group", "aria-label": "Filter the log" } });
  for (const [k, label, ico] of FILTERS) {
    const b = el("button", { class: "lchip f-" + k, data: { f: k }, attrs: { type: "button" } }, icon(ico),
      el("span", { text: label }));
    b.addEventListener("click", () => {
      if (s.filter === k) return;
      s.filter = k;
      paintChips();
      reload();
    });
    chips.set(k, b);
    chipBox.appendChild(b);
  }
  const state = el("div", { class: "lstate", attrs: { role: "status" } });
  const bar = el("div", { class: "lbar" }, chipBox, state);
  const banner = el("div", { class: "lnew", attrs: { hidden: true } });
  const list = el("div", { class: "lg-list", attrs: { role: "log", "aria-live": "off", "aria-busy": "true",
    "aria-label": "Log lines, newest first" } });
  const foot = el("div", { class: "lfoot" });
  const logCard = el("section", { class: "card lcard rise", attrs: { "aria-label": "Log" } }, banner, list, foot);
  const root = el("div", { class: "logs" }, head, bar, logCard);
  view.appendChild(root);
  paintChips();

  function paintChips() { for (const [k, b] of chips) b.setAttribute("aria-pressed", String(k === s.filter)); }

  function query(before) {
    const p = new URLSearchParams({ filter: s.filter });
    if (s.q) p.set("q", s.q);
    if (before) p.set("before", before);
    return "/api/logs?" + p.toString();
  }

  function setBusy(on) {
    busy = on;
    list.setAttribute("aria-busy", String(on));
  }

  /** The API's rows (oldest first) as row records, newest first; a row without a readable time is left out. */
  function toRows(data, enter) {
    const out = [];
    for (const r of data) {
      if (!r || typeof r.at !== "string") continue;
      const at = parseTime(r.at);
      if (!at) continue;
      out.push({ key: keyOf(r), t: at.getTime(), day: dayOf(at), node: rowNode(r, at, enter) });
    }
    return out.reverse();
  }

  function rowNode(r, at, enter) {
    const lvl = levelOf(r);
    return el("li", { class: ["lrow", "lv-" + lvl, enter ? "enter" : ""].filter(Boolean).join(" ") },
      el("span", { class: "lts", text: hhmmss(at) }),
      el("span", { class: ["lsrc", sourceKind(r.source)].filter(Boolean).join(" ") }, el("bdi", { text: String(r.source ?? "") })),
      el("span", { class: "ltxt" },
        lvl === "info" ? null : el("span", { class: "lvl", text: lvl === "bad" ? (r.level === "CRITICAL" ? "CRIT" : "ERROR") : "WARN" }),
        el("bdi", { class: "lmsg", text: String(r.text ?? "") })));
  }

  function group(day) {
    const [title, sub] = dayTitle(day);
    return el("section", { class: "lday", data: { day } },
      el("h3", { class: "lday-h" }, el("b", { text: title }), sub ? " · " + sub : null),
      el("ol", { class: "lrows" }));
  }

  function prepend(batch) { // newer than everything shown, newest first
    if (!rows.length) clear(list); // the empty state or an error note goes when the first lines arrive
    for (let i = batch.length - 1; i >= 0; i -= 1) { // the oldest goes on top first, each newer one above it
      const r = batch[i];
      let g = list.firstElementChild;
      if (!g || g.dataset.day !== r.day) { g = group(r.day); list.prepend(g); }
      g.lastElementChild.prepend(r.node);
    }
    rows = batch.concat(rows);
    note(batch);
  }

  function append(batch) { // older than everything shown, newest first
    if (!rows.length) clear(list);
    for (const r of batch) {
      let g = list.lastElementChild;
      if (!g || g.dataset.day !== r.day) { g = group(r.day); list.append(g); }
      g.lastElementChild.append(r.node);
    }
    rows = rows.concat(batch);
    note(batch);
  }

  function note(batch) {
    for (const r of batch) { keys.add(r.key); seen.add(r.key); topT = Math.max(topT, r.t); }
    if (seen.size > 5000) { seen.clear(); for (const k of keys) seen.add(k); }
  }

  function remove(r) {
    keys.delete(r.key);
    const ol = r.node.parentElement;
    r.node.remove();
    if (ol && !ol.firstElementChild) ol.parentElement.remove(); // an empty day goes with its last line
  }

  /** Back to MAX_ROWS: from the bottom (the oldest) as new lines arrive, from the top (the newest) when paging back. */
  function trim(fromTop) {
    if (rows.length <= MAX_ROWS) return false;
    if (fromTop) rows.splice(0, rows.length - MAX_ROWS).forEach(remove);
    else rows.splice(MAX_ROWS).forEach(remove);
    return true;
  }

  // ---- loading
  async function reload() {
    const my = ++seq;
    window.clearTimeout(retryT);
    retryT = null;
    setBusy(true);
    let data;
    try { data = await api(query()); } catch (e) {
      if (dead || my !== seq) return;
      setBusy(false);
      if (!loaded || !rows.length) {
        clear(list, el("div", { class: "load-error", attrs: { role: "status" } }, icon("warn"),
          el("span", { text: "Couldn't load the log. Trying again…" })));
        clear(foot);
      }
      retryT = window.setTimeout(reload, RETRY_MS);
      root.dataset.ready = "1";
      return;
    }
    if (dead || my !== seq || !Array.isArray(data)) return;
    loaded = true;
    rows = [];
    keys.clear();
    seen.clear();
    topT = -Infinity;
    Object.assign(s, { browsing: false, exhausted: data.length < PAGE, fresh: 0 });
    clear(list);
    append(toRows(data, false));
    paint();
    setBusy(false);
    root.dataset.ready = "1";
  }

  /** The newest lines: on screen at once when he's at the top, otherwise only counted ("N new lines"). */
  async function pull() {
    if (dead || busy || !loaded) return;
    const my = seq;
    let data;
    try { data = await api(query()); } catch (e) { return; }
    if (dead || my !== seq || busy || !Array.isArray(data)) return;
    const fresh = toRows(data.filter((r) => !seen.has(keyOf(r))), !s.browsing).filter((r) => r.t >= topT);
    if (!fresh.length) return;
    if (s.browsing) {
      s.fresh += fresh.length;
      for (const r of fresh) { seen.add(r.key); topT = Math.max(topT, r.t); }
    } else {
      prepend(fresh);
      if (trim(false)) s.exhausted = false; // older lines left at the bottom: there is something older again
    }
    paint();
  }

  async function older() {
    if (busy || s.exhausted || !rows.length) return;
    const last = rows[rows.length - 1];
    const my = ++seq;
    setBusy(true);
    let data;
    // just past the oldest line shown: a line in the same second that the last page cut off comes too (a repeat is
    // dropped by its key)
    try { data = await api(query(new Date(last.t + 1).toISOString())); } catch (e) {
      if (dead || my !== seq) return;
      setBusy(false);
      paintFoot("Couldn't load older lines. Try again.");
      return;
    }
    if (dead || my !== seq || !Array.isArray(data)) return;
    const batch = toRows(data.filter((r) => !keys.has(keyOf(r))), false).filter((r) => r.t <= last.t);
    s.exhausted = data.length < PAGE || !batch.length;
    append(batch);
    if (trim(true)) s.browsing = true; // the newest lines left the page: it holds still until "Back to newest"
    paint();
    setBusy(false);
  }

  // ---- painting
  function paint() {
    if (!rows.length) paintEmpty();
    paintFoot();
    paintBanner();
    paintState();
  }

  function paintEmpty() {
    const [t, sub] = s.q ? [`No lines match “${s.q}”`, "Try fewer words, or another filter."]
      : s.filter !== "all" ? ["Nothing matches this filter", "Lines show up here as the agent writes them."]
        : ["No log lines yet", "The agent writes a line at every tick; they appear here as they happen."];
    clear(list, el("div", { class: "lempty" },
      el("div", { class: "lempty-orb", attrs: { "aria-hidden": "true" } }, icon(s.q ? "search" : "term")),
      el("div", { class: "lempty-t", text: t }), el("div", { class: "lempty-s", text: sub })));
  }

  function paintFoot(error) {
    if (!rows.length) { clear(foot); return; }
    if (s.exhausted) {
      clear(foot, el("span", { class: "lend" }, icon("check"), "The start of the log"));
      return;
    }
    const b = el("button", { class: "btn lmore", attrs: { type: "button" } }, icon("clock"),
      el("span", { text: "Load older lines" }));
    b.addEventListener("click", older);
    const had = foot.contains(document.activeElement);
    clear(foot, b, error ? el("span", { class: "lend", text: error }) : null);
    if (had) b.focus({ preventScroll: true });
  }

  function paintBanner() {
    banner.hidden = !s.browsing;
    if (!s.browsing) { clear(banner); return; }
    const back = el("button", { class: "btn primary", attrs: { type: "button" } }, icon("up"),
      el("span", { text: "Back to newest" }));
    back.addEventListener("click", () => {
      reload();
      window.scrollTo({ top: 0, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    });
    const had = banner.contains(document.activeElement);
    clear(banner, el("span", {}, icon("clock"), s.fresh
      ? `Showing older lines · ${s.fresh} new ${s.fresh === 1 ? "line" : "lines"} since`
      : "Showing older lines · new ones wait here"), back);
    if (had) back.focus({ preventScroll: true });
  }

  function paintState() {
    const n = rows.length;
    const shown = el("b", { class: "num", text: String(n) });
    state.className = ["lstate", s.browsing ? "paused" : "", connected ? "" : "off"].filter(Boolean).join(" ");
    clear(state, el("span", { class: "lpulse", attrs: { "aria-hidden": "true" } }),
      s.browsing ? "Paused · " : connected ? "Live · " : "Reconnecting · ", shown, n === 1 ? " line" : " lines");
  }

  // ---- search: 250 ms after the last keystroke
  function paintSearch() { xBtn.hidden = !input.value; }

  function runSearch() {
    window.clearTimeout(searchT);
    searchT = null;
    const q = input.value.trim().slice(0, Q_MAX);
    if (q === s.q) return;
    s.q = q;
    reload();
  }

  function clearSearch() {
    window.clearTimeout(searchT);
    searchT = null;
    input.value = "";
    paintSearch();
    input.focus();
    if (!s.q) return;
    s.q = "";
    reload();
  }

  input.addEventListener("input", () => {
    paintSearch();
    window.clearTimeout(searchT);
    searchT = window.setTimeout(runSearch, SEARCH_MS);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") runSearch();
    else if (e.key === "Escape" && (input.value || s.q)) { e.preventDefault(); clearSearch(); }
  });
  xBtn.addEventListener("click", clearSearch);
  const onSlash = (e) => { // "/" jumps to the search box, unless he is typing somewhere
    if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target;
    if (t instanceof Element && t.closest("input, textarea, select, [contenteditable]")) return;
    if (!input.isConnected) return;
    e.preventDefault();
    input.focus();
  };
  document.addEventListener("keydown", onSlash);

  // ---- live
  function pullSoon() {
    if (soonT) return;
    soonT = window.setTimeout(() => { soonT = null; pull(); }, EVENT_MS);
  }
  offs.push(on("event", pullSoon));
  offs.push(on("stream", (st) => {
    connected = !!(st && st.connected);
    if (loaded) paintState();
  }));
  timers.push(window.setInterval(pull, REFRESH_MS));
  reload();
  return () => {
    dead = true;
    timers.forEach((x) => window.clearInterval(x));
    offs.forEach((f) => f());
    [searchT, soonT, retryT].forEach((x) => window.clearTimeout(x));
    document.removeEventListener("keydown", onSlash);
  };
}
