"""A subprocess.run stand-in that runs the command in its own session and process group, so a timeout kills the
whole group (Chrome's helpers, Codex's node and code-mode children), not only the direct child."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from haseeb_agent.offline import guard


def _kill_group(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)  # start_new_session: the group id is the child's pid
    except (ProcessLookupError, PermissionError):
        pass


def run_group(argv: list[str], *, timeout: float, capture_output: bool = True, text: bool = True, stdin=None,
              cwd=None, env=None) -> subprocess.CompletedProcess:
    if argv and Path(str(argv[0])).name == "codex":
        guard("codex")
    pipe = subprocess.PIPE if capture_output else None
    p = subprocess.Popen(argv, stdout=pipe, stderr=pipe, stdin=stdin, cwd=cwd, env=env, text=text,
                         start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(p)
        try:
            p.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # something outside the group still holds the pipes
            pass
        raise subprocess.TimeoutExpired(argv, timeout) from None
    except BaseException:  # the tick watchdog, Ctrl-C: never leave the group behind
        _kill_group(p)
        raise
    return subprocess.CompletedProcess(argv, p.returncode, out, err)
