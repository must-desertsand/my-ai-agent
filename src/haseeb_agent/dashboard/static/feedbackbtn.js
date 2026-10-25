// "Draft feedback" for a teammate's post: the button on the History page (in an item's toolbar) and in the Live
// page's Team posts list. A click asks for `haseeb-agent feedback --ts <ts>` (jobs.runJob); the agent drafts feedback
// with the same quality gate as its own scan and sends it to Haseeb's DM, where he posts, revises or skips it as for
// any draft. There is no confirm: a draft goes to his DM only. The control shows the post's feedback state instead of
// the button once one exists, is disabled while the agent is paused or another draft runs, and takes the server's
// fixed words for a refusal. It is built from the server's `feedback` field (null: not a teammate's post, so none).
import { el } from "./dom.js";
import { explain, isDrafting, isPaused, jobInfo, onJobs, runJob } from "./jobs.js";

const NOTE_MS = 6000;
// what a post's feedback item says once there is one (the item's status): its words and its icon
const STATE = {
  delivered: ["Draft in your DM", "chat"], drafted: ["Draft in your DM", "chat"], posted: ["Feedback posted", "check"],
  skipped: ["Draft skipped", "skip"], failed: ["Draft failed", "warn"], expired: ["Draft expired", "clock"],
};
const live = new Set(); // the controls on screen; one listener for all of them, pruned as their nodes go

function icon(name) { return el("span", { class: "ico i-" + name, attrs: { "aria-hidden": "true" } }); }

function paint(c) {
  const post = c.post;
  const s = post.feedback;
  const settled = typeof s === "string" && Object.hasOwn(STATE, s);
  const busy = c.sending || (!settled && (isDrafting(post.ts) || s === "drafting")); // an item settles it at once
  const fbRunning = !!(jobInfo("feedback") || {}).running;
  const paused = isPaused();
  c.chip.hidden = !settled || busy;
  if (settled) {
    c.chipText.textContent = STATE[s][0];
    c.chipIco.className = "ico i-" + STATE[s][1];
    if (c.compact) c.chip.setAttribute("title", STATE[s][0]); // the words are in the tooltip and for screen readers
  }
  c.btn.hidden = settled && !busy;
  const label = busy ? "Drafting…" : "Draft feedback";
  c.label.textContent = label;
  c.spin.hidden = !busy;
  c.ico.hidden = busy;
  c.btn.disabled = busy || paused || fbRunning;
  const why = busy ? "" : paused ? "Paused: resume the agent first" : fbRunning ? "Another draft is running" : "";
  const tip = why || (c.compact ? label : "");
  if (tip) c.btn.setAttribute("title", tip); else c.btn.removeAttribute("title");
  c.btn.setAttribute("aria-busy", String(busy));
  c.note.hidden = !c.noteText;
  c.note.textContent = c.noteText || "";
}

async function click(c) {
  if (c.sending || c.btn.disabled) return;
  c.sending = true;
  c.noteText = "";
  paint(c);
  const r = await runJob("feedback", c.post.ts);
  c.sending = false;
  if (r.ok) {
    c.post.feedback = "drafting"; // until the next look at the list says better
  } else {
    if (r.status === 409 && r.data && typeof r.data.state === "string") c.post.feedback = r.data.state; // it has one
    else {
      c.noteText = explain(r);
      window.clearTimeout(c.noteT);
      c.noteT = window.setTimeout(() => { c.noteText = ""; paint(c); }, NOTE_MS);
    }
  }
  paint(c);
  if (c.onChange) c.onChange(c.post, r);
}

/**
 * The control for one post, or null when the post cannot get a draft (`post.feedback` is null). `post` carries `ts`
 * and `feedback`; `onChange(post, result)` is told when a click settled. `compact` is for a toolbar with no room
 * for words (the History page's): the icon alone, the words in the tooltip and for screen readers. Returns
 * {node, button}: `button` is the one button, kept for the control's whole life (the History page gives its focus
 * back to it after a redraw).
 */
export function feedbackControl(post, { onChange, compact = false } = {}) {
  if (post.feedback === null || post.feedback === undefined) return null;
  const ico = icon("chat");
  const spin = el("span", { class: "spinner sm", attrs: { "aria-hidden": "true" } });
  const label = el("span", { class: compact ? "sr-only" : "", text: "Draft feedback" });
  const btn = el("button", { class: "fbk-btn", attrs: { type: "button" } }, ico, spin, label);
  const chipIco = icon("chat");
  const chipText = el("span", { class: compact ? "sr-only" : "" });
  const chip = el("span", { class: "fbk-chip" }, chipIco, chipText);
  const note = el("span", { class: "fbk-note", attrs: { role: "status" } });
  const node = el("span", { class: ["fbk", compact ? "compact" : ""].filter(Boolean).join(" ") }, btn, chip, note);
  const c = { post, node, btn, chip, chipIco, chipText, note, label, spin, ico, compact, sending: false, noteText: "",
    noteT: null, onChange, born: Date.now() };
  btn.addEventListener("click", () => click(c));
  live.add(c);
  paint(c);
  return { node, button: btn };
}

onJobs(() => {
  for (const c of [...live]) {
    // a control that was drawn over (or whose page went) is let go, once it has had time to be put on the page
    if (!c.node.isConnected && Date.now() - c.born > 3000) { live.delete(c); continue; }
    paint(c);
  }
});
