// The "Post to" choice on an idea's or a message's card: which of his channels the post goes to (post-channel design). The list
// comes from GET /api/channels (the leadership channel first, as the default, then his other channels A-Z; private ones are marked)
// and is shared by every card. "Refresh list" asks the tick for a new read (POST /api/channels/refresh, at most once in 10 minutes):
// the dashboard never calls Slack. A channel's name comes from Slack and is untrusted, so it is only ever put in the page as text,
// never as markup. The choice is off while a command is in flight, the card is closed, the agent is paused or the card's Confirm is
// open, and a card whose post may already have gone out is locked to the channel that post was recorded to: a retry goes only
// there, and the server refuses any other (a post that may have gone out with no channel recorded went to the leadership channel,
// where every post went before the choice existed, so that is where it is locked). What the card sends is `pick().arg`: the
// channel's id, or nothing for the leadership channel (none has always meant that). The card's Confirm posts, through actions.js;
// this module never acts on an item itself.
import { api, ApiError } from "./client.js";
import { PAUSED } from "./actions.js";
import { isPaused } from "./jobs.js";
import { ago, clear, el } from "./dom.js";

/** The kinds of draft that take a channel. Feedback and the Next Todo post only to their thread in the leadership channel. */
export const CHANNEL_KINDS = ["insight", "message"];

const ID_RE = /^[CG][A-Z0-9]{8,12}$/; // a channel id, as the server checks it (a DM or a user id is not one)
const NAME_MAX = 80; // the longest Slack channel name
const DEFAULT_WORDS = "the program channel"; // the leadership channel, when the list does not know its name
const POLL_MS = 3000; // after "Refresh list" (or a request already waiting): look again this often ...
const POLL_FOR_MS = 120000; // ... for this long; the inbox's own reload carries on after that
const FLASH_MS = 15000; // how long a note such as "Couldn't ask" stays

/** A channel as the page keeps it, or null when the row is not one. The name is only ever shown as text. */
function clean(row) {
  if (!row || typeof row !== "object" || typeof row.id !== "string" || !ID_RE.test(row.id)) return null;
  const name = typeof row.name === "string" && row.name ? row.name.slice(0, NAME_MAX) : null;
  return { id: row.id, name, private: row.private === true, leadership: row.leadership === true };
}

/** "#survive-ai"; "the program channel" for the leadership channel when its name is not known. */
export function channelWords(c) { return c && c.name ? `#${c.name}` : DEFAULT_WORDS; }

/** What an option says: the channel, then (default) for the leadership channel and (private) for a private one. */
function optionLabel(c) {
  const marks = [c.leadership ? "default" : "", c.private ? "private" : ""].filter(Boolean);
  return channelWords(c) + (marks.length ? ` (${marks.join(", ")})` : "");
}

/** The answer of /api/channels, or null when it is not one: the leadership channel must come first. */
function parseList(d) {
  if (!d || typeof d !== "object" || !Array.isArray(d.channels)) return null;
  const seen = new Set();
  const channels = d.channels.map(clean).filter((c) => c && !seen.has(c.id) && seen.add(c.id));
  if (!channels.length || !channels[0].leadership) return null;
  return { channels, ageS: Number.isFinite(d.age_s) && d.age_s >= 0 ? d.age_s : null, pending: d.refresh_pending === true };
}

/** The list the cards share: what the server last said, whether a refresh is asked for, and the call that asks. `onChange` runs
 * whenever the cards should look again. */
