from datetime import date, datetime, timezone

import pytest

from haseeb_agent.metrics import Stats, render_weekly, week_stats
from haseeb_agent.models import Item
from haseeb_agent.store import Store

WEEK = date(2026, 10, 19)  # a Monday
FRI = date(2026, 10, 23)
NOW = datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc)
N = [0]


def item(kind="insight", day=WEEK, status="posted", **kw):
    N[0] += 1
    base = dict(
        id=f"{kind}-{day.isoformat()}-{N[0]}", kind=kind, day=day, status=status, body="b", title="t",
        dm_ts="1.1", source_ts=None, score=None, models={}, attempts=0, edited=False,
        created_at=NOW, acted_at=None,
    )
    base.update(kw)
    return Item(**base)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "agent.sqlite")


def add(store, **kw):
    it = item(**kw)
    store.upsert_item(it)
    return it


def test_intervention_rate(store):
    for d in range(3):
        add(store, day=date(2026, 10, 19 + d))
    add(store, day=date(2026, 10, 22), edited=True)
    s = week_stats(store, WEEK, today=FRI)
    assert (s.posted, s.edited_then_posted) == (4, 1)
    assert s.intervention_rate == 0.25


def test_missed_days_counts_workdays_only(store):
    add(store, day=date(2026, 10, 19))                      # Mon posted
    add(store, day=date(2026, 10, 20), status="delivered")  # Tue delivered, never posted
    add(store, day=date(2026, 10, 21), kind="feedback")     # Wed: only a feedback post, not an insight
    add(store, day=date(2026, 10, 24))                      # Sat: a weekend post rescues nothing
    s = week_stats(store, WEEK, today=date(2026, 10, 25))   # looked at on Sunday
    assert s.missed_days == 4                               # Tue, Wed, Thu, Fri; Sat and Sun are not workdays


def test_missed_days_ignores_days_that_have_not_happened(store):
    add(store, day=date(2026, 10, 19))
    s = week_stats(store, WEEK, today=date(2026, 10, 21))  # Wednesday
    assert s.missed_days == 2  # Tue and Wed; Thu and Fri are still to come


def test_missed_days_follows_the_workday_list(store):
    s = week_stats(store, WEEK, workdays={0, 1}, today=date(2026, 10, 25))
    assert s.missed_days == 2


def test_no_posts_rate_none(store):
    add(store, status="delivered")
    s = week_stats(store, WEEK, today=FRI)
    assert s.posted == 0
    assert s.intervention_rate is None
    assert s.avg_score is None


def test_only_the_asked_week_counts(store):
    add(store, day=date(2026, 10, 16))  # previous Friday
    add(store, day=date(2026, 10, 26))  # next Monday
    add(store, day=date(2026, 10, 20))
    assert week_stats(store, WEEK, today=date(2026, 10, 30)).posted == 1


def test_skipped_and_ignored(store):
    add(store, day=date(2026, 10, 19), status="skipped")
    add(store, day=date(2026, 10, 20), status="delivered")  # left alone past its day
    add(store, day=FRI, status="delivered")                  # still today: not ignored yet
    add(store, day=date(2026, 10, 21), status="failed")
    s = week_stats(store, WEEK, today=FRI)
    assert (s.skipped, s.ignored, s.self_posted) == (1, 1, 0)


def test_todo_haseeb_posted_himself_is_not_a_skip(store):
    # After the 15:00 pre-draft was sent (dm_ts set), Haseeb posted his own todo: jobs/todo.py marks it skipped
    # and sets manual_todo:{id}.
    own = add(store, kind="todo", day=date(2026, 10, 20), status="skipped", dm_ts="1.5")
    store.set_cursor(f"manual_todo:{own.id}", "2026-10-20T15:10:00+03:00")
    no_dm = add(store, kind="todo", day=date(2026, 10, 21), status="skipped", dm_ts=None)
    store.set_cursor(f"manual_todo:{no_dm.id}", "2026-10-21T15:10:00+03:00")
    add(store, kind="todo", day=date(2026, 10, 22), status="skipped", dm_ts="1.6")  # a real `skip`
    s = week_stats(store, WEEK, today=FRI)
    assert (s.skipped, s.self_posted, s.posted) == (1, 2, 0)
    assert s.intervention_rate is None  # not in the rate either


def test_a_skipped_item_that_never_reached_the_dm_is_not_a_skip(store):
    add(store, status="skipped", dm_ts=None)
    s = week_stats(store, WEEK, today=FRI)
    assert (s.skipped, s.self_posted) == (0, 0)


