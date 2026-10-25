"""The repository's `data/site-reviews/` is where the agent keeps the screenshots of the pages it has looked at (for 7 days). A
test that turns the site review on with the default `db_path` ("data/agent.sqlite", relative) writes its fake screenshots
there, into the working directory. No test may: it puts files in the live agent's data folder when the suite is run in the
live checkout, and it leaves litter in a worktree. A test gives its settings a `db_path` inside its `tmp_path`.

`conftest.py` snapshots this folder around every test and fails the one that changed it. The check is for a checkout where
nothing else writes there: in the live checkout the agent's own site review (a few times a day) could trip it, so run the
suite in a worktree. Set HASEEB_TEST_GUARD_ALL_DATA=1 to guard the whole `data/` folder instead (nothing but the live agent
should write there; the suite was run that way once, in a worktree, to find every test that did)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALL_ENV = "HASEEB_TEST_GUARD_ALL_DATA"


def guarded_root() -> Path:
    return ROOT / "data" if os.environ.get(ALL_ENV) else ROOT / "data" / "site-reviews"


def snapshot(root: Path | None = None) -> dict[str, tuple[int, int]]:
    """The folder itself and every path under it, each with its modification time (ns) and size; empty when it is not there.
    A folder's own time moves when something is added to it or removed from it."""
    root = root or guarded_root()
    seen: dict[str, tuple[int, int]] = {}
    if not os.path.lexists(root):
        return seen
    for path in [str(root), *(os.path.join(d, n) for d, dirs, files in os.walk(root) for n in (*dirs, *files))]:
        try:
            st = os.lstat(path)
        except OSError:  # gone since it was listed
            continue
        seen[path] = (st.st_mtime_ns, st.st_size)
    return seen


def changes(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> list[str]:
    """The paths that are new, gone or changed between two snapshots, sorted."""
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


class Watch:
    """Snapshots the guarded folder when it is made; `finish` says what a test changed in it, and `message` says it in words."""

    def __init__(self, root: Path | None = None):
        self.root = root or guarded_root()
        self.before = snapshot(self.root)

    def finish(self) -> list[str]:
        return changes(self.before, snapshot(self.root))

    def message(self, changed: list[str]) -> str:
        shown = ", ".join(os.path.relpath(p, self.root.parent) for p in changed[:4]) + (" ..." if len(changed) > 4 else "")
        return (f"this test wrote under the repository's {os.path.relpath(self.root, ROOT)}/ ({shown}): "
                "give its settings a db_path inside its tmp_path")
