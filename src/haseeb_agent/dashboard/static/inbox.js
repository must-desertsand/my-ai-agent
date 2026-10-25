// "Waiting for you": the drafts in Haseeb's DM that he can still act on. Every action goes through actions.js (POST
// /api/action with the card's shown_hash; the tick runs it through the DM command path), and the card follows its
// command sending → queued → 👀 → ✅/⚠️. The buttons stay disabled while a command is in flight, across page changes
// (actions.js) and reloads (the server's `pending`), so a double click or a second visit can't send twice. A post
// that may have gone out leaves a banner until the item is posted, and posting again takes an explicit "I checked".
// All Slack text is shown inside <bdi>, as text. A `message` card (the composer's polished draft, or a `say:` from the
// DM) also shows what the polish changed and, folded away, Haseeb's own words; its Post makes a new message in the
// channel, and says so when the message really notifies people (@channel, @here). An insight whose `format` is "idea" (a
// v2 body: a daily AI business idea) says "Idea", with its header "title · price" for the heading; a v1 insight, stored
// before ideas, keeps "Insight" and its title. An insight also shows the deck that
// goes with it (deckstrip.js, from the item's `deck`), and its Post confirm says when a post made now would not carry
// the deck (the deck of the CURRENT text is not in his DM yet, R50). The list is grouped by day, newest day first, a
// header per day ("Today · Wed 21 Oct", and how many wait) that sticks while its day scrolls; the drafts are placed by the
// `order` key the API gives each of them (newest first), so one that arrives live lands in its group and an empty day goes.
// An idea, an insight and a message also have a "Post to" choice (postto.js): the leadership channel, as the default, or another of
// his channels; the Post confirm names the channel picked and Confirm sends its id. Feedback and the Next Todo have none.
import { api, on } from "./client.js";
import { COMPOSE_KEY, act, adopt, inFlight, watch } from "./actions.js";
import { isPaused, onJobs } from "./jobs.js";
import { cleanUrl, clear, countdown, el, extLink, parseTime, whenLabel } from "./dom.js";
import { buildDeckStrip } from "./deckstrip.js";
import { deckNoteText } from "./deckview.js";
import { CHANNEL_KINDS, buildPostTo, createChannelList } from "./postto.js";

const INSTRUCTION_MAX = 500; // D14: Edit sends an instruction to revise, like the DM's `edit:` (the runner's cap)
const REFRESH_MS = 30000;
const RETRY_MS = 5000;
const AFTER_SETTLE_MS = 1500; // a settled command changed the item: look again soon
const AFTER_COMPOSE_MS = 300; // a Polish that ended made the item before it said so: its card is there to be shown
const EVENT_MS = 2000; // a deck step on the stream: a deck may be about to land (its state is written just after the step)
const LINGER_MS = 2500; // a posted card stays this long with its link, then leaves (and it's out of the count at once)
const CLAMP_AT = 320;
const KIND = {
  insight: { label: "Insight", icon: "bulb" },
  feedback: { label: "Feedback", icon: "chat" },
  todo: { label: "Next Todo", icon: "todo" },
  message: { label: "Message", icon: "pen" },
};
// a feedback item that answers a teammate's reply in a thread (the thread watch): its own label on the card
const VARIANT_KIND = {
  followback: { label: "Follow-back", icon: "chat" },
  insight_reply: { label: "Reply", icon: "chat" },
};
// an insight item whose body is a v2 idea is still kind "insight"; its `format` says "idea" and the card's label follows
const IDEA = { label: "Idea", icon: "bulb" };
const VERB_LABEL = { post: "Post", edit: "Revise", shorter: "Shorter", skip: "Skip", cancel: "Cancel auto-post",
  attach: "Attach", compose: "Polish" };
// who a mass mention notifies; `where` is the channel's name ("#survive-ai") when the post goes to a named channel, else "" (the channel)
const PING_WORDS = { channel: (where) => `everyone in ${where || "the channel"} (@channel)`,
  here: (where) => `everyone who is online${where ? ` in ${where}` : ""} (@here)` };
