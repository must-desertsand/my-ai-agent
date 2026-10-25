// The History page (Task 9): the program channel's whole story, everyone's huddle notes, Next Todos, insights and the
// feedback on them, one week at a time (a week loads only when it is picked). People chips filter by person, tabs by
// kind, and the switch groups by day, by person or by topic; search covers every week. Each item links to its Slack
// message (a checked permalink only, cleanUrl) and can be re-labelled (optimistic, rolled back on an error).
// Slack text is untrusted: it reaches the DOM only as text, each piece inside a <bdi>, never in an attribute.
// The controls are built once and only change state, and a re-render gives the focus back to the same item, so a
// keyboard user never loses his place.
import { api } from "./client.js";
import { cleanUrl, clear, dayLabel, el, hhmm, setVars } from "./dom.js";
import { feedbackControl } from "./feedbackbtn.js";
import { jobInfo, onJobs } from "./jobs.js";

const KINDS = ["huddle_notes", "todo", "insight", "feedback", "other"];
const KIND = {
  huddle_notes: { label: "Huddle notes", tab: "Huddles", icon: "mic", words: ["huddle note", "huddle notes"] },
  todo: { label: "Next Todo", tab: "Todos", icon: "todo", words: ["Next Todo", "Next Todos"] },
  insight: { label: "Insight", tab: "Insights", icon: "bulb", words: ["insight", "insights"] },
  feedback: { label: "Feedback", tab: "Feedback", icon: "chat", words: ["feedback", "feedback"] },
  other: { label: "Other", tab: "Other", icon: "dots", words: ["other", "other"] },
};
const VIEWS = [["day", "By day"], ["person", "By person"], ["topic", "By topic"]];
const SEARCH_MS = 250;
const REFRESH_MS = 60000;
const RETRY_MS = 5000;
const REGROUP_MS = 600; // a saved label reloads the view, so the item moves to its new section
const NOTE_MS = 4000;
const COLORS = 12; // the API's person palette: `color` is 0–11
const Q_MAX = 200; // the API's cap on a query
const HEAT_WEEKS = 10;
const LANE_SHOW = 25;
const DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
  "November", "December"];

// ---------------------------------------------------------------- Slack text, made readable (as text, never markup)

