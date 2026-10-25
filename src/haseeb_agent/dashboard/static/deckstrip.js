// The deck strip on an insight's card in "Waiting for you": the first page of the deck that goes with the insight, what
// it is, and a button that opens it. It follows the API's `deck` object of the card (see api._deck_view), which is read
// for the card's CURRENT text:
//   - done: the thumbnail, the fact-check badge, "Codex · 11 pages · in your DM", and Open deck;
//   - pending: "Designing…" over a placeholder that breathes (the deck is being made);
//   - pending + stale (the text was edited, a new deck is being made): "New deck being made for your edit…", and the
//     deck of the text before, still openable, labelled "for the previous text";
//   - gave_up / off: "No deck" over a still placeholder. Nothing is requested and nothing keeps loading.
// The thumbnail is fetched with the session header (deckview.fetchDeckThumb, D11) and shown through a blob: URL that is
// revoked when it is replaced or the card goes; the PDF opens exactly as on the Decks page (deckview.openDeckPdf).
// Everything is text; none of the deck's fields is ever markup.
import { clear, el } from "./dom.js";
import {
  OPEN_BLOCKED, OPEN_FAILED, deckPrevWords, deckWords, fcBadge, fetchDeckThumb, flashNote, openDeckPdf, thumbImg,
} from "./deckview.js";

function icon(name) { return el("span", { class: "ico i-" + name, attrs: { "aria-hidden": "true" } }); }
function slide() { return el("span", { class: "slide" }, el("i"), el("i"), el("i")); }

/** The strip for the insight `id`: `node` to put on the card, `update(deck)` when the API's `deck` changed, `destroy()`
 * when the card goes. */
export function buildDeckStrip(id) {
  const safe = id.replace(/[^A-Za-z0-9_-]/g, "_");
  let dead = false, deck = null, shape = "", thumbFor = "", url = null, img = null, opening = false, noteT = null;

  const ph = el("span", { class: "dstrip-ph", attrs: { "aria-hidden": "true" } }, slide());
  const pic = el("span", { class: "dstrip-pic", attrs: { "aria-hidden": "true" } }, ph);
  const badge = el("span", { class: "dstrip-badge" });
  const words = el("span", { class: "dstrip-words", attrs: { role: "status", "aria-live": "polite" } });
  const prev = el("div", { class: "dstrip-prev", attrs: { id: `dstrip-p-${safe}`, hidden: true } });
  const note = el("div", { class: "dstrip-note", attrs: { role: "alert", hidden: true } });
  const openLabel = el("span", { text: "Open deck" });
  const open = el("button", { class: "btn dstrip-open", attrs: { type: "button", hidden: true }, on: { click: openPdf } },
    icon("ext"), openLabel);
  const node = el("div", { class: "dstrip", attrs: { role: "group", "aria-label": "Deck" }, data: { state: "pending" } },
    pic,
    el("div", { class: "dstrip-b" }, el("div", { class: "dstrip-line" }, badge, " ", words), prev, note),
    open);

  function setPic(mode) { // "loading": the placeholder breathes; "idle": it stands still; "shown": the picture covers it
    ph.classList.toggle("loading", mode === "loading");
    ph.hidden = mode === "shown";
  }

  function dropThumb() {
    if (img) img.remove();
    if (url) URL.revokeObjectURL(url);
    img = null;
    url = null;
    thumbFor = "";
  }

  function loadThumb(key) {
    thumbFor = key;
    setPic("loading");
    const alive = () => !dead && thumbFor === key; // a newer deck, or the card gone: no request, no URL
    fetchDeckThumb(id, alive).then((u) => {
      if (!alive()) return;
      if (!u) { setPic("idle"); return; } // no preview: a still placeholder, never a spinner that doesn't end
      const mine = thumbImg(u, "dstrip-img",
        () => { if (img === mine) setPic("shown"); }, // an <img> that was replaced meanwhile does not hide the placeholder
        () => {
          if (img !== mine) return;
          URL.revokeObjectURL(u);
          url = null;
          img = null;
          setPic("idle"); // the picture would not decode: a still placeholder
        });
      url = u;
      img = mine;
      pic.insertBefore(mine, pic.firstChild);
    });
  }

  function paintOpen() {
    openLabel.textContent = opening ? "Opening…" : "Open deck";
    open.disabled = opening;
  }

  async function openPdf() {
    if (opening || !deck || !deck.available) return;
    opening = true;
    paintOpen();
    const result = await openDeckPdf(id, () => !dead);
    opening = false;
    if (dead) return;
    paintOpen();
    if (result === "failed") noteT = flashNote(note, OPEN_FAILED, noteT);
    else if (result === "blocked") noteT = flashNote(note, OPEN_BLOCKED, noteT);
  }

  function paint() {
    const d = deck;
    const pending = d.state === "pending";
    const done = d.state === "done" && d.available;
    const old = pending && !!d.stale && !!d.available; // the deck of the text before the edit
    const openable = done || old;
    node.dataset.state = d.state;
    node.classList.toggle("is-previous", old);
    node.classList.toggle("is-none", !pending && !done);
    clear(badge, done ? fcBadge(d) : null);
    clear(words, pending ? el("span", { class: "spinner sm", attrs: { "aria-hidden": "true" } }) : null,
      el("span", { text: deckWords(d) }));
    const prevText = deckPrevWords(d);
    prev.hidden = !prevText;
    if (prevText) clear(prev, fcBadge(d), " ", el("span", { text: prevText }));
    else clear(prev);
    open.hidden = !openable;
    if (old) open.setAttribute("aria-describedby", prev.id); // the label "for the previous text" is part of its description
    else open.removeAttribute("aria-describedby");
    paintOpen();
    if (!openable) { // nothing to preview: no request; it breathes only while the deck is being made
      dropThumb();
      setPic(pending ? "loading" : "idle");
      return;
    }
    const key = [d.state, !!d.stale, d.dm_at, d.model, d.pages].join("|");
    if (key !== thumbFor) { dropThumb(); loadThumb(key); } // another deck: another first page
  }

  /** Follow the API's `deck`. Only a change repaints, so a focused button, a note and a loaded thumbnail stay put. */
  function update(next) {
    if (dead || !next || typeof next !== "object") return;
    const key = JSON.stringify([next.state, !!next.stale, next.model, next.pages, next.factcheck, !!next.available,
      next.dm_at]);
    if (key === shape) return;
    shape = key;
    deck = next;
    paint();
  }

  function destroy() {
    dead = true;
    dropThumb();
    window.clearTimeout(noteT);
  }

  return { node, update, destroy };
}
