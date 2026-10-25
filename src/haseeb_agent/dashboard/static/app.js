// The app shell: login exchange, theme toggle, hash router, the status pills and the shared live data.
// Pages are modules that export mount(view, ctx) and return an unmount function.
import { api, authenticate, emit, isExpired, on, startStream } from "./client.js";
import { ago, clear, clock, el, hhmm, parseTime } from "./dom.js";
import { mountDecks } from "./decks.js";
import { mountHistory } from "./history.js";
import { explain, jobs, onJobs, refreshJobs, setPaused, startJobs } from "./jobs.js";
import { mountLive } from "./live.js";
import { mountLogs } from "./logs.js";
import { mountScore } from "./score.js";
import { mountUsage } from "./usage.js";

const TICK_EVERY_S = 60; // launchd runs the tick every 60 s
const LIVE_REFRESH_MS = 20000;

const PAGES = {
  live: { title: "Live", mount: mountLive },
  history: { title: "History", mount: mountHistory },
  decks: { title: "Decks", mount: mountDecks },
  logs: { title: "Logs", mount: mountLogs },
  score: { title: "Score", mount: mountScore },
  usage: { title: "Usage", mount: mountUsage },
};

const view = document.getElementById("view");
const pills = document.getElementById("pills");
let unmount = null;

// ------------------------------------------------------------------ theme

const root = document.documentElement;

function currentTheme() { return root.getAttribute("data-theme") === "daylight" ? "daylight" : "midnight"; }

function paintThemeButtons() {
  for (const b of document.querySelectorAll("[data-theme-pick]")) {
    b.setAttribute("aria-pressed", String(b.dataset.themePick === currentTheme()));
  }
}

function setTheme(t, remember) {
  // a click always pins the choice, even on the theme already showing: the OS setting no longer decides
  if (remember) { try { window.localStorage.setItem("ha_theme", t); } catch (e) { /* storage blocked */ } }
  if (t === currentTheme()) return;
  root.classList.add("theme-anim");
  root.setAttribute("data-theme", t);
  window.setTimeout(() => root.classList.remove("theme-anim"), 700);
  paintThemeButtons();
}

function initTheme() {
  paintThemeButtons();
  for (const b of document.querySelectorAll("[data-theme-pick]")) {
    b.addEventListener("click", () => setTheme(b.dataset.themePick, true));
  }
  try {
    window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", (e) => {
      let stored = null;
      try { stored = window.localStorage.getItem("ha_theme"); } catch (err) { stored = null; }
      if (!stored) setTheme(e.matches ? "daylight" : "midnight", false);
    });
  } catch (e) { /* old browser: no live follow */ }
}

// ------------------------------------------------------------------ shared live data

// the latest /api/live answer, when it arrived (client clock), and whether the last try failed
const live = { data: null, at: 0, offline: false };
let liveTimer = null, liveSoon = null;

// Screen readers hear changes of state only (a job starts or ends, the Mac sleeps or wakes), never the ticking clocks.
const announcer = document.getElementById("announce");
let lastSaid = null;

function announce(d) {
  const job = d.now ? `${d.now.job} ${d.now.step}` : null;
  const key = `${d.agent.mac_awake}|${job}`;
  if (lastSaid === null) { lastSaid = key; return; }
  if (key === lastSaid) return;
  lastSaid = key;
  announcer.textContent = !d.agent.mac_awake ? "The agent is idle: the Mac looks asleep."
    : job ? `Agent working: ${d.now.job.replace(/_/g, " ")}, step ${d.now.step.replace(/_/g, " ")}.` : "The agent is quiet.";
}

async function refreshLive() {
  try {
    live.data = await api("/api/live");
    live.at = Date.now();
    live.offline = false;
    emit("live", live);
    paintPills();
    announce(live.data);
  } catch (e) {
    if (isExpired()) return;
    live.offline = true; // sticks until an answer comes: the 1 s pill tick must not paint the old data back
    paintPills();
  }
}

function refreshLiveSoon() {
  if (liveSoon) return;
  liveSoon = window.setTimeout(() => { liveSoon = null; refreshLive(); refreshJobs(); }, 350);
}

// ------------------------------------------------------------------ pills

const pillNodes = {};

