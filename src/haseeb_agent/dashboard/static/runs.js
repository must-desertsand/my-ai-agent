// The Live page's "Run now" card: the buttons Haseeb used to get from the command line. Each control starts one
// allowlisted job (POST /api/run, through jobs.js) and shows, from the shared /api/jobs state, whether it runs now and
// how its last run ended. Force insights asks first, inline (no window.confirm), like the Decks page's Attach. The
// card also shows which Slack account the last "Check Slack account" saw, in red when it was not Haseeb's (and amber,
// "check again", when a green check is too old to trust: the server says `stale`), and a
// banner with Resume while the agent is paused (the controls then wait, except the read-only Slack check).
// Everything the server says is shown as text.
import { clear, el, parseTime, whenLabel } from "./dom.js";
import { explain, isPaused, jobInfo, jobs, onJobs, runJob, setPaused } from "./jobs.js";

const NOTE_MS = 8000;

const CONTROLS = [
  { job: "tick", title: "Check round", icon: "bolt", verb: "Run now",
    about: "Runs the tick now: DM replies, deliveries, the Next Todo, feedback and page clicks." },
  { job: "insights", title: "Force insights", icon: "bulb", verb: "Force",
    about: "New research, then up to 3 more insights in your DM.",
    confirm: "Research new topics and send up to 3 more insights to your DM?" },
  { job: "decks", title: "Build decks", icon: "palette", verb: "Build",
    about: "Today's missing or given-up decks get another try." },
  { job: "weekly", title: "Weekly report", icon: "doc", verb: "Send",
    about: "This week's report to your DM, now. Friday's scheduled report still goes out." },
  { job: "history_import", title: "Import team history", icon: "down", verb: "Import",
    about: "Copies the channel's past messages into the History. The regular checks wait while it runs, "
      + "and it stops inside the huddle window." },
  { job: "enrich", title: "Label topics now", icon: "tag", verb: "Label",
    about: "Reads threads with new replies, labels the leftover messages and groups them into topics." },
  { job: "slack_check", title: "Check Slack account", icon: "users", verb: "Check", allowPaused: true,
    about: "Read-only: which Slack account would the agent post as? Nothing is sent." },
];

function icon(name) { return el("span", { class: "ico i-" + name, attrs: { "aria-hidden": "true" } }); }

function button(label, ico, cls, fn) {
  return el("button", { class: ["btn", cls].filter(Boolean).join(" "), attrs: { type: "button" }, on: { click: fn } },
    ico ? icon(ico) : null, el("span", { text: label }));
}

/** "14:32" today, "Tue 14:32" another day. */
function when(iso) {
  const d = parseTime(iso);
  return d ? whenLabel(d) : "";
}

