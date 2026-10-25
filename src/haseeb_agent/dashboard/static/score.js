// The Score page (Task 10): a page view of the weekly metrics (metrics.py, through /api/score). Four stat tiles, the
// weekly score (Lion's rule: +5 for each workday with a posted insight, −10 for each without one), % human
// intervention, Next Todo within 5 min of the huddle notes (D16) and failed job runs, each with its change since last
// week and an axis-free 8-week
// sparkline (inline SVG, built with createElementNS: a 2px line, a soft wash, a dot on the latest week, and a
// crosshair readout on hover). The same numbers follow in a compact table, week by week: it is also the sparklines'
// accessible equivalent, and each sparkline's label reads its numbers out. Weeks before the agent's first activity
// are left out (an empty week there means "not running yet", never a −50).
import { api, on } from "./client.js";
import { clear, el, svg } from "./dom.js";

const WEEKS = 8;
const REFRESH_MS = 60000;
const RETRY_MS = 5000;
const SOON_MS = 2000; // a command settled: a post or a skip may move the score
const VW = 240, VH = 56, PX = 6, PY = 7; // the sparkline's drawing box: it stretches to the tile, its strokes don't
const MINUS = "−";
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const int = (x) => (Number.isInteger(x) && x >= 0 ? x : 0);
const rate = (x) => (typeof x === "number" && Number.isFinite(x) ? x : null);
const signed = (n) => (n > 0 ? `+${n}` : n < 0 ? `${MINUS}${-n}` : "0");
const pct = (n) => (n === null ? "—" : `${n}%`);
const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;
const pad = (n) => String(n).padStart(2, "0");

function icon(name) { return el("span", { class: "ico i-" + name, attrs: { "aria-hidden": "true" } }); }

function isoDay(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || ""));
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
}
const isoOf = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
function addDays(d, n) { const x = new Date(d.getTime()); x.setDate(x.getDate() + n); return x; }
function mondayOf(d) { return addDays(new Date(d.getFullYear(), d.getMonth(), d.getDate()), -((d.getDay() + 6) % 7)); }

function span(iso) { // "12–18 Oct", "28 Sep – 4 Oct"
  const a = isoDay(iso);
  if (!a) return "";
  const b = addDays(a, 6);
  const mon = (d) => MONTHS[d.getMonth()];
  return a.getMonth() === b.getMonth() ? `${a.getDate()}–${b.getDate()} ${mon(a)}` : `${a.getDate()} ${mon(a)} – ${b.getDate()} ${mon(b)}`;
}

function weekName(iso) {
  const cur = mondayOf(new Date());
  if (iso === isoOf(cur)) return "This week";
  if (iso === isoOf(addDays(cur, -7))) return "Last week";
  return span(iso);
}

/** One week of /api/score, checked: counts are whole numbers, a rate is a number or null. */
function week(w) {
  return {
    week: typeof w.week_start === "string" ? w.week_start : "",
    posted: int(w.posted), edited: int(w.edited_then_posted), skipped: int(w.skipped), ignored: int(w.ignored),
    auto: int(w.auto_posted), self: int(w.self_posted), days: int(w.insight_days), missed: int(w.missed_days),
    workdays: int(w.workdays_so_far), failed: int(w.failed_runs), rate: rate(w.intervention_rate),
    onTime: int(w.todo_on_time), due: int(w.todo_due), by1605: int(w.todo_by_1605), pending: w.pending_today === true,
  };
}

const active = (w) => w.posted + w.skipped + w.ignored + w.auto + w.self + w.failed + w.onTime > 0;
// M4: today, while its insight isn't out, is pending: neither posted nor missed until the day ends
const settled = (w) => Math.max(0, w.workdays - (w.pending ? 1 : 0));
const missedNow = (w) => Math.max(0, w.missed - (w.pending ? 1 : 0));
const scoreOf = (w) => 5 * w.days - 10 * missedNow(w);

/** D16: on time is within 5 minutes of the huddle notes; "by 16:05" when no day shown had the notes' time. */
function todoLabel(weeks) {
  const due = weeks.reduce((n, w) => n + w.due, 0), fallback = weeks.reduce((n, w) => n + w.by1605, 0);
  return due > 0 && fallback === due ? "Next Todo by 16:05" : "Next Todo within 5 min";
}

function scoreSub(w) {
  const n = settled(w);
  const parts = n ? [`${w.days} of ${plural(n, "workday")} posted` + (missedNow(w) ? ` · ${missedNow(w)} missed` : "")] : [];
  if (w.pending) parts.push("Today pending");
  return parts.join(" · ");
}

