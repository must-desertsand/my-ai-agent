import json

import httpx
import pytest

from haseeb_agent.claude_cli import ClaudeResult
from haseeb_agent.config import Settings
from haseeb_agent.litellm import LLM, Completion, Image, LLMError, cosine
from datetime import time
from zoneinfo import ZoneInfo

KEY = "sk-secret-key-123"


def make_settings(**models) -> Settings:
    chains = {
        "feedback_writer": ["litellm:deepseek-v4-pro", "litellm:glm-5.3"],
        "embed": ["litellm:qwen3-embedding"],
        "research": ["claude:sonnet"],
        "mixed": ["claude:haiku", "litellm:glm-5.3"],
    }
    chains.update(models)
    return Settings(
        channel_id="C1", self_dm_id="U1", haseeb_id="U1", lion_id="U2",
        tz=ZoneInfo("Asia/Kuwait"), huddle_time=time(14, 30), insights_ready=time(9, 0),
        workdays={0, 1, 2, 3, 4}, models=chains,
        litellm_base_url="https://litellm.test", litellm_api_key=KEY,
    )


def chat(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok_claude(text="from claude", ok=True, error=None):
    calls = []

    def fake(prompt, *, model, allowed_tools, **kw):
        calls.append({"prompt": prompt, "model": model, "allowed_tools": allowed_tools})
        return ClaudeResult(final_text=text, tool_results=[], cost_usd=0.0, ok=ok, error=error)

    fake.calls = calls
    return fake


def test_fallback_on_empty_content():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body["model"])
        assert req.headers["authorization"] == f"Bearer {KEY}"
        assert req.url == "https://litellm.test/v1/chat/completions"
        return chat("" if body["model"] == "deepseek-v4-pro" else "ok")

    out = LLM(make_settings(), http=client(handler)).complete("feedback_writer", "sys", "usr")
    assert out == Completion(text="ok", model="glm-5.3")
    assert seen == ["deepseek-v4-pro", "glm-5.3"]


def test_request_shape_and_default_max_tokens():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return chat("ok")

    LLM(make_settings(), http=client(handler)).complete("feedback_writer", "SYS", "USR")
    b = bodies[0]
    assert b["max_tokens"] == 4000
    assert b["messages"] == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USR"}]
    assert "response_format" not in b


def test_fallback_on_http_error_and_timeout():
    def handler(req):
        if json.loads(req.content)["model"] == "deepseek-v4-pro":
            raise httpx.ReadTimeout("slow", request=req)
        return chat("ok")

    assert LLM(make_settings(), http=client(handler)).complete("feedback_writer", "s", "u").model == "glm-5.3"

    def handler2(req):
        if json.loads(req.content)["model"] == "deepseek-v4-pro":
            return httpx.Response(500, text="boom")
        return chat("ok")

    assert LLM(make_settings(), http=client(handler2)).complete("feedback_writer", "s", "u").model == "glm-5.3"


def test_malformed_response_falls_through():
    def handler(req):
        if json.loads(req.content)["model"] == "deepseek-v4-pro":
            return httpx.Response(200, json={"choices": []})
        return chat("ok")

    assert LLM(make_settings(), http=client(handler)).complete("feedback_writer", "s", "u").model == "glm-5.3"


def test_all_fail_raises_with_models_listed():
    def handler(req):
        return chat("   ")

    with pytest.raises(LLMError) as e:
        LLM(make_settings(), http=client(handler)).complete("feedback_writer", "s", "u")
    msg = str(e.value)
    assert "deepseek-v4-pro" in msg and "glm-5.3" in msg and "feedback_writer" in msg


def test_unknown_step_raises():
    with pytest.raises(LLMError):
        LLM(make_settings(), http=client(lambda r: chat("x"))).complete("nope", "s", "u")


def test_claude_prefix_routes_to_runner():
    fake = ok_claude("claude says hi")

    def handler(req):
        raise AssertionError("litellm must not be called")

    out = LLM(make_settings(), http=client(handler), claude=fake).complete("research", "SYS", "USR")
    assert out == Completion(text="claude says hi", model="sonnet")
    assert fake.calls[0]["model"] == "sonnet"
    assert fake.calls[0]["allowed_tools"] == []
    assert fake.calls[0]["prompt"] == "SYSTEM:\nSYS\n\nUSER:\nUSR"


