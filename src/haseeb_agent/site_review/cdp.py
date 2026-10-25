"""A small Chrome DevTools protocol client over `--remote-debugging-pipe`.

Chrome reads commands from file descriptor 3 and writes replies and events to file descriptor 4, as NUL-terminated JSON
messages (`chrome.py` starts it that way, so there is no debugging port for any page, or any other process, to reach).
This client is blocking, single-threaded and bounded: every read waits with `select` against a deadline, so a Chrome that
hangs raises `CdpTimeout` instead of hanging the tick, and a Chrome that dies raises `CdpClosed`. Events that arrive while
a call waits for its reply are kept, in order, for `take`/`wait_event`. Nothing here ever logs a message body: page text and
URLs are data.
"""
from __future__ import annotations

import json
import os
import select
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


class CdpError(Exception):
    """A protocol command failed (Chrome answered with an error). `code` is the protocol's error code."""

    def __init__(self, method: str, code: int | None = None):
        super().__init__(f"{method} failed" + (f" ({code})" if code is not None else ""))
        self.method, self.code = method, code


class CdpTimeout(Exception):
    """Chrome did not answer within the time left."""


class CdpClosed(Exception):
    """Chrome closed its end of the pipe (it exited or crashed)."""


class Deadline:
    """A point in time on the monotonic clock, shared by everything one render does."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.end = clock() + seconds

    def left(self) -> float:
        return self.end - self._clock()

    def expired(self) -> bool:
        return self.left() <= 0


@dataclass
class Event:
    method: str
    params: dict
    session: str | None


class CdpPipe:
    """The two pipe ends (our read side of Chrome's fd 4, our write side of Chrome's fd 3) and the protocol on them."""

    MAX_MESSAGE = 64 * 1024 * 1024  # a screenshot or a page's text is far below this; a runaway reply is cut off

    def __init__(self, read_fd: int, write_fd: int, deadline: Deadline):
        self._r, self._w = read_fd, write_fd
        self.deadline = deadline
        self._buf = b""
        self._id = 0
        self._events: deque[Event] = deque()
        self._closed = False

    # ------------------------------------------------------------ reading

    def _message(self, timeout: float) -> dict | None:
        """The next whole message, or None when `timeout` ran out with none."""
        end = time.monotonic() + timeout
        while b"\0" not in self._buf:
            left = min(end - time.monotonic(), self.deadline.left())
            if left <= 0:
                return None
            ready, _, _ = select.select([self._r], [], [], left)
            if not ready:
                return None
            chunk = os.read(self._r, 262144)
            if not chunk:
                self._closed = True
                raise CdpClosed("chrome closed the pipe")
            self._buf += chunk
            if len(self._buf) > self.MAX_MESSAGE:
                raise CdpClosed("a message was too large")
        raw, self._buf = self._buf.split(b"\0", 1)
        try:
            msg = json.loads(raw)
        except ValueError:
            return {}  # not JSON: skipped
        return msg if isinstance(msg, dict) else {}

    def _file(self, msg: dict) -> None:
        if "method" in msg:
            p = msg.get("params")
            self._events.append(Event(str(msg["method"]), p if isinstance(p, dict) else {}, msg.get("sessionId")))

    # ------------------------------------------------------------ commands

    def call(self, method: str, params: dict | None = None, *, session: str | None = None,
             timeout: float = 15.0) -> dict:
        """Send one command and wait for its reply. CdpError when Chrome refuses it, CdpTimeout when nothing came in
        time, CdpClosed when Chrome is gone."""
        if self._closed:
            raise CdpClosed("chrome closed the pipe")
        self._id += 1
        mid = self._id
        msg: dict = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        try:
            os.write(self._w, json.dumps(msg).encode() + b"\0")
        except (BrokenPipeError, OSError):
            self._closed = True
            raise CdpClosed("chrome closed the pipe") from None
        end = time.monotonic() + timeout
        while True:
            m = self._message(max(0.0, end - time.monotonic()))
            if m is None:
                raise CdpTimeout(f"{method} did not answer in time")
            if m.get("id") == mid:
                if "error" in m:
                    err = m["error"] if isinstance(m["error"], dict) else {}
                    raise CdpError(method, err.get("code") if isinstance(err.get("code"), int) else None)
                res = m.get("result")
                return res if isinstance(res, dict) else {}
            self._file(m)

    # ------------------------------------------------------------ events

    def pump(self, seconds: float) -> None:
        """Read whatever arrives for up to `seconds` (events are kept)."""
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            m = self._message(left)
            if m is None:
                return
            self._file(m)

    def take(self) -> list[Event]:
        """Every event kept so far, oldest first, and forget them."""
        out = list(self._events)
        self._events.clear()
        return out

    def wait_event(self, method: str, *, session: str | None = None, timeout: float = 15.0,
                   sink: Callable[[Event], None] | None = None) -> Event | None:
        """The first event named `method` (for `session`) that arrives within `timeout`; None if none did. Every other
        event seen on the way goes to `sink` when one is given (so a caller can count them), else is kept."""
        end = time.monotonic() + timeout
        while True:
            while self._events:
                ev = self._events.popleft()
                if ev.method == method and (session is None or ev.session == session):
                    return ev
                if sink is not None:
                    sink(ev)
            m = self._message(max(0.0, end - time.monotonic()))
            if m is None:
                return None
            self._file(m)
