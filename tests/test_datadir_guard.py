"""The guard that keeps tests from writing under the repository's data/site-reviews/ (tests/datadir_guard.py, used by an autouse
fixture in conftest.py). These tests use a folder of their own: they never touch the real one."""
from __future__ import annotations

import os

from tests import datadir_guard as g


def test_a_folder_that_is_not_there_is_an_empty_snapshot_and_nothing_changes(tmp_path):
    root = tmp_path / "data" / "site-reviews"
    w = g.Watch(root)
    assert w.before == {} and w.finish() == []


def test_the_folder_appearing_even_empty_a_file_added_changed_and_removed_are_all_seen(tmp_path):
    root = tmp_path / "data" / "site-reviews"
    w = g.Watch(root)
    root.mkdir(parents=True)
    assert w.finish() == [str(root)]  # an empty folder is already a trace
    w = g.Watch(root)
    (root / "feedback-1").mkdir()
    (root / "feedback-1" / "link1-desktop.jpg").write_bytes(b"x")
    assert {os.path.basename(p) for p in w.finish()} == {"site-reviews", "feedback-1", "link1-desktop.jpg"}
    w = g.Watch(root)
    assert w.finish() == []  # nothing happened since
    (root / "feedback-1" / "link1-desktop.jpg").write_bytes(b"xy")  # same name, new content
    assert [os.path.basename(p) for p in w.finish()] == ["link1-desktop.jpg"]
    w = g.Watch(root)
    (root / "feedback-1" / "link1-desktop.jpg").unlink()
    assert {os.path.basename(p) for p in w.finish()} == {"feedback-1", "link1-desktop.jpg"}


def test_what_was_there_before_the_test_is_not_blamed_on_it(tmp_path):
    root = tmp_path / "site-reviews"
    (root / "feedback-old").mkdir(parents=True)
    (root / "feedback-old" / "a.jpg").write_bytes(b"old")
    w = g.Watch(root)
    assert w.finish() == []


def test_a_link_out_of_the_folder_is_not_followed(tmp_path):
    root, outside = tmp_path / "site-reviews", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "linked").symlink_to(outside)
    w = g.Watch(root)
    (outside / "new.txt").write_text("a change outside the guarded folder")
    assert w.finish() == []


def test_the_message_names_the_paths_and_says_what_to_do(tmp_path):
    root = tmp_path / "data" / "site-reviews"
    w = g.Watch(root)
    for i in range(6):
        (root / f"feedback-{i}").mkdir(parents=True, exist_ok=True)
    text = w.message(w.finish())
    assert "give its settings a db_path inside its tmp_path" in text
    assert text.count("feedback-") == 3 and "site-reviews" in text and text.count(" ...") == 1  # the first four paths, then "..."


def test_the_guarded_folder_is_the_site_reviews_one_unless_the_whole_data_folder_is_asked_for(monkeypatch):
    monkeypatch.delenv(g.ALL_ENV, raising=False)
    assert g.guarded_root() == g.ROOT / "data" / "site-reviews"
    monkeypatch.setenv(g.ALL_ENV, "1")
    assert g.guarded_root() == g.ROOT / "data"