def test_marker_on_an_item_later_posted_by_command_is_just_posted(store):
    it = add(store, kind="todo", status="posted")
    store.set_cursor(f"manual_todo:{it.id}", "x")
    s = week_stats(store, WEEK, today=FRI)
    assert (s.posted, s.self_posted, s.skipped) == (1, 0, 0)


def test_todo_autopost_counts_as_posted_unchanged(store):
    auto = add(store, kind="todo", day=FRI)
    store.set_cursor(f"autopost_try:{auto.id}", "2026-10-23T15:03:00+03:00")
    by_hand = add(store, kind="todo", day=date(2026, 10, 22))
    store.set_cursor(f"permalink:{by_hand.id}", "https://x")
    s = week_stats(store, WEEK, today=FRI)
    assert (s.posted, s.auto_posted, s.edited_then_posted) == (2, 1, 0)
    assert s.intervention_rate == 0.0


def test_autopost_that_failed_is_not_counted(store):
    failed = add(store, kind="todo", status="failed")
    store.set_cursor(f"autopost_try:{failed.id}", "x")
    s = week_stats(store, WEEK, today=FRI)
    assert (s.posted, s.auto_posted) == (0, 0)


def test_average_score_covers_posted_items_that_have_one(store):
    add(store, score=90.0)
    add(store, score=86.0, day=date(2026, 10, 20))
    add(store, day=date(2026, 10, 21))                      # todo-like, no score
    add(store, day=date(2026, 10, 22), score=50.0, status="skipped")
    assert week_stats(store, WEEK, today=FRI).avg_score == 88.0


# ---- render_weekly ----------------------------------------------------------

def stats(**kw):
    base = dict(posted=9, edited_then_posted=2, skipped=3, ignored=1, auto_posted=1, missed_days=0,
                avg_score=88.4, intervention_rate=2 / 9, week_start=WEEK)
    base.update(kw)
    return Stats(**base)


def test_render_weekly_sample():
    text = render_weekly(stats())
    assert text.splitlines()[0] == "**Haseeb's Agent: weekly report, Mon 19 Oct**"
    assert "Posted: 9 (2 edited first, 1 auto-posted)" in text
    assert "Skipped: 3 · Not used (alternates): 1" in text  # M5: was "Ignored"
    assert "Ignored" not in text
    assert "Todo posted by Haseeb himself: 0" in text
    assert "Human intervention: 22% (edited then posted ÷ posted)" in text
    assert "Average quality score: 88/100" in text
    assert "Missed days: 0" in text


def test_render_weekly_without_data():
    text = render_weekly(stats(posted=0, edited_then_posted=0, auto_posted=0, avg_score=None,
                               intervention_rate=None, missed_days=5))
    assert "Human intervention: n/a (nothing was posted)" in text
    assert "Average quality score: n/a" in text
    assert "Missed days: 5" in text


def test_render_weekly_is_defused():
    # Nothing in the stats is free text, but the output still goes through render's sanitizer.
    text = render_weekly(stats(week_start=None))
    assert "<" not in text
    assert text.startswith("**Haseeb's Agent: weekly report**")


def test_render_weekly_floors_the_score_like_the_model_line():
    assert "Average quality score: 84/100" in render_weekly(stats(avg_score=84.9))



# ---- M5: "Not used (alternates)" and the days line ------------------------------------------------------------

def test_m5_days_with_an_insight_posted(store):
    add(store, day=date(2026, 10, 19))                      # Mon posted
    add(store, day=date(2026, 10, 19))                      # a second Mon insight: still one day
    add(store, day=date(2026, 10, 20), status="delivered")  # Tue: an unused alternate, nothing posted
    add(store, day=date(2026, 10, 21))                      # Wed posted
    s = week_stats(store, WEEK, today=date(2026, 10, 22))   # Thursday
    assert (s.insight_days, s.workdays_so_far) == (2, 4)
    assert "Days with an insight posted: 2 of 4" in render_weekly(s)


def test_m5_not_used_counts_the_alternates_left_in_the_dm(store):
    add(store, day=date(2026, 10, 19))                      # the one Haseeb posted
    add(store, day=date(2026, 10, 19), status="delivered")  # two alternates he did not pick
    add(store, day=date(2026, 10, 19), status="delivered")
    s = week_stats(store, WEEK, today=date(2026, 10, 20))
    assert s.ignored == 2 and "Not used (alternates): 2" in render_weekly(s)
