// The Usage page: what each model costs and how many tokens it uses (usage_report.py, through /api/usage).
// Three totals cards (Today, 7 days, 30 days), each with a line for every provider: the three kinds of cost never add up,
// because Claude's is a list price paid from a subscription, the company proxy's is real money and Codex has none (tokens
// only). Then the last 14 days as a stacked bar chart per model that shows one measure at a time (inline SVG, built with
// createElementNS: no library, no inline script), a table by model and one by job (sortable, for the chosen range), and
// what it all means. The page only reads. Every piece of text goes in as text; a model or job name is shown in a <bdi>.
// A cost is shown only where somebody reported one: a run with no price adds tokens and no dollars, and says so.
import { api, on } from "./client.js";
import { clear, el, svg } from "./dom.js";

const REFRESH_MS = 60000;
const RETRY_MS = 5000;
const SOON_MS = 3000; // a job step started or ended: its model calls may have been counted
const RANGES = [["today", "Today"], ["7d", "7 days"], ["30d", "30 days"]];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const PROVIDER = { claude: "Claude", litellm: "LiteLLM", codex: "Codex" };
const JOB = {
  insights: "Insights", feedback: "Feedback", todo: "Next Todo", decks: "Decks", slack: "Slack (DMs and clicks)",
  site_review: "Site review", compose: "Composer", history: "History", weekly: "Weekly report", deck_test: "Deck test",
  unknown: "Earlier runs (job unknown)", other: "Other",
};
const BASIS = { list: "list price", proxy: "company proxy", subscription: "plan", unknown: "unknown", mixed: "mixed" };
// what a provider's figure is, said next to it on the cards
const BASIS_LINE = {
  list: "list price, paid from your subscription", proxy: "company proxy, real cost", subscription: "ChatGPT Pro, tokens only",
  unknown: "no cost reported, tokens only", mixed: "more than one kind of cost, not added up",
};
// what the chart can show: a cost on one basis (the key is the basis) or tokens
const METRICS = [["list", "List price"], ["proxy", "Proxy cost"], ["tokens", "Tokens"]];
const MEASURE = { list: "list price", proxy: "proxy cost", tokens: "tokens" };
const SLOT = 40, PLOT_H = 160, BAR_W = 26; // the chart's drawing box: a day is a 40-wide slot, a bar 26 of it
const SLOTS = 7;                           // colours: six models, then "Other" (the seventh)
const APPROX = "≈\u00a0";                  // before a figure that has an approximate run in it (the space does not break a line)
const LEAST = "≥\u00a0";                   // before a cost that leaves out runs with no price

// what the page remembers while it is open, across a change of page
const ui = { range: "7d", metric: null, model: { key: "cost", dir: "desc" }, job: { key: "proxy", dir: "desc" } };

// ------------------------------------------------------------------ small pure helpers

const whole = (x) => (typeof x === "number" && Number.isFinite(x) && x >= 0 ? Math.round(x) : 0);
const dollars = (x) => (typeof x === "number" && Number.isFinite(x) && x >= 0 ? x : null);
const plural = (n, one, many = one + "s") => `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
const rows = (list) => (Array.isArray(list) ? list.filter((r) => r && typeof r === "object") : []);
const basisName = (b) => (Object.hasOwn(BASIS, b) ? BASIS[b] : String(b));

/** 950 → "950", 1234 → "1.23k", 12345 → "12.3k", 123456 → "123k", 999999 → "1M", 4.2e9 → "4.2B". */
export function tokens(n) {
  n = whole(n);
  if (n < 1000) return String(n);
  const units = ["k", "M", "B"];
  let v = n, i = -1;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i += 1; }
  let s = v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2);
  if (s === "1000" && i < units.length - 1) { s = "1"; i += 1; }
  if (s.includes(".")) s = s.replace(/\.?0+$/, "");
  return s + units[i];
}

/** null → "—" (nobody reported a cost), 0 → "$0", 0.00001 → "<$0.0001", 0.0016 → "$0.0016", 0.123 → "$0.123", 4.17 → "$4.17". */
export function money(x) {
  if (x === null || x === undefined) return "—";
  if (x === 0) return "$0";
  if (x < 0.0001) return "<$0.0001";
  if (x >= 100) return "$" + Math.round(x).toLocaleString("en-US");
  if (x >= 1) return "$" + x.toFixed(2);
  if (x >= 0.01) return "$" + x.toFixed(3);
  return "$" + x.toFixed(4);
}

function wall(ms) {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60000)} m ${String(Math.round((ms % 60000) / 1000)).padStart(2, "0")} s`;
}