def test_claude_failure_falls_through_to_litellm():
    for bad in (ok_claude("", ok=True), ok_claude("x", ok=False, error="exit 1")):
        out = LLM(make_settings(), http=client(lambda r: chat("lit")), claude=bad).complete("mixed", "s", "u")
        assert out == Completion(text="lit", model="glm-5.3")


def test_claude_exception_falls_through():
    def boom(prompt, **kw):
        raise OSError("no claude")

    out = LLM(make_settings(), http=client(lambda r: chat("lit")), claude=boom).complete("mixed", "s", "u")
    assert out.model == "glm-5.3"


def test_json_mode_sends_response_format_and_retries_without_on_400():
    bodies = []

    def handler(req):
        b = json.loads(req.content)
        bodies.append(b)
        if "response_format" in b:
            return httpx.Response(400, text="unsupported")
        return chat('{"a": 1}')

    out = LLM(make_settings(), http=client(handler)).complete("feedback_writer", "SYS", "u", json_mode=True)
    assert out == Completion(text='{"a": 1}', model="deepseek-v4-pro")
    assert bodies[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in bodies[1]
    assert "json" in bodies[1]["messages"][0]["content"].lower()
    assert bodies[1]["messages"][0]["content"].startswith("SYS")


def test_json_mode_for_claude_adds_instruction():
    fake = ok_claude('{"a": 1}')
    LLM(make_settings(), http=client(lambda r: chat("x")), claude=fake).complete("research", "S", "U", json_mode=True)
    assert "only" in fake.calls[0]["prompt"].lower() and "json" in fake.calls[0]["prompt"].lower()


def test_api_key_never_in_error_message():
    def handler(req):
        return httpx.Response(401, text=f"bad key {KEY}")

    with pytest.raises(LLMError) as e:
        LLM(make_settings(), http=client(handler)).complete("feedback_writer", "s", "u")
    assert KEY not in str(e.value) and KEY not in repr(e.value)
    assert KEY not in repr(LLM(make_settings()))

    def handler2(req):
        raise httpx.ConnectError(f"cannot connect with {KEY}", request=req)

    with pytest.raises(LLMError) as e2:
        LLM(make_settings(), http=client(handler2)).complete("feedback_writer", "s", "u")
    assert KEY not in str(e2.value)


def test_embed_and_cosine():
    def handler(req):
        body = json.loads(req.content)
        assert req.url == "https://litellm.test/v1/embeddings"
        assert body == {"model": "qwen3-embedding", "input": ["a", "b"]}
        # returned out of order; must be sorted by index
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]})

    vecs = LLM(make_settings(), http=client(handler)).embed(["a", "b"])
    assert vecs == [[1.0, 0.0], [0.0, 1.0]]
    assert cosine([1, 0], [1, 0]) == 1.0
    assert cosine([1, 0], [0, 1]) == 0.0
    assert cosine([0, 0], [1, 0]) == 0.0


def test_embed_failure_raises_without_key():
    with pytest.raises(LLMError) as e:
        LLM(make_settings(), http=client(lambda r: httpx.Response(500, text=KEY))).embed(["a"])
    assert KEY not in str(e.value)
    assert "qwen3-embedding" in str(e.value)


def test_embed_count_mismatch_raises():
    with pytest.raises(LLMError):
        LLM(make_settings(), http=client(lambda r: httpx.Response(200, json={"data": []}))).embed(["a"])


def test_cosine_length_mismatch_raises():
    with pytest.raises(ValueError):
        cosine([1, 0], [1, 0, 0])


def test_error_message_has_status_but_not_body(caplog):
    import logging
    body = "SECRET-BODY-TEXT"
    with caplog.at_level(logging.WARNING):
        with pytest.raises(LLMError) as e:
            LLM(make_settings(), http=client(lambda r: httpx.Response(503, text=body))).complete(
                "feedback_writer", "s", "u")
    assert "HTTP 503" in str(e.value) and body not in str(e.value)
    assert body not in caplog.text
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LLMError):
            LLM(make_settings(), http=client(lambda r: httpx.Response(503, text=body))).complete(
                "feedback_writer", "s", "u")
    assert body in caplog.text