const TILES = [
  { k: "score", label: "Weekly score", icon: "trophy", better: "up", fmt: signed, count: true, per: settled,
    value: scoreOf, sub: scoreSub, step: (d) => String(Math.abs(d)) },
  { k: "intervention", label: "Human intervention", icon: "pen", better: "down", fmt: pct,
    value: (w) => (w.rate === null ? null : Math.round(w.rate * 100)),
    sub: (w) => (w.posted ? `${w.edited} of ${plural(w.posted, "post")} edited first` : "Nothing posted yet"),
    step: (d) => `${Math.abs(d)} pts` },
  { k: "todo", label: todoLabel, icon: "todo", better: "up", fmt: pct,
    value: (w) => (w.due ? Math.round((w.onTime / w.due) * 100) : null),
    sub: (w, weeks) => (!w.due ? "None due yet" : `${w.onTime} of ${plural(w.due, "workday")}`
      + (w.by1605 && todoLabel(weeks) !== "Next Todo by 16:05" ? ` · ${w.by1605} by 16:05` : "")),
    step: (d) => `${Math.abs(d)} pts` },
  { k: "failed", label: "Failed job runs", icon: "warn", better: "down", fmt: String, count: true, per: (w) => w.workdays,
    value: (w) => w.failed,
    sub: (w) => (w.failed ? "Runs that crashed or gave up" : "Every run finished"),
    step: (d) => String(Math.abs(d)) },
];

