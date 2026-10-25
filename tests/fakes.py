"""Test doubles shared by later tasks.

    from tests.fakes import FakeSlack

    fake = FakeSlack()
    fake.add_message("C1", "1000.000001", "hi", user_id="U9")        # seed a channel page
    fake.add_thread("C1", parent, [reply1, reply2])                    # seed a thread (Msg objects)
    fake.files["F1"] = FileContent(text="notes", local_path=None, mimetype="text/markdown")
    fake.fail_next("boom")                                             # the next call (any method) raises SlackError
    fake.fail_next("boom", method="react")                             # only the next react() raises; others succeed
    fake.profile_text = "..."                                          # what read_profile() (slack-check) returns
    fake.channels = [Channel("C093L3Y8UM6", "survive-ai", False)]      # what list_channels() returns, in this order
    fake.channel_reads                                                 # how many times list_channels() was called
    ...
    fake.sent / fake.reactions / fake.uploads                          # what the code under test did

Limits: send() puts a top-level message on the channel page and a threaded send into the thread view, but a
threaded send does NOT update the parent's reply_count or latest_reply on the channel page. Tests that depend on
those fields must seed them by hand (add_message(..., reply_count=, latest_reply=)).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from haseeb_agent.models import Channel, Msg
from haseeb_agent.slack_gateway import FileContent, SlackError, SlackPaused, SlackTimeout


@dataclass(frozen=True)
class Sent:
    channel_id: str
    text: str
    thread_ts: str | None
    ts: str


@dataclass(frozen=True)
class Reaction:
    channel_id: str
    ts: str
    emoji: str


@dataclass(frozen=True)
class Upload:
    channel_id: str
    path: Path
    title: str
    thread_ts: str | None
    comment: str | None


def _key(ts: str) -> tuple[int, int]:
    sec, _, frac = ts.partition(".")
    return int(sec), int(frac or 0)


class FakeSlack:
    def __init__(self, self_id: str = "U08CZCGJW3E", start_ts: int = 1_800_000_000):
        self.self_id = self_id
        self.sent: list[Sent] = []
        self.reactions: list[Reaction] = []
        self.uploads: list[Upload] = []
        self.files: dict[str, FileContent] = {}
        self.pages: dict[str, list[Msg]] = {}               # channel -> top-level messages, any order
        self.threads: dict[tuple[str, str], tuple[Msg, list[Msg]]] = {}
        self._fail: tuple[SlackError, str | None] | None = None
        self._next = start_ts
        self.profile_text: str | None = None
        self.channels: list[Channel] = []   # what list_channels() returns
        self.channel_reads = 0              # list_channels() calls, the failed ones too

    # -- scripting --------------------------------------------------------------------------

    def fail_next(self, message: str = "scripted failure", method: str | None = None) -> None:
        """The next call raises SlackError(message) and has no effect. With `method` (read_channel, read_thread,
        send, react, read_file, upload_pdf, read_profile, list_channels) only the next call of that method fails;
        other methods are untouched and the failure stays armed until that method is called."""
        self._fail = (SlackError(message), method)

    def add_message(
        self, channel_id: str, ts: str, text: str, *, user_id: str = "U1", user_name: str = "User",
        is_bot: bool = False, files=None, reply_count: int = 0, latest_reply: str | None = None,
    ) -> Msg:
        msg = Msg(
            ts=ts, user_id=user_id, user_name=user_name, text=text,
            thread_ts=ts if reply_count else None, reply_count=reply_count, latest_reply=latest_reply,
            files=list(files or []), is_bot=is_bot,
        )
        self.pages.setdefault(channel_id, []).append(msg)
        return msg

    def add_thread(self, channel_id: str, parent: Msg, replies: list[Msg]) -> None:
        self.threads[(channel_id, parent.ts)] = (parent, replies)

    def _check(self, method: str) -> None:
        if self._fail is not None and self._fail[1] in (None, method):
            err, self._fail = self._fail[0], None
            raise err

    def _new_ts(self) -> str:
        self._next += 1
        return f"{self._next}.000001"

    # -- SlackGateway -----------------------------------------------------------------------

    def read_channel(self, channel_id: str, oldest: str | None, limit: int = 50, *,
                     latest: str | None = None) -> list[Msg]:
        self._check("read_channel")
        msgs = [m for m in self.pages.get(channel_id, []) if (oldest is None or _key(m.ts) > _key(oldest))
                and (latest is None or _key(m.ts) < _key(latest))]
        return sorted(msgs, key=lambda m: _key(m.ts), reverse=True)[:limit]  # newest first, like the connector

    def read_thread(self, channel_id: str, ts: str) -> tuple[Msg, list[Msg]]:
        self._check("read_thread")
        try:
            return self.threads[(channel_id, ts)]
        except KeyError:
            raise SlackError("thread unreadable: not seeded") from None

    def send(self, channel_id: str, text: str, thread_ts: str | None = None) -> str:
        self._check("send")
        ts = self._new_ts()
        self.sent.append(Sent(channel_id, text, thread_ts, ts))
        if thread_ts is None:
            self.add_message(channel_id, ts, text, user_id=self.self_id)
        else:
            parent, replies = self.threads.get((channel_id, thread_ts), (None, []))
            if parent is None:
                parent = next((m for m in self.pages.get(channel_id, []) if m.ts == thread_ts), None)
            if parent is not None:
                reply = Msg(ts=ts, user_id=self.self_id, user_name="Me", text=text, thread_ts=thread_ts,
                            reply_count=0, latest_reply=None)
                self.threads[(channel_id, thread_ts)] = (parent, [*replies, reply])
        return ts

    def react(self, channel_id: str, ts: str, emoji: str) -> None:
        self._check("react")
        self.reactions.append(Reaction(channel_id, ts, emoji.strip(":")))

    def read_file(self, file_id: str) -> FileContent:
        self._check("read_file")
        try:
            return self.files[file_id]
        except KeyError:
            raise SlackError(f"slack_read_file: file not found: {file_id}") from None

    def upload_pdf(
        self, channel_id: str, path: Path, title: str, thread_ts: str | None, comment: str | None
    ) -> None:
        self._check("upload_pdf")
        self.uploads.append(Upload(channel_id, Path(path), title, thread_ts, comment))

    def read_profile(self) -> str:
        """The connected account's own profile, as the connector's text. By default the account is `self_id`."""
        self._check("read_profile")
        if self.profile_text is not None:
            return self.profile_text
        return json.dumps({"result": f"Display name: test\nUser ID: {self.self_id}\nEmail: test@example.com\n"})

    def list_channels(self) -> list[Channel]:
        """The channels Haseeb is a member of, as scripted in `channels`. (The real gateway never returns an empty list:
        a page with no channel in it raises SlackError. An unscripted fake returns [] so a test can see what the code
        under test does with one.)"""
        self.channel_reads += 1
        self._check("list_channels")
        return list(self.channels)


class PausedWrites:
    """A mixin for a fake behind the Slack gateway's pause (the dashboard's Pause): while `paused`, `send`, `react` and
    `upload_pdf` raise SlackPaused before they do anything, as the gateway does before any call, so nothing is sent or
    recorded; `refused` lists the writes it turned away. Reads work. `paused = False` is the Resume.

        class PausedSlack(PausedWrites, FakeSlack): ...
    """
    paused = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.refused: list[str] = []

    def _refuse(self, what: str) -> None:
        if self.paused:
            self.refused.append(what)
            raise SlackPaused(f"slack_{what}: the agent is paused since 2026-10-20T10:00:00+03:00, nothing was sent")

    def send(self, channel_id, text, thread_ts=None):
        self._refuse("send")
        return super().send(channel_id, text, thread_ts)

    def react(self, channel_id, ts, emoji):
        self._refuse("react")
        return super().react(channel_id, ts, emoji)

    def upload_pdf(self, channel_id, path, title, thread_ts, comment):
        self._refuse("upload_pdf")
        return super().upload_pdf(channel_id, path, title, thread_ts, comment)


class PausedSlack(PausedWrites, FakeSlack):
    pass


TIMEOUT = "slack_send_message: tool-call mismatch: no call seen (timeout after 180s)"


class LandsThenTimesOut(FakeSlack):
    """R43: after `skip` good sends, the next `timeouts` sends raise SlackTimeout (the stream is lost). By default
    they reach the DM first (`lands`); with `lands=False` they do not. Later sends succeed. `reads` logs every
    read_channel and read_thread call."""

    def __init__(self, timeouts: int = 1, lands: bool = True, skip: int = 0):
        super().__init__()
        self.timeouts, self.lands, self.skip = timeouts, lands, skip
        self.reads: list[tuple[str, str | None]] = []

    def send(self, channel_id, text, thread_ts=None):
        if self.skip > 0:
            self.skip -= 1
        elif self.timeouts > 0:
            self.timeouts -= 1
            if self.lands:
                super().send(channel_id, text, thread_ts)
            raise SlackTimeout(TIMEOUT)
        return super().send(channel_id, text, thread_ts)

    def read_channel(self, channel_id, oldest, limit=50, *, latest=None):
        self.reads.append(("channel", None))
        if latest is None:
            return super().read_channel(channel_id, oldest, limit)
        return super().read_channel(channel_id, oldest, limit, latest=latest)

    def read_thread(self, channel_id, ts):
        self.reads.append(("thread", ts))
        return super().read_thread(channel_id, ts)