export function createChannelList(onChange) {
  const st = { data: null, at: 0, tried: false, busy: false, blockedUntil: 0, note: "", noteUntil: 0 };
  let timer = null, flashTimer = null, pollUntil = 0, dead = false, loading = null;

  function schedule() {
    window.clearTimeout(timer);
    timer = null;
    if (dead || !st.data || !st.data.pending || Date.now() > pollUntil) return;
    timer = window.setTimeout(load, POLL_MS);
  }

  /** One request at a time: a call while one is out shares it. A failed one keeps the list on screen. */
  function load() {
    if (loading) return loading;
    loading = (async () => {
      try {
        const d = parseList(await api("/api/channels"));
        if (d && !dead) {
          st.data = d;
          st.at = Date.now();
          if (!d.pending) pollUntil = 0; else if (!pollUntil) pollUntil = Date.now() + POLL_FOR_MS;
        }
      } catch (e) { /* the list on screen stays; the next load tries again */ }
      st.tried = true;
      loading = null;
      if (!dead) { onChange(); schedule(); }
    })();
    return loading;
  }

  function flash(text) {
    st.note = text;
    st.noteUntil = Date.now() + FLASH_MS;
    window.clearTimeout(flashTimer);
    flashTimer = window.setTimeout(() => { if (!dead) onChange(); }, FLASH_MS + 50);
  }

  async function ask() {
    if (st.busy || dead || isPaused()) return;
    st.busy = true;
    st.note = "";
    onChange();
    try {
      await api("/api/channels/refresh", { method: "POST", body: {} });
      if (st.data) st.data.pending = true;
      pollUntil = Date.now() + POLL_FOR_MS;
    } catch (e) {
      if (e instanceof ApiError && e.status === 429 && e.data && Number.isFinite(e.data.retry_after_s)) {
        st.blockedUntil = Date.now() + Math.max(1, e.data.retry_after_s) * 1000; // asked a moment ago: the server says when again
      } else if (e instanceof ApiError && e.status === 409 && e.message === "paused") {
        flash(PAUSED);
      } else {
        flash("Couldn't ask · try again");
      }
    }
    st.busy = false;
    onChange();
    await load(); // the server's word: is it waiting for the tick, and how old is the list
  }

  return {
    state: st,
    channels: () => (st.data ? st.data.channels : []),
    ready: () => st.tried,
    load,
    ask,
    destroy() { dead = true; window.clearTimeout(timer); window.clearTimeout(flashTimer); },
  };
}

/** How old the list is, in words; what to say when there is none. */
function ageText(list) {
  const s = list.state;
  if (!s.data) return s.tried ? "Channel list unavailable" : "Loading the channel list…";
  if (s.data.ageS === null) return "No channel list yet";
  const secs = s.data.ageS + (Date.now() - s.at) / 1000;
  return secs < 60 ? "List from just now" : `List from ${ago(secs)}`;
}

/**
 * One card's choice. `onChange` runs when the picked channel changes (the card's warning about who a message notifies follows it).
 * Returns {node, sync(it), repaint(), set({off, hold}), carry(id), id(), pick()}:
 *   sync     the server's view of the item: a recorded post channel locks the choice to it (so does a post that may have gone out
 *            with none recorded: it is locked to the leadership channel);
 *   repaint  the shared list changed (or its age did);
 *   set      off: nothing can be picked now; hold: the card's Confirm is open, so the options stay as they are;
 *   carry    take the channel picked on the card this one replaces;
 *   pick     what a post would send now: {arg, words, where}: `arg` the channel id or undefined for the leadership channel, `words`
 *            what the confirm says ("#survive-ai"), `where` the same when it is a name and "" when it is not.
 */
