// Safe DOM builders. Every piece of text, trusted or not, goes in through textContent or createTextNode: Slack text
// is untrusted, so no data ever reaches an HTML parser. Nodes are emptied with replaceChildren(), never innerHTML.
// Tasks 9 and 10 build their pages with these helpers too.

const SVG_NS = "http://www.w3.org/2000/svg";

/**
 * el("div", {class: "card", text: "hi", attrs: {role: "status"}, on: {click: fn}, data: {id: "x"}}, ...children)
 * Children may be nodes, strings (added as text nodes), numbers, or null/false (skipped). Arrays are flattened.
 * `attrs` refuses event-handler names (on*), `style` and `srcdoc`; `href` is set only to a Slack workspace permalink
 * and `src` only to a blob: URL or a same-origin path (see cleanUrl); anything else is simply left unset.
 */
export function el(tag, opts = {}, ...children) {
  const node = document.createElement(tag);
  apply(node, opts);
  append(node, children);
  return node;
}

/** An SVG element (for small decorative icons), with the same attribute rules. */
export function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) setAttr(node, k, v);
  append(node, children);
  return node;
}

function apply(node, opts) {
  if (opts.class) node.className = Array.isArray(opts.class) ? opts.class.filter(Boolean).join(" ") : opts.class;
  if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
  if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) setAttr(node, k, v);
  if (opts.data) for (const [k, v] of Object.entries(opts.data)) node.dataset[k] = String(v);
  if (opts.on) for (const [k, fn] of Object.entries(opts.on)) node.addEventListener(k, fn);
  if (opts.vars) setVars(node, opts.vars);
}

function setAttr(node, k, v) {
  if (v === undefined || v === null || v === false) return;
  const name = String(k).toLowerCase();
  if (name.startsWith("on") || name === "style" || name === "srcdoc") throw new Error("refused attribute " + k);
  if (name === "action" || name === "formaction" || name.endsWith(":href")) return; // never needed: never set
  if (name === "href" || name === "src") {
    const clean = cleanUrl(String(v), name);
    if (clean !== null) node.setAttribute(k, clean); // the parsed, normalized form, not the raw text
    return;
  }
  node.setAttribute(k, v === true ? "" : String(v));
}

const SLACK = ".slack.com";

/**
 * The URL to put in an attribute, or null. For href: a Slack workspace permalink only (https, a host under
 * .slack.com, no user, password or port, a path under /archives/), normalized by the URL parser. For src: a blob:
 * URL or a same-origin absolute path.
 */
export function cleanUrl(u, name = "href") {
  if (typeof u !== "string") return null;
  if (name === "src") return /^(blob:|\/[A-Za-z0-9_])/.test(u) ? u : null;
  let url;
  try { url = new URL(u); } catch (e) { return null; } // relative, protocol-less or broken: never a permalink
  const host = url.hostname;
  if (url.protocol !== "https:" || !host.endsWith(SLACK) || host.length <= SLACK.length || host.startsWith(".")) return null;
  if (url.username || url.password || url.port || !url.pathname.startsWith("/archives/")) return null;
  return url.href;
}

export function safeUrl(u, name = "href") { return cleanUrl(u, name) !== null; }

function append(node, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

/** CSS custom properties through the CSSOM (allowed by the CSP, unlike style="" markup). */
export function setVars(node, vars) {
  for (const [k, v] of Object.entries(vars)) node.style.setProperty(k.startsWith("--") ? k : "--" + k, String(v));
}

/** Empty a node and optionally fill it again. */
export function clear(node, ...children) {
  node.replaceChildren();
  append(node, children);
  return node;
}

/** An external link: API permalinks only, opened in a new tab without a referrer. */
export function extLink(href, text, cls) {
  const a = el("a", { class: cls, text, attrs: { href, target: "_blank", rel: "noopener noreferrer" } });
  return a;
}

// ------------------------------------------------------------------ formatting (pure)

const pad = (n) => String(n).padStart(2, "0");

/** 75 → "1:15", 3725 → "1:02:05". Negative counts as 0. */
export function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return h ? `${h}:${pad(m)}:${pad(r)}` : `${m}:${pad(r)}`;
}

/** A friendly countdown: "in 4:12", "in 2 h 05 m", "in 1 d 3 h". */
export function countdown(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 3600) return clock(s);
  if (s < 86400) return `${Math.floor(s / 3600)} h ${pad(Math.floor((s % 3600) / 60))} m`;
  return `${Math.floor(s / 86400)} d ${Math.floor((s % 86400) / 3600)} h`;
}

export function hhmm(d) { return `${pad(d.getHours())}:${pad(d.getMinutes())}`; }
export function hhmmss(d) { return `${hhmm(d)}:${pad(d.getSeconds())}`; }

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function dayLabel(d) { return `${DAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`; }

/** "14:30" today, "Tue 08:15" another day. */
export function whenLabel(d, now = new Date()) {
  const same = d.toDateString() === now.toDateString();
  return same ? hhmm(d) : `${DAYS[d.getDay()]} ${hhmm(d)}`;
}

export function parseTime(iso) {
  if (typeof iso !== "string" || !iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "8 s ago", "3 min ago", "2 h ago". */
export function ago(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
