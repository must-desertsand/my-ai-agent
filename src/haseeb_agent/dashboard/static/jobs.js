// The run controls' shared state: GET /api/jobs (is the agent paused, which Slack account the last check saw, and for
// each job whether it runs and how its last run ended), and the two calls that change things: POST /api/run and
// POST /api/pause. Every page that shows a control (the header's Pause, the Live page's Run card, the Draft feedback
// buttons) reads this one copy, so they never disagree. The state is polled, not streamed: the answer is a handful of
// lock probes and cursor reads, every 10 s, and every 2 s while a job runs or a request has just been made.
import { api, ApiError, emit, isExpired, on } from "./client.js";

const SLOW_MS = 10000;
const FAST_MS = 2000;
const JOB_NAMES = ["tick", "insights", "decks", "weekly", "history_import", "enrich", "slack_check", "feedback"];

/** The latest /api/jobs answer, when it arrived (client clock), and whether the last try failed. */
export const jobs = { data: null, at: 0, offline: false };

const SETTLE_MS = 4000; // an answer older than a click cannot say the click's job is not running
const drafting = new Map(); // ts → when: the posts whose draft this tab asked for and that has not settled yet
let timer = null, stopped = true, busy = null, boost = 0;

export function jobList() { return jobs.data && Array.isArray(jobs.data.jobs) ? jobs.data.jobs : []; }
export function jobInfo(name) { return jobList().find((j) => j && j.name === name) || null; }
export function isPaused() { return !!(jobs.data && jobs.data.paused); }
/** True while a tick holds the composer back (the huddle window, until today's Next Todo is out). */
export function isComposeHeld() { return !!(jobs.data && jobs.data.compose_hold); }
export function anyRunning() { return jobList().some((j) => j && j.running); }
export function isDrafting(ts) { return drafting.has(ts); }
export function onJobs(fn) { return on("jobs", fn); }

function schedule() {
  window.clearTimeout(timer);
  timer = null;
  if (stopped || isExpired()) return;
  const fast = anyRunning() || Date.now() < boost;
  timer = window.setTimeout(refreshJobs, fast ? FAST_MS : SLOW_MS);
}

/** Ask for the state now. One request at a time: a call while one is out shares it. */
export function refreshJobs() {
  if (busy) return busy;
  busy = (async () => {
    try {
      const d = await api("/api/jobs");
      if (d && typeof d === "object" && Array.isArray(d.jobs)) {
        jobs.data = d;
        jobs.at = Date.now();
        jobs.offline = false;
        const fb = jobInfo("feedback");
        if (!fb || !fb.running) { // the job a click started has ended (or never started)
          for (const [ts, at] of drafting) if (Date.now() - at > SETTLE_MS) drafting.delete(ts);
        }
        emit("jobs", jobs);
      }
    } catch (e) {
      if (isExpired()) return;
      jobs.offline = true;
      emit("jobs", jobs);
    } finally {
      busy = null;
      schedule();
    }
  })();
  return busy;
}

/** An answer that is newer than now: waits for a request that is already out, then asks again. */
async function refreshJobsFresh() {
  if (busy) await busy;
  return refreshJobs();
}

/** Start polling; returns the function that stops it. */
export function startJobs() {
  stopped = false;
  refreshJobs();
  return () => { stopped = true; window.clearTimeout(timer); timer = null; };
}

function failure(e) {
  if (e instanceof ApiError) return { ok: false, status: e.status, error: e.message, data: e.data || null };
  return { ok: false, status: 0, error: "failed", data: null };
}

/** A refused or failed call in words for the page: the server's fixed answers, or a plain account of a failure. */
export function explain(r) {
  if (r.status === 0) return "The dashboard didn't answer. Check the status.";
  if (r.status === 500) return "Couldn't start it. Try again.";
  if (r.status === 404) return "That post isn't in the history.";
  const msg = typeof r.error === "string" ? r.error : "";
  if (msg === "paused") return "Paused: resume first.";
  if (msg === "already running") return "Already running.";
  return msg ? msg[0].toUpperCase() + msg.slice(1) + "." : "Couldn't start it.";
}

/** Start a job. `ts` is the one value a run takes (a feedback run's post). Resolves {ok: true} or
 * {ok: false, status, error, data}: a 409 carries the job's state (already running, or paused). */
export async function runJob(name, ts) {
  if (!JOB_NAMES.includes(name)) return { ok: false, status: 0, error: "bad job", data: null };
  const body = ts === undefined ? { job: name } : { job: name, ts };
  let res;
  try {
    res = await api("/api/run", { method: "POST", body });
  } catch (e) {
    boost = Date.now() + 10000;
    refreshJobsFresh();
    return failure(e);
  }
  if (name === "feedback" && ts !== undefined) drafting.set(ts, Date.now());
  boost = Date.now() + 10000; // look often for a while: the job is about to take its lock and then end
  await refreshJobsFresh();
  return { ok: true, data: res };
}

/** Pause or resume. Resolves {ok: true, paused} or {ok: false, ...}. */
export async function setPaused(paused) {
  let res;
  try {
    res = await api("/api/pause", { method: "POST", body: { paused: !!paused } });
  } catch (e) {
    return failure(e);
  }
  await refreshJobsFresh();
  return { ok: true, paused: !!(res && res.paused) };
}

on("expired", () => { stopped = true; window.clearTimeout(timer); timer = null; drafting.clear(); });