function isoDay(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || ""));
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
}
const dayName = (iso) => { const d = isoDay(iso); return d ? `${WEEKDAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}` : ""; };
const shortDay = (iso) => { const d = isoDay(iso); return d ? `${d.getDate()} ${MONTHS[d.getMonth()]}` : ""; };

function icon(name) { return el("span", { class: "ico i-" + name, attrs: { "aria-hidden": "true" } }); }

function jobName(job) {
  if (Object.hasOwn(JOB, job)) return JOB[job];
  const t = String(job).replace(/_/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
}

function badge(provider) {
  const known = Object.hasOwn(PROVIDER, provider);
  return el("span", { class: "u-prov p-" + (known ? provider : "other"), text: known ? PROVIDER[provider] : String(provider) });
}

// ------------------------------------------------------------------ checking what the API sent

/** The costs of a group, apart for each basis: only real amounts. */
function costsOf(by) {
  const out = {};
  if (by && typeof by === "object") {
    for (const [k, v] of Object.entries(by)) { if (dollars(v) !== null) out[k] = v; }
  }
  return out;
}

/** "list price $1.00 + company proxy $1.00": what a group that mixes bases shows instead of a sum. */
const costParts = (by) => Object.entries(by).map(([b, v]) => `${basisName(b)} ${money(v)}`).join(" + ");

/** The counts every group has: tokens (input is the whole prompt, cached and written to the cache included), runs, failures. */
function counts(t) {
  return {
    input: whole(t.input_tokens), output: whole(t.output_tokens), cached: whole(t.cache_read_tokens),
    written: whole(t.cache_write_tokens), runs: whole(t.calls), failed: whole(t.failures), unpriced: whole(t.unpriced_calls),
    approx: whole(t.approx_calls),
  };
}

function providerOf(p) {
  return { provider: String(p.provider || ""), cost: dollars(p.cost_usd), basis: String(p.cost_basis || "unknown"),
    by: costsOf(p.cost_by_basis), ...counts(p) };
}

function totals(t) {
  t = t && typeof t === "object" ? t : {};
  return { by: costsOf(t.cost_by_basis), providers: rows(t.by_provider).map(providerOf), ...counts(t) };
}

function modelRow(r) {
  return {
    provider: String(r.provider || ""), model: String(r.model || ""), ...counts(r), cost: dollars(r.cost_usd),
    by: costsOf(r.cost_by_basis), basis: String(r.cost_basis || "unknown"),
    time: typeof r.latency_ms === "number" && Number.isFinite(r.latency_ms) ? r.latency_ms : null,
  };
}

function jobRow(r) {
  return { job: String(r.job || "other"), ...counts(r), by: costsOf(r.cost_by_basis) };
}

/** A cost with its caveat in front: "≈" when a run in the group is approximate, "≥" when some runs in it had no price. */
const costText = (r, amount, approx = true) => (approx && r.approx > 0 ? APPROX : r.unpriced > 0 ? LEAST : "") + money(amount);
const noPrice = (r) => `${r.unpriced.toLocaleString("en-US")} of ${plural(r.runs, "run")} had no price`;
const caveats = (r, approx = true) => [approx && r.approx > 0 ? `${plural(r.approx, "run")} with approximate figures` : null,
  r.unpriced > 0 ? noPrice(r) : null].filter(Boolean).join("; ") || null;

// ------------------------------------------------------------------ the page

export function mountUsage(view) {
  const timers = [];
  const offs = [];
  let dead = false, loaded = false, data = null, sig = "", seq = 0, soonT = null, retryT = null;
  const root = el("div", { class: "usage" });
  view.appendChild(root);
  clear(root, header(), el("div", { class: "skeleton", attrs: { "aria-hidden": "true" } }, el("i"), el("i")));

  function header() {
    return el("header", { class: "pg-head" },
      el("div", { class: "pg-title" }, el("h1", { text: "Usage" }),
        el("p", { text: "What each model costs and how many tokens it uses." })));
  }

  function loadSoon(ms) {
    if (soonT) return;
    soonT = window.setTimeout(() => { soonT = null; load(); }, ms);
  }

  async function load() {
    window.clearTimeout(retryT);
    retryT = null;
    const mine = ++seq;
    let d;
    try { d = await api(`/api/usage?range=${ui.range}`); } catch (e) {
      if (dead || mine !== seq) return;
      if (!loaded) {
        clear(root, header(), el("div", { class: "load-error", attrs: { role: "status" } }, icon("warn"),
          el("span", { text: "Couldn't load the usage. Trying again…" })));
        root.dataset.ready = "1";
      }
      retryT = window.setTimeout(load, RETRY_MS);
      return;
    }
    if (dead || mine !== seq || !d || typeof d !== "object") return; // a newer request is on its way
    const s = JSON.stringify(d);
    if (s === sig) return; // nothing moved: no repaint, no blink
    sig = s;
    const first = !loaded;
    loaded = true;
    data = d;
    paint(first);
    root.dataset.ready = "1";
  }

  /** Repaint, and give the focus back to the control that had it (a repaint replaces every node). */
  function paint(enter) {
    const key = document.activeElement && root.contains(document.activeElement) ? document.activeElement.dataset.fk : null;
    if (!data.has_data) paintEmpty(); else paintAll(enter);
    if (key) {
      const again = [...root.querySelectorAll("[data-fk]")].find((n) => n.dataset.fk === key);
      if (again) again.focus({ preventScroll: true });
    }
  }

  function paintEmpty() {
    clear(root, header(), el("section", { class: "card u-empty rise" },
      el("div", { class: "pg-orb", attrs: { "aria-hidden": "true" } }, icon("coin")),
      el("p", { class: "empty-t", text: "No usage recorded yet" }),
      el("p", { class: "empty-s" }, "Every model call the agent makes from now on is counted here. To add the history from before, run ",
        el("code", { text: "uv run haseeb-agent usage-backfill" }), " in Terminal, from the agent's folder.")));
  }

  function paintAll(enter) {
    const t = Object.fromEntries(RANGES.map(([k]) => [k, totals((data.ranges || {})[k])]));
    clear(root, header(),
      el("div", { class: "u-cards" }, RANGES.map(([k, label], i) => totalsCard(k, label, t[k], k === data.range, enter ? i : -1))),
      chartCard(enter),
      el("div", { class: "u-bar" },
        el("h2", { text: "Breakdown" }),
        segmented("Range", RANGES, ui.range, "range", (v) => { ui.range = v; sig = ""; load(); })),
      modelCard(), jobCard(), notesCard());
  }

  // ---------------------------------------------------------------- a controls helper

  /** A row of toggle buttons (aria-pressed), one pressed. */
  function segmented(label, options, current, fk, pick) {
    return el("div", { class: "u-seg", attrs: { role: "group", "aria-label": label } },
      options.map(([value, text]) => el("button", {
        class: "u-seg-b", text, data: { fk: `${fk}:${value}` },
        attrs: { type: "button", "aria-pressed": String(value === current) },
        on: { click: () => { if (value !== current) pick(value); } },
      })));
  }

  // ---------------------------------------------------------------- the three cards

  /** One provider in a card: its cost on its own basis (never added to another's), then its tokens and runs. */
  function providerBlock(p) {
    const parts = Object.keys(p.by).length ? costParts(p.by) : "";
    const value = p.cost !== null ? costText(p, p.cost) : parts || "—";
    const inside = [p.cached ? `${tokens(p.cached)} cached` : null, p.written ? `${tokens(p.written)} cache write` : null].filter(Boolean);
    const priced = p.cost !== null; // a provider with no cost at all is "tokens only": no run of it is "unpriced"
    const runs = [plural(p.runs, "run"),
      p.failed ? el("span", { class: "u-bad", text: `${p.failed.toLocaleString("en-US")} failed` }) : null,
      p.approx ? `${p.approx.toLocaleString("en-US")} approximate` : null,
      priced && p.unpriced ? `${p.unpriced.toLocaleString("en-US")} unpriced` : null].filter(Boolean);
    return el("li", { class: "u-p", data: { provider: p.provider } },
      el("div", { class: "u-pt" }, badge(p.provider), el("span", { class: "u-pb", text: Object.hasOwn(BASIS_LINE, p.basis) ? BASIS_LINE[p.basis] : basisName(p.basis) })),
      el("div", { class: ["u-pv", "num", p.cost === null && !parts ? "none" : ""].filter(Boolean).join(" "), attrs: { title: caveats(p) }, text: value }),
      el("div", { class: "u-ps num" }, el("b", { text: tokens(p.input) }), " in", inside.length ? ` (${inside.join(", ")})` : "", " · ",
        el("b", { text: (p.approx ? APPROX : "") + tokens(p.output) }), " out"),
      el("div", { class: "u-ps num" }, runs.flatMap((x, i) => (i ? [" · ", x] : [x]))));
  }

  function totalsCard(name, label, t, shown, idx) {
    return el("section", {
      class: ["card", "ucard", shown ? "on" : "", idx >= 0 ? "rise" : ""].filter(Boolean).join(" "), data: { range: name },
      attrs: { "aria-label": label }, vars: idx >= 0 ? { "--d": `${idx * 70}ms` } : {},
    },
    el("div", { class: "u-h" }, el("span", { text: label }), shown ? el("span", { class: "u-here", text: "in the tables below" }) : null),
    t.providers.length ? el("ul", { class: "u-pl" }, t.providers.map(providerBlock)) : el("p", { class: "u-none", text: "Nothing yet" }));
  }

  // ---------------------------------------------------------------- the chart

  function chartCard(enter) {
    const daily = rows(data.daily);
    const chartModels = rows(data.chart_models);
    const costOf = (o, basis) => dollars(o.cost_by_basis && typeof o.cost_by_basis === "object" ? o.cost_by_basis[basis] : null);
    const avail = METRICS.filter(([k]) => k === "tokens" || chartModels.some((m) => costOf(m, k) !== null));
    const metric = avail.some(([k]) => k === ui.metric) ? ui.metric : avail[0][0];
    const isCost = metric !== "tokens";
    const valueOf = (o) => (isCost ? costOf(o, metric) || 0 : whole(o.tokens));
    const fmt = isCost ? money : tokens;
    const unit = isCost ? MEASURE[metric] : "tokens";
    // the models drawn: those with something in this measure, the most first (so a model with none, a Codex run on a plan
    // in a cost chart, takes no colour), the first six coloured and the rest together as "Other"
    const maxSlots = Math.min(SLOTS - 1, Math.max(1, whole(data.chart_max_models) || SLOTS - 1));
    const ranked = chartModels.map((m) => ({ m, v: valueOf(m) })).filter((x) => x.v > 0)
      .sort((a, b) => b.v - a.v || String(a.m.key).localeCompare(String(b.m.key))).map((x) => x.m);
    const slotOf = new Map(ranked.map((m, i) => [m.key, Math.min(i, maxSlots)]));
    const shown = ranked.slice(0, maxSlots);
    const others = ranked.slice(maxSlots);
    const cols = daily.map((d) => {
      const parts = Array.from({ length: maxSlots + 1 }, () => 0);
      const models = rows(d.models);
      for (const m of models) if (valueOf(m) > 0) parts[slotOf.has(m.key) ? slotOf.get(m.key) : maxSlots] += valueOf(m);
      return { day: d.day, parts, total: parts.reduce((a, b) => a + b, 0), by: costsOf(d.cost_by_basis), tokens: whole(d.tokens), models };
    });
    const max = Math.max(...cols.map((c) => c.total), 0);
    const nameOf = new Map(chartModels.map((m) => [m.key, String(m.model || m.key)]));

    const cap = el("div", { class: "u-cap", attrs: { "aria-hidden": "true" } });
    const total = cols.reduce((a, c) => a + c.total, 0);
    const capRest = () => { cap.classList.remove("on"); clear(cap, `Last ${cols.length} days · ${fmt(total)} ${unit}`); };
    capRest();

    const W = Math.max(1, cols.length) * SLOT;
    const bars = svg("svg", { class: "u-bars", viewBox: `0 0 ${W} ${PLOT_H}`, preserveAspectRatio: "none", focusable: "false", role: "group",
      "aria-label": `Last ${cols.length} days, by model, ${unit}`, });
    for (const f of [0, 0.5, 1]) {
      const y = PLOT_H - Math.round(f * (PLOT_H - 2)) - 1;
      bars.appendChild(svg("line", { class: "u-grid" + (f === 0 ? " base" : ""), x1: "0", y1: String(y), x2: String(W), y2: String(y) }));
    }
    cols.forEach((c, i) => {
      const costs = Object.entries(c.by).map(([b, v]) => `${basisName(b)} ${money(v)}`);
      const drawn = c.models.filter((m) => valueOf(m) > 0);
      const read = `${dayName(c.day)}: ${[...costs, `${tokens(c.tokens)} tokens`].join(", ")}`
        + (drawn.length ? `. By model, ${unit}: ` + drawn.map((m) => `${nameOf.get(m.key) || m.key} ${fmt(valueOf(m))}`).join(", ") : "");
      const g = svg("g", { class: "u-day", tabindex: "0", role: "img", "aria-label": read, "data-day": c.day });
      const x = i * SLOT + (SLOT - BAR_W) / 2;
      g.appendChild(svg("rect", { class: "u-hit", x: String(i * SLOT), y: "0", width: String(SLOT), height: String(PLOT_H) }));
      let top = PLOT_H - 1;
      c.parts.forEach((v, slot) => {
        if (!(v > 0) || !(max > 0)) return;
        const h = Math.max(2, (v / max) * (PLOT_H - 6));
        top -= h;
        g.appendChild(svg("rect", { class: "u-seg-r m" + slot, x: String(x), y: top.toFixed(1), width: String(BAR_W), height: h.toFixed(1) },
          svg("title", {}, `${slot < shown.length ? nameOf.get(shown[slot].key) : "Other"}: ${fmt(v)}`)));
      });
      const show = () => {
        clear(cap, el("b", { text: dayName(c.day) }), (isCost ? ` · ${money(c.by[metric] ?? 0)} ${unit}` : "") + ` · ${tokens(c.tokens)} tokens`);
        cap.classList.add("on");
      };
      g.addEventListener("pointerenter", show);
      g.addEventListener("pointerdown", show);
      g.addEventListener("focus", show);
      g.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse") capRest(); });
      g.addEventListener("blur", capRest);
      bars.appendChild(g);
    });

    const ylab = el("div", { class: "u-y", attrs: { "aria-hidden": "true" } },
      el("span", { text: max > 0 ? fmt(max) : "" }), el("span", { text: max > 0 ? fmt(max / 2) : "" }), el("span", { text: max > 0 ? "0" : "" }));
    const xlab = el("div", { class: "u-x", attrs: { "aria-hidden": "true" } },
      cols.map((c, i) => el("span", { class: i === cols.length - 1 ? "u-today" : "", text: i === 0 || i === cols.length - 1 || i === Math.floor(cols.length / 2) ? shortDay(c.day) : "" })));

    const legend = el("ul", { class: "u-legend" },
      shown.map((m, i) => el("li", {}, el("span", { class: "u-sw m" + i, attrs: { "aria-hidden": "true" } }), badge(String(m.provider || "")), el("bdi", { text: nameOf.get(m.key) }))),
      others.length ? el("li", {}, el("span", { class: "u-sw m" + maxSlots, attrs: { "aria-hidden": "true" } }), `Other (${plural(others.length, "model")})`) : null);

    const left = isCost && chartModels.some((m) => valueOf(m) === 0 && whole(m.tokens) > 0);
    const costCols = avail.filter(([k]) => k !== "tokens");
    const sr = el("div", { class: "sr-only" }, el("table", {},
      el("caption", { text: `Last ${cols.length} days, by day` }),
      el("thead", {}, el("tr", {}, el("th", { attrs: { scope: "col" }, text: "Day" }),
        costCols.map(([, label]) => el("th", { attrs: { scope: "col" }, text: label })), el("th", { attrs: { scope: "col" }, text: "Tokens" }))),
      el("tbody", {}, cols.map((c) => el("tr", {}, el("td", { text: dayName(c.day) }),
        costCols.map(([k]) => el("td", { text: money(c.by[k] ?? 0) })), el("td", { text: tokens(c.tokens) }))))));

    return el("section", { class: ["card", "u-chart", enter ? "rise" : ""].filter(Boolean).join(" "), attrs: { "aria-label": "Last 14 days" }, vars: enter ? { "--d": "210ms" } : {} },
      el("div", { class: "card-h" }, el("h2", { text: `Last ${cols.length} days` }), // (the buttons are no part of the heading)
        avail.length > 1 ? segmented("Chart shows", avail, metric, "metric", (v) => { ui.metric = v; paint(false); }) : null),
      el("div", { class: "u-plot" }, ylab, bars, xlab),
      cap, legend,
      left ? el("p", { class: "u-hint", text: `Only models with a ${unit} are drawn. Codex runs on a plan, and some proxy models send no price. Use Tokens to see every model.` }) : null,
      sr);
  }

  // ---------------------------------------------------------------- the tables

  const MODEL_COLS = [
    { k: "model", label: "Model", left: true, get: (r) => r.model.toLowerCase(), first: "asc" },
    { k: "runs", label: "Runs", get: (r) => r.runs, hint: "Runs the model took part in: a run that used two models is a run in each" },
    { k: "input", label: "Input", get: (r) => r.input, hint: "The whole prompt, cached tokens included" },
    { k: "output", label: "Output", get: (r) => r.output },
    { k: "cached", label: "Cached", get: (r) => r.cached, hint: "Of the input, read from the cache" },
    { k: "written", label: "Cache write", get: (r) => r.written, hint: "Of the input, written to the cache" },
    { k: "cost", label: "Cost", get: (r) => r.cost },
    { k: "time", label: "Median time", get: (r) => r.time },
    { k: "failed", label: "Failed", get: (r) => r.failed },
  ];
  const JOB_COLS = [
    { k: "job", label: "Job", left: true, get: (r) => jobName(r.job).toLowerCase(), first: "asc" },
    { k: "runs", label: "Runs", get: (r) => r.runs },
    { k: "input", label: "Input", get: (r) => r.input, hint: "The whole prompt, cached tokens included" },
    { k: "output", label: "Output", get: (r) => r.output },
    { k: "list", label: "List price", get: (r) => (Object.hasOwn(r.by, "list") ? r.by.list : null), hint: "Claude, at list prices: paid from your subscription" },
    { k: "proxy", label: "Proxy cost", get: (r) => (Object.hasOwn(r.by, "proxy") ? r.by.proxy : null), hint: "The company proxy: real money" },
    { k: "failed", label: "Failed", get: (r) => r.failed },
  ];

  /** Sorted by the chosen column; a missing value (no cost, no time) always last; ties by tokens, then by name. */
  function sorted(list, cols, state, nameKey) {
    const col = cols.find((c) => c.k === state.key) || cols[0];
    const dir = state.dir === "asc" ? 1 : -1;
    return [...list].sort((a, b) => {
      const x = col.get(a), y = col.get(b);
      if (x === null && y !== null) return 1;
      if (y === null && x !== null) return -1;
      if (x !== y && x !== null) return (x < y ? -1 : 1) * dir;
      return (b.input + b.output) - (a.input + a.output) || String(a[nameKey]).localeCompare(String(b[nameKey]));
    });
  }

  /** Each cell knows its column's name: on a phone a row is a card, and the small figures say what they are. */
  function labelled(tr, cols) {
    [...tr.children].forEach((td, i) => { if (cols[i] && !td.classList.contains("l")) td.dataset.label = cols[i].label; });
    return tr;
  }

  function sortableTable(label, cols, list, state, which, cell) {
    const head = cols.map((c) => {
      const here = state.key === c.k;
      return el("th", { class: c.left ? "l" : null, attrs: { scope: "col", "aria-sort": here ? (state.dir === "asc" ? "ascending" : "descending") : "none" } },
        el("button", {
          class: "th-sort" + (here ? " on" : ""), attrs: { type: "button", title: c.hint || null }, data: { fk: `${which}:${c.k}` },
          on: { click: () => { ui[which] = here ? { key: c.k, dir: state.dir === "asc" ? "desc" : "asc" } : { key: c.k, dir: c.first || "desc" }; paint(false); } },
        }, c.label, el("span", { class: "caret", attrs: { "aria-hidden": "true" }, text: here ? (state.dir === "asc" ? "▲" : "▼") : "" })));
    });
    return el("div", { class: "tbl" }, el("table", { class: "u-table" },
      el("caption", { class: "sr-only", text: label }),
      el("thead", {}, el("tr", {}, head)),
      el("tbody", {}, list.length ? list.map((r) => labelled(cell(r), cols)) : el("tr", {}, el("td", { class: "nil l", attrs: { colspan: String(cols.length) }, text: "Nothing in this range" })))));
  }

  /** The cost of a model: on its one basis with the basis beside it; none at all is a dash with the reason; a model that
   *  mixes bases lists its parts, never a sum. */
  const costCell = (r) => {
    if (r.cost === null) {
      const parts = Object.keys(r.by).length ? costParts(r.by) : "";
      if (parts) return el("td", { class: "num cost", attrs: { title: "More than one kind of cost: they are not added up" }, text: parts });
      return el("td", { class: "num nil cost", attrs: { title: r.basis === "subscription" ? "Paid from a plan: no price per run" : "No cost was reported" }, text: "—" });
    }
    return el("td", { class: "num cost", attrs: { title: caveats(r) } }, costText(r, r.cost), el("small", { text: basisName(r.basis) }));
  };
  /** A job's cost on one basis: a dash when the job has none on it. */
  const jobCost = (r, basis, approx) => (Object.hasOwn(r.by, basis)
    ? el("td", { class: "num jcost", attrs: { title: caveats(r, approx) }, text: costText(r, r.by[basis], approx) })
    : el("td", { class: "num nil jcost", text: "—", attrs: { title: "No cost reported on this basis" } }));
  const numCell = (n) => el("td", { class: "num" + (n ? "" : " nil"), text: n ? n.toLocaleString("en-US") : "0" });
  const tokCell = (n, approx = false) => el("td", { class: "num" + (n ? "" : " nil"), text: (approx && n ? APPROX : "") + tokens(n), attrs: { title: n >= 1000 ? n.toLocaleString("en-US") : null } });
  const failCell = (n) => el("td", { class: "num" + (n ? " bad" : " nil"), text: n ? n.toLocaleString("en-US") : "0" });

  function modelCard() {
    const list = sorted(rows(data.by_model).map(modelRow), MODEL_COLS, ui.model, "model");
    return el("section", { class: "card u-tbl", attrs: { "aria-label": "By model" } },
      el("h3", { class: "card-h" }, el("span", { text: "By model" }), el("span", { class: "card-meta", text: rangeLabel() })),
      sortableTable(`Usage by model, ${rangeLabel()}`, MODEL_COLS, list, ui.model, "model", (r) => el("tr", { data: { model: r.model } },
        el("td", { class: "l" }, el("div", { class: "u-m" }, badge(r.provider), el("bdi", { class: "u-mn", text: r.model }))),
        numCell(r.runs), tokCell(r.input), tokCell(r.output, r.approx > 0), tokCell(r.cached), tokCell(r.written), costCell(r),
        el("td", { class: "num" + (r.time === null ? " nil" : ""), text: wall(r.time) }), failCell(r.failed))));
  }

  function jobCard() {
    const list = sorted(rows(data.by_job).map(jobRow), JOB_COLS, ui.job, "job");
    return el("section", { class: "card u-tbl", attrs: { "aria-label": "By job" } },
      el("h3", { class: "card-h" }, el("span", { text: "By job" }), el("span", { class: "card-meta", text: rangeLabel() })),
      sortableTable(`Usage by job, ${rangeLabel()}`, JOB_COLS, list, ui.job, "job", (r) => el("tr", { data: { job: r.job } },
        el("td", { class: "l" }, el("bdi", { text: jobName(r.job) })),
        numCell(r.runs), tokCell(r.input), tokCell(r.output, r.approx > 0),
        jobCost(r, "list", true), jobCost(r, "proxy", false), failCell(r.failed))));
  }

  const rangeLabel = () => (data.range === "today" ? "today" : data.range === "30d" ? "the last 30 days" : "the last 7 days");

  // ---------------------------------------------------------------- what it all means

  function notesCard() {
    // what the server says about the kinds of cost, the proxy's days, approximate figures, history and runs; no badge for
    // a note that belongs to no provider
    const items = rows(data.notes).map((n) => el("li", {}, String(n.provider || "") ? badge(String(n.provider)) : null,
      el("span", { text: String(n.text || "") })));
    const h = data.history && typeof data.history === "object" ? data.history : {};
    const lite = h.litellm && typeof h.litellm === "object" ? h.litellm : {};
    const from = typeof lite.counted_from === "string" && isoDay(lite.counted_from) ? `; counted from ${shortDay(lite.counted_from)}` : "";
    if (lite.state !== "imported") {
      const what = lite.state === "unavailable" ? "Company-proxy history not available" : "Company-proxy history not imported yet";
      items.push(el("li", { class: "u-hist" }, icon("clock"), el("span", { text: what + from })));
    }
    items.push(el("li", {}, icon("layers"), el("span", { text: "Input is the whole prompt, cached tokens included. Cached is the part of it read from the cache, and Cache write the part written to it." })));
    const leftOut = (r) => whole(r.unpriced_calls) > 0 && Object.keys(costsOf(r.cost_by_basis)).length > 0;
    if (rows(data.by_model).some(leftOut) || rows(data.by_job).some(leftOut)) {
      items.push(el("li", {}, icon("warn"), el("span", { text: "≥ means some runs in that row had no price, so its cost is at least that much." })));
    }
    return el("section", { class: "card u-notes", attrs: { "aria-label": "What the numbers mean" } },
      el("h2", { class: "card-h" }, el("span", { text: "What the numbers mean" })),
      el("ul", { class: "u-notes-l" }, items));
  }

  offs.push(on("event", () => loadSoon(SOON_MS)));
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