// Every pattern here runs on untrusted text of any length, so each is linear: a repeated class always ends at a
// character it can't match (`[a-z]+` before `^`, `|` or `>`; `[^*\n]*?` before `*`), never where another repeat or
// optional run could take the same characters (no two ways to split a run), and a trailing run is trimmed by a loop,
// never by an unanchored /[…]+$/.
// Slack's own markup inside message text: <@U123>, <#C123|name>, <!here>, <!subteam^S1|@team>, <URL|label>. Each
// becomes plain words, never a link (only a checked permalink is ever a link); then &lt; &gt; &amp; are decoded.
// Anything else, a literal "<img ...>" say, stays exactly as typed.
const SLACK_TOKEN = /<(?:@([UWB][A-Z0-9]+)|#(C[A-Z0-9]+)|!([a-z]+)(?:\^[^<>|]*)?|((?:https?|mailto|tel):[^<>|\s]+))(?:\|([^<>]*))?>/g;
const SHORTCODE = /:([a-z0-9_+-]+):/g;
const EMOJI = new Map(Object.entries({
  bulb: "💡", white_check_mark: "✅", heavy_check_mark: "✔️", rocket: "🚀", fire: "🔥", "+1": "👍", thumbsup: "👍",
  clap: "👏", eyes: "👀", tada: "🎉", heart: "❤️", pushpin: "📌", memo: "📝", point_right: "👉", dart: "🎯",
  chart_with_upwards_trend: "📈", warning: "⚠️", 100: "💯", raised_hands: "🙌", pray: "🙏", muscle: "💪",
  star: "⭐", sparkles: "✨", zap: "⚡", link: "🔗", calendar: "📅", moneybag: "💰", brain: "🧠", bar_chart: "📊",
  speech_balloon: "💬", studio_microphone: "🎙️", spiral_note_pad: "🗒️", rotating_light: "🚨", wave: "👋",
}));
const BOLD = /(^|[^\w*])\*(?=\S)([^*\n]*?\S)\*(?![\w*])/g;
// what comes before the words of a title: spaces, emoji (with their variation selectors and joiners), :shortcodes:
// and markdown marks
const LEAD = /^(?:\s|:[a-z0-9_+-]+:|[*_~`>#•·–—-]|\p{Extended_Pictographic}|\p{Variation_Selector}|\p{Join_Control})+/u;
const INSIGHT_PREFIX = /^(?:daily\s+)?insight\b[\s:.\-–—·|]*(?:#\d+\b[\s:.\-–—·|]*)?/i;
const TODO_PREFIX = /^next\s+todo\b[\s:.\-–—·|]*/i;
const TAIL = /[\s*_~]/; // tested one character at a time (trimTail)

function plain(text, people) {
  return String(text ?? "")
    .replace(SLACK_TOKEN, (_, user, chan, special, url, label) => {
      if (user) return "@" + (label || (people.get(user) || {}).name || "someone");
      if (chan) return "#" + (label || "channel");
      if (special) return label || (["here", "channel", "everyone"].includes(special) ? "@" + special : "");
      return label || url;
    })
    .replace(SHORTCODE, (m, code) => EMOJI.get(code) || m)
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
}

/** Text as nodes inside one <bdi>: *bold* spans become <b>, everything else stays text. */
function txt(s, cls) {
  const parts = [];
  let at = 0;
  for (const m of s.matchAll(BOLD)) {
    const start = m.index + m[1].length;
    if (start > at) parts.push(s.slice(at, start));
    parts.push(el("b", { text: m[2] }));
    at = m.index + m[0].length;
  }
  if (at < s.length) parts.push(s.slice(at));
  return el("bdi", { class: ["txt", cls].filter(Boolean).join(" ") }, parts);
}

/** `s` without its trailing spaces and markdown marks: a loop, linear on any input. */
function trimTail(s) {
  let end = s.length;
  while (end > 0 && TAIL.test(s[end - 1])) end -= 1;
  return s.slice(0, end);
}

function tidy(line, prefix) {
  let t = line.replace(LEAD, "");
  if (prefix) t = t.replace(prefix, "").replace(LEAD, "");
  return trimTail(t.replace(/\*([^*\n]+)\*/g, "$1"));
}

/** {title, body}: the first line (without the kind's own "Daily insight:" prefix) and the rest. */
function split(s, prefix) {
  const lines = s.split("\n");
  let i = lines.findIndex((l) => l.trim());
  if (i < 0) return { title: "", body: "" };
  let title = tidy(lines[i], prefix);
  if (!title) { // "Daily insight" alone on its line: the next line is the title
    const j = lines.findIndex((l, k) => k > i && l.trim());
    if (j > i) { title = tidy(lines[j], null); i = j; }
  }
  return { title, body: lines.slice(i + 1).join("\n").trim() };
}

const todoText = (s) => s.replace(LEAD, "").replace(TODO_PREFIX, "").trim();

// ---------------------------------------------------------------- small helpers

const arr = (x) => (Array.isArray(x) ? x : []);
const kindOf = (k) => (Object.hasOwn(KIND, k) ? KIND[k] : KIND.other);
const kindKey = (k) => (Object.hasOwn(KIND, k) ? k : "other");
const pc = (c) => "pc-" + (Number.isInteger(c) && c >= 0 && c < COLORS ? c : 0);
const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;
const pad = (n) => String(n).padStart(2, "0");
const tsNum = (m) => { const n = Number.parseFloat(m && m.ts); return Number.isFinite(n) ? n : 0; };
const byNewest = (a, b) => tsNum(b) - tsNum(a);
const itemKey = (m) => `${m.channel} ${m.ts}`;

function icon(name, cls) {
  return el("span", { class: ["ico", "i-" + name, cls].filter(Boolean).join(" "), attrs: { "aria-hidden": "true" } });
}

function isoDay(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || ""));
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
}
const isoOf = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
function addDays(d, n) { const x = new Date(d.getTime()); x.setDate(x.getDate() + n); return x; }
function mondayOf(d) { return addDays(new Date(d.getFullYear(), d.getMonth(), d.getDate()), -((d.getDay() + 6) % 7)); }
const longDay = (d) => `${DAY_NAMES[d.getDay()]} ${d.getDate()} ${MONTH_NAMES[d.getMonth()]}`;
const shortDay = (iso) => { const d = isoDay(iso); return d ? dayLabel(d) : ""; };

function span(iso) { // "12–18 Oct", "28 Sep – 4 Oct"
  const a = isoDay(iso);
  if (!a) return "";
  const b = addDays(a, 6);
  const mon = (d) => MONTH_NAMES[d.getMonth()].slice(0, 3);
  return a.getMonth() === b.getMonth() ? `${a.getDate()}–${b.getDate()} ${mon(a)}` : `${a.getDate()} ${mon(a)} – ${b.getDate()} ${mon(b)}`;
}

function weekName(iso) {
  const cur = isoOf(mondayOf(new Date()));
  if (iso === cur) return "This week";
  if (iso === isoOf(addDays(isoDay(cur), -7))) return "Last week";
  return span(iso);
}

function timeOf(m) { const n = tsNum(m); return n ? hhmm(new Date(n * 1000)) : ""; }

function initials(name) {
  const words = String(name || "").replace(/[^\p{L}\p{N}\s]/gu, " ").trim().split(/\s+/).filter(Boolean);
  if (!words.length) return "?";
  const first = [...words[0]];
  return (words.length > 1 ? first[0] + [...words[1]][0] : first.slice(0, 2).join("")).toUpperCase();
}

function avatar(name, color, cls) {
  return el("span", { class: ["av", pc(color), cls].filter(Boolean).join(" "), text: initials(name),
    attrs: { "aria-hidden": "true" } });
}

const who = (m) => el("b", { class: "who" }, el("bdi", { text: m.user_name || "Someone" }));
const you = () => el("span", { class: "you", text: "You" });

function say(text) {
  const live = document.getElementById("announce");
  if (live) live.textContent = text;
}

function countIn(groups, view) {
  if (view !== "day") return groups.reduce((n, g) => n + arr(g.messages).length, 0);
  return groups.reduce((n, g) => {
    const h = g.huddle || {};
    return n + arr(h.messages).length + arr(h.todos).length + arr(g.other).length
      + arr(g.insights).reduce((k, x) => k + 1 + arr(x.feedback).length, 0);
  }, 0);
}

// ---------------------------------------------------------------- the page

export function mountHistory(view) {
  const people = new Map(); // user_id → {user_id, name, color}
  const s = { view: "day", kind: "", person: "", q: "", week: "", weeks: [], first: true };
  const pending = new Set(); // item keys of labels on their way to the server
  const openOther = new Set(); // days whose "Other messages" he opened: they stay open across a refresh
  const timers = [];
  // focus keys: each focusable thing in the content has one ("<channel> <ts>|lbl", "other|<day>", ...), so a
  // re-render can give the focus back to the same thing in the new nodes
  const keyOf = new WeakMap();
  let byKey = new Map();
  const reg = (node, key) => { keyOf.set(node, key); byKey.set(key, node); return node; };
  let dead = false, seq = 0, searchT = null, soonT = null, retryT = null, lastSig = "", weeksSig = "", menu = null;
  let reveal = null; // the focus key of an item just re-labelled: the next render scrolls it into view (it moved)

  // ---- the frame
  const input = el("input", { attrs: { type: "search", placeholder: "Search… “pricing”, “Africa”, person:\"Sara Ali\"",
    "aria-label": "Search the whole history", maxlength: String(Q_MAX), autocomplete: "off", spellcheck: "false",
    enterkeyhint: "search" } });
  const xBtn = el("button", { class: "search-x", attrs: { type: "button", "aria-label": "Clear the search", hidden: true } },
    icon("x"));
  const searchBox = el("div", { class: "search", attrs: { role: "search" } }, icon("search"), input, xBtn,
    el("kbd", { class: "kbd", text: "/", attrs: { "aria-hidden": "true" } }));
  searchBox.addEventListener("click", (e) => { if (e.target === searchBox) input.focus(); });
  const title = () => el("div", { class: "hist-title" }, el("h1", { text: "Team history" }),
    el("p", { text: "Everyone's huddles, Next Todos, insights and feedback, organised by day." }));
  const head = el("header", { class: "hist-head" }, title(), searchBox);
  const peopleBox = el("div", { class: "people", attrs: { role: "group", "aria-label": "Filter by person" } });
  const kindsBox = el("div", { class: "kinds", attrs: { role: "group", "aria-label": "Filter by kind" } });
  const viewsBox = el("div", { class: "vswitch", attrs: { role: "group", "aria-label": "Group by" } });
  const bar = el("div", { class: "hist-bar" }, kindsBox, viewsBox);
  const side = el("aside", { class: "card hist-side", attrs: { "aria-label": "Weeks" } });
  const main = el("div", { class: "hist-main", attrs: { "aria-busy": "true", tabindex: "-1" } });
  const layout = el("div", { class: "hist-layout" }, side, main);
  const root = el("div", { class: "hist" }, head,
    el("div", { class: "skeleton", attrs: { "aria-hidden": "true" } }, el("i"), el("i"), el("i")));
  view.appendChild(root);

  // ---- boot: people and weeks, then the newest week that has messages
  async function boot() {
    retryT = null;
    let ppl, weeks;
    try { [ppl, weeks] = await Promise.all([api("/api/people"), api("/api/weeks")]); } catch (e) {
      if (dead) return;
      clear(root, head, el("div", { class: "load-error", attrs: { role: "status" } }, icon("warn"),
        el("span", { text: "Couldn't load the history. Trying again…" })));
      root.dataset.ready = "1";
      retryT = window.setTimeout(boot, RETRY_MS);
      return;
    }
    if (dead) return;
    setPeople(ppl);
    setWeeks(weeks);
    const newest = [...s.weeks].reverse().find((w) => w.total > 0);
    if (!newest) { paintEmpty(); return; }
    s.week = newest.week;
    clear(root, head, peopleBox, bar, layout);
    side.classList.add("rise");
    buildPeople();
    buildKinds();
    buildViews();
    buildSide();
    await load();
  }

  function setPeople(list) {
    people.clear();
    for (const p of arr(list)) if (p && typeof p.user_id === "string" && p.user_id) people.set(p.user_id, p);
  }

  function setWeeks(list) {
    s.weeks = arr(list).filter((w) => w && isoDay(w.week) && Array.isArray(w.days) && w.days.length === 7)
      .map((w) => ({ week: w.week, days: w.days.map((n) => (Number.isInteger(n) && n > 0 ? n : 0)),
        total: Number.isInteger(w.total) ? w.total : 0 }));
    weeksSig = JSON.stringify(s.weeks);
  }

  function paintEmpty() {
    clear(root, el("header", { class: "hist-head" }, title()),
      el("section", { class: "card hist-empty rise" },
        el("div", { class: "hist-orb", attrs: { "aria-hidden": "true" } }, icon("clock")),
        el("p", { class: "hist-empty-t" }, "No history yet — run ", el("code", { text: "haseeb-agent history-import" })),
        el("p", { class: "hist-empty-s", text: "It copies the program channel's past messages into your dashboard, "
          + "outside the huddle window (14:25–16:05 on workdays). New messages then arrive on their own." })));
    root.dataset.ready = "1";
  }

  // ---- loading one view of one week (or of a search)
  function query() {
    const p = new URLSearchParams({ view: s.view });
    if (s.q) p.set("q", s.q); else if (s.week) p.set("week", s.week);
    if (s.person) p.set("person", s.person);
    if (s.kind) p.set("kind", s.kind);
    return "/api/history?" + p.toString();
  }

  async function load({ quiet = false } = {}) {
    const my = ++seq;
    window.clearTimeout(soonT);
    soonT = null;
    const path = query();
    main.setAttribute("aria-busy", "true");
    if (!quiet) main.classList.add("loading");
    let data;
    try { data = await api(path); } catch (e) {
      if (dead || my !== seq) return;
      main.classList.remove("loading");
      main.setAttribute("aria-busy", "false");
      if (!quiet) {
        lastSig = "";
        clear(main, el("div", { class: "load-error", attrs: { role: "status" } }, icon("warn"),
          el("span", { text: "Couldn't load this view. Trying again…" })));
      }
      reloadSoon(RETRY_MS);
      root.dataset.ready = "1";
      return;
    }
    if (dead || my !== seq) return;
    main.classList.remove("loading");
    const sig = path + JSON.stringify(data);
    if (!quiet || sig !== lastSig) {
      lastSig = sig;
      const n = render(data);
      if (!quiet) say(plural(n, s.q ? "result" : "message")); // what a filter, a view, a week or a search gave
    }
    main.setAttribute("aria-busy", "false");
    root.dataset.ready = "1";
  }

  function reloadSoon(ms) {
    window.clearTimeout(soonT);
    soonT = window.setTimeout(() => {
      soonT = null;
      if (pending.size || menu) { reloadSoon(ms); return; } // never pull an item out from under a click
      load({ quiet: true });
    }, ms);
  }

  async function refresh() { // new messages arrive all day: look again, quietly, when the counts move
    if (dead || menu || pending.size) return;
    let weeks, ppl;
    try { [weeks, ppl] = await Promise.all([api("/api/weeks"), api("/api/people")]); } catch (e) { return; }
    if (dead) return;
    const before = weeksSig;
    setWeeks(weeks);
    if (weeksSig === before) return;
    if (!s.week) { boot(); return; } // the first messages have arrived
    setPeople(ppl);
    buildPeople();
    buildSide();
    if (!s.q) load({ quiet: true });
  }

  // ---- the controls: built once (the people and weeks again only when they change); using one changes only its
  // pressed or current state, so the focus stays on it
  const chips = new Map(); // user_id ("" for Everyone) → chip
  const tabs = new Map(); // kind ("" for All) → tab
  const viewBtns = new Map(); // view → button
  const weekBtns = new Map(); // week → button in the list
  let cells = []; // the activity grid: [{node, week}]
  let peopleSig = null, sideSig = null;

  const focusedIn = (map) => { for (const [k, b] of map) if (b === document.activeElement) return k; return undefined; };

  function buildPeople() {
    const sig = JSON.stringify([...people.values()].map((p) => [p.user_id, p.name, p.color]));
    if (sig !== peopleSig) {
      peopleSig = sig;
      const had = focusedIn(chips);
      chips.clear();
      const all = el("button", { class: "pchip all", attrs: { type: "button" } }, icon("users"),
        el("span", { class: "pname", text: "Everyone" }));
      all.addEventListener("click", () => setPerson(""));
      chips.set("", all);
      for (const p of people.values()) {
        const b = el("button", { class: ["pchip", pc(p.color)].join(" "), attrs: { type: "button" } },
          avatar(p.name, p.color), el("bdi", { class: "pname", text: p.name || p.user_id }));
        b.addEventListener("click", () => setPerson(s.person === p.user_id ? "" : p.user_id)); // again: clears
        chips.set(p.user_id, b);
      }
      clear(peopleBox, ...chips.values());
      if (had !== undefined) (chips.get(had) || all).focus({ preventScroll: true }); // a refresh never scrolls the page
    }
    for (const [id, b] of chips) b.setAttribute("aria-pressed", String(id === s.person));
  }

  function setPerson(id) {
    if (id === s.person) return;
    s.person = id;
    buildPeople();
    load();
  }

  function buildKinds() {
    if (!tabs.size) {
      for (const [k, label, ico] of [["", "All", "layers"], ...KINDS.map((k) => [k, KIND[k].tab, KIND[k].icon])]) {
        const b = el("button", { class: ["ktab", k ? "k-" + k : ""].filter(Boolean).join(" "), data: { kind: k },
          attrs: { type: "button" } }, icon(ico), el("span", { text: label }));
        b.addEventListener("click", () => {
          if (s.kind === k) return;
          s.kind = k;
          buildKinds();
          load();
        });
        tabs.set(k, b);
      }
      clear(kindsBox, ...tabs.values());
    }
    for (const [k, b] of tabs) b.setAttribute("aria-pressed", String(k === s.kind));
  }

  function buildViews() {
    if (!viewBtns.size) {
      for (const [v, label] of VIEWS) {
        const b = el("button", { class: "vbtn", text: label, data: { view: v }, attrs: { type: "button" } });
        b.addEventListener("click", () => {
          if (s.view === v) return;
          s.view = v;
          buildViews();
          load();
        });
        viewBtns.set(v, b);
      }
      clear(viewsBox, ...viewBtns.values());
    }
    for (const [v, b] of viewBtns) b.setAttribute("aria-pressed", String(v === s.view));
  }

  function buildSide() {
    if (weeksSig !== sideSig) {
      sideSig = weeksSig;
      const had = focusedIn(weekBtns);
      weekBtns.clear();
      cells = [];
      const list = el("div", { class: "weeks" }, [...s.weeks].reverse().map((w) => {
        const b = el("button", { class: ["wk", w.total ? "" : "wk-none"].filter(Boolean).join(" "), data: { week: w.week },
          attrs: { type: "button" } },
        el("span", { class: "wk-l", text: weekName(w.week) }), el("span", { class: "wk-n num", text: String(w.total) }));
        b.addEventListener("click", () => pickWeek(w.week));
        weekBtns.set(w.week, b);
        return b;
      }));
      // the activity grid: the latest weeks, oldest at the top, Monday to Sunday; a shortcut for the mouse (the week
      // list above is the control for everyone)
      const shown = s.weeks.slice(-HEAT_WEEKS);
      const max = Math.max(1, ...shown.flatMap((w) => w.days));
      const today = isoOf(new Date());
      const heat = el("div", { class: "heat", attrs: { "aria-hidden": "true" } },
        ["M", "T", "W", "T", "F", "S", "S"].map((l) => el("span", { class: "heat-d", text: l })),
        shown.flatMap((w) => w.days.map((n, i) => {
          const day = isoOf(addDays(isoDay(w.week), i));
          const future = day > today;
          const lvl = n ? Math.min(4, Math.ceil((n / max) * 4)) : 0;
          const c = el("button", { class: ["cell", "l" + lvl, future ? "future" : ""].filter(Boolean).join(" "),
            attrs: { type: "button", tabindex: "-1", disabled: future || null,
              title: `${shortDay(day)} · ${plural(n, "message")}` } });
          c.addEventListener("click", () => pickWeek(w.week, day));
          cells.push({ node: c, week: w.week });
          return c;
        })));
      const legend = el("div", { class: "heat-legend", attrs: { "aria-hidden": "true" } }, el("span", { text: "Less" }),
        [0, 1, 2, 3, 4].map((l) => el("span", { class: "cell l" + l })), el("span", { text: "More" }));
      clear(side, el("h3", { class: "side-h", text: "Weeks" }), list,
        el("div", { class: "heat-box" }, el("h3", { class: "side-h", text: "Activity" }), heat, legend));
      if (had !== undefined && weekBtns.has(had)) weekBtns.get(had).focus({ preventScroll: true });
    }
    syncSide();
  }

  function syncSide() {
    const cur = s.q ? "" : s.week; // results cover every week: no week is current meanwhile
    for (const [w, b] of weekBtns) {
      if (w === cur) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current");
    }
    for (const c of cells) c.node.classList.toggle("sel", c.week === cur);
    // the phone's strip of weeks: bring the current one into view (the strip only, never the page)
    const list = side.querySelector(".weeks");
    const on = weekBtns.get(cur);
    if (on && list && list.scrollWidth > list.clientWidth) list.scrollLeft = Math.max(0, on.offsetLeft - 8);
  }

  function pickWeek(week, day) {
    const searching = !!s.q;
    if (searching) { // a week picked from the list ends the search
      s.q = "";
      input.value = "";
      paintSearch();
    }
    if (week === s.week && !searching) { if (day) toDay(day); return; }
    s.week = week;
    syncSide();
    load().then(() => { if (day) toDay(day); });
  }

  function toDay(day) {
    const card = main.querySelector(`.hday[data-day="${CSS.escape(day)}"]`);
    const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (card) card.scrollIntoView({ block: "start", behavior: calm ? "auto" : "smooth" });
  }

  // ---- search: 250 ms after the last keystroke; person: and kind: words go to the server as they are
  function paintSearch() { xBtn.hidden = !input.value; }

  function runSearch() {
    window.clearTimeout(searchT);
    searchT = null;
    const q = input.value.trim().slice(0, Q_MAX);
    if (q === s.q) return;
    s.q = q;
    syncSide();
    load();
  }

  function clearSearch() {
    window.clearTimeout(searchT);
    searchT = null;
    input.value = "";
    paintSearch();
    input.focus();
    if (!s.q) return;
    s.q = "";
    syncSide();
    load();
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

  // ---- rendering
  /** Paint the answer and return how many messages it holds. The focus, if it was in the content, goes back to the
   * same item's same control in the new nodes (opening "Other messages" if the item moved there). */
  function render(data) {
    const active = document.activeElement;
    let fk = null;
    if (active === main) fk = "";
    else if (active && main.contains(active)) {
      fk = keyOf.get(active) ?? (menu && menu.node.contains(active) ? keyOf.get(menu.btn) : undefined) ?? "";
    }
    closeMenu(false);
    byKey = new Map();
    const enter = s.first;
    s.first = false;
    const groups = arr(data && data.groups);
    const kind = data && (data.view === "person" || data.view === "topic") ? data.view : "day";
    const n = countIn(groups, kind);
    const nodes = [s.q ? resultsHead(n) : weekHead(groups, kind, n)];
    if (!groups.length) nodes.push(emptyLine());
    else if (kind === "day") nodes.push(...groups.map(dayCard));
    else {
      if (kind === "topic" && groups.every((g) => !g.topic)) {
        nodes.push(el("p", { class: "hint" }, icon("hash"), el("span", {},
          "Topics come from the nightly enrichment (", el("code", { text: "haseeb-agent history-enrich" }), ").")));
      }
      nodes.push(el("div", { class: "lanes" }, groups.map((g) => lane(g, kind))));
    }
    clear(main, ...nodes);
    if (enter) { // the entrance plays on the first paint only: a filter, a week or a refresh never replays it
      [...main.querySelectorAll(".hday, .lane, .results, .week-h, .week-empty")].slice(0, 8).forEach((c, i) => {
        c.classList.add("rise");
        setVars(c, { "--d": `${i * 70}ms` });
      });
    }
    if (fk !== null) refocus(fk);
    return n;
  }

  function refocus(fk) {
    const moved = fk !== "" && fk === reveal;
    reveal = null;
    const node = fk ? byKey.get(fk) : null;
    if (!node) { main.focus({ preventScroll: true }); return; } // gone: the content itself, never <body>
    const box = node.closest("details");
    if (box && !box.open && node.tagName !== "SUMMARY") box.open = true;
    node.focus({ preventScroll: true }); // a refresh never scrolls the page: he stays where he was reading
    if (moved) node.scrollIntoView({ block: "nearest" }); // ... but a re-labelled item went elsewhere: follow it
  }

  function weekHead(groups, kind, n) {
    const days = kind === "day" ? ` · ${plural(groups.length, "day")}` : "";
    const name = weekName(s.week);
    return el("div", { class: "week-h" },
      el("h2", {}, icon("cal"), el("span", { text: name === span(s.week) ? name : `${name} · ${span(s.week)}` })),
      el("p", { class: "num", text: plural(n, "message") + days }),
      filterNote());
  }

  function filterNote() {
    if (!s.person && !s.kind) return null;
    const p = people.get(s.person);
    const all = reg(el("button", { class: "linkish", text: "Show everything", attrs: { type: "button" } }), "showall");
    all.addEventListener("click", () => { s.person = ""; s.kind = ""; buildPeople(); buildKinds(); load(); });
    return el("div", { class: "filter-note" }, el("span", {}, "Only ",
      s.person ? el("bdi", { class: "who-f", text: p ? p.name : s.person }) : null,
      s.person && s.kind ? " · " : "", s.kind ? kindOf(s.kind).tab : ""), all);
  }

  function resultsHead(n) {
    const clr = reg(el("button", { class: "btn ghost clear", attrs: { type: "button" } }, icon("x"),
      el("span", { text: "Clear search" })), "clear");
    clr.addEventListener("click", clearSearch);
    return el("section", { class: "card results", attrs: { "aria-label": "Search results" } },
      el("div", { class: "results-t" },
        el("h2", {}, "Results for “", el("bdi", { text: s.q }), "”"),
        el("p", { class: "num", text: `${plural(n, "message")}, newest first${n >= 500 ? " (the newest 500)" : ""}` },
          filterNote())),
      clr);
  }

  function emptyLine() {
    const text = s.q ? "Nothing matches that search." : s.person || s.kind ? "Nothing here with this filter this week."
      : "Nothing in this week.";
    return el("div", { class: "card week-empty", attrs: { role: "status" } }, icon(s.q ? "search" : "sunrise"),
      el("span", { text }));
  }

  /** A long text shows its first lines and a Show more toggle. */
  function clampable(node, text, lines, key) {
    if (!text || (text.length <= lines * 80 && text.split("\n").length <= lines)) return [node];
    node.classList.add("clamp", "c" + lines);
    const more = reg(el("button", { class: "linkish show-more", text: "Show more", attrs: { type: "button", "aria-expanded": "false" } }),
      key + "|more");
    more.addEventListener("click", () => {
      const open = node.classList.toggle("open");
      more.textContent = open ? "Show less" : "Show more";
      more.setAttribute("aria-expanded", String(open));
    });
    return [node, more];
  }

  // ---- by day: the huddle, then the insights with their feedback, then the rest
  function dayCard(g) {
    const d = isoDay(g.day);
    const h = g.huddle || {};
    const notes = arr(h.messages);
    const todos = arr(h.todos);
    const insights = arr(g.insights).filter((x) => x && x.msg);
    const other = arr(g.other);
    const loose = other.filter((m) => m.kind === "feedback"); // replies whose insight isn't in this day or filter
    const rest = other.filter((m) => m.kind !== "feedback");
    const nFb = insights.reduce((k, x) => k + arr(x.feedback).length, 0) + loose.length;
    const counts = [["huddle_notes", notes.length], ["todo", todos.length], ["insight", insights.length],
      ["feedback", nFb], ["other", rest.length]].filter(([, k]) => k > 0);
    const sum = counts.map(([k, c]) => plural(c, KIND[k].words[0], KIND[k].words[1])).join(" · ");
    const body = el("div", { class: "hday-b" });
    if (notes.length || todos.length) {
      const notesBox = notes.length ? el("div", { class: "box notes" }, notes.map(noteItem)) : null;
      const todoBox = todos.length ? el("div", { class: "box todos" },
        el("div", { class: "box-h" }, icon("todo", "k-todo"), el("b", { text: "Next Todos" }),
          el("span", { text: `· ${todos.length} posted` })),
        el("div", { class: "todo-list" }, todos.map(todoRow))) : null;
      body.appendChild(sec("huddle", "Huddle", "huddle_notes",
        el("div", { class: notesBox && todoBox ? "huddle" : "huddle one" }, notesBox, todoBox)));
    }
    if (insights.length) {
      body.appendChild(sec("insights", "Insights · with their feedback", "insight",
        el("div", { class: "ins-grid" }, insights.map(insightCard))));
    }
    if (loose.length) {
      body.appendChild(sec("feedback", "Feedback · not under a shown insight", "feedback",
        el("div", { class: "rows" }, loose.map((m) => mrow(m)))));
    }
    if (rest.length) body.appendChild(otherBlock(g.day, rest));
    return el("section", { class: "card hday", data: { day: g.day }, attrs: { "aria-label": d ? longDay(d) : "A day" } },
      el("header", { class: "hday-h" },
        d ? el("div", { class: "date-tile", attrs: { "aria-hidden": "true" } },
          el("small", { text: DAY_NAMES[d.getDay()].slice(0, 3) }), el("b", { class: "num", text: String(d.getDate()) }),
          el("small", { text: MONTH_NAMES[d.getMonth()].slice(0, 3) })) : null,
        el("div", { class: "hday-t" }, el("h2", { text: d ? longDay(d) : "Undated" }), el("p", { text: sum || "Nothing" })),
        el("div", { class: "counts", attrs: { "aria-hidden": "true" } },
          counts.map(([k, c]) => el("span", { class: "cnt k-" + k }, icon(KIND[k].icon), el("span", { class: "num", text: String(c) }))))),
      body);
  }

  function sec(name, label, kind, ...children) {
    return el("div", { class: "sec sec-" + name, data: { sec: name } },
      el("h3", { class: "sec-h k-" + kind }, icon(KIND[kind].icon), el("span", { text: label })), ...children);
  }

  function noteItem(m) {
    const { title: head1, body } = split(plain(m.text, people), null);
    const host = el("div", { class: "note-item item" });
    clear(host,
      el("div", { class: "box-h" }, avatar(m.user_name, m.color, "sm"), who(m),
        el("span", { class: "num", text: timeOf(m) })),
      head1 ? el("div", { class: "note-t" }, el("bdi", { text: head1 })) : null,
      body ? clampable(txt(body), body, 8, itemKey(m)) : null,
      tools(m, host));
    return host;
  }

  function todoRow(m) {
    const t = todoText(plain(m.text, people));
    const row = el("div", { class: ["todo-row", "item", m.mine ? "mine" : ""].filter(Boolean).join(" ") });
    clear(row, avatar(m.user_name, m.color),
      el("div", { class: "row-main" },
        el("div", { class: "row-top" }, who(m), m.mine ? you() : null, el("span", { class: "num", text: timeOf(m) })),
        clampable(txt(t || "(empty)"), t, 3, itemKey(m))),
      tools(m, row));
    return row;
  }

  function insightCard(x) {
    const m = x.msg;
    const { title: head1, body } = split(plain(m.text, people), INSIGHT_PREFIX);
    const fb = arr(x.feedback);
    // under a filter or a search, replies may be filtered out: never claim there are none
    const filtered = !!(s.kind || s.person || s.q);
    const replies = fb.length ? plural(fb.length, "reply", "replies") + (filtered ? " shown" : "")
      : filtered ? "No replies shown" : "No feedback yet";
    const card = el("article", { class: ["ins", "item", m.mine ? "mine" : ""].filter(Boolean).join(" ") });
    clear(card,
      el("div", { class: "ins-top" }, avatar(m.user_name, m.color),
        el("div", { class: "ins-by" }, who(m), el("span", { class: "num", text: timeOf(m) }))),
      el("h4", { class: "ins-t" }, el("bdi", { text: head1 || "Insight" })),
      body ? clampable(txt(body, "ins-body"), body, 4, itemKey(m)) : null,
      tools(m, card), // the card's own tools before its replies' (reading and Tab order)
      fb.length ? el("div", { class: "fb", attrs: { role: "list", "aria-label": "Feedback" } }, fb.map(fbRow)) : null,
      el("div", { class: "ins-foot" },
        el("span", { class: "meta" }, icon("chat"), replies),
        m.mine ? el("span", { class: "meta you-meta", text: "Yours" }) : null));
    return card;
  }

  function fbRow(m) {
    const t = plain(m.text, people);
    const row = el("div", { class: ["fb-row", "item", m.mine ? "mine" : ""].filter(Boolean).join(" "), attrs: { role: "listitem" } });
    clear(row, avatar(m.user_name, m.color, "sm"),
      el("div", { class: "row-main" },
        el("div", { class: "row-top" }, who(m), m.mine ? you() : null, el("span", { class: "num", text: timeOf(m) })),
        clampable(txt(t), t, 3, itemKey(m))),
      tools(m, row));
    return row;
  }

  function otherBlock(day, list) {
    const summary = reg(el("summary", { class: "sec-h k-other" }, icon("chev", "chev"),
      el("span", { text: `Other messages (${list.length})` })), "other|" + day);
    const d = el("details", { class: "sec sec-other", data: { sec: "other" } }, summary,
      el("div", { class: "rows" }, list.map((m) => mrow(m))));
    const byTab = s.kind === "other"; // the Other tab opens them all: that isn't his choice, so it isn't remembered
    d.open = openOther.has(day) || byTab;
    d.addEventListener("toggle", () => {
      if (byTab) return;
      if (d.open) openOther.add(day); else openOther.delete(day);
    });
    return d;
  }

  /** A message's words by its kind: an insight or notes have a title line, a todo loses its "Next todo:". */
  function words(m) {
    const t = plain(m.text, people);
    const k = kindKey(m.kind);
    if (k === "insight") return split(t, INSIGHT_PREFIX);
    if (k === "huddle_notes") return split(t, null);
    return { title: "", body: k === "todo" ? todoText(t) : t };
  }

  /** One message as a row: in Feedback and Other, and in the lanes (with its day; a person's lane leaves out who). */
  function mrow(m, { dated = false, tag = "div", author = true } = {}) {
    const k = kindKey(m.kind);
    const { title: head1, body } = words(m);
    const row = el(tag, { class: ["mrow", "item", author ? "" : "solo", author && m.mine ? "mine" : ""].filter(Boolean).join(" ") });
    clear(row, author ? avatar(m.user_name, m.color, "sm") : null,
      el("div", { class: "row-main" },
        el("div", { class: "row-top" }, author ? who(m) : null, author && m.mine ? you() : null,
          el("span", { class: "kchip k-" + k }, icon(KIND[k].icon), el("span", { text: KIND[k].label })),
          el("span", { class: "num", text: [dated ? shortDay(m.day) : "", timeOf(m)].filter(Boolean).join(" · ") })),
        head1 ? el("div", { class: "mrow-t" }, el("bdi", { text: head1 })) : null,
        body ? clampable(txt(body), body, 3, itemKey(m)) : null),
      tools(m, row));
    return row;
  }

  // ---- by person, by topic: one lane each
  function lane(g, kind) {
    const list = arr(g.messages).slice().sort(byNewest);
    const isPerson = kind === "person";
    const p = g.person || {};
    const byKind = KINDS.map((k) => [k, list.filter((m) => kindKey(m.kind) === k).length]).filter(([, c]) => c);
    const sub = byKind.map(([k, c]) => plural(c, KIND[k].words[0], KIND[k].words[1])).join(" · ");
    const name = isPerson ? el("bdi", { text: p.name || "Someone" })
      : g.topic ? el("bdi", { text: g.topic }) : el("span", { class: "muted", text: "No topic yet" });
    const row = (m) => mrow(m, { dated: true, tag: "li", author: !isPerson });
    const ol = el("ol", { class: "lane-list" }, list.slice(0, LANE_SHOW).map(row));
    let more = null;
    if (list.length > LANE_SHOW) {
      more = reg(el("button", { class: "linkish lane-more", text: `Show ${list.length - LANE_SHOW} more`, attrs: { type: "button" } }),
        "lanemore|" + kind + "|" + (isPerson ? p.user_id : g.topic));
      more.addEventListener("click", () => {
        const added = list.slice(LANE_SHOW).map(row);
        ol.append(...added);
        more.remove();
        added[0].querySelector(".lbl").focus(); // the button is gone: the focus goes on to the first row it revealed
      });
    }
    return el("section", { class: ["card", "lane", isPerson && list.some((m) => m.mine) ? "mine" : ""].filter(Boolean).join(" ") },
      el("header", { class: "lane-h" },
        isPerson ? avatar(p.name, p.color, "lg") : el("span", { class: "topic-ico", attrs: { "aria-hidden": "true" } }, icon("hash")),
        el("div", { class: "lane-t" }, el("h2", { class: "lane-n" }, name), el("p", { class: "lane-s", text: sub })),
        el("span", { class: "lane-c num", text: String(list.length) })),
      ol, more);
  }

  // ---- each item's tools: its label (a menu) and its Slack message. Their accessible names say whose message and
  // what kind, as hidden text inside them (never an attribute: the name is Slack text).
  function tools(m, host) {
    const key = itemKey(m);
    const by = () => el("bdi", { text: m.user_name || "Someone" });
    const lbl = reg(el("button", { class: "lbl", attrs: { type: "button", "aria-haspopup": "menu", "aria-expanded": "false" } },
      icon("tag"), el("span", { class: "sr-only", text: "Change the label (now " }), el("span", { class: "lbl-t" }),
      el("span", { class: "sr-only" }, ") of the message by ", by())), key + "|lbl");
    paintLabel(lbl, m.kind, pending.has(key));
    lbl.addEventListener("click", (e) => {
      e.stopPropagation();
      if (menu && menu.btn === lbl) closeMenu(true); else openMenu(lbl, m, host);
    });
    const href = cleanUrl(m.permalink);
    const link = href ? reg(el("a", { class: "slack-link", attrs: { href, target: "_blank", rel: "noopener noreferrer" } },
      "Open in Slack", el("span", { class: "arr", text: " ↗", attrs: { "aria-hidden": "true" } }),
      el("span", { class: "sr-only" }, ` — ${kindOf(m.kind).label} by `, by())), key + "|slack") : null;
    // a teammate's top-level post can get a feedback draft: the server says so (`feedback` is not null)
    const draft = feedbackControl(m, { onChange: () => reloadSoon(REGROUP_MS), compact: true });
    if (draft) reg(draft.button, key + "|fbk");
    return el("div", { class: "tools", attrs: { role: "group", "aria-label": "Message tools" } },
      draft ? draft.node : null, lbl, link);
  }

  function paintLabel(btn, kind, busy) {
    btn.querySelector(".lbl-t").textContent = kindOf(kind).label;
    btn.classList.toggle("pending", busy);
    btn.closest(".tools")?.classList.toggle("keep", busy);
  }

  function openMenu(btn, m, host) {
    closeMenu(false);
    const items = KINDS.map((k) => {
      const on = k === kindKey(m.kind);
      const it = el("button", { class: "menu-it", data: { kind: k },
        attrs: { type: "button", role: "menuitemradio", "aria-checked": String(on), tabindex: "-1" } },
      icon(KIND[k].icon, "k-" + k), el("span", { text: KIND[k].label }), on ? icon("check", "menu-on") : null);
      it.addEventListener("click", (e) => {
        e.stopPropagation();
        closeMenu(true);
        relabel(m, k, btn);
      });
      return it;
    });
    const node = el("div", { class: "menu", attrs: { role: "menu", "aria-label": "Label as" } },
      el("div", { class: "menu-h", text: "Label as", attrs: { "aria-hidden": "true" } }), items);
    node.addEventListener("keydown", (e) => {
      const i = items.indexOf(document.activeElement);
      const go = (j) => { e.preventDefault(); items[(j + items.length) % items.length].focus(); };
      if (e.key === "Escape") { e.preventDefault(); closeMenu(true); } else if (e.key === "ArrowDown") go(i + 1);
      else if (e.key === "ArrowUp") go(i < 0 ? -1 : i - 1);
      else if (e.key === "Home") go(0);
      else if (e.key === "End") go(-1);
      else if (e.key === "Tab") closeMenu(true); // back on its button, from where Tab carries on as usual
    });
    btn.parentNode.appendChild(node);
    const r = btn.getBoundingClientRect();
    if (window.innerHeight - r.bottom < 260 && r.top > 260) node.classList.add("up"); // no room below: open upward
    btn.setAttribute("aria-expanded", "true");
    const lifted = []; // the item and every item or card around it, above what follows them while the menu is open
    for (let n = host; n && n !== main; n = n.parentElement) if (n.matches(".item, .card")) lifted.push(n);
    lifted.forEach((n) => n.classList.add("menu-open"));
    const outside = (e) => { if (!node.contains(e.target) && !btn.contains(e.target)) closeMenu(false); };
    document.addEventListener("pointerdown", outside, true);
    menu = { node, btn, off: () => { document.removeEventListener("pointerdown", outside, true); lifted.forEach((n) => n.classList.remove("menu-open")); } };
    (items.find((it) => it.getAttribute("aria-checked") === "true") || items[0]).focus();
  }

  function closeMenu(refocus) {
    if (!menu) return;
    const { node, btn, off } = menu;
    menu = null;
    off();
    node.remove();
    btn.setAttribute("aria-expanded", "false");
    if (refocus && btn.isConnected) btn.focus();
  }

  async function relabel(m, kind, btn) {
    const key = itemKey(m);
    if (kind === kindKey(m.kind) || pending.has(key)) return;
    const was = m.kind;
    pending.add(key);
    btn.closest(".tools")?.querySelector(".tool-err")?.remove();
    paintLabel(btn, kind, true); // optimistic: the new label shows at once
    let failed = null;
    try {
      await api("/api/label", { method: "POST", body: { channel: m.channel, ts: m.ts, kind } });
    } catch (e) {
      failed = e;
    }
    pending.delete(key);
    if (dead) return;
    if (failed) { // rolled back, with a short note (the server's own fixed words)
      paintLabel(btn, was, false);
      note(btn, "Couldn't change the label" + (failed.message ? ` · ${failed.message}` : ""));
      say("Couldn't change the label.");
      return;
    }
    m.kind = kind;
    paintLabel(btn, kind, false);
    btn.classList.add("saved");
    say(`Labelled as ${KIND[kind].label}.`);
    reveal = key + "|lbl";
    reloadSoon(REGROUP_MS);
  }

  function note(btn, text) {
    const box = btn.closest(".tools");
    if (!box) return;
    const n = el("span", { class: "tool-err", attrs: { role: "alert" } }, icon("warn"), el("span", { text }));
    box.appendChild(n);
    box.classList.add("keep");
    window.setTimeout(() => { n.remove(); if (!box.querySelector(".tool-err, .pending")) box.classList.remove("keep"); }, NOTE_MS);
  }

  timers.push(window.setInterval(refresh, REFRESH_MS));
  // a draft that was running has ended: its post has a feedback item now, so the view looks again
  let wasDrafting = false;
  const offJobs = onJobs(() => {
    const running = !!(jobInfo("feedback") || {}).running;
    if (wasDrafting && !running) reloadSoon(REGROUP_MS);
    wasDrafting = running;
  });
  boot();
  return () => {
    dead = true;
    offJobs();
    closeMenu(false);
    timers.forEach((t) => window.clearInterval(t));
    [searchT, soonT, retryT].forEach((t) => window.clearTimeout(t));
    document.removeEventListener("keydown", onSlash);
  };
}