export function mountScore(view) {
  const timers = [];
  const offs = [];
  let dead = false, loaded = false, sig = "", soonT = null, retryT = null;
  const root = el("div", { class: "score" });
  view.appendChild(root);
  clear(root, header(null), el("div", { class: "skeleton", attrs: { "aria-hidden": "true" } }, el("i"), el("i")));

  function header(cur) {
    return el("header", { class: "pg-head" },
      el("div", { class: "pg-title" }, el("h1", { text: "Score" }),
        el("p", { text: "Your weekly score, human intervention and Next Todo streak, week by week." })),
      el("div", { class: "pg-meta" },
        cur ? el("span", { class: "tally" }, icon("cal"), el("b", { text: "This week" }), " · " + span(cur.week)) : null,
        el("span", { class: "tally" }, icon("trophy"), el("b", { class: "num", text: "+5" }), " a posted day · ",
          el("b", { class: "num", text: `${MINUS}10` }), " a miss")));
  }

  function loadSoon(ms) {
    if (soonT) return;
    soonT = window.setTimeout(() => { soonT = null; load(); }, ms);
  }

  async function load() {
    window.clearTimeout(retryT);
    retryT = null;
    let data;
    try { data = await api(`/api/score?weeks=${WEEKS}`); } catch (e) {
      if (dead) return;
      if (!loaded) {
        clear(root, header(null), el("div", { class: "load-error", attrs: { role: "status" } }, icon("warn"),
          el("span", { text: "Couldn't load your score. Trying again…" })));
        root.dataset.ready = "1";
      }
      retryT = window.setTimeout(load, RETRY_MS);
      return;
    }
    if (dead || !Array.isArray(data)) return;
    const s = JSON.stringify(data);
    if (s === sig) return; // nothing moved: no repaint, no blink
    sig = s;
    const first = !loaded;
    loaded = true;
    const weeks = data.filter((w) => w && typeof w === "object").map(week).filter((w) => isoDay(w.week));
    const from = weeks.findIndex(active);
    if (from < 0) paintEmpty(); else paint(weeks.slice(from), first);
    root.dataset.ready = "1";
  }

  function paintEmpty() {
    clear(root, header(null), el("section", { class: "card score-empty rise" },
      el("div", { class: "pg-orb", attrs: { "aria-hidden": "true" } }, icon("trophy")),
      el("p", { class: "empty-t", text: "No score yet" }),
      el("p", { class: "empty-s", text: "Your score starts with the agent's first posted, skipped or missed insight: "
        + "+5 for each workday with a posted insight, −10 for each workday without one." })));
  }

  function paint(weeks, enter) {
    const cur = weeks[weeks.length - 1];
    const labels = weeks.map((w) => weekName(w.week));
    const stats = el("div", { class: "stats" }, TILES.map((tile, i) => statTile(tile, weeks, labels, enter ? i : -1)));
    clear(root, header(cur), stats, weekTable(weeks, labels, enter));
  }

  // ---------------------------------------------------------------- a stat tile

  function statTile(tile, weeks, labels, idx) {
    const label = typeof tile.label === "function" ? tile.label(weeks) : tile.label;
    const vals = weeks.map(tile.value);
    const n = vals.length;
    const now = vals[n - 1];
    const prev = n > 1 ? vals[n - 2] : undefined;
    const cap = el("div", { class: "spark-cap", attrs: { "aria-hidden": "true" } });
    const capRest = () => {
      cap.classList.remove("on");
      clear(cap, el("span", { text: labels[0] }), el("span", { text: n > 1 ? labels[n - 1] : "" }));
    };
    capRest();
    const node = el("section", {
      class: ["card", "stat", "k-" + tile.k, idx >= 0 ? "rise" : ""].filter(Boolean).join(" "),
      data: { k: tile.k }, attrs: { "aria-label": label }, vars: idx >= 0 ? { "--d": `${idx * 70}ms` } : {},
    },
    el("div", { class: "stat-h" }, el("span", { class: "stat-ico" }, icon(tile.icon)), el("span", { text: label })),
    el("div", { class: ["stat-v", now === null ? "none" : ""].filter(Boolean).join(" "), text: now === null ? "—" : tile.fmt(now) }),
    el("div", { class: "stat-row" }, delta(tile, weeks, now, prev)),
    el("div", { class: "stat-s", text: tile.sub(weeks[n - 1], weeks) }),
    sparkline(tile, label, vals, labels, cap, capRest),
    cap);
    return node;
  }

  /** The change since last week: an arrow and its size, green when it moved the right way, red when not. A count
   * (the score, failed runs) in a week still under way is compared by its pace, per workday so far (for the score,
   * per settled workday: a pending today isn't one yet): a Monday's +5 is on pace with a +25 week, not 20 behind. */
  function delta(tile, weeks, now, prev) {
    if (prev === undefined) return el("span", { class: "stat-d flat", text: "First week" });
    if (now === null || prev === null) return el("span", { class: "stat-d flat", text: "No comparison yet" });
    const [last, cur] = weeks.slice(-2); // oldest first: last week, then this one
    const a = tile.count ? tile.per(cur) : 0, b = tile.count ? tile.per(last) : 0;
    if (tile.count && a === 0) return el("span", { class: "stat-d flat", text: "No comparison yet" });
    const pace = tile.count && b > 0 && a < b;
    const d = pace ? Math.round((now / a - prev / b) * 10) / 10 : now - prev;
    if (d === 0) return el("span", { class: "stat-d flat", text: pace ? "On pace with last week" : "Same as last week" });
    const up = d > 0;
    const good = up === (tile.better === "up");
    return el("span", { class: `stat-d ${up ? "up" : "down"} ${good ? "good" : "bad"}` }, icon(up ? "up" : "down"),
      el("span", { class: "sr-only", text: up ? "Up " : "Down " }),
      pace ? `${Math.abs(d)} a day vs last week` : `${tile.step(d)} vs last week`);
  }

  function sparkline(tile, label, vals, labels, cap, capRest) {
    const n = vals.length;
    const nums = vals.filter((v) => v !== null);
    const lo = nums.length ? Math.min(...nums) : 0, hi = nums.length ? Math.max(...nums) : 0;
    const x = (i) => (n === 1 ? VW / 2 : PX + (i * (VW - 2 * PX)) / (n - 1));
    const y = (v) => (hi === lo ? VH / 2 : PY + ((hi - v) * (VH - 2 * PY)) / (hi - lo));
    const f = (v) => v.toFixed(1);
    // runs of weeks with a value: a week without one (nothing posted, say) breaks the line
    const runs = [];
    let run = [];
    vals.forEach((v, i) => {
      if (v === null) { if (run.length) runs.push(run); run = []; } else run.push([x(i), y(v)]);
    });
    if (run.length) runs.push(run);
    const line = runs.map((r) => r.map(([px, py], j) => `${j ? "L" : "M"}${f(px)} ${f(py)}`).join(" ")).join(" ");
    const area = runs.filter((r) => r.length > 1).map((r) => `M${f(r[0][0])} ${VH} `
      + r.map(([px, py]) => `L${f(px)} ${f(py)}`).join(" ") + ` L${f(r[r.length - 1][0])} ${VH} Z`).join(" ");
    const dotAt = (px, py, cls) => svg("path", { class: cls, d: `M${f(px)} ${f(py)} L${f(px)} ${f(py)}` }); // a round cap
    let last = -1;
    vals.forEach((v, i) => { if (v !== null) last = i; });
    const read = vals.map((v) => (v === null ? "no data" : tile.fmt(v))).join(", ");
    const hx = svg("line", { class: "spark-x", x1: "0", y1: "0", x2: "0", y2: String(VH) });
    const hov = svg("path", { class: "spark-hov", d: "M0 0 L0 0" });
    const node = svg("svg", {
      class: "spark", viewBox: `0 0 ${VW} ${VH}`, preserveAspectRatio: "none", role: "img", focusable: "false",
      "aria-label": `${label}, last ${plural(n, "week")}: ${read}.`,
    },
    area ? svg("path", { class: "spark-area", d: area }) : null,
    svg("path", { class: "spark-line", d: line || "M0 0" }),
    runs.filter((r) => r.length === 1).map((r) => dotAt(r[0][0], r[0][1], "spark-pt")), // a lone week: a small dot
    hx,
    last >= 0 ? [dotAt(x(last), y(vals[last]), "spark-ring"), dotAt(x(last), y(vals[last]), "spark-dot")] : null,
    hov);
    // the crosshair: the nearest week to the pointer, its line, its point and its value under the chart
    node.addEventListener("pointermove", (e) => {
      const r = node.getBoundingClientRect();
      if (!r.width) return;
      const at = ((e.clientX - r.left) / r.width) * VW;
      const i = n === 1 ? 0 : Math.max(0, Math.min(n - 1, Math.round(((at - PX) / (VW - 2 * PX)) * (n - 1))));
      const v = vals[i];
      hx.setAttribute("x1", f(x(i)));
      hx.setAttribute("x2", f(x(i)));
      hov.setAttribute("d", v === null ? "M0 0 L0 0" : `M${f(x(i))} ${f(y(v))} L${f(x(i))} ${f(y(v))}`);
      hov.classList.toggle("on", v !== null);
      node.classList.add("hover");
      cap.classList.add("on");
      clear(cap, el("span", { text: `${labels[i]} · ${v === null ? "no data" : tile.fmt(v)}` }));
    });
    node.addEventListener("pointerleave", () => { node.classList.remove("hover"); capRest(); });
    return node;
  }

  // ---------------------------------------------------------------- the table

  function weekTable(weeks, labels, enter) {
    const td = (text, cls) => el("td", { class: cls || null, text });
    const frac = (a, b) => el("td", {}, String(a), el("small", { text: `/ ${b}` }));
    const rows = weeks.map((w, i) => {
      const score = scoreOf(w);
      const iv = w.rate === null ? null : Math.round(w.rate * 100);
      return el("tr", { class: i === weeks.length - 1 ? "cur" : null },
        el("td", {}, labels[i], w.pending ? el("small", { text: "today pending" }) : null),
        td(signed(score), score > 0 ? "pos" : score < 0 ? "neg" : "nil"),
        frac(w.days, settled(w)),
        td(pct(iv), iv === null ? "nil" : null),
        w.due ? frac(w.onTime, w.due) : td("—", "nil"),
        td(String(w.failed), w.failed ? null : "nil"));
    }).reverse(); // newest first
    const th = (text) => el("th", { attrs: { scope: "col" }, text });
    return el("section", { class: ["card", "weeks", enter ? "rise" : ""].filter(Boolean).join(" "),
      attrs: { "aria-label": "Week by week" }, vars: enter ? { "--d": "280ms" } : {} },
    el("h2", { class: "card-h" }, el("span", { text: "Week by week" }),
      el("span", { class: "card-meta", text: plural(weeks.length, "week") })),
    el("div", { class: "tbl" }, el("table", { class: "wk-table" },
      el("caption", { class: "sr-only", text: "Your score, week by week, newest first" }),
      el("thead", {}, el("tr", {}, th("Week"), th("Score"), th("Insight days"), th("Human intervention"),
        th(todoLabel(weeks)), th("Failed runs"))),
      el("tbody", {}, rows))),
    el("p", { class: "rules" },
      el("span", {}, el("b", { text: "Score" }), ": +5 for each workday with a posted insight, −10 for each without one "
        + "(today counts once its insight is out, or the day ends)"),
      el("span", {}, el("b", { text: "Human intervention" }), ": posts you edited first, of all posted"),
      el("span", {}, el("b", { text: "Next Todo" }), ": in the huddle thread within 5 minutes of the huddle notes, "
        + "or by 16:05 when their time isn't known; a day without a huddle isn't counted")));
  }

  offs.push(on("command", (c) => { if (c && (c.state === "done" || c.state === "failed")) loadSoon(SOON_MS); }));
  timers.push(window.setInterval(load, REFRESH_MS));
  load();
  return () => {
    dead = true;
    timers.forEach((t) => window.clearInterval(t));
    offs.forEach((f) => f());
    window.clearTimeout(soonT);
    window.clearTimeout(retryT);
  };
}