function pill(key, cls) {
  if (!pillNodes[key]) {
    pillNodes[key] = el("div", { class: "pill" });
    pills.appendChild(pillNodes[key]);
  }
  pillNodes[key].className = ["pill", cls].filter(Boolean).join(" ");
  return pillNodes[key];
}

function paintPills() {
  const d = live.data;
  if (live.offline || !d) {
    clear(pill("live", "off"), el("span", { class: "dot off" }), el("b", { text: "Offline" }), " dashboard unreachable");
    return;
  }
  const last = d.agent.last_tick ? Date.parse(d.agent.last_tick) : NaN;
  const awake = !!d.agent.mac_awake;
  const p = pill("live", awake ? "" : "off");
  if (awake) {
    let next = "soon";
    if (d.agent.running) next = "after this job";
    else if (!Number.isNaN(last)) {
      const left = TICK_EVERY_S - (Date.now() - last) / 1000;
      next = left > 0 ? clock(left) : "due";
    }
    clear(p, el("span", { class: "dot" }), el("b", { text: "Live" }), " next tick ", el("b", { class: "num", text: next }));
  } else {
    const seen = Number.isNaN(last) ? "never seen" : "last tick " + ago((Date.now() - last) / 1000);
    clear(p, el("span", { class: "dot off" }), el("b", { text: "Idle" }), " " + seen);
  }
  clear(pill("mac", awake ? "" : "warn"), el("span", { class: "ico i-mac", attrs: { "aria-hidden": "true" } }),
    "Mac ", el("b", { text: awake ? "awake" : "asleep" }));
  const gb = typeof d.disk_free_gb === "number" ? d.disk_free_gb : null;
  clear(pill("disk", gb !== null && gb < 2 ? "warn" : ""), el("span", { class: "ico i-disk", attrs: { "aria-hidden": "true" } }),
    el("b", { class: "num", text: gb === null ? "?" : `${gb.toFixed(1)} GB` }), " free");
}

// ------------------------------------------------------------------ pause (the kill switch, on every page)

const pauseBtn = document.getElementById("pause-btn");
let pausing = false;

function paintPause() {
  const d = jobs.data;
  const paused = !!(d && d.paused);
  // before the first answer there is no button: it must never offer the wrong action
  pauseBtn.hidden = !d || document.body.classList.contains("is-expired");
  pauseBtn.disabled = pausing;
  pauseBtn.classList.toggle("on", paused);
  pauseBtn.querySelector(".ico").className = "ico " + (paused ? "i-play" : "i-pause");
  pauseBtn.querySelector(".pause-lbl").textContent = paused ? "Resume" : "Pause";
  pauseBtn.title = paused ? "Let the agent send to Slack and run its jobs again" : "Stop every job and every Slack write now";
  if (paused) {
    const at = parseTime(d.paused_at);
    clear(pill("paused", "paused"), el("span", { class: "ico i-pause", attrs: { "aria-hidden": "true" } }),
      el("b", { text: "Paused" }), at ? " since " + hhmm(at) : "");
    pills.prepend(pillNodes.paused);
  } else if (pillNodes.paused) {
    pillNodes.paused.remove();
    delete pillNodes.paused;
  }
  const state = paused ? "paused" : "running";
  if (lastPause !== null && lastPause !== state) {
    announcer.textContent = paused ? "The agent is paused: nothing is sent and no job runs."
      : "The agent is running again.";
  }
  lastPause = state;
}
let lastPause = null;

pauseBtn.addEventListener("click", async () => {
  if (pausing) return;
  pausing = true;
  paintPause();
  const r = await setPaused(!(jobs.data && jobs.data.paused));
  pausing = false;
  if (!r.ok) announcer.textContent = explain(r);
  paintPause();
});

// ------------------------------------------------------------------ router

