"""The checks before any designer or cover Codex run (the review of 466e622). Both fail closed.

1. The version pin. The read restriction rests on `default_permissions` / `permissions.<name>` (codex.permission_config).
   A Codex that ignored those keys would fall back to reading the whole disk with no error. So designer and cover
   Codex run only when `codex --version` reports exactly the pinned version (config `codex_version`, 0.163.0).
2. The preflight, once per process and kind: `codex debug prompt-input` with the identical -c, --enable and --disable
   set, from the work dir. It renders the resolved permissions without a model call. The profile must be restricted
   and hold `:minimal` read and the -C dir (write for the deck, read for the cover). Every other readable or
   writable entry fails it (`:root`, `:slash_tmp`, `:tmpdir`, /tmp, any other path), except Codex's own arg0 helper.
   Deny entries are fine. There must be exactly one <permission_profile> block, and the permissions instructions
   must say "Network access is restricted." (prompt-input renders network only in that sentence).
   `codex debug prompt-input` reads ~/.codex/config.toml, while our `exec` runs pass --ignore-user-config. That drift
   only goes the safe way: the -c overrides win in both, and the user config can only add to what the preflight
   sees (and refuses), never take a restriction away from the exec run.
   The preflight runs from a fresh, empty work dir, so nothing left from a crashed run is in it.

A refusal logs one line (a reason, no content) and the caller uses the template deck or cover.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

from ..offline import guard
from .codex import _env

log = logging.getLogger(__name__)

CODEX_VERSION = "0.163.0"
CHECK_TIMEOUT_S = 60
_VERSION = re.compile(r"codex-cli (\S+)")
_PROFILE = re.compile(r"<permission_profile\b[^>]*>(.*?)</permission_profile>", re.DOTALL)
_ENTRY = re.compile(r'<entry access="([a-z]+)"[^>]*><(special|path)>([^<]*)</\2></entry>')
NETWORK_RESTRICTED = "Network access is restricted."
NETWORK_ENABLED = "Network access is enabled"
_ARG0 = re.compile(re.escape(str(Path.home() / ".codex" / "tmp" / "arg0-macos")) + r"/codex-arg0[A-Za-z0-9]+")

_REAL_RUN = subprocess.run  # I1: captured at import; the default runner is offline-guarded against this, not
# against `subprocess.run` at call time (a test may have faked it while the default still holds the real one)

_versions: dict[str, str | None] = {}  # codex path -> its version, once per process
_preflights: dict[tuple[str, str], str | None] = {}  # (codex path, kind) -> refusal reason or None


def reset() -> None:
    """Forget the per-process results (tests)."""
    _versions.clear()
    _preflights.clear()


def _run(runner, argv: list[str], **kw):
    if runner is _REAL_RUN:
        guard("codex")
    return runner(argv, **kw)


def _version(codex: str, runner) -> str | None:
    if codex not in _versions:
        try:
            r = _run(runner, [codex, "--version"], env=_env(codex), capture_output=True, text=True,
                     timeout=CHECK_TIMEOUT_S, stdin=subprocess.DEVNULL)
            m = _VERSION.fullmatch((r.stdout or "").strip()) if r.returncode == 0 else None
            _versions[codex] = m.group(1) if m else None
        except (OSError, subprocess.SubprocessError):
            _versions[codex] = None
    return _versions[codex]


def version_ok(codex: str, version: str = CODEX_VERSION, runner=subprocess.run) -> bool:
    return _version(codex, runner) == version


def check_profile(output: str, work: Path, *, write: bool) -> str | None:
    """None when prompt-input's rendered permissions are the expected restricted profile, else a short reason."""
    text = (output or "").replace('\\"', '"')
    found = _PROFILE.findall(text)
    if len(found) != 1:
        return "no permissions profile" if not found else "more than one permissions profile"
    if NETWORK_RESTRICTED not in text or NETWORK_ENABLED in text:
        return "network not shown as restricted"
    body = found[0]
    if '<file_system type="restricted">' not in body:
        return "file system not restricted"
    entries = _ENTRY.findall(body)
    if body.count("<entry") != len(entries):
        return "unreadable entry"
    roots = {str(work), os.path.realpath(work)}
    minimal = own = False
    for access, kind, value in entries:
        if access in ("deny", "none"):
            continue
        if (access, kind, value) == ("read", "special", ":minimal"):
            minimal = True
        elif kind == "path" and value in roots and access == ("write" if write else "read"):
            own = True
        elif not (access == "read" and kind == "path" and _ARG0.fullmatch(value)):
            return f"unexpected {access} entry" + (f" {value}" if kind == "special" else "")
    if not minimal:
        return "no :minimal read"
    if not own:
        return "no work-dir entry"
    return None


def _preflight_argv(codex: str, argv: list[str]) -> list[str]:
    out = [codex, "debug", "prompt-input"]
    for i, a in enumerate(argv[:-1]):
        if a in ("-c", "--enable", "--disable"):
            out += [a, argv[i + 1]]
    return out + ["preflight"]


def codex_check(codex: str, *, kind: str, argv: list[str], cwd: Path, write: bool, version: str = CODEX_VERSION,
                runner=subprocess.run) -> str | None:
    """None when this Codex may run `argv` from `cwd`; else the reason ("version", or the preflight's)."""
    found = _version(codex, runner)
    if found != version:
        if (codex, "version") not in _preflights:
            _preflights[(codex, "version")] = "version"
            log.warning("codex: version %s is not the pinned %s; designer decks and covers paused",
                        found or "unknown", version)
        return "version"
    key = (codex, kind)
    if key not in _preflights:
        try:
            r = _run(runner, _preflight_argv(codex, argv), cwd=cwd, env=_env(codex), capture_output=True, text=True,
                     timeout=CHECK_TIMEOUT_S, stdin=subprocess.DEVNULL)
            reason = check_profile(r.stdout or "", Path(cwd), write=write) if r.returncode == 0 else "preflight exit"
        except (OSError, subprocess.SubprocessError):
            reason = "preflight could not run"
        _preflights[key] = reason
        if reason:
            log.warning("codex: the %s permissions preflight failed (%s); using the template", kind, reason)
    return _preflights[key]
