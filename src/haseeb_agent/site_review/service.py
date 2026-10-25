"""The site review of one post: find what it links to, render each target, have a vision model review it, and say what
was reviewed. The feedback job calls `review_post` once, before it drafts; whatever happens in here, the draft still comes
(from the post alone when nothing could be reviewed), with a line that says what was and what was not looked at.

Limits. At most 10 site reviews a day (a cursor, counted per target when its render starts, in one atomic store operation,
and only for a link the policy has let through and a Chrome that is current; over the cap a target is reported as "daily
limit"), 45 s of rendering per target and 90 s per post, 150 s for the whole review (no new work starts after that).
Screenshots are kept in `data/site-reviews/<item id>/` (git-ignored), pruned after 7 days (here, and every night by the
enrichment run), and sent nowhere except the vision call. Logs name the host (or file name), the step, timings and counts,
and never page text.

Failures. Every failure of a target, from a refused address to an unreadable answer, is a short fixed reason in the DM line
(`Couldn't review <host>: <reason>`); the offline guard is the one exception, and passes through (in a test it means a real
step was reached that should have been replaced, and it must be loud).
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .. import events
from ..config import Settings
from ..litellm import LLM
from ..models import FileRef
from ..offline import OfflineError
from ..store import Store
from . import chrome, review as review_mod
from .chrome import Rendered, RenderError
from .review import Review, ReviewError
from .targets import HTML_MAX_BYTES, Target, find_targets

log = logging.getLogger(__name__)

JOB = "feedback"
STEP = "site_review"
DAILY_CAP = 10
TARGET_BUDGET_S = 45.0
RENDER_BUDGET_S = 90.0
TOTAL_BUDGET_S = 150.0
KEEP_DAYS = 7
MIN_RENDER_S = 8.0           # a target is not started with less render time than this left
MIN_REVIEW_S = 5.0           # nor a model call with less than this
LINE_CHARS = 240
NOTES_CHARS = 4000
_SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,80}")


class AttachmentError(Exception):
    """The HTML attachment could not be used. `reason` is the short phrase for the DM line."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Outcome:
    target: Target
    review: Review | None = None
    rendered: Rendered | None = None
    reason: str | None = None   # why it was not reviewed

    @property
    def ok(self) -> bool:
        return self.review is not None


@dataclass
class Result:
    outcomes: list[Outcome] = field(default_factory=list)
    line: str | None = None     # "Reviewed: ..." and/or "Couldn't review ...: ..." for the DM card and the inbox card
    notes: str | None = None    # for the feedback writer; None when nothing was reviewed

    @property
    def reviewed(self) -> int:
        return sum(o.ok for o in self.outcomes)

    @property
    def failed(self) -> int:
        return sum(not o.ok for o in self.outcomes)


@dataclass
class Renderers:
    """The two real steps, replaceable in tests (the defaults start Chrome, behind the offline guard)."""
    link: Callable[..., Rendered] = chrome.render_link
    html: Callable[..., Rendered] = chrome.render_html


