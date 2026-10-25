"""Shared fakes and shapes for the thread watch tests (follow-backs and replies in the threads of Haseeb's insights).

The two real threads of 2026-10-21 are the models, with every name, text and number invented or anonymised:
  - Iqbal's thread. A teammate posts a demo; Haseeb's feedback reply is posted; the teammate answers with four claimed fixes and
    a new build, @mentioning him; Haseeb answers by hand 6 minutes later.
  - Zakir's thread. Haseeb posts an insight; a teammate puts a full update of their own inside its thread (it was meant as a
    top-level post); Haseeb answers it by hand later.
In both, Haseeb's own message is newer than the teammate's reply, so nothing may be drafted for them.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from haseeb_agent.litellm import Completion
from haseeb_agent.models import FileRef, Item, Msg
from haseeb_agent.followback_prompt import CLASSES
from tests.test_feedback import CH, DRAFT, IQBAL
from tests.test_feedback_site_review import SiteLLM
from tests.test_insights import good_meta

KW = ZoneInfo("Asia/Kuwait")
ZAKIR = "U0ZAKIR0001"
ARSHMAN = "U06FH6HRG1Z"

# the real timestamps (2026-10-21, Kuwait time)
IQBAL_POST = "1792574403.734689"           # 12:20, Iqbal's post
HASEEB_FEEDBACK = "1792575100.000200"      # Haseeb's posted feedback reply in it (invented ts)
IQBAL_REPLY = "1792581205.027989"          # 14:13, Iqbal's reply: four fixes, build 3c5e08e, @mentions Haseeb
HASEEB_FOLLOWBACK = "1792581562.804599"    # 14:19, Haseeb's manual follow-back
MASTERCARD = "1792571842.122349"           # 11:37, Haseeb's insight "Mastercard opens Agent Pay for Machines"
ZAKIR_REPLY = "1792581035.472729"          # 14:10, Zakir's update inside its thread
HASEEB_ANSWER = "1792583555.926659"        # 14:52, Haseeb's answer by hand
NOW = datetime.fromtimestamp(1792590000, KW)  # 16:40, after all of them

IQBAL_TEXT = ("<@U08CZCGJW3E|Haseeb Zafar> Thanks for the review. Fixed all four: 1) the build opens on the Client view, "
              "2) none of the 70 page views scrolls sideways at 390 px, 3) red badges show only on decisions, 4) the build "
              "stamp shows in the corner. New build 3c5e08e is attached. Do you want an on-screen timer too?")
ZAKIR_TEXT = ("Update on the lead-sourcing workflow: it now runs end to end on its own. Last night's run pulled 51 leads from "
              "three sources, scored them against the ideal-customer profile and wrote the top 12 into the sheet, no manual "
              "step. Next: add the enrichment call and measure how many of the 12 reply.")
MASTERCARD_TITLE = "Mastercard opens Agent Pay for Machines"
HTML_BUILD = FileRef("F3C5E08E", "reach-3c5e08e.html", "text/html")
PAGE = "<!doctype html><title>Reach</title><h1>Client view</h1><p>build 3c5e08e</p>"

FOLLOW_OK = ("Opens on Client: seen. Sideways scroll on the phone: none. Red badges: not checked, needs the decisions view. "
             "Build stamp: not checked. Yes to the timer, as a small clock in the corner.")
FOLLOW_NUM = ("Opens on Client: seen. The 70 page views at 390 px: not checked. Red badges: not checked. "
              "Build stamp: not checked. Yes to the timer.")
REPLY_OK = "The article does not give the fee. Not verified: whether it applies to small merchants. Worth checking the issuer docs."


def msg(ts, user_id, name, text, thread_ts, *, files=(), is_bot=False) -> Msg:
    return Msg(ts=ts, user_id=user_id, user_name=name, text=text, thread_ts=thread_ts, reply_count=0, latest_reply=None,
               files=list(files), is_bot=is_bot)


def make_thread(slack, parent_ts, replies, *, author=IQBAL, name="Iqbal Qazi", text="Our command center prototype.") -> Msg:
    """Seed a thread: its parent on the channel page (with its reply count and newest reply, in whole seconds) and the
    thread as `read_thread` gives it. `replies` are Msgs. Returns the parent."""
    latest = str(int(float(replies[-1].ts))) if replies else None
    parent = slack.add_message(CH, parent_ts, text, user_id=author, user_name=name, reply_count=len(replies),
                               latest_reply=latest)
    slack.add_thread(CH, parent, list(replies))
    return parent


def posted_feedback(store, thread_ts=IQBAL_POST, *, acted=None, name="Iqbal Qazi", status="posted", body=DRAFT) -> Item:
    acted = acted or NOW - timedelta(days=1)
    item = Item(id=f"feedback-{thread_ts}", kind="feedback", day=acted.date(), status=status, body=body,
                title=f"Feedback: {name}", dm_ts="1.1", source_ts=thread_ts, score=90.0, models={}, attempts=0,
                edited=False, created_at=acted - timedelta(hours=1), acted_at=acted if status == "posted" else None)
    store.upsert_item(item)
    return item


def posted_insight(store, ts=MASTERCARD, *, n=1, acted=None, status="posted", title=MASTERCARD_TITLE) -> Item:
    """An insight Haseeb posted: the item (`posted`) and the cursor with the ts of its top-level post."""
    acted = acted or NOW - timedelta(days=1)
    meta = good_meta(n=n, batch=f"insight-t5-{n}")
    meta["insight"]["title"] = title
    meta["insight"]["what_happened"] = "Mastercard now lets software agents pay on a customer's behalf, with limits set by the card holder, starting with 100 pilot merchants."
    meta["insight"]["evidence"] = [{"title": "Mastercard press release", "url": "https://www.mastercard.com/news/agent-pay"},
                                   {"title": "Analyst note", "url": "https://example.com/analyst-note"}]
    item = Item(id=meta["batch"], kind="insight", day=acted.date(), status=status, body=json.dumps(meta), title=title,
                dm_ts="2.1", source_ts=None, score=88.0, models={}, attempts=0, edited=False,
                created_at=acted - timedelta(hours=2), acted_at=acted if status == "posted" else None)
    store.upsert_item(item)
    if status == "posted":
        store.set_cursor(f"post_ts:{item.id}", ts)
    return item


class ThreadLLM(SiteLLM):
    """The feedback job's scripted LLM and the vision step, plus the thread watch's three prompts: the follow-back writer, the
    writer of a reply on an insight and the sorter of a reply. Each has its own script (a text, an Exception, or for the
    sorter a class); the last entry repeats."""

    def __init__(self, *args, followback=(FOLLOW_OK,), insreply=(REPLY_OK,), sorting=("about_insight",), **kw):
        super().__init__(*args, **kw)
        self.scripts = {"followback": list(followback), "insreply": list(insreply), "sorting": list(sorting)}

    def complete(self, step, system, user, *, max_tokens=4000, json_mode=False, after=None, images=None, timeout_s=None):
        key = None
        if step == "feedback_writer" and "short follow-back for Haseeb" in system:
            key = "followback"
        elif step == "feedback_writer" and "a short reply for Haseeb Zafar, in the thread of an insight" in system:
            key = "insreply"
        elif step == "summarize" and system.startswith("You sort ONE reply"):
            key = "sorting"
        if key is None:
            return super().complete(step, system, user, max_tokens=max_tokens, json_mode=json_mode, after=after,
                                    images=images, timeout_s=timeout_s)
        self.calls.append(dict(step=step, system=system, user=user, after=after, max_tokens=max_tokens, key=key,
                               json_mode=json_mode))
        q = self.scripts[key]
        r = q.pop(0) if len(q) > 1 else q[0]
        if isinstance(r, Exception):
            raise r
        if key == "sorting" and r in CLASSES:
            r = json.dumps({"kind": r})
        return Completion(text=r, model="sort-m" if key == "sorting" else "writer-m")

    def keyed(self, key):
        return [c for c in self.calls if c.get("key") == key]


def claims_answer(claims, **over) -> str:
    """A vision answer in claims mode (the base review of tests.test_site_review_review.GOOD, plus the claims)."""
    from tests.test_site_review_review import GOOD
    return json.dumps({**GOOD, "claims": claims, **over})


VERIFIED_AND_UNCHECKED = [
    {"claim": "the build opens on the Client view", "status": "verified", "evidence": "the desktop screenshot shows the Client view"},
    # one first screen of one page cannot verify a claim about 70 views (review I3): the model may not say it, and the code would not let it
    {"claim": "none of the 70 page views scrolls sideways at 390 px", "status": "not_checked", "evidence": ""},
    {"claim": "red badges show only on decisions", "status": "not_checked", "evidence": ""},
    {"claim": "the build stamp shows in the corner", "status": "not_checked", "evidence": ""},
]