export function mountRuns(card, banner) {
  const tiles = new Map(); // job → tile state
  const offs = [];

  // ---- which Slack account (from the last Check Slack account)
  const ident = el("div", { class: "run-ident", attrs: { role: "status" } });
  const grid = el("div", { class: "run-grid" });
  let identKey = null; // what the strip shows now: an unchanged one is left alone (a status region must not repeat itself)
  clear(card.body, ident, grid);

  function paintIdent() {
    const id = jobs.data ? jobs.data.slack_identity : null;
    let cls = "is-none", ico = "users", parts;
    if (jobs.offline && !jobs.data) { cls = "is-none"; parts = ["Slack: waiting for the dashboard…"]; }
    else if (!id) parts = ["Slack: not checked yet. Check it after switching Claude accounts."];
    else {
      const at = when(id.at);
      const checked = at ? ` (checked ${at})` : "";
      if (id.ok && id.stale) { // from before an account switch or a restart, or older than 6 hours: no green tick
        cls = "is-warn"; ico = "warn";
        parts = [`Slack: the last check is old${checked} — check again.`];
      } else if (id.ok) { cls = "is-ok"; ico = "check"; parts = [`Slack: posting as you ✓${checked}`]; }
      else if (id.user_id) {
        cls = "is-bad"; ico = "warn";
        parts = ["Slack: posting as another account (", el("bdi", { text: id.user_id }), `) ✗${checked}. Pause the agent and fix the connector.`];
      } else {
        cls = "is-warn"; ico = "warn";
        parts = ["Slack: couldn't check", id.error ? [" — ", el("bdi", { text: id.error })] : null, `${checked}.`];
      }
    }
    const key = cls + "|" + JSON.stringify(id) + "|" + String(jobs.offline);
    if (identKey === key) return;
    identKey = key;
    ident.className = "run-ident " + cls;
    clear(ident, icon(ico), el("span", {}, ...parts));
  }

  // ---- the paused banner
  const bannerText = el("span", { class: "pb-text" });
  const bannerNote = el("span", { class: "pb-note", attrs: { role: "status" } });
  const resume = button("Resume", "play", "primary", async () => {
    resume.disabled = true;
    bannerNote.textContent = "";
    const r = await setPaused(false);
    if (!r.ok) { bannerNote.textContent = explain(r); resume.disabled = false; }
  });
  clear(banner, icon("pause"), bannerText, bannerNote, resume);

  function paintBanner() {
    const d = jobs.data;
    const paused = !!(d && d.paused);
    banner.hidden = !paused;
    if (!paused) { resume.disabled = false; return; }
    const at = when(d.paused_at);
    const text = `Paused${at ? " since " + at : ""}. Nothing is sent to Slack and no job runs.`;
    if (bannerText.textContent !== text) bannerText.textContent = text;
  }

  // ---- the controls
  for (const def of CONTROLS) {
    const t = { def, busy: false, confirming: false, note: null, noteT: null, statusKey: null, actionsKey: null };
    t.title = el("h3", { class: "run-t", attrs: { id: `run-t-${def.job}` }, text: def.title });
    t.status = el("div", { class: "run-status", attrs: { role: "status", "aria-live": "polite" } });
    t.actions = el("div", { class: "run-actions" });
    t.node = el("article", { class: "run-tile", data: { job: def.job }, attrs: { "aria-labelledby": `run-t-${def.job}`,
      tabindex: "-1" } },
    el("div", { class: "run-top" }, el("span", { class: "run-ico", attrs: { "aria-hidden": "true" } }, icon(def.icon)), t.title),
    el("p", { class: "run-about", text: def.about }), t.status, t.actions);
    t.go = button(def.verb, null, "", () => ask(t));
    t.go.setAttribute("aria-labelledby", `run-b-${def.job} run-t-${def.job}`);
    t.go.firstChild.setAttribute("id", `run-b-${def.job}`);
    tiles.set(def.job, t);
    grid.appendChild(t.node);
  }

  function setNote(t, text) {
    window.clearTimeout(t.noteT);
    t.note = text;
    t.noteT = text ? window.setTimeout(() => { t.note = null; paintTile(t); }, NOTE_MS) : null;
  }

  function line(t) {
    const def = t.def, info = jobInfo(def.job), paused = isPaused();
    if (t.note) return { cls: "s-note", spin: false, text: t.note };
    if (t.busy) return { cls: "s-run", spin: true, text: "Starting…" };
    if (info && info.running) {
      const importing = !!(jobInfo("history_import") || {}).running;
      if (def.job === "tick" && importing) {
        return { cls: "s-run", spin: true, text: "The history import holds the tick lock; the regular checks wait." };
      }
      const since = info.started_at ? when(info.started_at) : "";
      const text = `Running${since ? " since " + since : ""}…` + (def.job === "history_import" ? " The regular checks wait." : "");
      return { cls: "s-run", spin: true, text };
    }
    if (paused && !def.allowPaused) return { cls: "s-paused", spin: false, text: "Paused: resume to run this." };
    const last = info && info.last;
    if (last) {
      const at = when(last.at);
      const text = `Last run ${at} · ${last.ok ? "ok" : "failed"}` + (last.summary ? ` · ${last.summary}` : "");
      return { cls: last.ok ? "s-ok" : "s-bad", spin: false, text };
    }
    return { cls: "s-none", spin: false, text: "Not run yet." };
  }

  function paintTile(t) {
    const info = jobInfo(t.def.job);
    const s = line(t);
    const key = `${s.cls}|${s.spin}|${s.text}`;
    if (t.statusKey !== key) {
      t.statusKey = key;
      t.status.className = "run-status " + s.cls;
      clear(t.status, s.spin ? el("span", { class: "spinner sm", attrs: { "aria-hidden": "true" } }) : null,
        el("span", { class: "run-status-t" }, el("bdi", { text: s.text })));
    }
    const running = !!(info && info.running);
    const off = running || t.busy || (isPaused() && !t.def.allowPaused) || (jobs.offline && !jobs.data);
    t.node.classList.toggle("is-running", running || t.busy);
    t.node.setAttribute("aria-busy", String(running || t.busy));
    if (t.confirming && off) t.confirming = false; // it started meanwhile (another tab) or the agent was paused
    const actionsKey = `${t.confirming}|${off}`;
    if (t.actionsKey === actionsKey) return;
    t.actionsKey = actionsKey;
    const had = t.actions.contains(document.activeElement);
    if (t.confirming) { paintConfirm(t); return; }
    t.actions.classList.remove("confirm");
    t.go.disabled = off;
    clear(t.actions, t.go);
    if (had) t.go.focus({ preventScroll: true });
  }

  function paintConfirm(t) {
    const finish = (yes) => {
      t.confirming = false;
      t.actionsKey = null;
      if (yes) start(t); else paintTile(t);
      t.node.focus({ preventScroll: true }); // back to the control, never lost to <body>
    };
    const yes = button("Confirm", t.def.icon, "primary", () => finish(true));
    const no = button("Cancel", null, "ghost", () => finish(false));
    for (const b of [yes, no]) b.addEventListener("keydown", (e) => { if (e.key === "Escape") finish(false); });
    clear(t.actions, el("span", { class: "confirm-q", text: t.def.confirm }), no, yes);
    t.actions.classList.add("confirm");
    window.setTimeout(() => { if (no.isConnected) no.focus(); }, 0); // Enter lands on Cancel
  }

  function ask(t) {
    if (t.busy || t.go.disabled) return;
    if (t.def.confirm) { t.confirming = true; t.actionsKey = null; paintTile(t); return; }
    start(t);
  }

  async function start(t) {
    if (t.busy) return;
    t.busy = true;
    setNote(t, null);
    paintTile(t);
    const r = await runJob(t.def.job);
    t.busy = false;
    if (!r.ok) setNote(t, explain(r));
    paintTile(t);
  }

  function paintAll() {
    paintBanner();
    paintIdent();
    for (const t of tiles.values()) paintTile(t);
  }

  paintAll();
  offs.push(onJobs(paintAll));
  return () => {
    offs.forEach((f) => f());
    for (const t of tiles.values()) window.clearTimeout(t.noteT);
  };
}
