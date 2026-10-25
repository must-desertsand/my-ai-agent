// The Live page: what the agent is doing right now (a step pipeline), what's coming up (ticking countdowns), the
// run controls (buttons for the jobs Haseeb used to start by hand, and a banner while the agent is paused), what's
// waiting for Haseeb (the composer for a message he writes, the inbox of drafts, and the teammates' posts he can ask
// feedback for), today's timeline and the live log tail.
import { api, on } from "./client.js";
import { clear, clock, countdown, dayLabel, el, hhmm, hhmmss, parseTime, whenLabel, ago } from "./dom.js";
import { mountComposer } from "./compose.js";
import { mountInbox } from "./inbox.js";
import { mountRuns } from "./runs.js";
import { mountTeamPosts } from "./teamposts.js";

// Event job names as the jobs write them, plus the tick's job names, which mean the same thing.
const JOBS = {
  process_dm: { title: "Reading your DMs", steps: ["read_dm", "command"] },
  process_page: { title: "Running your dashboard command", steps: ["command"] },
  feedback: { title: "Drafting feedback", steps: ["read_channel", "read_thread", "site_review", "write_draft", "deliver"] },
  todo: { title: "Writing the Next Todo", steps: ["find_huddle", "read_notes", "write_todo", "deliver", "autopost"] },
  insights: { title: "Researching today's insights", steps: ["research", "write", "judge", "deliver"] },
  decks: { title: "Designing a deck", steps: ["design", "render", "upload"] },
  weekly: { title: "Sending the weekly report", steps: ["send"] },
  slack_check: { title: "Checking the Slack account", steps: ["check"] },
  history_import: { title: "Importing the team history", steps: ["import"] },
  enrich: { title: "Labelling and grouping the history", steps: ["enrich"] },
  compose: { title: "Polishing your message", steps: ["polish", "deliver"] },
};
const ALIAS = { run_feedback: "feedback", run_todo: "todo" };
// a step that only some runs have (a feedback draft looks at a site only when its post links to one): the pipeline shows it
// while it runs and once it has run, and not at all in a run that skipped it
const OPTIONAL = new Set(["site_review", "read_thread"]);

const STEPS = {
  read_dm: ["Read DM", "inbox"], command: ["Run command", "bolt"], read_channel: ["Read channel", "eye"],
  write_draft: ["Write draft", "pen"], deliver: ["Your DM", "send"], find_huddle: ["Find huddle", "search"],
  read_notes: ["Read notes", "doc"], write_todo: ["Write todo", "pen"], autopost: ["Auto-post", "clock"],
  research: ["Research", "search"], write: ["Write", "pen"], judge: ["Judge", "scale"], design: ["Design", "palette"],
  render: ["Render", "layers"], upload: ["Upload", "send"], read_post: ["Read post", "eye"], send: ["Send", "send"],
  site_review: ["Site review", "eye"], read_thread: ["Read thread", "chat"],
  check: ["Check", "users"], import: ["Import", "down"], enrich: ["Label", "tag"], polish: ["Polish", "spark"],
};

const UPCOMING = {
  "Feedback scan": { sub: "every 10 min on workdays", horizon: 600 },
  Huddle: { sub: "Next Todo auto-drafts after the notes", horizon: 86400 },
  Insights: { sub: "research → write → judge → your DM", horizon: 86400 },
};

const LOG_FILTERS = [["all", "All"], ["slack", "Slack"], ["models", "Models"], ["decks", "Decks"], ["problems", "Problems"]];
const LOG_SHOW = 14;
const RUN_WINDOW_MS = 15 * 60000; // a run's earlier steps started at most this long before its current one
const STALE_MS = 15 * 60000; // an event still open after this long never finished (the API's RUNNING_MAX)

const jobKey = (j) => ALIAS[j] || j;
const jobInfo = (j) => JOBS[jobKey(j)] || { title: humanize(j), steps: [] };
const stepInfo = (s) => STEPS[s] || [humanize(s), "dot"];

function humanize(s) {
  const t = String(s || "").replace(/_/g, " ").trim();
  return t ? t[0].toUpperCase() + t.slice(1) : "Working";
}

function icon(name, cls = "") {
  return el("span", { class: ["ico", "i-" + name, cls].filter(Boolean).join(" "), attrs: { "aria-hidden": "true" } });
}

