"""Model routing. Every draft, judgement and embedding goes through `LLM`.

A step name (config.toml [models]) maps to an ordered list of models:
  litellm:<model>  -> the company LiteLLM proxy (OpenAI-compatible)
  claude:<model>   -> headless Claude via claude_cli.run_claude
Each model is tried in order; HTTP errors, timeouts and empty content fall through.
A call may carry images (`complete(..., images=[Image(...)])`): the same chat request, whose user message becomes a list
of parts (the text, then one `image_url` data URI per image), for the `litellm:` models that read them. A `claude:` model
cannot take images, so it is skipped (a failed attempt that is not an outage).
The API key is never put into an error message, log line or repr, and neither are an image's bytes.
"""
from __future__ import annotations

import base64
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import httpx

from haseeb_agent.claude_cli import run_claude
from haseeb_agent.config import Settings
from haseeb_agent.offline import guard
from haseeb_agent.usage import recorded_post, stepped

_MOCK_TRANSPORT = httpx.MockTransport  # I1: captured at import, so a test's faked httpx attribute cannot hide a client

log = logging.getLogger(__name__)

TIMEOUT_S = 90
_JSON_INSTRUCTION = "Respond with only a single valid JSON object. No prose, no markdown fences."


class LLMError(RuntimeError):
    """`infra` is True only when every model failed on HTTP, a timeout, a connection error or a failed `claude`
    run: the models were unreachable, not wrong. Callers retry those instead of counting them as bad output (I8)."""

    def __init__(self, msg: str, *, infra: bool = False):
        super().__init__(msg)
        self.infra = infra


@dataclass(frozen=True)
class Completion:
    text: str
    model: str  # the model id without its litellm:/claude: prefix


IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 6 * 1024 * 1024


@dataclass(frozen=True)
class Image:
    """An image for a vision call: its media type and its bytes. The bytes go out as a base64 data URI and are never
    part of a repr, a log line or an error."""
    mime: str
    data: bytes = field(repr=False)

    def data_uri(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode('ascii')}"


class _Fail(Exception):
    """One model attempt failed. str() is status code / exception type only (safe for WARNING and
    LLMError); `detail` may hold response-body or exception text and is logged at DEBUG, scrubbed."""

    def __init__(self, msg: str, detail: str = "", *, infra: bool = False):
        super().__init__(msg)
        self.detail = detail
        self.infra = infra  # unreachable (HTTP, timeout, connection, claude run failed), not bad content


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"cosine: vector length mismatch ({len(a)} vs {len(b)})")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _infra_status(status: int) -> bool:
    """N6: an HTTP status that means the proxy or the model behind it is unreachable or overloaded: 5xx, 429 (rate
    limited) and 408 (request timeout). Every other status (400, 413, 422, ...) is deterministic for the request."""
    return status >= 500 or status in (408, 429)


def _split(spec: str) -> tuple[str, str]:
    provider, sep, model = spec.partition(":")
    if not sep or provider not in ("litellm", "claude") or not model:
        raise LLMError(f"bad model spec {spec!r}: expected litellm:<model> or claude:<model>")
    return provider, model