export function buildPostTo({ itemId, list, onChange }) {
  const id = "post-to-" + itemId.replace(/[^A-Za-z0-9_-]/g, "");
  const select = el("select", { class: "post-to-sel", attrs: { id } });
  const age = el("span", { class: "post-to-age" });
  const act = el("span", { class: "post-to-act", attrs: { "aria-live": "polite" } });
  const meta = el("span", { class: "post-to-meta" }, age, act);
  const note = el("p", { class: "post-to-note", attrs: { hidden: true, id: id + "-note" } });
  const node = el("div", { class: "post-to" }, el("label", { class: "post-to-l", attrs: { for: id }, text: "Post to" }),
    select, meta, note);
  let chosen = ""; // the id of the channel picked ("" is the leadership channel when the list is not known)
  let recorded = null; // {id, label}: the channel the draft's post was recorded to, as the server says it
  let unsureOld = false; // a post may have gone out and no channel is recorded: it went to the leadership channel
  let off = false, hold = false, shownKey = null, actKey = null;
  select.addEventListener("change", () => { chosen = select.value; onChange(); });

  /** {id, label} when a post of this draft may already be in a channel, so the post can go only there; else null. An id of "" is
   * the leadership channel before the list says which it is. */
  function lockedNow() {
    if (recorded) return recorded;
    if (!unsureOld) return null;
    const lead = list.channels().find((c) => c.leadership);
    return { id: lead ? lead.id : "", label: channelWords(lead) };
  }

  function entries() {
    const locked = lockedNow();
    if (locked) return [{ value: locked.id, label: locked.label }];
    const rows = list.channels();
    if (!rows.length) return [{ value: "", label: `${DEFAULT_WORDS} (default)` }]; // no list: the leadership channel, as before
    return rows.map((r) => ({ value: r.id, label: optionLabel(r) }));
  }

  /** The options, in place, only when they changed (an open menu is not rebuilt under him). True when the choice moved: the channel
   * picked is no longer in the list, so it falls back to the first, the leadership channel. */
  function paintOptions() {
    const es = entries();
    const key = es.map((e) => `${e.value}\n${e.label}`).join("\n\n");
    if (key !== shownKey) {
      shownKey = key;
      clear(select, es.map((e) => el("option", { text: e.label, attrs: { value: e.value } })));
    }
    const keep = es.some((e) => e.value === chosen) ? chosen : es[0].value;
    const moved = keep !== chosen;
    chosen = keep;
    if (select.value !== keep) select.value = keep;
    return moved;
  }

  function paintAct() {
    const s = list.state;
    const paused = isPaused();
    const blocked = s.blockedUntil - Date.now();
    let key, kids;
    if (s.busy) {
      key = "busy"; kids = ["Asking…"];
    } else if (s.data && s.data.pending) {
      key = "pending"; kids = ["Refresh asked · the next tick reads the list"];
    } else if (blocked > 0) {
      const m = Math.ceil(blocked / 60000);
      key = "blocked" + m; kids = [`Asked a moment ago · try again in ${m} min`];
    } else {
      const flash = s.noteUntil > Date.now() ? s.note : "";
      key = `link|${paused}|${flash}`;
      const attrs = paused ? { type: "button", disabled: true, title: PAUSED } : { type: "button" };
      kids = [flash ? `${flash} · ` : null, el("button", { class: "linkish", text: "Refresh list", attrs, on: { click: () => list.ask() } })];
    }
    if (key === actKey) return;
    actKey = key;
    clear(act, kids);
  }

  function paintMeta() {
    meta.hidden = !!lockedNow(); // a locked choice has no use for the list's age or its refresh
    const text = ageText(list);
    if (age.textContent !== text) age.textContent = text;
    paintAct();
  }

  function paintNote() {
    const locked = lockedNow();
    note.hidden = !locked;
    if (locked) select.setAttribute("aria-describedby", note.id); else select.removeAttribute("aria-describedby"); // why it is locked
    if (!locked) return;
    const text = `A post of this draft may already be in ${locked.label}, so it can only go there. If it isn't there, post it by hand.`;
    if (note.textContent !== text) note.textContent = text;
  }

  function paint() {
    const moved = paintOptions();
    select.disabled = off || hold || !!lockedNow();
    paintMeta();
    paintNote();
    return moved;
  }

  return {
    node,
    sync(it) {
      const rec = typeof it.post_channel_id === "string" && ID_RE.test(it.post_channel_id) ? it.post_channel_id : null;
      const label = typeof it.post_channel === "string" && it.post_channel ? it.post_channel.slice(0, NAME_MAX + 20) : rec;
      recorded = rec ? { id: rec, label } : null;
      unsureOld = !rec && it.post_may_have_gone_out === true;
      if (paint()) onChange();
    },
    repaint() {
      if (hold) { paintMeta(); return; } // the options stay as they are while his Confirm is open
      if (paint()) onChange();
    },
    set(next) {
      const was = hold;
      off = !!next.off;
      hold = !!next.hold;
      if (was && !hold) { if (paint()) onChange(); return; } // the list may have changed meanwhile
      select.disabled = off || hold || !!lockedNow();
      paintMeta();
    },
    carry(prevId) { if (prevId) { chosen = prevId; paint(); } },
    id: () => chosen,
    pick() {
      const locked = lockedNow();
      if (locked) {
        const lead = list.channels().find((c) => c.leadership);
        return { arg: !locked.id || (lead && lead.id === locked.id) ? undefined : locked.id, words: locked.label,
          where: locked.label.startsWith("#") ? locked.label : "" };
      }
      const row = list.channels().find((c) => c.id === chosen);
      const words = channelWords(row);
      return { arg: !row || row.leadership ? undefined : row.id, words, where: row && row.name ? words : "" };
    },
  };
}
