// What the Decks page and the deck strip on an insight's card share: the fact-check badge, how a deck's words are put
// together, and the three things that need the session header or the DOM, a deck's first-page thumbnail, its PDF and a
// note that goes away by itself. The thumbnail and the PDF are fetched with apiBlob (D11) and shown through blob: URLs,
// never a plain link, so the page's CSP needs nothing new. The caller owns the URLs it gets: a thumbnail's is revoked by
// whoever shows it, a PDF's a minute after its tab opened. Text is shown as text only.
import { apiBlob } from "./client.js";
import { clear, el } from "./dom.js";

const PDF_KEEP_MS = 60000; // the new tab has read the PDF long before this
const NOTE_MS = 6000;
const THUMBS_AT_ONCE = 2; // sips draws each preview once: a couple at a time keep a page responsive
export const OPEN_FAILED = "Couldn't open the PDF. Try again in a moment.";
export const OPEN_BLOCKED = "The browser blocked the new tab. Allow pop-ups for this page, then try again.";
// what the Post confirm adds on an insight whose deck is not ready (R50: a deck is attached to a post only when its DM
// upload came before the post; a deck that finishes later can be attached afterwards)
export const DECK_NOT_IN_DM = "The deck isn't in your DM yet: post now and it won't be attached (you can attach it later).";
export const EDIT_NEEDS_DECK = "Your edit needs a new deck. It isn't ready yet, so it won't be attached now (you can attach it when it's ready).";
export const FOR_PREVIOUS = "for the previous text"; // the label on the deck of the text before an edit

function icon(name) { return el("span", { class: "ico i-" + name, attrs: { "aria-hidden": "true" } }); }

// the fact-check badge: the API's `factcheck`, told apart by the deck job's model label ("codex <model>",
// "codex <model> (repaired)", "template (fact-check)" or "template")
export const FC = {
  passed: { label: "Passed", icon: "check", tip: "The fact check passed the designer's deck" },
  repaired: { label: "Repaired", icon: "wrench", tip: "The fact check found problems and the designer fixed them" },
  template: { label: "Template", icon: "layers", tip: "The template deck, made without the designer" },
  refused: { label: "Template", icon: "warn", tip: "The fact check refused the designer's deck, so the template went out" },
  designer: { label: "Designer", icon: "palette", tip: "Made with Codex; no fact-check result was recorded" },
  none: { label: "No check", icon: "dot", tip: "No fact-check result was recorded for this deck" },
};

/** Which badge a deck gets: the API's `factcheck` and the model label (`d.model`, `d.factcheck`). */
export function fcOf(d) {
  const m = typeof d.model === "string" ? d.model.trim() : "";
  if (d.factcheck === "refused") return "refused";
  if (d.factcheck === "passed") return m.endsWith("(repaired)") ? "repaired" : "passed";
  if (m.startsWith("template")) return "template";
  if (m === "codex") return "designer"; // a bare "codex" never claims a fact check the API didn't report
  return "none";
}

/** The model label without the deck job's notes in brackets: the badge says those. */
export function modelName(m) {
  let s = typeof m === "string" ? m.trim() : "";
  for (const tail of [" (repaired)", " (fact-check)"]) if (s.endsWith(tail)) s = s.slice(0, -tail.length);
  return s || "—";
}

/** The fact-check badge element for a deck. */
export function fcBadge(d) {
  const kind = fcOf(d);
  const fc = FC[kind];
  return el("span", { class: "fc fc-" + kind, attrs: { title: fc.tip } }, icon(fc.icon), el("span", { text: fc.label }));
}

/** "Codex" for a deck the designer made (or put a Codex cover on), "Template" for the template, "" when the label is
 * unknown (the badge says "No check" then). */
export function engineOf(d) {
  const kind = fcOf(d);
  if (kind === "passed" || kind === "repaired" || kind === "designer") return "Codex";
  if (kind === "template" || kind === "refused") return "Template";
  return "";
}

/** What the Post confirm adds about the deck, or null when there is nothing to add. The deck is judged for the card's
 * CURRENT text: a deck that is coming (the deck job has it, or the text was edited and its new deck is being made) or
 * one that is not in his DM yet would not be attached to a post made now. A deck that gave up, or that is switched off,
 * is not "coming": there is nothing to attach, and the strip says "No deck". */
export function deckNoteText(deck) {
  if (!deck) return null;
  if (deck.state === "pending") return deck.stale ? EDIT_NEEDS_DECK : DECK_NOT_IN_DM;
  if (deck.state === "done" && !deck.dm_at) return DECK_NOT_IN_DM;
  return null;
}