def test_bad_spec_later_in_chain_raises_before_any_call():
    calls = []

    def handler(req):
        calls.append(1)
        return chat("ok")

    s = make_settings(feedback_writer=["litellm:glm-5.3", "bogus"])
    with pytest.raises(LLMError, match="bogus"):
        LLM(s, http=client(handler)).complete("feedback_writer", "s", "u")
    assert calls == []


def test_after_restricts_to_models_following_the_given_one():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content)["model"])
        return chat("ok")

    s = make_settings(judge=["litellm:a", "litellm:b", "claude:c"])
    out = LLM(s, http=client(handler)).complete("judge", "s", "u", after="a")
    assert out.model == "b" and seen == ["b"]


def test_after_last_model_or_unknown_model_raises():
    s = make_settings(judge=["litellm:a", "litellm:b"])
    llm = LLM(s, http=client(lambda r: chat("ok")))
    with pytest.raises(LLMError, match="no models"):
        llm.complete("judge", "s", "u", after="b")
    with pytest.raises(LLMError, match="not in the chain"):
        llm.complete("judge", "s", "u", after="zzz")


def test_finish_reason_length_with_empty_content_falls_through_as_length(caplog):
    import logging

    def handler(req):
        if json.loads(req.content)["model"] == "deepseek-v4-pro":
            return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": ""}}]})
        return chat("ok")

    with caplog.at_level(logging.WARNING):
        out = LLM(make_settings(), http=client(handler)).complete("feedback_writer", "s", "u")
    assert out.model == "glm-5.3"
    assert "deepseek-v4-pro failed: length" in caplog.text

    # null content (no message text at all) with length is also reported as length
    def handler2(req):
        return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": None}}]})

    with pytest.raises(LLMError, match=r"deepseek-v4-pro \(length\)"):
        LLM(make_settings(), http=client(handler2)).complete("feedback_writer", "s", "u")


# -- I8: an infrastructure failure is told apart from a content failure ----------------------------------------

def test_llmerror_is_infra_when_every_model_failed_on_http_timeout_or_connection():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="busy")
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError) as e:
        LLM(make_settings(), http=client(handler)).complete("feedback_writer", "s", "u")
    assert e.value.infra is True


def test_llmerror_is_not_infra_when_a_model_returned_bad_content():
    def handler(req):
        body = json.loads(req.content)
        if body["model"] == "deepseek-v4-pro":
            return chat("")
        raise httpx.ReadTimeout("slow")

    with pytest.raises(LLMError) as e:
        LLM(make_settings(), http=client(handler)).complete("feedback_writer", "s", "u")
    assert e.value.infra is False


def test_claude_provider_failure_counts_as_infra():
    def handler(req):
        return httpx.Response(500, text="x")

    llm = LLM(make_settings(), http=client(handler), claude=ok_claude(ok=False, error="claude exit 1"))
    with pytest.raises(LLMError) as e:
        llm.complete("mixed", "s", "u")
    assert e.value.infra is True


def test_plain_llmerror_defaults_to_not_infra():
    assert LLMError("no model produced a valid draft").infra is False


# -- N6: only 5xx, 429, timeouts and connection errors are infra ------------------------------------------------

@pytest.mark.parametrize("status", [400, 401, 404, 413, 422])
def test_n6_a_deterministic_4xx_is_not_infra(status):
    def handler(req):
        return httpx.Response(status, text="bad request")

    with pytest.raises(LLMError) as e:
        LLM(make_settings(), http=client(handler)).complete("feedback_writer", "s", "u")
    assert e.value.infra is False


@pytest.mark.parametrize("status", [429, 408, 500, 502, 503, 504])
def test_n6_rate_limit_timeout_and_5xx_are_infra(status):
    def handler(req):
        return httpx.Response(status, text="busy")

    with pytest.raises(LLMError) as e:
        LLM(make_settings(), http=client(handler)).complete("feedback_writer", "s", "u")
    assert e.value.infra is True


def test_n6_one_4xx_in_the_chain_makes_the_failure_not_infra():
    def handler(req):
        body = json.loads(req.content)
        return httpx.Response(422 if body["model"] == "deepseek-v4-pro" else 503, text="x")

    with pytest.raises(LLMError) as e:
        LLM(make_settings(), http=client(handler)).complete("feedback_writer", "s", "u")
    assert e.value.infra is False