class _Failed(Exception):
    """A target that could not be reviewed: `reason` is the short phrase for the DM line, `detail` a fixed word for the log.
    The two subclasses exist for their names: a step event records the exception's class name as the kind of its failure,
    and the Live and Logs pages show it."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


class RenderFailed(_Failed):
    """The page could not be loaded, or the attachment not read."""


class ReviewFailed(_Failed):
    """The page was rendered, but there was no time left or no model could review it."""


class ChromeStale(RenderFailed):
    """The Chrome on this Mac is too old to be trusted with a hostile page (security review M11): nothing was rendered. The
    version and the age in days are for the log, which says so once a day."""

    def __init__(self, reason: str, detail: str = "", version: str = "", age_days: int = 0):
        super().__init__(reason, detail)
        self.version, self.age_days = version, age_days


def _record_refusal(store: Store, failure: _Failed) -> None:
    """A link the policy refused is a failed target like any other on the Live and Logs pages (a step event of the failure's
    class), though it never became a review."""
    try:
        with events.step(store, JOB, STEP):
            raise failure
    except _Failed:
        pass


def root_dir(settings: Settings) -> Path:
    return Path(settings.db_path).parent / "site-reviews"


def prune(root: Path, now: datetime) -> int:
    """Delete the review folders older than KEEP_DAYS. Returns how many went. Never raises."""
    gone = 0
    try:
        cutoff = now.timestamp() - KEEP_DAYS * 86400
        for d in root.iterdir() if root.is_dir() else ():
            try:
                if d.is_dir() and not d.is_symlink() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    gone += 1
            except OSError:
                continue
    except OSError:
        pass
    if gone:
        log.info("site review: pruned %d screenshot folder(s) older than %d days", gone, KEEP_DAYS)
    return gone


def _save(settings: Settings, item_id: str, n: int, target: Target, r: Rendered) -> None:
    """Keep the screenshots (for Haseeb to look at, never sent anywhere). A failure to save is not a failure to review."""
    if not _SAFE_ID.fullmatch(item_id):
        return
    try:
        folder = root_dir(settings) / item_id
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, data in review_mod.shot_files(r):
            (folder / f"{target.kind}{n}-{name}").write_bytes(data)
    except OSError as e:
        log.warning("site review: could not keep the screenshots (%s)", type(e).__name__)


def cap_key(settings: Settings, now: datetime) -> str:
    return f"site_review_day:{now.astimezone(settings.tz).date().isoformat()}"


def take_slot(store: Store, key: str, cap: int) -> bool:
    """Count one review against the day's cap, in ONE store operation (M10): the counter is read, refused when the cap is
    reached and otherwise raised by one inside a single write transaction (`Store.update_cursor`), so the tick and a dashboard
    draft, which are separate processes, cannot both take the last slot. True when a slot was taken."""
    taken: list[bool] = []

    def bump(old: str | None) -> str:
        try:
            used = int(old or 0)
        except ValueError:  # a counter that is not a number is a counter nobody can trust: start it again
            used = 0
        if used >= cap:
            return str(used)
        taken.append(True)
        return str(used + 1)
    store.update_cursor(key, bump)
    return bool(taken)


def release_slot(store: Store, key: str) -> None:
    """Give back a slot that `take_slot` took for a review that never started (one atomic operation, never below zero)."""
    def less(old: str | None) -> str:
        try:
            return str(max(0, int(old or 0) - 1))
        except ValueError:
            return "0"
    try:
        store.update_cursor(key, less)
    except Exception as e:  # noqa: BLE001 - housekeeping: a counter that stays one too high costs one review
        log.warning("site review: could not give back a review slot (%s)", type(e).__name__)


STALE_CHROME_KEY = "site_review_chrome_old_day"


def _warn_old_chrome(store: Store, settings: Settings, now: datetime, f: ChromeStale) -> None:
    """The log says that Chrome is out of date, once a day (a cursor holds the day it last did), however many posts had a
    site to look at: every draft in between goes out from the post alone, with the "Couldn't review" line."""
    today = now.astimezone(settings.tz).date().isoformat()
    try:
        if store.get_cursor(STALE_CHROME_KEY) == today:
            return
        store.set_cursor(STALE_CHROME_KEY, today)
    except Exception as e:  # noqa: BLE001 - a log line is not worth a failed review; say it, since it could not be counted
        log.warning("site review: could not record the daily Chrome notice (%s)", type(e).__name__)
    log.warning("site review: Chrome %s is %d days old (the limit is %d): sites and demos are not rendered and drafts are "
                "made from the post alone. Update Chrome.", f.version or "?", f.age_days, chrome.CHROME_MAX_AGE.days)


def _refusal(target: Target, settings: Settings) -> _Failed | None:
    """The policy's say about a link itself (its scheme, port, password, a non-public literal, a network of the setting), asked
    BEFORE the review is counted (M10): a link that could never be opened must not spend one of the day's reviews. None for
    a link the policy lets through, and for an attachment (which never touches a network)."""
    if target.kind != "link" or target.url is None:
        return None
    try:
        chrome.check_link(target.url, refused_networks=getattr(settings, "site_review_refused_networks", ()))
    except RenderError as e:
        return RenderFailed(e.reason, e.detail)
    return None


def _line(outcomes: list[Outcome]) -> str | None:
    ok = [o.target.label for o in outcomes if o.ok]
    parts = [f"Reviewed: {', '.join(ok)} (desktop + phone)"] if ok else []
    parts += [f"Couldn't review {o.target.label}: {o.reason}" for o in outcomes if not o.ok]
    return " · ".join(parts)[:LINE_CHARS] if parts else None


def _notes(outcomes: list[Outcome]) -> str | None:
    blocks = [review_mod.notes_for(o.target.label, o.target.kind, o.rendered, o.review)
              for o in outcomes if o.ok and o.rendered is not None and o.review is not None]
    return "\n\n".join(blocks)[:NOTES_CHARS] if blocks else None


def review_post(text: str, files: list[FileRef], *, read_html: Callable[[FileRef], str], llm: LLM, store: Store,
                settings: Settings, now: datetime, item_id: str, renderers: Renderers | None = None,
                clock: Callable[[], float] = time.monotonic, claims: bool = False) -> Result:
    """Review what a post links to. Never raises (except the offline guard): each target ends as a review or a reason.
    `claims`: the text is a teammate's reply to Haseeb's feedback, and the claims it makes are checked too (review.py)."""
    targets = find_targets(text, files)
    if not targets or not getattr(settings, "site_review_enabled", True):
        return Result()
    renderers = renderers or Renderers()
    prune(root_dir(settings), now)
    began = clock()
    cap = int(getattr(settings, "site_review_daily_cap", DAILY_CAP))
    key = cap_key(settings, now)
    out = Result()
    spent_render = 0.0
    for n, target in enumerate(targets, 1):
        outcome = Outcome(target)
        out.outcomes.append(outcome)
        if (refusal := _refusal(target, settings)) is not None:  # M10: before anything is counted
            outcome.reason = refusal.reason
            _record_refusal(store, refusal)
            log.warning("site review: %s %s: %s (%s)", target.kind, target.label, refusal.reason, refusal.detail)
            continue
        used = int(store.get_cursor(key) or 0)
        if used >= cap:
            outcome.reason = "daily limit"
            log.info("site review: %s %s: daily limit (%d)", target.kind, target.label, cap)
            continue
        render_left = RENDER_BUDGET_S - spent_render
        total_left = TOTAL_BUDGET_S - (clock() - began)
        if render_left < MIN_RENDER_S or total_left < MIN_RENDER_S + MIN_REVIEW_S:
            outcome.reason = "ran out of time"
            log.info("site review: %s %s: out of time", target.kind, target.label)
            continue
        if not take_slot(store, key, cap):  # counted when it starts, whatever follows; atomic against another run (M10)
            outcome.reason = "daily limit"
            log.info("site review: %s %s: daily limit (%d), taken by another run", target.kind, target.label, cap)
            continue
        t0 = clock()
        try:
            with events.step(store, JOB, STEP) as ev:
                budget = min(TARGET_BUDGET_S, render_left, total_left - MIN_REVIEW_S)
                rendered = _render(target, renderers, settings, read_html, budget)
                outcome.rendered = rendered
                spent_render += clock() - t0
                _save(settings, item_id, n, target, rendered)
                left = TOTAL_BUDGET_S - (clock() - began)
                if left < MIN_REVIEW_S:
                    raise ReviewFailed("ran out of time", "before the review")
                try:
                    outcome.review = review_mod.review(llm, text, target.label, target.kind, rendered,
                                                       timeout_s=min(review_mod.CALL_TIMEOUT_S, left), claims=claims)
                except ReviewError as e:
                    raise ReviewFailed(e.reason, "infra" if e.infra else "answer") from None
                except OfflineError:
                    raise
                except Exception as e:  # noqa: BLE001 - M1: whatever else goes wrong is a review that did not come
                    raise ReviewFailed("the review wasn't readable", type(e).__name__) from None
                ev.model = outcome.review.model
        except ChromeStale as f:
            # Nothing was rendered, so the review does not count against the day (the count is for work started), and the
            # reason stays "update Chrome" instead of turning into "daily limit" after ten tries. Logged once a day.
            outcome.review, outcome.reason = None, f.reason
            release_slot(store, key)
            _warn_old_chrome(store, settings, now, f)
        except _Failed as f:
            outcome.review, outcome.reason = None, f.reason
            log.warning("site review: %s %s: %s (%s) in %.1fs", target.kind, target.label, f.reason, f.detail,
                        clock() - t0)
        else:
            log.info("site review: %s %s: reviewed in %.1fs (%s)", target.kind, target.label, clock() - t0,
                     outcome.review.model if outcome.review else "?")
    out.line = _line(out.outcomes)
    out.notes = _notes(out.outcomes)
    return out


def _render(target: Target, renderers: Renderers, settings: Settings, read_html: Callable[[FileRef], str],
            budget: float) -> Rendered:
    """Render one target. RenderFailed with a short reason for anything that goes wrong, an exception nobody planned
    for included (M1); OfflineError passes."""
    try:
        if target.kind == "link":
            assert target.url is not None
            return renderers.link(target.url, chrome=settings.chrome_path, budget_s=budget,
                                  refused_networks=getattr(settings, "site_review_refused_networks", ()))
        assert target.ref is not None
        try:
            html_text = read_html(target.ref)
        except AttachmentError as e:
            raise RenderFailed(e.reason, "attachment") from None
        except OfflineError:
            raise
        except Exception as e:  # noqa: BLE001 - the read is Slack's; whatever it throws is "couldn't read it"
            raise RenderFailed("couldn't read the file", type(e).__name__) from None
        if len(html_text.encode("utf-8", "ignore")) > HTML_MAX_BYTES:
            raise RenderFailed("the file is too large", "bytes")
        return renderers.html(html_text, chrome=settings.chrome_path, budget_s=budget)
    except chrome.ChromeOutOfDate as e:
        raise ChromeStale(e.reason, e.detail, e.version, e.age_days) from None
    except RenderError as e:
        raise RenderFailed(e.reason, e.detail) from None
    except OfflineError:
        raise
    except _Failed:
        raise  # the attachment's own reasons, above
    except Exception as e:  # noqa: BLE001 - M1: a renderer that breaks in a way nobody planned is a page that could not be
        # loaded, not a failed feedback run; only its type is kept, never its message (it may hold page text)
        raise RenderFailed("couldn't load the page", type(e).__name__) from None