const plural = (n) => `${n} ${n === 1 ? "page" : "pages"}`;

/** "Codex · 11 pages" or "Template", for the deck the fields describe (the engine is told from the fact-check kind).
 * The template's page count is not said: it is the same fixed layout each time. */
function whatItIs(d) {
  const engine = engineOf(d) || "Deck";
  const parts = [engine];
  if (engine !== "Template" && Number.isInteger(d.pages) && d.pages > 0) parts.push(plural(d.pages));
  return parts;
}

/** The words of a deck's status line: "Designing…", "New deck being made for your edit…", "Codex · 11 pages · in your
 * DM", "Template · in your DM" or "No deck". "In your DM" is said only for the deck of the current text that has its
 * `dm_at`. The fact-check note is the badge (`fcBadge`), as on the Decks page. */
export function deckWords(deck) {
  if (!deck) return "";
  if (deck.state === "pending") return deck.stale ? "New deck being made for your edit…" : "Designing…";
  if (deck.state !== "done" || !deck.available) return "No deck";
  return [...whatItIs(deck), ...(deck.dm_at ? ["in your DM"] : [])].join(" · ");
}

/** The second line of a card whose text was edited: the deck of the text before, which can still be opened, as that
 * ("Codex · 11 pages · for the previous text"); "" when there is none. */
export function deckPrevWords(deck) {
  if (!deck || deck.state !== "pending" || !deck.stale || !deck.available) return "";
  return [...whatItIs(deck), FOR_PREVIOUS].join(" · ");
}

const waiting = []; // thumbnails waiting for a turn, oldest first
let busy = 0;

function turn() {
  return new Promise((resolve) => { waiting.push(resolve); pump(); });
}

function pump() {
  while (busy < THUMBS_AT_ONCE && waiting.length) { busy += 1; waiting.shift()(); }
}

/**
 * The deck's first page as a blob: URL (the caller revokes it), or null when there is no preview. At most two are
 * fetched at a time across the whole page. `alive()` says whether the one who asked still wants it: asked again when
 * its turn comes (no request is made for one that has gone) and when the bytes are back (no URL is made for it).
 */
export async function fetchDeckThumb(id, alive = () => true) {
  await turn();
  let blob = null;
  try {
    if (alive()) blob = await apiBlob(`/api/decks/${encodeURIComponent(id)}/thumb.png`);
  } catch (e) { blob = null; } finally { busy -= 1; pump(); }
  if (!alive() || !blob || !String(blob.type).startsWith("image/")) return null;
  return URL.createObjectURL(blob);
}

/** The <img> of a thumbnail: it fades in (`.on`) when it has decoded; `onFail` runs (the image is gone) when it can't. */
export function thumbImg(url, cls, onLoad, onFail) {
  const img = el("img", { class: cls, attrs: { src: url, alt: "", decoding: "async" } });
  img.addEventListener("load", () => { img.classList.add("on"); onLoad(); }, { once: true });
  img.addEventListener("error", () => { img.remove(); onFail(); }, { once: true });
  return img;
}

/** Show `text` in `box` (a role=alert element) for a few seconds. Returns the timer: pass it back in as `prev`. */
export function flashNote(box, text, prev) {
  window.clearTimeout(prev);
  clear(box, icon("warn"), el("span", { text }));
  box.hidden = false;
  return window.setTimeout(() => { box.hidden = true; }, NOTE_MS);
}

/**
 * Open a deck's PDF in a new tab, from a blob: URL made from the bytes fetched with the session header. Resolves
 * "ok", "failed" (the PDF could not be fetched: OPEN_FAILED), "blocked" (the browser stopped the new tab:
 * OPEN_BLOCKED) or "gone" (`alive()` said the page that asked has gone, so nothing was opened).
 */
export async function openDeckPdf(id, alive = () => true) {
  let blob;
  try { blob = await apiBlob(`/api/decks/${encodeURIComponent(id)}.pdf`); } catch (e) { blob = null; }
  if (!alive()) return "gone";
  if (!blob) return "failed";
  const url = URL.createObjectURL(blob.type === "application/pdf" ? blob : new Blob([blob], { type: "application/pdf" }));
  window.setTimeout(() => URL.revokeObjectURL(url), PDF_KEEP_MS); // even after the page unmounts
  // No "noopener": with it, window.open always returns null, and a blocked pop-up couldn't be told apart (and
  // reported). The new tab is our own PDF as a same-origin blob: document (Chrome's viewer), and its opener is
  // cut just below.
  const w = window.open(url, "_blank");
  if (!w) return "blocked";
  try { w.opener = null; } catch (e) { /* nothing to cut */ }
  return "ok";
}