class LLM:
    def __init__(self, settings: Settings, http: httpx.Client | None = None, claude=run_claude):
        self._s = settings
        self._http = http or httpx.Client(timeout=TIMEOUT_S)
        self._claude = claude

    def __repr__(self) -> str:
        return f"LLM(base_url={self._s.litellm_base_url!r})"

    # -- helpers -----------------------------------------------------------
    def _scrub(self, text: str) -> str:
        key = self._s.litellm_api_key
        return text.replace(key, "***") if key else text

    @recorded_post  # the usage row of every request (tokens, the proxy's cost header): usage.py
    def _post(self, path: str, payload: dict, timeout_s: float | None = None) -> httpx.Response:
        # I1: only a test double (an httpx client on a MockTransport) skips the offline guard; anything else, even a
        # client whose transport cannot be told, counts as a real network client (fail closed).
        if not isinstance(getattr(self._http, "_transport", None), _MOCK_TRANSPORT):
            guard("litellm")
        limit = TIMEOUT_S if timeout_s is None else max(1.0, float(timeout_s))
        try:
            return self._http.post(
                f"{self._s.litellm_base_url.rstrip('/')}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {self._s.litellm_api_key}"},
                timeout=limit,
            )
        except httpx.TimeoutException:
            raise _Fail(f"timeout after {limit:g}s", infra=True) from None
        except httpx.HTTPError as e:
            raise _Fail(type(e).__name__, str(e), infra=True) from None

    # -- completion --------------------------------------------------------
    def _litellm_chat(self, model: str, system: str, user: str, max_tokens: int, json_mode: bool,
                      images: Sequence[Image] = (), timeout_s: float | None = None) -> str:
        content: str | list[dict] = user
        if images:  # the text first, then each image as a data URI (the OpenAI-style content array)
            content = [{"type": "text", "text": user},
                       *({"type": "image_url", "image_url": {"url": img.data_uri()}} for img in images)]

        def body(with_format: bool) -> dict:
            sys_text = system
            if json_mode:
                sys_text = f"{system}\n\n{_JSON_INSTRUCTION}"
            b = {
                "model": model,
                "messages": [{"role": "system", "content": sys_text}, {"role": "user", "content": content}],
                "max_tokens": max_tokens,
            }
            if with_format:
                b["response_format"] = {"type": "json_object"}
            return b

        resp = self._post("/v1/chat/completions", body(json_mode), timeout_s)
        if resp.status_code == 400 and json_mode:
            resp = self._post("/v1/chat/completions", body(False), timeout_s)  # model rejects response_format
        if resp.status_code != 200:
            # N6: only an unreachable or overloaded proxy is infra (retried later, not counted). A 4xx such as
            # 400/413/422 is deterministic for this request and fails like a bad answer would.
            raise _Fail(f"HTTP {resp.status_code}", resp.text, infra=_infra_status(resp.status_code))
        finish = None
        try:
            choice = resp.json()["choices"][0]
            finish = choice.get("finish_reason")
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise _Fail("length" if finish == "length" else "malformed response") from None
        if not isinstance(content, str) or not content.strip():
            # finish_reason "length" with no content: the token budget went on reasoning (status only, no body)
            raise _Fail("length" if finish == "length" else "empty content")
        return content

    def _claude_chat(self, model: str, system: str, user: str, json_mode: bool) -> str:
        parts = [f"SYSTEM:\n{system}", f"USER:\n{user}"]
        if json_mode:
            parts.append(_JSON_INSTRUCTION)
        try:
            r = self._claude("\n\n".join(parts), model=model, allowed_tools=[])
        except Exception as e:  # runner crashed; try the next model
            raise _Fail(type(e).__name__, str(e), infra=True) from None
        if not r.ok:
            raise _Fail("claude failed", r.error or "", infra=True)
        if not (r.final_text or "").strip():
            raise _Fail("empty content")
        return r.final_text

    @stepped()  # the usage rows of this call name the model step (insight_writer, judge, ...)
    def complete(self, step: str, system: str, user: str, *, max_tokens: int = 4000,
                 json_mode: bool = False, after: str | None = None, images: Sequence[Image] | None = None,
                 timeout_s: float | None = None) -> Completion:
        """`after` is a model id (as in Completion.model): try only the models that follow it in the
        step's chain. A caller whose output failed validation uses it to retry with the next model.
        `images` makes it a vision call (litellm models only; a claude model is skipped). `timeout_s` bounds each
        model's request (default TIMEOUT_S), for a caller that has a budget."""
        images = tuple(images or ())
        if len(images) > MAX_IMAGES or any(not isinstance(i, Image) or i.mime not in IMAGE_TYPES or not i.data
                                           or len(i.data) > MAX_IMAGE_BYTES for i in images):
            raise LLMError(f"bad images: at most {MAX_IMAGES}, each a png, jpeg, webp or gif of at most "
                           f"{MAX_IMAGE_BYTES // (1024 * 1024)} MB")
        chain = self._s.models.get(step)
        if not chain:
            raise LLMError(f"no models configured for step {step!r}")
        parsed = [(spec, *_split(spec)) for spec in chain]  # validate every spec before calling any
        if after is not None:
            ids = [model for _, _, model in parsed]
            if after not in ids:
                raise LLMError(f"model {after!r} is not in the chain for step {step!r}")
            parsed = parsed[ids.index(after) + 1:]
            if not parsed:
                raise LLMError(f"no models after {after!r} for step {step!r}")
        failures: list[str] = []
        infra = True
        for spec, provider, model in parsed:
            try:
                if provider == "litellm":
                    text = self._litellm_chat(model, system, user, max_tokens, json_mode, images, timeout_s)
                elif images:
                    raise _Fail("images not supported")  # a headless claude run reads text only
                else:
                    text = self._claude_chat(model, system, user, json_mode)
                return Completion(text=text, model=model)
            except _Fail as e:
                log.warning("step %s: %s failed: %s", step, spec, e)
                if e.detail:
                    log.debug("step %s: %s detail: %s", step, spec, self._scrub(e.detail)[:500])
                failures.append(f"{spec} ({e})")
                infra = infra and e.infra
        raise LLMError(f"all models failed for step {step!r}: " + "; ".join(failures), infra=infra)

    # -- embeddings --------------------------------------------------------
    @stepped("embed")
    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        chain = self._s.models.get("embed")
        if not chain:
            raise LLMError("no models configured for step 'embed'")
        spec = chain[0]
        provider, model = _split(spec)
        if provider != "litellm":
            raise LLMError(f"embed model must be litellm:<model>, got {spec!r}")
        try:
            resp = self._post("/v1/embeddings", {"model": model, "input": texts})
            if resp.status_code != 200:
                raise _Fail(f"HTTP {resp.status_code}", resp.text)
            try:
                data = sorted(resp.json()["data"], key=lambda d: d["index"])
                vecs = [[float(x) for x in d["embedding"]] for d in data]
            except (ValueError, KeyError, TypeError):
                raise _Fail("malformed response") from None
            if len(vecs) != len(texts):
                raise _Fail(f"expected {len(texts)} embeddings, got {len(vecs)}")
            return vecs
        except _Fail as e:
            if e.detail:
                log.debug("embed: %s detail: %s", spec, self._scrub(e.detail)[:500])
            raise LLMError(f"embedding failed with {spec} ({e})") from None