const GLYPH = { running: "👀", done: "✅", failed: "⚠️" };
export const UNSURE_BANNER = "⚠️ Post may have gone out — check the channel before posting again";
/** Where to check: the channel the draft's post was recorded to before it was sent, as the server says it ("#survive-ai", "the
 * leadership channel"; a channel is named only by the server, never taken from the page). "the channel" when none is recorded. */
function whereWords(where) { return typeof where === "string" && where ? where : "the channel"; }
const LINK_RE = /https:\/\/\S+/;
const GONE_OUT = /may have gone out/i; // the runner's wording for a post that may have reached the channel (C1)
const REASON = /\(([^()]+)\)\s*$/; // its reason, at the end in brackets: "(SlackTimeout)"

function icon(name) { return el("span", { class: "ico i-" + name, attrs: { "aria-hidden": "true" } }); }

/** The mass mentions a message really sends, as the API names them (`channel`, `here`); only the ones this page knows. */
function pingKeys(pings) { return (Array.isArray(pings) ? pings : []).filter((p) => Object.hasOwn(PING_WORDS, p)); }

/** "everyone in the channel (@channel)" for those mentions, or "everyone in #survive-ai (@channel)" with a `where`; "" for none. */
function pingWords(keys, where = "") { return keys.map((k) => PING_WORDS[k](where)).join(" and "); }

/** `s` without the trailing characters in `chars`: a plain loop, linear on any input (a regex like /[…]+$/ can
 * backtrack quadratically on a long run that doesn't end the string). */
function trimTail(s, chars) {
  let end = s.length;
  while (end > 0 && chars.includes(s[end - 1])) end -= 1;
  return s.slice(0, end);
}

/** The day of a draft as the API gave it ("2026-10-21"); "" for one that has none. */
function dayOf(it) { return typeof it.day === "string" && /^\d{4}-\d{2}-\d{2}$/.test(it.day) ? it.day : ""; }

function skeleton() { return el("div", { class: "skeleton", attrs: { "aria-hidden": "true" } }, el("i"), el("i"), el("i")); }

/** A card's heading and its text. An insight's text starts with its own title, which the heading shows, so the text
 * starts after it. An idea's text starts with its header, "title · price" (the DM's header): that is its heading. */
function headAndText(it, kind, idea) {
  const text = it.body || "";
  const end = idea ? text.indexOf("\n\n") : -1;
  if (end > 0) return { title: text.slice(0, end), shown: text.slice(end + 2) };
  const title = it.title || kind.label;
  return { title, shown: text.startsWith(title + "\n\n") ? text.slice(title.length + 2) : text };
}

/** What the buttons of a card depend on: a change repaints them. While the agent is paused none of them works
 * (the server refuses every click with 409), so they are disabled and the banner above says why. */
function actionKey(st) { return `${!!inFlight(st.id)}|${!!st.autopost}|${st.closed}|${isPaused()}`; }