# ---- vision calls: the same chat request with image parts, and a bound on how long each model may take

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 20
JPG = b"\xff\xd8\xff" + b"pixels" * 20


def test_images_become_data_uri_parts_after_the_text():
    import base64
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return chat("seen")

    out = LLM(make_settings(), http=client(handler)).complete(
        "feedback_writer", "SYS", "USR", images=[Image("image/jpeg", JPG), Image("image/png", PNG)])
    assert out == Completion(text="seen", model="deepseek-v4-pro")
    msgs = bodies[0]["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}  # the system prompt stays text
    assert msgs[1]["role"] == "user" and msgs[1]["content"] == [
        {"type": "text", "text": "USR"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(JPG).decode()}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(PNG).decode()}},
    ]


def test_without_images_the_user_message_is_still_a_plain_string():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return chat("ok")

    LLM(make_settings(), http=client(handler)).complete("feedback_writer", "SYS", "USR", images=[])
    assert bodies[0]["messages"][1] == {"role": "user", "content": "USR"}


def test_json_mode_with_images_keeps_the_images_when_the_model_rejects_response_format():
    bodies = []

    def handler(req):
        b = json.loads(req.content)
        bodies.append(b)
        return httpx.Response(400, json={"error": "no response_format"}) if "response_format" in b else chat("{}")

    LLM(make_settings(), http=client(handler)).complete("feedback_writer", "S", "U", json_mode=True,
                                                         images=[Image("image/png", PNG)])
    assert len(bodies) == 2 and "response_format" in bodies[0] and "response_format" not in bodies[1]
    for b in bodies:  # the retry carries the pictures too
        assert [p["type"] for p in b["messages"][1]["content"]] == ["text", "image_url"]


def test_a_claude_model_is_skipped_for_images_and_the_next_one_answers():
    claude = ok_claude()
    seen = []

    def handler(req):
        seen.append(json.loads(req.content)["model"])
        return chat("from litellm")

    out = LLM(make_settings(), http=client(handler), claude=claude).complete(
        "mixed", "S", "U", images=[Image("image/png", PNG)])
    assert out == Completion(text="from litellm", model="glm-5.3")
    assert claude.calls == [] and seen == ["glm-5.3"]  # claude never ran; it could not have read the picture


def test_a_chain_of_claude_models_only_fails_without_calling_anything_and_is_not_an_outage():
    claude = ok_claude()
    with pytest.raises(LLMError) as e:
        LLM(make_settings(), claude=claude).complete("research", "S", "U", images=[Image("image/png", PNG)])
    assert claude.calls == [] and "images not supported" in str(e.value) and e.value.infra is False


@pytest.mark.parametrize("images", [
    [Image("text/plain", b"x")],
    [Image("image/png", b"")],
    [Image("image/png", PNG)] * 5,
    [Image("image/png", b"x" * (6 * 1024 * 1024 + 1))],
    ["not an image"],
])
def test_bad_images_are_refused_before_any_call(images):
    def handler(req):
        raise AssertionError("no request may be made")

    with pytest.raises(LLMError, match="bad images"):
        LLM(make_settings(), http=client(handler)).complete("feedback_writer", "S", "U", images=images)


def test_an_image_never_reaches_a_log_a_repr_or_an_error(caplog):
    import logging
    caplog.set_level(logging.DEBUG, logger="haseeb_agent.litellm")

    def handler(req):
        return httpx.Response(500, text="boom " + req.content.decode()[:50])

    img = Image("image/jpeg", JPG)
    with pytest.raises(LLMError) as e:
        LLM(make_settings(feedback_writer=["litellm:m"]), http=client(handler)).complete("feedback_writer", "S", "U",
                                                                                          images=[img])
    import base64
    b64 = base64.b64encode(JPG).decode()
    assert b64 not in str(e.value) and b64 not in caplog.text and repr(JPG) not in repr(img) and "pixels" not in repr(img)


def test_timeout_s_bounds_each_models_request():
    seen = []

    def handler(req):
        seen.append(req.extensions["timeout"])
        return chat("ok")

    llm = LLM(make_settings(), http=client(handler))
    llm.complete("feedback_writer", "S", "U", timeout_s=12.5)
    llm.complete("feedback_writer", "S", "U")
    assert seen[0]["read"] == 12.5 and seen[1]["read"] == 90  # the default stays TIMEOUT_S