function route(ev) {
  const m = /^#\/([a-z]+)$/.exec(window.location.hash);
  const known = !!m && Object.hasOwn(PAGES, m[1]); // never a key from Object.prototype ("#/constructor")
  const name = known ? m[1] : "live";
  if (!known) {
    window.history.replaceState(null, "", window.location.pathname + window.location.search + "#/live");
  }
  for (const a of document.querySelectorAll(".nav a")) {
    const here = a.dataset.route === name;
    a.classList.toggle("on", here);
    if (here) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  moveGlider();
  revealActive();
  if (unmount) { try { unmount(); } catch (e) { /* a page's cleanup never blocks the next page */ } unmount = null; }
  const page = PAGES[name];
  document.title = `${page.title} · Haseeb's Agent`;
  clear(view);
  view.className = "page page-" + name;
  unmount = page.mount(view, { live });
  if (ev) view.focus({ preventScroll: true }); // a page change moves focus to the new content
}

function moveGlider() {
  const nav = document.querySelector(".nav");
  const on = nav && nav.querySelector("a.on");
  const glider = nav && nav.querySelector(".nav-glider");
  if (!on || !glider) return;
  glider.style.setProperty("--x", `${on.offsetLeft}px`);
  glider.style.setProperty("--w", `${on.offsetWidth}px`);
  glider.classList.add("ready");
}

/** On a phone too narrow for six tabs the nav scrolls sideways (its scrollbar is hidden): scroll it so the tab of the page
 *  being shown is whole inside it, with a little of its neighbour beside it. The nav is set directly, never the page. */
function revealActive() {
  const nav = document.querySelector(".nav");
  const on = nav && nav.querySelector("a.on");
  if (!on || nav.scrollWidth <= nav.clientWidth) return;
  const edge = 10;
  const left = on.offsetLeft - edge, right = on.offsetLeft + on.offsetWidth + edge;
  if (left < nav.scrollLeft) nav.scrollLeft = Math.max(0, left);
  else if (right > nav.scrollLeft + nav.clientWidth) nav.scrollLeft = right - nav.clientWidth;
}

// ------------------------------------------------------------------ session expired

function showExpired() {
  if (stopStream) { stopStream(); stopStream = null; }
  if (stopJobs) { stopJobs(); stopJobs = null; }
  pauseBtn.hidden = true;
  if (liveTimer) window.clearInterval(liveTimer);
  if (unmount) { try { unmount(); } catch (e) { /* ignore */ } unmount = null; }
  document.body.classList.add("is-expired");
  clear(pills);
  for (const k of Object.keys(pillNodes)) delete pillNodes[k];
  clear(view);
  view.className = "page page-expired";
  view.appendChild(el("section", { class: "card expired rise", attrs: { role: "alert" } },
    el("div", { class: "lock", attrs: { "aria-hidden": "true" } }, el("span", { class: "ico i-lock" })),
    el("h1", { text: "Session expired" }),
    el("p", {}, "Run ", el("code", { text: "haseeb-agent dashboard open" }), " in Terminal to sign in again."),
    el("p", { class: "muted small", text: "Sessions end when the dashboard restarts or after 12 hours idle. Nothing was lost." })));
}

// ------------------------------------------------------------------ boot

let stopStream = null, stopJobs = null;

function initSkipLink() {
  // a plain #view link would change the hash, and the router would take it for a page
  document.querySelector("a.skip").addEventListener("click", (e) => {
    e.preventDefault();
    view.focus();
  });
}

async function boot() {
  initTheme();
  initSkipLink();
  on("expired", showExpired);
  const ok = await authenticate();
  if (!ok) return;
  on("event", refreshLiveSoon);
  window.addEventListener("hashchange", route);
  window.addEventListener("resize", () => { moveGlider(); revealActive(); });
  route();
  await refreshLive();
  if (isExpired()) return;
  stopStream = startStream();
  stopJobs = startJobs();
  onJobs(paintPause);
  // a page put away (closed, or into the back/forward cache) drops its stream; a restored one opens it again
  window.addEventListener("pagehide", () => {
    if (stopStream) { stopStream(); stopStream = null; }
    if (stopJobs) { stopJobs(); stopJobs = null; }
  });
  window.addEventListener("pageshow", (e) => {
    if (e.persisted && !isExpired() && !stopStream) {
      stopStream = startStream();
      stopJobs = startJobs();
      refreshLive();
    }
  });
  liveTimer = window.setInterval(refreshLive, LIVE_REFRESH_MS);
  window.setInterval(() => { if (live.data && !isExpired()) paintPills(); }, 1000);
}

boot();
