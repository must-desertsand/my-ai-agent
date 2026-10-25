// "Team posts (last 7 days)": the teammates' top-level posts from the stored channel history, newest first, each with
// its feedback state and a "Draft feedback" button where there is no draft yet (feedbackbtn.js). It sits under the
// drafts in "Waiting for you": the drafts there are the agent's offers, this list is where Haseeb asks for one.
// Slack text is untrusted: it reaches the DOM as text only, inside <bdi>, never in an attribute; the one link is a
// checked Slack permalink (dom.cleanUrl).
import { api } from "./client.js";
import { cleanUrl, clear, el, extLink, whenLabel } from "./dom.js";
import { feedbackControl } from "./feedbackbtn.js";
import { jobInfo, onJobs } from "./jobs.js";

const REFRESH_MS = 60000;
const RETRY_MS = 5000;
const SOON_MS = 800;
const SHOW = 6; // rows shown before "Show all"
const COLORS = 12; // the API's person palette: `color` is 0–11
const LINE_MAX = 220;

function icon(name) { return el("span", { class: "ico i-" + name, attrs: { "aria-hidden": "true" } }); }

function initials(name) {
  const words = String(name || "").replace(/[^\p{L}\p{N}\s]/gu, " ").trim().split(/\s+/).filter(Boolean);
  if (!words.length) return "?";
  const first = [...words[0]];
  return (words.length > 1 ? first[0] + [...words[1]][0] : first.slice(0, 2).join("")).toUpperCase();
}

/** A post's opening as one line of plain words: Slack's own markup becomes words, nothing becomes a link. */
function opening(text) {
  const first = String(text ?? "").slice(0, 600).split("\n").map((l) => l.trim()).find((l) => l) || "";
  const plain = first
    .replace(/<@[UW][A-Z0-9]+>/g, "@someone")
    .replace(/<![a-z]+(?:\^[^<>|]*)?>/g, "")
    .replace(/<#C[A-Z0-9]+\|([^<>]*)>/g, "#$1")
    .replace(/<(?:https?|mailto):[^<>|\s]+\|([^<>]*)>/g, "$1")
    .replace(/<((?:https?|mailto):[^<>|\s]+)>/g, "$1")
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
  return plain.length > LINE_MAX ? plain.slice(0, LINE_MAX - 1) + "…" : plain;
}

export function mountTeamPosts(box) {
  const timers = [];
  const offs = [];
  let dead = false, loaded = false, soonT = null, soonAt = 0, retryT = null, all = false, sig = null;
  let posts = [];
  let wasDrafting = false;

  const count = el("span", { class: "team-count num" });
  const head = el("div", { class: "team-head" }, el("h3", { class: "team-h", text: "Team posts (last 7 days)" }), count);
  const body = el("div", { class: "team-body", attrs: { "aria-busy": "true" } },
    el("div", { class: "skeleton", attrs: { "aria-hidden": "true" } }, el("i"), el("i")));
  clear(box, head, body);

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
    try { list = await api("/api/teamposts"); } catch (e) {
      if (dead) return;
      retryT = window.setTimeout(load, RETRY_MS);
      if (!loaded) {
        body.setAttribute("aria-busy", "false");
        clear(body, el("div", { class: "load-error", attrs: { role: "status" } }, icon("warn"),
          el("span", { text: "Couldn't load the team posts. Trying again…" })));
      }
      return;
    }
    if (dead || !Array.isArray(list)) return;
    loaded = true;
    posts = list.filter((p) => p && typeof p.ts === "string" && /^\d+(\.\d+)?$/.test(p.ts));
    paint();
  }

  function paint() {
    const left = posts.filter((p) => p.feedback === "none").length;
    count.textContent = posts.length ? `${posts.length} · ${left} without a draft` : "";
    const next = JSON.stringify([all, posts.map((p) => [p.ts, p.user_name, p.color, p.text, p.feedback, p.permalink])]);
    body.setAttribute("aria-busy", "false");
    if (next === sig) return; // unchanged: the rows (and the focus on one) stay
    sig = next;
    const active = document.activeElement;
    const focusTs = active instanceof Element && body.contains(active) ? (active.closest(".team-row") || {}).dataset?.ts : null;
    if (!posts.length) {
      clear(body, el("div", { class: "team-empty" }, icon("users"),
        el("span", { text: "No teammate posts in the last 7 days yet." }),
        el("span", { class: "muted", text: " Import the team history below to fill this list." })));
      return;
    }
    const shown = all ? posts : posts.slice(0, SHOW);
    const rows = shown.map(row);
    let more = null;
    if (posts.length > SHOW) {
      more = el("button", { class: "linkish team-more", attrs: { type: "button", "aria-expanded": String(all) },
        text: all ? "Show fewer" : `Show all ${posts.length}`,
        on: { click: () => { all = !all; sig = null; paint(); } } });
    }
    clear(body, el("ul", { class: "team-list" }, rows), more);
    if (focusTs) {
      const again = body.querySelector(`.team-row[data-ts="${CSS.escape(focusTs)}"] .fbk-btn:not([hidden])`);
      if (again) again.focus({ preventScroll: true });
    }
  }

  function row(p) {
    const c = Number.isInteger(p.color) && p.color >= 0 && p.color < COLORS ? p.color : 0;
    const when = new Date(Number.parseFloat(p.ts) * 1000);
    const href = cleanUrl(p.permalink);
    const ctl = feedbackControl(p, { onChange: () => loadSoon(SOON_MS) });
    return el("li", { class: "team-row", data: { ts: p.ts } },
      el("span", { class: "team-av pc-" + c, text: initials(p.user_name), attrs: { "aria-hidden": "true" } }),
      el("div", { class: "team-main" },
        el("div", { class: "team-top" },
          el("b", { class: "who" }, el("bdi", { text: p.user_name || "Someone" })),
          Number.isNaN(when.getTime()) ? null : el("span", { class: "num muted", text: whenLabel(when) })),
        el("p", { class: "team-text" }, el("bdi", { text: opening(p.text) || "(no text)" })),
        el("div", { class: "team-tools" },
          href ? extLink(href, "Open in Slack ↗", "slack-link") : null,
          ctl ? ctl.node : null)));
  }

  // a draft that was running has ended: the list changes (its post has a draft now)
  offs.push(onJobs(() => {
    const running = !!(jobInfo("feedback") || {}).running;
    if (wasDrafting && !running) loadSoon(SOON_MS);
    wasDrafting = running;
  }));
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