function card(cls, title, extra) {
  const head = el("h2", { class: "card-h" }, el("span", { text: title }), extra || null);
  const body = el("div", { class: "card-b" });
  const node = el("section", { class: ["card", "rise", cls].join(" "), attrs: { "aria-label": title } }, head, body);
  return { node, head, body };
}

export function mountLive(view, ctx) {
  const offs = [];
  const timers = [];
  const runSteps = new Map(); // "job step" → {at, ok, secs}: the latest stream event per step, for the pipeline

  const grid = el("div", { class: "grid" });
  view.appendChild(grid);
  // ---- Paused banner (while the agent is paused; filled and shown by runs.js)
  const pausedBanner = el("section", { class: "paused-banner", attrs: { hidden: true, "aria-label": "Paused" } });

  // ---- Right now
  const now = card("now", "Right now", el("span", { class: "card-meta num" }));
  const nowMeta = now.head.lastChild;
  // ---- Coming up
  const up = card("upcoming", "Coming up");
  // ---- Run controls
  const runs = card("runs", "Run now");
  // ---- Inbox: the composer on top, then the drafts, and under them the team's posts that can get a draft
  const inboxCount = el("span", { class: "count-badge", attrs: { "aria-live": "polite" } });
  const inbox = card("inbox", "Waiting for you", inboxCount);
  // a div, not a section: a nested section would end the inbox card's own section early for anything that reads it
  const composeBox = el("div", { class: "composer", attrs: { role: "group", "aria-label": "Write a message" } });
  const inboxList = el("div", { class: "inbox-main" });
  const teamBox = el("section", { class: "team", attrs: { "aria-label": "Team posts" } });
  inbox.body.append(composeBox, inboxList, teamBox);
  // ---- Today
  const todayMeta = el("span", { class: "card-meta", text: dayLabel(new Date()) });
  const today = card("today", "Today", todayMeta);
  // ---- Log
  const filters = el("span", { class: "filters", attrs: { role: "group", "aria-label": "Log filter" } });
  const log = card("log", "Live log", filters);
  grid.append(pausedBanner, now.node, up.node, runs.node, inbox.node, today.node, log.node);

  // ----------------------------------------------------------- right now
  let nowRun = null; // {job, step, startedAt} of the step on screen
  let nowSig = null; // what the pipeline on screen shows: an unchanged one is left alone (its glow keeps its rhythm)
  function paintNow(live) {
    const d = live.data;
    const cur = d && d.now;
    now.node.classList.toggle("running", !!cur);
    if (!cur) {
      nowRun = null;
      nowSig = null;
      nowMeta.textContent = "";
      const last = d && d.agent.last_tick ? Date.parse(d.agent.last_tick) : NaN;
      const lastDone = d && d.today && d.today.length ? d.today[0] : null;
      let quiet = now.body.querySelector(".quiet");
      if (!quiet) { // built once: later refreshes only change its words, so the orb never restarts
        quiet = el("div", { class: "quiet" },
          el("div", { class: "quiet-orb", attrs: { "aria-hidden": "true" } }, el("i"), el("i"), el("i"), el("i", { class: "orbit" }, el("b"))),
          el("div", { class: "quiet-text" },
            el("div", { class: "now-title", text: "Quiet right now" }),
            el("div", { class: "now-sub" }),
            el("div", { class: "quiet-last-box" })));
        clear(now.body, quiet);
      }
      quiet.querySelector(".now-sub").textContent = Number.isNaN(last)
        ? "Nothing is running. The agent hasn't ticked yet."
        : `Nothing is running. Last tick ${ago((Date.now() - last) / 1000)}.`;
      const box = quiet.querySelector(".quiet-last-box");
      const lastSig = lastDone ? `${lastDone.title}|${lastDone.at}|${lastDone.ok}` : "";
      if (box.dataset.sig !== lastSig) {
        box.dataset.sig = lastSig;
        clear(box, lastDone ? el("div", { class: "quiet-last" },
          lastDone.ok === false ? icon("warn", "warn") : lastDone.ok === true ? icon("check", "ok") : icon("dot"),
          el("span", { text: "Last: " + timelineTitle(lastDone.title) }),
          el("span", { class: "muted num", text: " · " + hhmm(parseTime(lastDone.at) || new Date()) })) : null);
      }
      return;
    }
    const job = jobKey(cur.job);
    const info = jobInfo(job);
    const listed = info.steps.slice();
    if (!listed.includes(cur.step)) listed.push(cur.step);
    // the same step as last time (the start, rebuilt from elapsed_s, jitters by up to a second): keep its start
    const same = nowRun && nowRun.job === job && nowRun.step === cur.step &&
      Math.abs(nowRun.startedAt - (live.at - cur.elapsed_s * 1000)) < 5000;
    const startedAt = same ? nowRun.startedAt : live.at - cur.elapsed_s * 1000;
    nowMeta.dataset.start = String(startedAt);
    nowMeta.textContent = "running " + clock((Date.now() - startedAt) / 1000);

    // each earlier step's latest outcome in this run, from today's list and the stream: a failure that a retry
    // then fixed counts as done, and an older run's failure doesn't count at all
    const latest = new Map();
    const note = (s, tAt, ok) => {
      if (!(tAt >= startedAt - RUN_WINDOW_MS)) return;
      const prev = latest.get(s);
      if (!prev || tAt > prev.at || (tAt === prev.at && ok !== null)) latest.set(s, { at: tAt, ok });
    };
    for (const t of (d.today || [])) {
      const [j, s] = String(t.title).split(" ");
      if (jobKey(j) === job) note(s, Date.parse(t.at), t.ok);
    }
    for (const [k, v] of runSteps) { // the stream sends each event once, when it starts: open ones say nothing yet
      const [j, s] = k.split(" ");
      if (j === job && v.ok !== null) note(s, v.at, v.ok);
    }
    const failed = new Set([...latest].filter(([, v]) => v.ok === false).map(([s]) => s));
    const steps = listed.filter((s) => !OPTIONAL.has(s) || s === cur.step || latest.has(s));
    const at = steps.indexOf(cur.step);

    const pipe = el("ol", { class: "pipe", attrs: { "aria-label": "Steps" } });
    steps.forEach((s, i) => {
      const [label, ico] = stepInfo(s);
      let state = i < at ? "done" : i === at ? "live" : "todo";
      if (failed.has(s) && i < at) state = "fail";
      const meta = runSteps.get(`${job} ${s}`);
      let small = "";
      if (state === "live") small = cur.model || "working…";
      else if (state === "fail") small = "failed";
      else if (state === "done") small = meta && meta.secs ? `${meta.secs.toFixed(1)} s` : "done";
      if (i > 0) {
        const prev = i - 1 < at ? (failed.has(steps[i - 1]) ? "fail" : "done") : "todo";
        const linkCls = prev === "done" && state === "live" ? "flow" : prev === "done" && state !== "todo" ? "full" : prev === "fail" ? "failed" : "";
        pipe.appendChild(el("li", { class: ["link", linkCls].join(" "), attrs: { "aria-hidden": "true" } }, el("i")));
      }
      pipe.appendChild(el("li", { class: "step " + state, attrs: { "aria-current": state === "live" ? "step" : null } },
        el("div", { class: "node" }, icon(state === "done" ? "check" : state === "fail" ? "warn" : ico), state === "live" ? el("i", { class: "ring" }) : null),
        el("span", { class: "step-l", text: label }),
        el("small", { class: "step-s", text: small || " " })));
    });

    const chips = el("div", { class: "chips" },
      cur.model ? el("span", { class: "chip", text: "model · " + cur.model }) : null,
      el("span", { class: "chip c2", text: `step ${at + 1} of ${steps.length}` }),
      el("span", { class: "chip c3", text: "job · " + cur.job }));

    const sig = [...pipe.children].map((c) => c.className + ":" + c.textContent).join("|") + "#" + chips.textContent;
    if (same) {
      if (sig !== nowSig) { // the same step, but an earlier one changed: swap the pipeline in place
        nowSig = sig;
        const old = now.body.querySelector(".pipe");
        if (old) old.replaceWith(pipe);
        const oc = now.body.querySelector(".chips");
        if (oc) oc.replaceWith(chips);
      }
      return;
    }
    nowRun = { job, step: cur.step, startedAt };
    nowSig = sig;
    clear(now.body,
      el("div", { class: "now-title", text: info.title }),
      el("div", { class: "now-sub" }, el("span", { class: "live-dot", attrs: { "aria-hidden": "true" } }),
        `${stepInfo(cur.step)[0]} · started ${hhmm(new Date(startedAt))}`),
      pipe, chips);
  }

  // ----------------------------------------------------------- coming up
  let upShape = null, upFirst = true;
  function paintUpcoming(live) {
    const list = ((live.data && live.data.upcoming) || []).filter((u) => parseTime(u.at));
    const shape = JSON.stringify(list.map((u) => u.label));
    if (shape === upShape) { // the same rows (an overdue scan's time moves on every answer): retime them in place
      up.body.querySelectorAll(".nx").forEach((nx, i) => {
        const at = parseTime(list[i].at);
        nx.querySelector(".cd").dataset.at = String(at.getTime());
        nx.querySelector(".at").textContent = "at " + whenLabel(at);
      });
      tickCountdowns();
      return;
    }
    upShape = shape;
    const enter = upFirst;
    upFirst = false;
    if (!list.length) {
      clear(up.body, el("p", { class: "empty-line", text: "Nothing scheduled." }));
      return;
    }
    const wrap = el("div", { class: "next" });
    list.forEach((u, i) => {
      const at = parseTime(u.at);
      if (!at) return;
      const meta = UPCOMING[u.label] || { sub: "", horizon: 86400 };
      const cd = el("b", { class: "cd num", data: { at: String(at.getTime()), horizon: String(meta.horizon) } });
      const bar = el("div", { class: enter ? "bar enter" : "bar", attrs: { "aria-hidden": "true" } }, el("i"));
      wrap.appendChild(el("div", { class: ["nx", i === 0 ? "soonest" : "", enter ? "enter" : ""].join(" "), vars: { "--d": `${i * 70}ms` } },
        el("div", { class: "nx-top" },
          el("div", {}, el("b", { class: "nx-l", text: u.label }), el("div", { class: "t", text: meta.sub })),
          el("div", { class: "nx-r" }, cd, el("div", { class: "t num at", text: "at " + whenLabel(at) }))),
        bar));
    });
    clear(up.body, wrap);
    tickCountdowns();
  }

  function tickCountdowns() {
    for (const cd of up.body.querySelectorAll(".cd")) {
      const left = (Number(cd.dataset.at) - Date.now()) / 1000;
      cd.textContent = left <= 0 ? "now" : countdown(left);
      const h = Number(cd.dataset.horizon) || 86400;
      const bar = cd.closest(".nx").querySelector(".bar");
      bar.style.setProperty("--p", String(Math.max(0.02, Math.min(1, 1 - left / h))));
    }
    if (nowMeta.dataset.start && nowRun) {
      nowMeta.textContent = "running " + clock((Date.now() - Number(nowMeta.dataset.start)) / 1000);
    }
    const day = dayLabel(new Date());
    if (todayMeta.textContent !== day) todayMeta.textContent = day; // after midnight
  }

  // ----------------------------------------------------------- today
  function timelineTitle(title) {
    const [j, s] = String(title).split(" ");
    const info = JOBS[jobKey(j)];
    const name = info ? { process_dm: "DMs", process_page: "Dashboard", feedback: "Feedback", todo: "Next Todo",
      insights: "Insights", decks: "Deck", weekly: "Weekly report", slack_check: "Slack check",
      history_import: "History import", enrich: "History labels" }[jobKey(j)] || humanize(j) : humanize(j);
    return `${name} · ${stepInfo(s)[0]}`;
  }

  let todaySig = null, todayFirst = true;
  function paintToday(live) {
    const items = ((live.data && live.data.today) || []).slice().reverse(); // oldest first, like a day reads
    const shown = items.slice(-9);
    const staleOf = (t) => { const at = parseTime(t.at); return t.ok === null && (!at || Date.now() - at.getTime() > STALE_MS); };
    const sig = JSON.stringify([items.length, shown.map((t) => [t.title, t.at, t.ok, t.detail, t.icon, staleOf(t)])]);
    if (sig === todaySig) return;
    todaySig = sig;
    const enter = todayFirst ? " enter" : "";
    todayFirst = false;
    if (!items.length) {
      clear(today.body, el("div", { class: "empty-mini" }, icon("sunrise"), el("span", { text: "No activity yet today." })));
      return;
    }
    const tl = el("ol", { class: "tl" });
    if (items.length > shown.length) tl.appendChild(el("li", { class: "ev more" + enter, text: `${items.length - shown.length} earlier` }));
    for (const t of shown) {
      const at = parseTime(t.at);
      const stale = staleOf(t); // open, but never finished
      const st = t.ok === true ? "ok" : t.ok === false ? "w" : stale ? "stale" : "run";
      tl.appendChild(el("li", { class: "ev " + st + enter },
        el("div", { class: "h" },
          el("b", {}, icon(t.ok === false ? "warn" : t.icon === "deck" ? "palette" : t.icon === "model" ? "spark" : "gear"),
            el("span", { text: timelineTitle(t.title) })),
          el("em", { class: "num", text: at ? hhmm(at) : "" })),
        el("p", { text: t.ok === null ? (stale ? "no end recorded" : "running now…")
          : t.ok === false ? "failed" + (t.detail ? " · " + t.detail : "") : (t.detail || "done") })));
    }
    clear(today.body, tl);
  }

  // ----------------------------------------------------------- log
  let filter = "all";
  const seenRows = new Set();
  let firstLog = true;
  for (const [k, label] of LOG_FILTERS) {
    filters.appendChild(el("button", {
      class: "f", text: label, attrs: { type: "button", "aria-pressed": String(k === filter) }, data: { f: k },
      on: { click: () => { filter = k; firstLog = true; logSig = null; seenRows.clear(); paintFilters(); loadLog(); } },
    }));
  }
  function paintFilters() {
    for (const b of filters.children) b.setAttribute("aria-pressed", String(b.dataset.f === filter));
  }
  const logBox = el("div", { class: "tail", attrs: { role: "log", "aria-live": "off", "aria-label": "Latest log lines" } });
  log.body.appendChild(logBox);

  let logSig = null;
  async function loadLog() {
    const asked = filter;
    let rows;
    try { rows = await api(`/api/logs?filter=${asked}`); } catch (e) {
      if (logSig === null) {
        clear(logBox, el("div", { class: "load-error" }, icon("warn"), el("span", { text: "Couldn't load the log. Trying again…" })));
      }
      return;
    }
    if (!Array.isArray(rows) || asked !== filter) return;
    const recent = rows.slice(-LOG_SHOW).reverse(); // newest first
    const sig = asked + JSON.stringify(recent.map((r) => [r.at, r.source, r.text, r.level]));
    if (sig === logSig) return;
    logSig = sig;
    if (!recent.length) {
      clear(logBox, el("div", { class: "empty-mini" }, icon("doc"), el("span", { text: filter === "all" ? "No log lines yet." : "Nothing matches this filter." })));
      return;
    }
    const nodes = recent.map((r, i) => {
      const key = `${r.at}|${r.source}|${r.text}`;
      const fresh = !seenRows.has(key);
      seenRows.add(key);
      const at = parseTime(r.at);
      const lvl = r.level === "ERROR" || r.level === "CRITICAL" ? "bad" : r.level === "WARNING" ? "w" : /deck/i.test(r.source) ? "d" : "i";
      return el("div", { class: fresh ? "row new" : "row", vars: fresh ? { "--d": `${(firstLog ? i : 0) * 40}ms` } : {} },
        el("span", { class: "ts num", text: at ? hhmmss(at) : "" }),
        el("span", { class: "tag " + lvl }, el("bdi", { text: r.source })),
        el("span", { class: "msg" }, el("bdi", { text: r.text })));
    });
    clear(logBox, ...nodes);
    firstLog = false;
    if (seenRows.size > 2000) seenRows.clear();
  }

  // ----------------------------------------------------------- wiring
  const paint = (live) => { paintNow(live); paintUpcoming(live); paintToday(live); };
  if (ctx.live.data) paint(ctx.live);
  offs.push(on("live", paint));
  offs.push(on("event", (e) => {
    loadLogSoon();
    const s = parseTime(e.started), en = parseTime(e.ended);
    if (!s) return;
    runSteps.delete(`${jobKey(e.job)} ${e.step}`); // re-insert: the Map's order stays oldest first for the cap below
    runSteps.set(`${jobKey(e.job)} ${e.step}`, { at: s.getTime(), ok: e.ok, secs: en ? (en - s) / 1000 : null });
    if (runSteps.size > 100) runSteps.delete(runSteps.keys().next().value);
  }));
  let logSoon = null;
  function loadLogSoon() {
    if (logSoon) return;
    logSoon = window.setTimeout(() => { logSoon = null; loadLog(); }, 600);
  }
  loadLog();
  timers.push(window.setInterval(tickCountdowns, 1000));
  timers.push(window.setInterval(loadLog, 10000));
  const unCompose = mountComposer(composeBox);
  const unInbox = mountInbox(inboxList, inboxCount);
  const unTeam = mountTeamPosts(teamBox);
  const unRuns = mountRuns(runs, pausedBanner);

  return () => {
    offs.forEach((f) => f());
    timers.forEach((t) => window.clearInterval(t));
    if (logSoon) window.clearTimeout(logSoon);
    unCompose();
    unInbox();
    unTeam();
    unRuns();
  };
}