export function mountInbox(body, countBadge) {
  const cards = new Map(); // item_id → card state
  const groups = new Map(); // day → {day, node, title, count, body}: the day's header and where its drafts sit
  const timers = [];
  let list = el("div", { class: "inbox-list" });
  let loaded = false, dead = false, soon = null, soonAt = 0;
  // his channels, for the "Post to" choice: one list for every card; when it changes each card looks again (and the warning about
  // who a message notifies follows the channel it would go to)
  const channelList = createChannelList(() => {
    for (const c of cards.values()) { if (c.postTo) { c.postTo.repaint(); paintPings(c); } }
  });
  clear(body, skeleton());

  function loadSoon(ms) {
    const at = Date.now() + ms;
    if (soon && soonAt <= at) return;
    if (soon) window.clearTimeout(soon);
    soonAt = at;
    soon = window.setTimeout(() => { soon = null; load(); }, ms);
  }

  async function load() {
    if (dead) return;
    let items;
    try { items = await api("/api/inbox"); } catch (e) { if (!dead) loadFailed(); return; }
    if (dead || !Array.isArray(items)) return;
    if (items.some((it) => CHANNEL_KINDS.includes(it.kind))) {
      // the first paint waits a moment for the list, so the choice never flashes from one option to many; after that it loads beside
      if (channelList.ready()) {
        channelList.load();
      } else {
        let waiting = null;
        await Promise.race([channelList.load(), new Promise((r) => { waiting = window.setTimeout(r, 2000); })]);
        window.clearTimeout(waiting);
      }
      if (dead) return;
    }
    loaded = true;
    const keep = new Set();
    for (const it of items) {
      keep.add(it.item_id);
      if (it.pending) adopt(it.item_id, it.pending);
      const c = cards.get(it.item_id);
      // same draft, or a card in use: update it in place, so a notice, an open editor or a confirm stays put
      if (c && (c.hash === it.shown_hash || inFlight(c.id) || c.editing || c.confirming)) { sync(c, it); continue; }
      const fresh = buildCard(it, c);
      if (c) { dispose(c); c.node.replaceWith(fresh.node); } // a new card is placed by arrange(), in paintFrame
      cards.set(it.item_id, fresh);
    }
    let leaving = 0;
    for (const [id, c] of cards) {
      if (keep.has(id) || inFlight(id)) continue;
      if (c.lingerUntil && Date.now() < c.lingerUntil) { loadSoon(c.lingerUntil - Date.now() + 100); continue; }
      cards.delete(id);
      c.unwatch();
      leaving += 1;
      c.node.classList.add("leaving");
      window.setTimeout(() => { // its picture stays while it fades; then its day may have nothing left
        c.node.remove();
        if (c.strip) c.strip.destroy();
        if (!dead) arrange();
      }, 380);
    }
    if (leaving && cards.size === 0) window.setTimeout(paintFrame, 400); else paintFrame();
  }

  /** A card that is replaced by a newer one (or at the end): its command watch and its deck strip's picture go. */
  function dispose(c) {
    c.unwatch();
    if (c.strip) c.strip.destroy();
  }

  function loadFailed() {
    loadSoon(RETRY_MS);
    if (loaded) return; // the cards on screen stay; the next try refreshes them
    clear(body, el("div", { class: "load-error", attrs: { role: "status" } }, icon("warn"),
      el("span", { text: "Couldn't load your drafts. Trying again…" })));
  }

  function paintFrame() {
    const open = [...cards.values()].filter((c) => !c.closed).length; // a posted card on its way out isn't waiting
    countBadge.textContent = open ? String(open) : "";
    countBadge.classList.toggle("on", open > 0);
    if (!loaded) return;
    if (cards.size === 0) {
      clear(body, el("div", { class: "empty" },
        el("div", { class: "empty-orb", attrs: { "aria-hidden": "true" } }, icon("check")),
        el("div", { class: "empty-t", text: "Nothing waiting for you" }),
        el("div", { class: "empty-s", text: "New drafts land here and in your DM. You're all caught up." })));
      list = el("div", { class: "inbox-list" });
      groups.clear();
    } else {
      arrange();
      if (list.parentNode !== body) clear(body, list);
    }
  }

  // ---------------------------------------------------------------- the days

  function buildGroup(day) {
    const id = "inbox-day-" + (day || "other");
    const title = el("span", { class: "inbox-day-t" });
    const count = el("span", { class: "inbox-day-n num" });
    const head = el("h3", { class: "inbox-day-h", attrs: { id } }, title,
      el("span", { class: "inbox-day-rule", attrs: { "aria-hidden": "true" } }), count);
    const cardsBox = el("div", { class: "inbox-day-cards" });
    const node = el("div", { class: "inbox-day", data: { day }, attrs: { role: "group", "aria-labelledby": id } },
      head, cardsBox);
    return { day, node, title, count, body: cardsBox };
  }

  /** `nodes` as the children of `parent`, in this order, moving only the ones that are out of place (a card that is
   * moved loses its focus and plays its entrance again). A child that is not in `nodes`, a card fading out, stays. */
  function place(parent, nodes) {
    const want = new Set(nodes);
    let ref = parent.firstChild;
    for (const n of nodes) {
      while (ref && ref !== n && !want.has(ref)) ref = ref.nextSibling;
      if (ref === n) ref = ref.nextSibling; else parent.insertBefore(n, ref);
    }
  }

  /** Put every draft where it belongs: a group per day, the newest day first, under a header with the day and how many
   * wait; inside a day the newest draft first. All by the `order` key the API gave each draft (it sorts as a plain
   * string), so a draft that arrives live lands in its own group, a group with nothing left goes, and a draft that is
   * already in place is not touched. */
  function arrange() {
    const live = [...cards.values()].sort((a, b) => (a.order < b.order ? 1 : a.order > b.order ? -1 : 0));
    const days = new Map();
    for (const c of live) {
      if (!days.has(c.day)) days.set(c.day, []);
      days.get(c.day).push(c);
    }
    const nodes = [];
    for (const [day, cs] of days) {
      let g = groups.get(day);
      if (!g) { g = buildGroup(day); groups.set(day, g); }
      const label = cs.map((c) => c.dayLabel).find(Boolean) || "Waiting";
      if (g.title.textContent !== label) g.title.textContent = label;
      const waiting = `${cs.filter((c) => !c.closed).length} waiting`; // a posted card on its way out isn't waiting
      if (g.count.textContent !== waiting) g.count.textContent = waiting;
      place(g.body, cs.map((c) => c.node));
      nodes.push(g.node);
    }
    place(list, nodes);
    for (const [day, g] of groups) {
      if (days.has(day) || g.body.firstChild) continue; // a day whose last card is still fading out stays till it is gone
      g.node.remove();
      groups.delete(day);
    }
  }

  // ---------------------------------------------------------------- a card

  function buildCard(it, prev) {
    const idea = it.kind === "insight" && it.format === "idea"; // a v2 insight; a v1 one keeps its old name
    const kind = VARIANT_KIND[it.variant] || (idea ? IDEA : KIND[it.kind]) || { label: it.kind, icon: "doc" };
    const { title, shown } = headAndText(it, kind, idea);
    const st = { id: it.item_id, kind: it.kind, hash: it.shown_hash, status: it.status, autopost: it.autopost_at || null,
      unsure: !!it.post_may_have_gone_out, where: "", editing: false, confirming: false, closed: false, notice: null,
      lingerUntil: 0, lastEdit: "", actionsKey: "", pingKeys: pingKeys(it.pings), pingText: null, deck: null, deckNote: null,
      confirmBtns: [], day: dayOf(it), order: "", dayLabel: "" };
    // an idea, an insight or a message takes a channel; feedback and the Next Todo post only to their thread in the leadership channel
    st.postTo = CHANNEL_KINDS.includes(it.kind)
      ? buildPostTo({ itemId: it.item_id, list: channelList, onChange: () => paintPings(st) }) : null;
    if (st.postTo && prev && prev.postTo) st.postTo.carry(prev.postTo.id()); // a revised draft keeps the channel he picked
    st.strip = it.kind === "insight" && it.deck ? buildDeckStrip(it.item_id) : null; // fed by sync() just below
    st.statusLine = el("div", { class: "cstatus", attrs: { role: "status", "aria-live": "polite" } });
    st.actions = el("div", { class: "actions" });
    st.banner = el("div", { class: "banner" });
    st.review = it.kind === "feedback" ? el("p", { class: "review-line", attrs: { hidden: true } }, icon("eye"),
      el("span")) : null; // what the site review looked at, as the DM card says it (sync() fills it)
    st.flag = el("span", { class: "flag bad", text: "needs a retry" });
    st.chipBox = el("span", { class: "chip-box" });
    const bodyText = el("p", { class: "draft" }, el("bdi", { text: shown || "(empty draft)" }));
    let more = null;
    if (shown.length > CLAMP_AT || shown.split("\n").length > 6) {
      bodyText.classList.add("clamped");
      more = el("button", { class: "linkish", text: "Show more", attrs: { type: "button", "aria-expanded": "false" },
        on: { click: () => {
          const open = bodyText.classList.toggle("clamped") === false;
          more.textContent = open ? "Show less" : "Show more";
          more.setAttribute("aria-expanded", String(open));
        } } });
    }
    st.showAll = () => { if (more && bodyText.classList.contains("clamped")) more.click(); };
    st.node = el("article", { class: ["icard", "k-" + st.kind].join(" "), data: { item: st.id },
      attrs: { "aria-label": `${kind.label}: ${title}`, tabindex: "-1" } },
      el("div", { class: "icard-top" },
        el("span", { class: "kind k-" + st.kind }, icon(kind.icon), el("span", { text: kind.label })),
        st.flag, st.chipBox),
      st.banner,
      el("h4", { class: "icard-title" }, el("bdi", { text: title })),
      st.review,
      bodyText, more, it.kind === "message" ? messageExtras(it, st) : null, st.strip ? st.strip.node : null,
      st.postTo ? st.postTo.node : null, st.actions, st.statusLine);
    // what the status line says before anything happens here: a fresh success carried over from the card this one
    // replaces (a revised draft), or the server's record of the failure the item is in
    if (prev && prev.notice && prev.notice.live && prev.notice.state === "done") st.notice = prev.notice;
    else st.notice = serverNotice(it);
    st.unwatch = watch(st.id, (u) => onCommand(st, u));
    sync(st, it);
    return st;
  }

  /** What a post would notify, in words, for the channel picked now; "" when it notifies nobody. */
  function pingsFor(st) {
    return pingWords(st.pingKeys, st.postTo ? st.postTo.pick().where : "");
  }

  /** The warning on a message card follows the channel picked: "everyone in #survive-ai (@channel)". */
  function paintPings(st) {
    if (!st.pingText) return;
    const text = `Posting this notifies ${pingsFor(st)}.`;
    if (st.pingText.textContent !== text) st.pingText.textContent = text;
  }

  /** A message draft's extras: a warning when posting it notifies people, what the polish changed, and his own words
   * (folded). The note and his words are for him; they are never part of the post. */
  function messageExtras(it, st) {
    const out = [];
    if (st.pingKeys.length) {
      st.pingText = el("span");
      out.push(el("p", { class: "ping-note", attrs: { role: "note" } }, icon("warn"), st.pingText));
      paintPings(st);
    }
    if (typeof it.note === "string" && it.note) {
      out.push(el("p", { class: "msg-note" }, el("span", { class: "msg-note-k", text: "What changed" }), " ",
        el("bdi", { text: it.note })));
    }
    if (typeof it.original === "string" && it.original) {
      out.push(el("details", { class: "msg-orig" }, el("summary", { text: "Your words" }),
        el("p", { class: "draft msg-orig-text" }, el("bdi", { text: it.original }))));
    }
    return out;
  }

  /** The site review's line under a feedback card's title: "Reviewed: host (desktop + phone)" or "Couldn't review host:
   * reason". Text only; a failure reads in the warning colour. */
  function paintReview(st, line) {
    st.review.hidden = !line;
    st.review.classList.toggle("bad", line.startsWith("Couldn't review") || line.includes(" · Couldn't review"));
    const span = st.review.lastChild;
    if (span.textContent !== line) span.textContent = line;
  }

  function serverNotice(it) {
    const r = it.last_result;
    if (it.status !== "failed" || !r || r.state !== "failed") return null;
    return { state: "failed", text: typeof r.result === "string" && r.result ? r.result : "Failed", live: false,
      verb: r.verb };
  }

  /** A failed post whose words say it may have reached the channel. */
  function goneOut(notice) {
    return !!notice && notice.state === "failed" && notice.verb === "post" && GONE_OUT.test(notice.text);
  }

  /** The single persistent warning for a post that may have gone out, with the runner's reason when it gave one. */
  function paintBanner(st) {
    st.banner.hidden = !st.unsure;
    const m = goneOut(st.notice) ? REASON.exec(st.notice.text) : null;
    const key = `${st.unsure}|${m ? m[1] : ""}|${st.where}`;
    if (st.banner.dataset.key === key) return;
    st.banner.dataset.key = key;
    const words = UNSURE_BANNER.replace("the channel", whereWords(st.where));
    clear(st.banner, el("span", {}, words, m ? [" (", el("bdi", { text: m[1] }), ")"] : null));
  }

  /** Bring a card up to date with the server's view of its item, in place. */
  function sync(st, it) {
    const was = st.unsure;
    st.status = it.status;
    if (st.review) paintReview(st, typeof it.review === "string" ? it.review : "");
    st.order = typeof it.order === "string" ? it.order : st.order; // where it sits among the drafts (see arrange)
    st.dayLabel = typeof it.day_label === "string" ? it.day_label : st.dayLabel; // "Today · …" turns into "Yesterday · …" at midnight
    st.deck = st.strip && it.deck ? it.deck : null; // the strip and the Post confirm follow the item's deck, in place
    if (st.deck) st.strip.update(st.deck);
    paintDeckNote(st);
    // the server's word, or a failure that just arrived and that it hasn't caught up with yet
    st.unsure = !!it.post_may_have_gone_out || (goneOut(st.notice) && st.notice.live);
    st.where = typeof it.post_channel === "string" ? it.post_channel : ""; // where it may have gone, said by the server
    if (st.postTo) { st.postTo.sync(it); paintPings(st); } // a recorded channel locks the choice to it
    paintBanner(st);
    st.flag.hidden = !(it.status === "failed" && !st.unsure); // the banner says more when a post may have gone out
    st.node.classList.toggle("is-failed", it.status === "failed");
    const auto = it.kind === "todo" ? (it.autopost_at || null) : null;
    if (auto !== st.autopost || !st.chipBox.firstChild !== !parseTime(auto)) {
      st.autopost = auto;
      const at = parseTime(auto);
      clear(st.chipBox, at ? el("span", { class: "auto-chip num", data: { at: String(at.getTime()) } }) : null);
      tickAuto(st);
    }
    if (!st.notice && !inFlight(st.id)) st.notice = serverNotice(it);
    paintBanner(st);
    paintStatus(st);
    if (st.confirming && was !== st.unsure) paintConfirm(st); // an open confirm row now needs the explicit check
    if (actionKey(st) !== st.actionsKey && !st.editing && !st.confirming) paintActions(st);
  }

  function onCommand(st, u) {
    const was = st.unsure;
    if (u.state === "done" || u.state === "failed") {
      st.notice = { state: u.state, text: u.result || (u.state === "done" ? "Done" : "Failed"), live: true,
        verb: u.verb };
      if (goneOut(st.notice)) st.unsure = true; // gated from this moment, not from the next reload
      if (u.state === "done" && (u.verb === "post" || u.verb === "skip")) {
        st.closed = true; // it has left the DM's open drafts: nothing more to do on this card
        if (u.verb === "post") st.lingerUntil = Date.now() + LINGER_MS; // keep its link on screen a moment
      }
      loadSoon(AFTER_SETTLE_MS);
      paintFrame();
    }
    paintBanner(st);
    paintStatus(st);
    if (st.confirming && was !== st.unsure) paintConfirm(st);
    if (!st.editing && !st.confirming) paintActions(st);
  }

  function button(label, ico, cls, fn) {
    return el("button", { class: ["btn", cls].join(" "), attrs: { type: "button" }, on: { click: fn } },
      ico ? icon(ico) : null, el("span", { text: label }));
  }

  /** The choice is off while a command is in flight, the card is closed, the agent is paused or an editor is open, and held while
   * the Confirm is open (what he confirms is what he picked). */
  function paintPostTo(st) {
    if (!st.postTo) return;
    st.postTo.set({ off: !!inFlight(st.id) || st.closed || isPaused() || st.editing, hold: st.confirming });
  }

  function paintActions(st) {
    const a = st.actions;
    st.actionsKey = actionKey(st);
    paintPostTo(st);
    if (st.confirming) { paintConfirm(st); return; }
    a.classList.remove("confirm", "has-note");
    st.deckNote = null;
    st.confirmBtns = [];
    if (st.editing) { paintEditor(st); return; }
    const isTodo = st.kind === "todo";
    const btns = [
      button(isTodo ? "Post now" : "Post", "send", "primary", () => { st.confirming = true; paintActions(st); }),
      button("Edit", "pen", "", () => { st.editing = true; paintActions(st); }),
      button("Shorter", "scissors", "", () => run(st, "shorter")),
      button("Skip", "skip", "ghost", () => run(st, "skip")),
    ];
    if (isTodo && st.autopost) btns.push(button("Cancel auto-post", "x", "ghost warn", () => run(st, "cancel")));
    clear(a, ...btns);
    const off = !!inFlight(st.id) || st.closed || isPaused();
    for (const b of btns) b.disabled = off;
  }

  function paintConfirm(st) {
    const a = st.actions;
    const pick = st.postTo ? st.postTo.pick() : null; // the channel he picked: the question names it and Confirm sends its id
    const where = pick ? pick.words : "the program channel";
    const finish = (go) => {
      st.confirming = false;
      if (go) run(st, "post", pick ? pick.arg : undefined); else paintActions(st);
      st.node.focus({ preventScroll: true }); // back to the card, never lost to <body>
    };
    const pings = pingsFor(st);
    // C1: after a post that may have gone out, a plain Confirm isn't enough
    const q = st.unsure ? `The last post may have gone out. Check ${whereWords(st.where)} first.`
      : st.kind === "message"
        ? `Post this as a new message in ${where}, as you?${pings ? ` It notifies ${pings}.` : ""}`
        : `Post to ${where} as you?`;
    const yes = button(st.unsure ? "I checked — post again" : "Confirm", "send", "primary", () => finish(true));
    const no = button("Cancel", null, "ghost", () => finish(false));
    for (const b of [yes, no]) b.addEventListener("keydown", (e) => { if (e.key === "Escape") finish(false); });
    // a deck that a post made now would not carry: the one line more, under the question (it follows the deck while open)
    st.deckNote = el("p", { class: "confirm-note", attrs: { role: "note", id: "deck-note-" + st.id.replace(/[^A-Za-z0-9_-]/g, "") } },
      icon("warn"), el("span"));
    st.confirmBtns = [no, yes];
    clear(a, el("span", { class: "confirm-q", text: q }), no, yes, st.deckNote);
    paintDeckNote(st);
    a.classList.add("confirm");
    window.setTimeout(() => no.focus(), 0); // Enter lands on Cancel unless he moves to the post button
  }

  /** The Post confirm's line about the deck: its words follow the item's deck, and it is read with the two buttons. */
  function paintDeckNote(st) {
    const box = st.deckNote;
    if (!box) return;
    const text = deckNoteText(st.deck);
    box.hidden = text === null;
    st.actions.classList.toggle("has-note", text !== null); // on a phone the line sits between the question and the buttons
    if (text !== null && box.lastChild.textContent !== text) box.lastChild.textContent = text;
    for (const b of st.confirmBtns) {
      if (text === null) b.removeAttribute("aria-describedby"); else b.setAttribute("aria-describedby", box.id);
    }
  }

  function paintEditor(st) {
    // D14: the box holds an instruction (empty to start); the draft itself stays readable above it
    st.showAll();
    const id = "edit-" + st.id.replace(/[^A-Za-z0-9_-]/g, "");
    const ta = el("textarea", { class: "editor", attrs: { id, maxlength: String(INSTRUCTION_MAX), rows: "3",
      placeholder: "Tell the agent what to change — e.g. 'lead with the deadline'", spellcheck: "true" } });
    ta.value = st.lastEdit || ""; // only an instruction whose sending failed comes back; .value is never parsed
    const counter = el("span", { class: "counter num" });
    const sendBtn = button("Revise", "pen", "primary", () => {
      const v = ta.value;
      if (!v.trim()) { ta.focus(); return; }
      st.editing = false;
      st.lastEdit = v;
      run(st, "edit", v.slice(0, INSTRUCTION_MAX));
    });
    const sync = () => {
      counter.textContent = `${ta.value.length} / ${INSTRUCTION_MAX}`;
      counter.classList.toggle("full", ta.value.length >= INSTRUCTION_MAX);
      sendBtn.disabled = !ta.value.trim();
    };
    const close = () => { st.editing = false; paintActions(st); st.node.focus({ preventScroll: true }); };
    ta.addEventListener("input", sync);
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Escape") close();
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) sendBtn.click();
    });
    sync();
    clear(st.actions, el("div", { class: "edit-box" },
      el("label", { class: "edit-hint", attrs: { for: id }, text: "Revise this draft. The new version lands here and in your DM." }),
      ta,
      el("div", { class: "edit-row" }, counter,
        el("span", { class: "grow" }),
        button("Cancel", null, "ghost", close),
        sendBtn)));
    window.setTimeout(() => { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }, 0);
  }

  function run(st, verb, arg) {
    if (inFlight(st.id) || st.closed) return; // the double-click guard (actions.js and the backend guard too)
    st.notice = null;
    act(st.id, verb, { arg, shownHash: st.hash });
    if (verb === "post" || verb === "edit") st.node.focus({ preventScroll: true });
  }

  // ---------------------------------------------------------------- the status line

  function paintStatus(st) {
    const cmd = inFlight(st.id);
    if (cmd) {
      const label = VERB_LABEL[cmd.verb] || cmd.verb;
      const text = cmd.state === "sending" ? `${label}: sending…`
        : cmd.state === "running" ? `${label}: working on it…` : `${label}: queued for the next tick…`;
      fill(st.statusLine, cmd.state === "sending" || cmd.state === "running" ? cmd.state : "queued", text);
    } else if (st.notice && !(st.unsure && goneOut(st.notice))) { // the banner already says that one
      fill(st.statusLine, st.notice.state, st.notice.text);
    } else if (st.statusLine.dataset.key !== "") {
      st.statusLine.dataset.key = "";
      clear(st.statusLine);
      st.statusLine.className = "cstatus";
    }
    st.node.classList.toggle("busy", !!cmd);
    st.node.setAttribute("aria-busy", String(!!cmd));
    st.node.classList.toggle("settled", !cmd && !!st.notice && st.notice.state === "done" && st.notice.live);
  }

  /** The state glyph, then the status text (the runner's own leading emoji gives way to the glyph). A Slack
   * permalink in it (✅ Posted · <link>) becomes a safe link; everything else stays text. */
  function fill(line, state, text) {
    const key = `${state}|${text}`;
    if (line.dataset.key === key) return; // the same words again: an aria-live region must not repeat them
    line.dataset.key = key;
    const glyph = GLYPH[state] || "";
    let t = String(text);
    if (glyph) t = t.replace(/^(?:\p{Extended_Pictographic}|\uFE0F|\u200D|\s)+/u, "") || String(text);
    const m = LINK_RE.exec(t);
    const href = m ? cleanUrl(trimTail(m[0], ").,;:!?")) : null;
    const parts = href
      ? [el("bdi", { text: t.slice(0, m.index).trimEnd() }), " ", extLink(href, "Open in Slack ↗", "slack-link"),
        el("bdi", { text: t.slice(m.index + m[0].length) })]
      : [el("bdi", { text: t })];
    clear(line,
      state === "queued" || state === "sending" ? el("span", { class: "spinner sm", attrs: { "aria-hidden": "true" } }) : null,
      glyph ? el("span", { class: "glyph", text: glyph, attrs: { "aria-hidden": "true" } }) : null,
      ...parts);
    line.className = "cstatus on s-" + state;
  }

  function tickAuto(st) {
    const chip = st.chipBox.firstChild;
    if (!chip) return;
    if (isPaused()) { // a paused agent posts nothing and its tick cancels the auto-post: no countdown to show
      chip.textContent = "Paused";
      chip.setAttribute("title", "The agent is paused, so the auto-post is cancelled.");
      return;
    }
    chip.removeAttribute("title");
    const at = Number(chip.dataset.at);
    const left = (at - Date.now()) / 1000;
    chip.textContent = left > 0 ? `Auto-posts in ${countdown(left)}` : `Auto-post due ${whenLabel(new Date(at))}`;
  }

  // a Polish from the composer ended: its draft exists now, so look for the card (the composer is compose.js)
  const unCompose = watch(COMPOSE_KEY, (u) => { if (u.state === "done") loadSoon(AFTER_COMPOSE_MS); });
  // a step of the deck job (the design, the upload) ended: a card's deck may have just changed, so look again soon
  const offEvents = on("event", (e) => { if (e && e.job === "decks") loadSoon(EVENT_MS); });
  timers.push(window.setInterval(() => { for (const c of cards.values()) tickAuto(c); }, 1000));
  timers.push(window.setInterval(load, REFRESH_MS));
  // Pause or Resume (from the header or another tab): the cards' buttons follow
  const offJobs = onJobs(() => {
    for (const c of cards.values()) {
      tickAuto(c); // "Paused" in place of the countdown, and back
      if (!c.editing && !c.confirming && actionKey(c) !== c.actionsKey) paintActions(c);
    }
  });
  load();
  return () => {
    dead = true;
    channelList.destroy();
    offJobs();
    unCompose();
    offEvents();
    timers.forEach((t) => window.clearInterval(t));
    if (soon) window.clearTimeout(soon);
    for (const c of cards.values()) dispose(c);
  };
}
