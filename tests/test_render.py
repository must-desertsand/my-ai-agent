import re
from datetime import date, datetime, timezone, timedelta

from haseeb_agent.render import (
    FOOTER,
    COMMANDS_HINT,
    IDEA_COMMANDS_HINT,
    IDEA_EMOJI,
    MARKET_LABELS,
    MARKETS,
    MAX_DM_CHARS,
    Idea,
    Insight,
    Source,
    mass_mentions,
    model_line,
    render_feedback_dm,
    render_idea_dm,
    render_idea_post,
    render_index,
    render_insight_dm,
    render_insight_post,
    render_message_dm,
    render_message_post,
    render_todo_dm,
    split_message,
)

ML = "Wrote: claude-sonnet · Judged: gemini-3.1-pro 92/100 · Deck: —"


def make_insight(**kw):
    base = dict(
        title="Anthropic ships agent SDK",
        category="AI",
        what_happened="Something happened.",
        why_it_matters="It matters because X.",
        silicon_valley="They would ship it in a week.",
        evidence=[Source("Blog post", "https://example.com/a"), Source("Docs", "https://example.com/b")],
        not_verified=["Pricing unclear", "Launch date rumored"],
        read_min=3,
    )
    base.update(kw)
    return Insight(**base)


def strip_suffix(part):
    return re.sub(r" \(\d+/\d+\)$", "", part)


def test_post_has_footer_and_no_model_line():
    parts = render_insight_post(make_insight())
    assert len(parts) == 1
    text = parts[0]
    assert text.endswith("_— drafted by Haseeb's Agent_")
    assert FOOTER == "_— drafted by Haseeb's Agent_"
    assert "Wrote:" not in text
    for h in ("What happened", "Why it matters to us", "If we were in Silicon Valley", "Evidence", "Not verified"):
        assert f"**[{h}]**" in text
    assert "1. [Blog post (example.com)](https://example.com/a)" in text
    assert "2. [Docs (example.com)](https://example.com/b)" in text
    assert "- Pricing unclear" in text


def test_section_order():
    text = render_insight_post(make_insight())[0]
    idx = [text.index(f"**[{h}]**") for h in
           ("What happened", "Why it matters to us", "If we were in Silicon Valley", "Evidence", "Not verified")]
    assert idx == sorted(idx)


def test_dm_has_model_line_format():
    ml = model_line("claude-sonnet", "gemini-3.1-pro", 92.0, None)
    assert ml == ML
    parts = render_insight_dm(1, make_insight(), ml)
    assert parts[-1].endswith(f"_{ML}_")
    assert "drafted by Haseeb's Agent" not in parts[-1]


def test_model_line_variants():
    assert model_line("a", "b", 91.6, "c") == "Wrote: a · Judged: b 91/100 · Deck: c"
    assert model_line("a", "b", 84.9, None) == "Wrote: a · Judged: b 84/100 · Deck: —"
    assert model_line("a", "b", float("nan"), None) == "Wrote: a · Judged: b — · Deck: —"
    assert model_line("a", "b", float("inf"), None) == "Wrote: a · Judged: b — · Deck: —"
    assert model_line("a", None, None, None) == "Wrote: a · Judged: — · Deck: —"


def test_split_long_text_no_loss():
    paras = [f"P{i:02d} " + "word " * 58 for i in range(30)]
    paras = [p.strip() for p in paras]
    text = "\n\n".join(paras)
    assert 8500 < len(text) < 9500
    parts = split_message(text)
    assert len(parts) == 3
    for i, p in enumerate(parts, 1):
        assert p.endswith(f" ({i}/3)")
        assert len(strip_suffix(p)) <= 3800
        assert len(p) <= 5000
    assert "\n\n".join(strip_suffix(p) for p in parts) == text


def test_split_short_text_untouched():
    assert split_message("hello") == ["hello"]


def test_split_long_paragraph_on_lines_then_words():
    lines = "\n".join("line%03d " % i + "x" * 80 for i in range(100))
    parts = split_message(lines, limit=500)
    assert len(parts) > 1
    assert all(len(strip_suffix(p)) <= 500 for p in parts)
    assert "\n".join(strip_suffix(p) for p in parts) == lines
    one_line = " ".join(f"word{i}" for i in range(2000))
    parts = split_message(one_line, limit=500)
    assert all(len(strip_suffix(p)) <= 500 for p in parts)
    words = set(one_line.split())
    got = [w for p in parts for w in strip_suffix(p).split(" ")]
    assert set(got) == words and len(got) == 2000  # no word was cut


def test_split_unbreakable_word_respects_hard_limit():
    parts = split_message("x" * 12000, limit=9000)
    assert all(len(p) <= 5000 for p in parts)


def test_long_insight_dm_splits_with_model_line_last():
    ins = make_insight(what_happened="\n\n".join("para " * 100 for _ in range(20)))
    parts = render_insight_dm(2, ins, ML)
    assert len(parts) > 1
    assert all(len(p) <= 5000 for p in parts)
    assert ML in parts[-1]


def test_index_lists_titles_with_emoji():
    ins = [
        make_insight(title="One", category="AI"),
        make_insight(title="Two", category="MONEY"),
        make_insight(title="Three", category="PRODUCT"),
    ]
    text = render_index(date(2026, 10, 19), ins, flagged=False)
    assert text.splitlines()[0] == "☀️ Mon 19 Oct · Today's 3 insights"
    assert "1. 🤖 One" in text
    assert "2. 💰 Two" in text
    assert "3. 🧩 Three" in text
    assert COMMANDS_HINT in text
    assert "Reply in an insight's thread: post · edit: … · shorter · skip" in text
    assert "quality bar" not in text


def test_index_singular_gap_and_flagged():
    text = render_index(date(2026, 10, 20), [make_insight(title="Solo", category="GAP")], True, score=71.4)
    assert text.splitlines()[0] == "☀️ Tue 20 Oct · Today's 1 insight"
    assert "1. 🕳️ Solo" in text
    assert "quality bar" in text and "best draft" in text and "71/100" in text


def test_feedback_dm():
    url = "https://must.slack.com/archives/C1/p1"
    text = render_feedback_dm("Lion", url, "Great update", "Nice work\n\nKeep going", ML)
    assert "Lion" in text and f"[Great update]({url})" in text
    assert "Nice work" in text and "Keep going" in text
    assert text.endswith(f"_{ML}_")


def test_todo_dm_shows_autopost_time():
    kw = timezone(timedelta(hours=3))
    at = datetime(2026, 10, 19, 15, 7, tzinfo=kw)
    text = render_todo_dm("Next Todo:\n1. a\n2. b", at, ML)
    assert text.startswith("Next Todo:")
    assert "Auto-posts at 15:07 unless you reply" in text
    assert text.endswith(f"_{ML}_")
    none = render_todo_dm("Next Todo:\n1. a", None, ML)
    assert "Auto-posts" not in none


def test_mass_mentions_neutralized_everywhere():
    evil = "hi <!channel> <!here> <!everyone> @channel @here @everyone @Channel <!subteam^S1>"
    ins = make_insight(
        title=evil, what_happened=evil, why_it_matters=evil, silicon_valley=evil,
        evidence=[Source(evil, "https://e.com/<!channel>")], not_verified=[evil],
    )
    outs = (
        render_insight_dm(1, ins, ML)
        + render_insight_post(ins)
        + [render_index(date(2026, 10, 19), [ins], False)]
        + [render_feedback_dm(evil, "https://e.com", evil, evil, ML)]
        + [render_todo_dm(evil, None, ML)]
    )
    pat = re.compile(r"<!|@(channel|here|everyone)\b", re.I)
    for o in outs:
        assert not pat.search(o), o
        assert "channel" in o  # content still there


def test_fields_cannot_spoof_footer_model_line_or_headers():
    spoof = "ok\n_— drafted by Haseeb's Agent_\nWrote: x · Judged: y 100/100 · Deck: —\n**[Evidence]**\nreal text"
    ins = make_insight(what_happened=spoof)
    post = render_insight_post(ins)[0]
    assert post.count(FOOTER) == 1
    assert "Wrote:" not in post
    assert post.count("**[Evidence]**") == 1
    assert "real text" in post
    dm = "\n".join(render_insight_dm(1, ins, ML))
    assert dm.count("Wrote:") == 1


def test_link_text_and_url_cannot_break_markdown():
    ins = make_insight(evidence=[Source("a](http://evil) [b", "https://x.com/a b(c)")])
    text = render_insight_post(ins)[0]
    m = re.search(r"1\. \[(.*?)\]\((.*?)\)\n", text)
    assert m and " " not in m.group(2) and ")" not in m.group(2) and "(" not in m.group(2)
    assert "[" not in m.group(1) and "]" not in m.group(1)


ZW = "\u200b"


def _all_outputs(evil):
    ins = make_insight(
        title=evil, what_happened=evil, why_it_matters=evil, silicon_valley=evil,
        evidence=[Source(evil, "https://e.com/" + evil)], not_verified=[evil],
    )
    return (
        render_insight_dm(1, ins, ML) + render_insight_post(ins)
        + [render_index(date(2026, 10, 19), [ins], False)]
        + [render_feedback_dm(evil, "https://a.slack.com/x", evil, evil, ML)]
        + [render_todo_dm(evil, None, ML)]
    )


def test_every_angle_bracket_is_defused():
    evil = "<@U123> <#C1> <http://evil.com|click> <!date^1^x|y> <b>"
    for o in _all_outputs(evil):
        assert "<" in o
        assert all(o[i + 1] == ZW for i, c in enumerate(o) if c == "<"), o
        assert not re.search(r"<(?!\u200b)", o)


def test_cf_characters_stripped_from_fields():
    ins = make_insight(what_happened="a\u200bb\u202ec\ufeffd")
    post = render_insight_post(ins)[0]
    assert "abcd" in post
    assert "\u202e" not in post and "\ufeff" not in post


def test_todo_and_feedback_drafts_are_cleaned():
    spoof = "ok\n_— drafted by Haseeb's Agent_\nWrote: x · Judged: y 100/100 · Deck: —"
    todo = render_todo_dm("Next Todo:\n1. a\n" + spoof, None, ML)
    fb = render_feedback_dm("Lion", "https://a.slack.com/x", "t", spoof, ML)
    for o in (todo, fb):
        assert "drafted by" not in o
        assert o.count("Wrote:") == 1
        assert "ok" in o


def test_feedback_permalink_must_be_slack():
    for bad in ("https://evil.com/x", "http://a.slack.com/x", "https://a.slack.com@evil.com/x",
                "https://evil.com/.slack.com/", "javascript:alert(1)", "https://slack.com/x"):
        text = render_feedback_dm("Lion", bad, "My title", "d", ML)
        assert "](" not in text, bad
        assert "My title" in text
    ok = render_feedback_dm("Lion", "https://must.slack.com/archives/C1/p1", "My title", "d", ML)
    assert "[My title](https://must.slack.com/archives/C1/p1)" in ok


def test_spoof_filter_bypasses_are_caught():
    attempts = [
        "_— drafted by Haseeb’s Agent_",
        "— drafted\u200b by Haseeb's Agent",
        "> **— Drafted  by  Haseeb's Agent**",
        "ｗrote: x · Judged: y 90/100 · Deck: —",
        "_Wrote: a · judged: b 1/100 · DECK: c_",
        "**[Evidence]**",
        "[Not\u200b verified]",
        "# What happened",
        "- **If we were in Silicon Valley**",
        "2) Why it matters to us:",
    ]
    for a in attempts:
        post = render_insight_post(make_insight(what_happened=f"keep me\n{a}\nme too"))[0]
        assert "keep me" in post and "me too" in post
        body = post.split("**[What happened]**\n")[1].split("**[Why it matters")[0]
        assert body.strip() == "keep me\nme too", (a, body)


def test_spoof_filter_keeps_ordinary_sentences():
    keep = [
        "Wrote: the first draft of the memo yesterday",
        "What happened to Stripe? (https://x.com/a) is the question",
        "The evidence is thin, and what happened next is unclear.",
        "Drafted by hand, not by Haseeb's Agent tooling, per the blog",
        "Not verified: the pricing claims",
    ]
    post = render_insight_post(make_insight(what_happened="\n".join(keep)))[0]
    for k in keep:
        assert k in post


def test_index_flagged_requires_score():
    import pytest
    with pytest.raises(ValueError):
        render_index(date(2026, 10, 19), [make_insight()], True)
    out = render_index(date(2026, 10, 19), [make_insight()], True, score=float("nan"))
    assert "score —" in out
    assert "84/100" in render_index(date(2026, 10, 19), [make_insight()], True, score=84.9)


def test_feedback_and_todo_truncate_under_limit():
    big = " ".join(f"word{i}" for i in range(5000))
    kw = timezone(timedelta(hours=3))
    for out in (
        render_feedback_dm("Lion", "https://a.slack.com/x", "t", big, ML),
        render_todo_dm("Next Todo:\n" + big, datetime(2026, 10, 19, 15, 7, tzinfo=kw), ML),
        render_feedback_dm("Lion", "https://a.slack.com/x", "t", "<" * 4000, ML),
    ):
        assert len(out) <= 3800
        assert out.endswith(f"_{ML}_")
    out = render_todo_dm("Next Todo:\n" + big, None, ML)
    assert out.startswith("Next Todo:")
    assert "…\n\n_Wrote:" in out
    body = out.split("…")[0].split()
    assert body[-1] == f"word{len(body) - 3}"  # cut at a word boundary, no partial word


def test_permalink_bypasses_rejected():
    good = "https://must.slack.com/archives/C0C62C128D8/p1760000000123456"
    assert "](" in render_feedback_dm("L", good, "t", "d", ML)
    assert "](" in render_feedback_dm("L", good + "?thread_ts=1.2&cid=C1", "t", "d", ML)
    assert "](" in render_feedback_dm("L", "https://MUST.Slack.com/archives/C1/p1", "t", "d", ML)
    assert "](" in render_feedback_dm("L", "https://must.enterprise.slack.com/archives/C1/p1", "t", "d", ML)
    bad = [
        "https://must.slack.com\\@evil.com/archives/C1/p1",
        "https://must.slack.com\\.evil.com/archives/C1/p1",
        "https://user:pw@must.slack.com/archives/C1/p1",
        "https://must.slack.com@evil.com/archives/C1/p1",
        "https://must.slack.com./archives/C1/p1",
        "https://must.slack.com:8443/archives/C1/p1",
        "https://must.slack.com/team/U1",
        "https://must.slack.com/archives/c1/p1",          # path is case-sensitive
        "https://must.slack.com/archives/C1/p1\n",
        "https://must.slack.com/archives/C1/p1 ",
        "https://must.slack.com/archives/C1/p1\x07",
        "https://must.slack.com/archives/C1/p1#frag",
        "https://must.slack.com/archives/C1/p1?x=(y)",
        "https://evil.com/?https://must.slack.com/archives/C1/p1",
        "http://must.slack.com/archives/C1/p1",
        "https://-.evil.com/archives/C1/p1",
        good + "?" + "a" * 600,
    ]
    for b in bad:
        text = render_feedback_dm("L", b, "T", "d", ML)
        assert "](" not in text, b


def test_free_text_links_show_destination():
    fields = {
        "what_happened": "see [the post](https://evil.com/x) now",
        "why_it_matters": "[https://good.com/safe](https://evil.com/phish) is fine",
        "silicon_valley": "![img](https://evil.com/i.png) and [a](b)(c)",
        "not_verified": ["[claim](https://evil.com/c)"],
    }
    ins = make_insight(**fields)
    post = render_insight_post(ins)[0]
    assert "see the post (https://evil.com/x) now" in post
    assert "https://good.com/safe (https://evil.com/phish) is fine" in post
    assert "- claim (https://evil.com/c)" in post
    assert "](https://evil.com" not in post
    # only our own evidence entries stay real links
    assert "1. [Blog post (example.com)](https://example.com/a)" in post
    ins2 = make_insight(title="[T](https://evil.com/t)")
    assert "](https://evil.com" not in render_insight_post(ins2)[0]
    assert "](https://evil.com" not in render_index(date(2026, 10, 19), [ins2], False)
    todo = render_todo_dm("Next Todo:\n1. [x](https://evil.com/y)", None, ML)
    assert "1. x (https://evil.com/y)" in todo
    fb = render_feedback_dm("Lion", "https://a.slack.com/archives/C1/p1", "t", "[nice](https://evil.com/z)", ML)
    assert "> nice (https://evil.com/z)" in fb
    # urls with parentheses and reference-style definitions cannot revive a link
    tricky = "[t](https://evil.com/a_(b)) and [r]: https://evil.com\n[r2]: https://evil.com"
    out = render_insight_post(make_insight(what_happened=tricky))[0]
    assert "](" not in out.split("**[Evidence]**")[0]
    assert not re.search(r"^\[[^\]]*\]:", out, re.M)


def test_model_line_filter_any_separator():
    for a in ("wrote: x - judged: y - deck: z", "Wrote: x | Judged: y 90/100", "_wrote: a, judged: b_"):
        post = render_insight_post(make_insight(what_happened=f"keep\n{a}"))[0]
        assert "wrote:" not in post.lower().replace("wrote: claude", "")


def test_length_caps_keep_dms_under_limit():
    big = "a" * 5000
    huge_model = "m" * 5000
    kw = timezone(timedelta(hours=3))
    fb = render_feedback_dm("A" * 5000, "https://a.slack.com/archives/C1/p1", "T" * 5000, big, huge_model)
    assert len(fb) <= 3800
    assert "A" * 201 not in fb and "T" * 201 not in fb and "…" in fb
    assert "m" * 301 not in fb
    td = render_todo_dm(big, datetime(2026, 10, 19, 15, 7, tzinfo=kw), huge_model)
    assert len(td) <= 3800 and "m" * 301 not in td
    worst = render_feedback_dm("<" * 5000, "https://a.slack.com/archives/C1/p1", "<" * 5000, "<" * 5000, "<" * 5000)
    assert len(worst) <= 3800


def test_html_entities_defused():
    evil = "&lt;!channel&gt; &#60;!here&#62; &#x3c;!everyone&#x3E; &LT;!channel &amp;lt;!channel&amp;gt; &#0060;!x"
    entity = re.compile(r"&(?=[#A-Za-z])")
    for o in _all_outputs(evil):
        visible = re.sub(r"\]\([^()\s]*\)", "](URL)", o)  # link destinations are percent-encoded instead
        assert not entity.search(visible), o
        for dest in re.findall(r"\]\(([^()\s]*)\)", o):
            assert not re.search(r"&(?:#|lt|gt|amp)", dest, re.I), dest
            assert ";" not in dest and "<" not in dest
    assert "AT&​T" in render_insight_post(make_insight(what_happened="R&D and AT&T"))[0]


def test_layered_entities_defused():
    out = render_insight_post(make_insight(what_happened="&amp;amp;lt;!channel&amp;amp;gt; &AMP;LT;"))[0]
    assert not re.search(r"&(?=[#A-Za-z])", out)
    assert "&​amp;amp;lt;!channel" in out
    ok = render_feedback_dm("L", "https://a.slack.com/archives/C1/p1?x=1&y=2", "t", "d", ML)
    assert "?x=1&y=2)" in ok  # the checked permalink keeps its query separators


REF_PROBES = [
    "[x][1]\n[1]: http://evil.com",
    "[x][1]\n> [1]: http://evil.com",
    "[x][1]\n- [1]: http://evil.com",
    "[x][1]\n1. [1]: http://evil.com",
    "[x][1]\n   [1]: http://evil.com",
    "[x][1]\r[1]: http://evil.com",
    "[x][1]\r\n[1]: http://evil.com",
    "[x][1] [1]: http://evil.com",
    "[x][1] [1]: http://evil.com",
    "[x][1]\x85[1]: http://evil.com",
    "[x][1]\x0c[1]: http://evil.com",
    "[x][1]\x0b[1]: http://evil.com",
    "[x][1]\x1c[1]: http://evil.com",
    "[x]\n\n[x]: http://evil.com",
    "![alt][r]\n[r]: http://evil.com/i.png",
    "[a]​(http://evil.com)",
    "[[nested]](http://evil.com)",
]


def test_no_square_brackets_survive_in_free_text():
    kw = timezone(timedelta(hours=3))
    for probe in REF_PROBES:
        ins = make_insight(title=probe, what_happened=probe, why_it_matters=probe, silicon_valley=probe,
                           not_verified=[probe])
        for o in render_insight_dm(1, ins, ML) + render_insight_post(ins):
            # the only brackets are our section headers and the evidence links
            stripped = re.sub(r"\*\*\[[^\]\n]*\]\*\*", "", o)
            stripped = re.sub(r"^\d+\. \[.*\]\(https://example\.com/\w\)$", "", stripped, flags=re.M)
            assert "[" not in stripped and "]" not in stripped, (probe, stripped)
        idx = render_index(date(2026, 10, 19), [ins], False)
        todo = render_todo_dm("Next Todo:\n" + probe, datetime(2026, 10, 19, 15, 7, tzinfo=kw), ML)
        for o in (idx, todo):
            assert "[" not in o and "]" not in o, (probe, o)
        fb = render_feedback_dm(probe, "https://a.slack.com/archives/C1/p1", probe, probe, ML)
        # feedback: exactly one link, the checked permalink
        assert fb.count("[") == 1 and fb.count("](") == 1, (probe, fb)


def test_evidence_link_text_shows_true_host():
    ins = make_insight(evidence=[
        Source("https://good.com", "https://evil.com/phish"),
        Source("bank", "https://good.com@evil.com/x"),
        Source("bank2", "https://good.com\\@evil.com/x"),
        Source("ftp", "ftp://evil.com/x"),
        Source("port", "HTTPS://Evil.COM:8443/x?a=1&b=2"),
    ])
    text = render_insight_post(ins)[0]
    assert "1. [https://good.com (evil.com)](https://evil.com/phish)" in text
    assert "2. bank\n" in text and "3. bank2\n" in text and "4. ftp\n" in text
    assert "5. [port (evil.com)](HTTPS://Evil.COM:8443/x?a=1&b=2)" in text


def test_link_shaped_free_text_is_defused():
    # R21: nothing is exempt from _safe just because it looks like "](...)"
    evil = "x](https://x/@channel) and ](<!here>) and ](&lt;!everyone&gt;)"
    for o in _all_outputs(evil):
        assert "@channel" not in o and "](<!here>)" not in o and "<!here" not in o, o
        assert "@​channel" in o or "%40channel" in o, o
        assert not re.search(r"<(?!​)", o), o
    ins = make_insight(what_happened="see ](https://x/@channel) now")
    post = render_insight_post(ins)[0]
    assert "https://x/@​channel" in post


def test_evidence_url_at_sign_is_encoded():
    ins = make_insight(evidence=[Source("t", "https://evil.com/@channel?q=@here")])
    text = render_insight_post(ins)[0]
    assert "1. [t (evil.com)](https://evil.com/%40channel?q=%40here)" in text
    assert "@channel" not in text and "@here" not in text


def test_forged_placeholder_lets_nothing_through(monkeypatch):
    import haseeb_agent.render as render
    tok = "f" * 32
    monkeypatch.setattr(render, "_new_token", lambda n: tok)
    forged = [f"⁠{tok}-0⁠", f"{tok}-0", f"⁠{tok}-7⁠", tok[:5] + tok + tok[5:] + "-0"]
    for f in forged:
        evil = f"<!channel> {f} @here {f}](<!here>)"
        ins = make_insight(title=evil, what_happened=evil, not_verified=[evil],
                           evidence=[Source(evil, "https://example.com/a")])
        outs = (render_insight_dm(1, ins, evil) + render_insight_post(ins)
                + [render_feedback_dm(evil, "https://a.slack.com/archives/C1/p1", evil, evil, evil)]
                + [render_feedback_dm(evil, "https://evil.com/x", evil, evil, evil)])
        for o in outs:
            assert tok not in o and "⁠" not in o, (f, o)
            assert not re.search(r"<(?!​)", o) and "@here" not in o, (f, o)
            # the only real destinations are the ones we built
            for dest in re.findall(r"\]\(([^()\s]*)\)", o):
                assert dest in ("https://example.com/a", "https://a.slack.com/archives/C1/p1"), (f, dest)


def test_built_links_still_work_with_query_separators():
    ins = make_insight(evidence=[Source("q", "https://example.com/s?a=1&b=2&c=3"),
                                 Source("r", "https://example.com/r")])
    for o in render_insight_post(ins) + render_insight_dm(1, ins, ML):
        assert "1. [q (example.com)](https://example.com/s?a=1&b=2&c=3)" in o
        assert "2. [r (example.com)](https://example.com/r)" in o
    fb = render_feedback_dm("L", "https://a.slack.com/archives/C1/p1?thread_ts=1.2&cid=C1", "Hi", "d", ML)
    assert "[Hi](https://a.slack.com/archives/C1/p1?thread_ts=1.2&cid=C1)" in fb


# ---------------------------------------------------------------- the message renderers (the composer)

def test_mass_mentions_counts_what_he_typed():
    # Changed in the composer fix round (review M1, I3): a mention counts at the start of the text or after a space.
    # Slack's own `<!channel>` is turned into `@channel` by message_prompt.normalize_raw before anything is counted, so
    # a `<!channel>` that is still in the text (an entity he typed, decoded after that step) stays text.
    assert mass_mentions("@channel hi @here and @Channel too, @HERE") == {"channel": 2, "here": 2}
    assert mass_mentions("hi\n@here after a line break, and\t@channel after a tab") == {"channel": 1, "here": 1}
    assert mass_mentions("<!channel> and <!here|here> are text here") == {"channel": 0, "here": 0}
    # not mentions: an email address, a longer word, @everyone (never allowed), a doubled "@"
    assert mass_mentions("mail a@channel.com or @channels or @everyone or @@here") == {"channel": 0, "here": 0}
    assert mass_mentions("") == {"channel": 0, "here": 0}


def test_a_mass_mention_inside_one_of_his_links_is_not_typed():
    """Review M1: `https://x.com/@channel` is a link, and used to be posted as `https://x.com/<!channel>`, a ping."""
    assert mass_mentions("see https://x.com/@channel and @here") == {"channel": 0, "here": 1}
    assert mass_mentions("see x.com/@here/a?b=@channel") == {"channel": 0, "here": 0}
    assert mass_mentions("(@channel) and *@here* and a@here") == {"channel": 0, "here": 0}  # after a space only
    out = render_message_post("See https://x.com/@channel and @channel", mass_mentions("see https://x.com/@channel"))
    assert out == "See https://x.com/@channel and @\u200bchannel"  # the link is whole, and nothing is a ping


def test_a_typed_channel_survives_and_goes_out_as_a_real_mention():
    out = render_message_post("@channel Standup moved to 3 pm.", mass_mentions("@channel standup 3pm"))
    assert out == "<!channel> Standup moved to 3 pm."
    out = render_message_post("@here quick one", mass_mentions("@here quick one"))
    assert out == "<!here> quick one"
    assert render_message_post("Update", mass_mentions("<!channel> update")) == "Update"  # none in the draft


def test_a_mention_the_model_added_stays_defused():
    zw = "@\u200b"
    for typed in (None, {}, {"channel": 0, "here": 0}, mass_mentions("no mention here at all")):
        out = render_message_post("@channel @here @everyone hello", typed)
        assert out == f"{zw}channel {zw}here {zw}everyone hello"
        assert "<!channel>" not in out and "<!here>" not in out


def test_only_as_many_mentions_as_he_typed_become_real():
    out = render_message_post("@channel one @channel two @here", {"channel": 1, "here": 0})
    assert out == "<!channel> one @\u200bchannel two @\u200bhere"
    assert render_message_post("@here a @here b", {"here": 2}) == "<!here> a <!here> b"


def test_everyone_and_other_slack_markup_are_always_defused():
    out = render_message_post("@everyone <!everyone> <@U123> <#C1|x> <!subteam^S1>", {"channel": 5, "here": 5})
    assert "<!everyone>" not in out and "<@U123>" not in out and "<#C1|x>" not in out and "<!subteam" not in out
    assert out.count("<\u200b") == 4  # every "<" got its zero-width space


def test_the_post_has_no_footer_note_or_model_line():
    out = render_message_post("Plain message.")
    assert out == "Plain message."
    assert FOOTER not in out and "drafted by" not in out.lower()


def test_the_outgoing_cleaner_still_runs_on_a_message():
    draft = "Read [the doc](https://example.com/doc) now.\n_— drafted by Haseeb's Agent_\nLink https://example.org/own"
    out = render_message_post(draft)
    assert "the doc (https://example.com/doc)" in out  # a link is shown with its destination, never hidden
    assert "[" not in out and "]" not in out
    assert "drafted by" not in out  # a fake footer line is dropped
    assert "https://example.org/own" in out  # his own URL stays a plain URL
    assert render_message_post("a\u202eb\u200bc") == "abc"  # hidden characters go
    assert render_message_post("AT&T <b>x</b>") == "AT&\u200bT <\u200bb>x<\u200b/b>"  # entities and tags defused


def test_the_dm_card_has_the_header_the_quoted_draft_the_note_and_the_model():
    card = render_message_dm("Line one.\nLine two.", "Fixed grammar.", "deepseek-v4-pro")
    assert card == ("**Message draft** · for the channel\n\n> Line one.\n> Line two.\n\n_Changed: Fixed grammar._\n\n"
                    "_Wrote: deepseek-v4-pro_")
    assert render_message_dm("x", None, None) == "**Message draft** · for the channel\n\n> x"
    assert render_message_dm("x", "n", "m", revised=True).startswith("**Message draft (revised)** · for the channel")


def test_the_dm_card_cleans_a_hostile_note_and_draft():
    card = render_message_dm("@channel <!here> hi [x](https://e.com)", "[a](b)\nline2 _— drafted by Haseeb's Agent_", "m")
    assert "@\u200bchannel" in card and "<!here>" not in card and "[" not in card
    assert card.count("_Changed:") == 1 and "\n_Changed: " in card
    assert "\n\n_Changed: a (b) line2" in card  # one line, the link shown with its destination


def test_the_dm_card_stays_within_the_dm_limit():
    card = render_message_dm("word " * 2000, "n", "m")
    assert len(card) <= MAX_DM_CHARS


def test_the_review_line_on_the_feedback_card_is_one_short_line_between_the_title_and_the_draft():
    perma = "https://a.slack.com/archives/C1/p1"
    draft = "\u2705 a\n\u26a0\ufe0f b\n\U0001f4a1 c\n\u2753 d"
    text = render_feedback_dm("Iqbal", perma, "Feedback: Iqbal", draft, "Model", review_line="Reviewed: a\nb  c " + "x" * 600)
    lines = text.splitlines()
    (i,) = [n for n, ln in enumerate(lines) if ln.startswith("Reviewed:")]
    assert lines[i].startswith("Reviewed: a b c xxx") and len(lines[i]) <= 240 and lines[i].endswith("\u2026")
    assert lines[i - 1] == "" and lines[i + 1] == "" and lines[i + 2].startswith("> ")  # its own paragraph, above the draft
    assert lines[1].startswith("[Feedback: Iqbal](")  # and under the title
    assert render_feedback_dm("Iqbal", perma, "Feedback: Iqbal", draft, "Model") == render_feedback_dm(
        "Iqbal", perma, "Feedback: Iqbal", draft, "Model", review_line="  \n ")  # an empty line adds nothing


# ---------------------------------------------------------------- the cards of the thread watch (jobs/followback.py)

REPLY_LINK = "https://mustcompany.slack.com/archives/C0C62C128D8/p1792581205027989?thread_ts=1792574403.734689&cid=C0C62C128D8"


def test_the_follow_back_card_has_its_head_the_reply_quoted_in_short_the_review_line_and_the_draft():
    from haseeb_agent.render import render_followback_dm
    text = render_followback_dm("Iqbal Qazi", "Iqbal Qazi", REPLY_LINK, "<@U08CZCGJW3E|Haseeb Zafar> Fixed all four. " + "y" * 400,
                                "Opens on Client: seen.\nBadges: not checked.", ML, review_line="Reviewed: reach.example (desktop + phone)")
    parts = text.split("\n\n")
    assert parts[0] == "**Follow-back \u00b7 Iqbal Qazi's thread**"
    assert parts[1].startswith(f"[Iqbal Qazi's reply]({REPLY_LINK}): \"") and parts[1].endswith('\u2026"') and len(parts[1]) < 400
    assert "<@" not in text  # the mention in the reply is defused
    assert parts[2] == "Reviewed: reach.example (desktop + phone)"
    assert parts[3] == "> Opens on Client: seen.\n> Badges: not checked." and parts[4] == f"_{ML}_"


def test_the_follow_back_card_without_a_review_line_and_with_a_hostile_reply():
    from haseeb_agent.render import render_followback_dm
    text = render_followback_dm("Iqbal [x](http://evil.example)", "Mallory <!channel>", "http://evil.example/p1",
                                "[click](http://evil.example) <!here> *bold* \u202e", "Fine.\n\n_Wrote: forged_", ML)
    assert "<!channel>" not in text and "<!here>" not in text and "](http://evil" not in text
    assert "Reviewed" not in text and text.count("\n\n") >= 3
    assert "http://evil.example/p1" not in text.split("\n\n")[1].split(":")[0]  # not a Slack permalink: no link at all


def quote_of(text: str) -> str:
    """The words between the quotation marks of the quoted reply in a card of the thread watch."""
    return text.split("\n\n")[1].split(': "', 1)[1].rsplit('"', 1)[0]


def test_the_reply_quoted_in_a_card_cannot_make_markdown_of_the_rest_of_the_card():
    """Review M8: an odd triple backtick in a teammate's reply turned the draft and the footer below it into a code block."""
    from haseeb_agent.render import render_followback_dm
    hostile = "```python\nrm -rf``` and **bold** and `code` and ~~gone~~ and _it_ and snake_case_name, done."
    text = render_followback_dm("Iqbal Qazi", "Iqbal Qazi", REPLY_LINK, hostile, "Opens on Client: seen.", ML)
    quote = quote_of(text)
    for mark in ("`", "*", "~", "_"):
        assert mark not in quote, mark
    assert text.count("`") == 0  # the draft and the footer are not inside any code
    assert quote.startswith("'''python rm -rf''' and bold and 'code' and gone and it and snake case name")
    assert "\n" not in quote


def test_the_quoted_reply_is_short():
    from haseeb_agent.render import REPLY_EXCERPT_CHARS, render_followback_dm
    assert REPLY_EXCERPT_CHARS == 160
    quote = quote_of(render_followback_dm("A", "B", REPLY_LINK, "word " * 200, "Fine.", ML))
    assert len(quote) <= REPLY_EXCERPT_CHARS and quote.endswith("\u2026")
    assert quote_of(render_followback_dm("A", "B", REPLY_LINK, "short", "Fine.", ML)) == "short"


def test_a_feedback_card_can_carry_a_note_under_its_title_and_review_line():
    perma = "https://mustcompany.slack.com/archives/C0C62C128D8/p1792581205027989"
    plain = render_feedback_dm("Zakir", perma, "Feedback: Zakir", "Fine.", "Model")
    noted = render_feedback_dm("Zakir", perma, "Feedback: Zakir", "Fine.", "Model", review_line="Reviewed: x.example",
                               note="Posts in the thread of your insight, mentioning Zakir.")
    parts = noted.split("\n\n")
    assert parts[1] == "Reviewed: x.example" and parts[2] == "Posts in the thread of your insight, mentioning Zakir."
    assert "Posts in" not in plain and render_feedback_dm("Zakir", perma, "Feedback: Zakir", "Fine.", "Model", note="  ") == plain


def test_the_mention_note_is_cleaned_like_every_name():
    from haseeb_agent.render import thread_mention_note
    assert thread_mention_note("Zakir Ullah") == "Posts in the thread of your insight, mentioning Zakir Ullah."
    perma = "https://mustcompany.slack.com/archives/C0C62C128D8/p1792581205027989"
    evil = "Z [x](http://evil.example) <!channel> " + "n" * 200
    card = render_feedback_dm("Z", perma, "Feedback: Z", "Fine.", "Model", note=thread_mention_note(evil))
    note = card.split("\n\n")[1]
    assert "<!channel>" not in card and "](" not in note and note.startswith("Posts in the thread of your insight, mentioning Z")
    assert len(note) < 260 and "\n" not in note  # one capped line (the defusing adds a character or two after the cap)


def test_the_reply_on_your_insight_card_says_what_post_will_do():
    from haseeb_agent.render import render_insreply_dm
    text = render_insreply_dm("Zakir Ullah", REPLY_LINK, "Is the fee only for big merchants?", "Not verified: the article is silent.", ML)
    parts = text.split("\n\n")
    assert parts[0] == "**Reply on your insight \u00b7 Zakir Ullah**" and parts[1].startswith("[Zakir Ullah's reply](")
    assert parts[2] == "Posts in the thread of your insight, mentioning Zakir Ullah."
    assert parts[3] == "> Not verified: the article is silent."


def test_a_long_draft_still_fits_a_card_of_the_thread_watch():
    from haseeb_agent.render import render_followback_dm
    text = render_followback_dm("A", "B", REPLY_LINK, "c", ("line of the draft that is long. " * 20 + "\n") * 80, ML)
    assert len(text) <= MAX_DM_CHARS


def test_a_reply_in_the_insight_thread_mentions_the_teammate_in_front_after_the_cleaning():
    from haseeb_agent.render import render_feedback_post
    out = render_feedback_post("Not verified: the fee. <!channel> <@U0EVIL123>", mention="U0ZAKIR0001")
    assert out.startswith("<@U0ZAKIR0001> Not verified: the fee.") and out.count("<@") == 1  # the draft's own are defused
    assert out.rstrip().endswith(FOOTER.strip())


def test_only_a_user_id_is_ever_a_mention():
    from haseeb_agent.render import render_feedback_post
    for bad in ("", None, "<!channel>", "U0ZAKIR0001> <!channel", "u0zakir", "everyone", "U", "UABC DEF"):
        assert render_feedback_post("Fine.", mention=bad) == render_feedback_post("Fine.")


# ---------------------------------------------------------------- the idea renderers (daily AI business ideas)

IDEA_SECTIONS = ("The idea", "Why now", "Who pays, and how much", "How we build it with AI", "First step this week",
                 "Evidence", "Risks · Not verified")
# (Idea field, its heading, the next heading): the prose fields, cleaned like v1's what_happened
IDEA_PROSE = (
    ("idea", "The idea", "Why now"),
    ("why_now", "Why now", "Who pays, and how much"),
    ("who_pays", "Who pays, and how much", "How we build it with AI"),
    ("build_with_ai", "How we build it with AI", "First step this week"),
    ("first_step", "First step this week", "Evidence"),
)


def make_idea(**kw):
    base = dict(
        title="AI receptionist for clinics",
        market="PK",
        price="Rs 15k/month",
        idea="Clinics lose patients to missed calls. We answer on WhatsApp, book and remind.",
        why_now="A new voice API cut the price of a call this month.",
        who_pays="Clinic owners pay. Our estimate is Rs 15k/month. 50 clinics × Rs 15k = Rs 750k/month.",
        build_with_ai="Two people ship a WhatsApp bot on a hosted model in two weeks. Staff keep the hard calls.",
        first_step="Call 10 clinics by Friday. Get 3 to pre-commit at Rs 15k.",
        risks=["Clinics may not pay", "A local rival exists"],
        evidence=[Source("Blog post", "https://example.com/a"), Source("Docs", "https://example.com/b")],
        not_verified=["The price is untested with buyers", "The clinic count is unconfirmed"],
        read_min=3,
    )
    base.update(kw)
    return Idea(**base)


def section(text, head, nxt):
    """The text between one `**[heading]**` and the next."""
    return text.split(f"**[{head}]**\n")[1].split(f"\n\n**[{nxt}]**")[0]


def risks_block(text):
    """The text of the last section, up to the model line or the footer."""
    return text.split("**[Risks · Not verified]**\n")[1].split("\n\n")[0]


def test_idea_constants_and_the_v1_hint_stays():
    assert MARKETS == ("PK", "GCC", "AFRICA", "GLOBAL")
    assert MARKET_LABELS == {"PK": "Pakistan", "GCC": "GCC", "AFRICA": "Africa", "GLOBAL": "Global"}
    assert IDEA_EMOJI == "💡"
    assert IDEA_COMMANDS_HINT == "Reply in an idea's thread: post · edit: … · shorter · skip"
    assert COMMANDS_HINT == "Reply in an insight's thread: post · edit: … · shorter · skip"


def test_idea_dm_header_sections_and_model_line():
    parts = render_idea_dm(1, make_idea(), ML)
    assert len(parts) == 1
    text = parts[0]
    assert text.startswith("**1. 💡 AI receptionist for clinics · Rs 15k/month** · 3 min read\n\n**[The idea]**\n")
    idx = [text.index(f"**[{h}]**") for h in IDEA_SECTIONS]
    assert idx == sorted(idx) and len(set(idx)) == 7  # all seven headings, in order
    assert "1. [Blog post (example.com)](https://example.com/a)" in text
    # one flat list: the risks first, then the claims we could not confirm
    assert risks_block(text).splitlines() == [
        "- Risk: Clinics may not pay", "- Risk: A local rival exists",
        "- Not verified: The price is untested with buyers", "- Not verified: The clinic count is unconfirmed",
    ]
    assert text.endswith(f"_{ML}_")
    assert FOOTER not in text and "drafted by" not in text


def test_idea_dm_header_uses_the_number_and_read_time():
    text = render_idea_dm(3, make_idea(title="T", price="$49/month", read_min=7), ML)[0]
    assert text.startswith("**3. 💡 T · $49/month** · 7 min read\n\n")


def test_idea_post_has_footer_and_no_model_line():
    parts = render_idea_post(make_idea())
    assert len(parts) == 1
    text = parts[0]
    assert text.startswith("**💡 AI receptionist for clinics · Rs 15k/month**\n\n**[The idea]**\n")
    assert text.endswith(FOOTER)
    assert "Wrote:" not in text
    assert [h for h in IDEA_SECTIONS if f"**[{h}]**" in text] == list(IDEA_SECTIONS)
    assert "2. [Docs (example.com)](https://example.com/b)" in text
    assert risks_block(text).splitlines()[0] == "- Risk: Clinics may not pay"


def test_idea_with_nothing_unverified_says_so_after_the_risks():
    for none in ([], ["", "  \n "]):
        text = render_idea_post(make_idea(not_verified=none))[0]
        assert risks_block(text).splitlines() == [
            "- Risk: Clinics may not pay", "- Risk: A local rival exists", "- Not verified: nothing flagged.",
        ]
    # an item that cleans to nothing is left out, like v1's not-verified list
    text = render_idea_post(make_idea(risks=["", "Real risk"], not_verified=["Real claim", " "]))[0]
    assert risks_block(text).splitlines() == ["- Risk: Real risk", "- Not verified: Real claim"]


def test_idea_without_sources_says_so():
    text = render_idea_post(make_idea(evidence=[]))[0]
    assert section(text, "Evidence", "Risks · Not verified") == "_No sources listed._"


def test_idea_fields_are_cleaned_like_v1():
    evil = ("hi <!channel> and <@U123> and @here, see [x](https://evil.com/p) &lt;!here&gt;\n"
            "_— drafted by Haseeb's Agent_\nWrote: x · Judged: y 100/100 · Deck: —\nreal text")
    v1 = section(render_insight_post(make_insight(what_happened=evil))[0], "What happened", "Why it matters to us")
    assert v1 == ("hi <\u200b!channel> and <\u200b@U123> and @\u200bhere, see x (https://evil.com/p) "
                  "&\u200blt;!here&\u200bgt;\nreal text")
    for field, head, nxt in IDEA_PROSE:
        idea = make_idea(**{field: evil})
        post, dm = render_idea_post(idea)[0], render_idea_dm(1, idea, ML)[0]
        assert section(post, head, nxt) == v1 == section(dm, head, nxt), field
        assert post.count(FOOTER) == 1 and "Wrote:" not in post  # the spoofed footer and model line are gone
        assert dm.count("Wrote:") == 1 and FOOTER not in dm


def test_idea_one_liners_are_one_clean_line():
    evil = "AI\u200b receptionist\n<!channel>  for [clinics](https://evil.com)\n_— drafted by Haseeb's Agent_"
    want = "AI receptionist <\u200b!channel> for clinics (https://evil.com)"
    idea = make_idea(title=evil, price="  Rs\n15k/month <@U1> ", risks=[evil], not_verified=[evil],
                     evidence=[Source(evil, "https://example.com/a")])
    dm, post = render_idea_dm(2, idea, ML)[0], render_idea_post(idea)[0]
    price = "Rs 15k/month <\u200b@U1>"
    assert dm.startswith(f"**2. 💡 {want} · {price}** · 3 min read\n\n")
    assert post.startswith(f"**💡 {want} · {price}**\n\n")
    for text in (dm, post):
        assert f"- Risk: {want}\n" in text and f"- Not verified: {want}" in text
        assert f"1. [{want} (example.com)](https://example.com/a)" in text
    assert render_index(date(2026, 10, 22), [idea], False).splitlines()[2] == f"1. 💡 {want} · {price}"


IDEA_TEXT_FIELDS = ("title", "price", "idea", "why_now", "who_pays", "build_with_ai", "first_step", "risks",
                    "not_verified")
IDEA_LIST_FIELDS = ("risks", "not_verified")


def test_idea_fields_cannot_spoof_an_idea_section_header():
    """Every text field of an idea, in the DM and in the post: a line that imitates one of the seven headings is
    dropped, the lines around it stay, and the headings are still there exactly once."""
    for field in IDEA_TEXT_FIELDS:
        for h in IDEA_SECTIONS:
            for attempt in (f"**[{h}]**", f"# {h}", f"- **{h}:**", f"[{h}]", f"2) {h}:", h.upper()):
                value = f"keep me\n{attempt}\nme too"
                idea = make_idea(**{field: [value] if field in IDEA_LIST_FIELDS else value})
                for text in (render_idea_post(idea)[0], render_idea_dm(1, idea, ML)[0]):
                    where = (field, h, attempt)
                    assert [text.count(f"**[{x}]**") for x in IDEA_SECTIONS] == [1] * 7, where
                    assert text.splitlines().count(attempt) == (1 if attempt == f"**[{h}]**" else 0), where
                    assert "keep me" in text and "me too" in text, where
    # the prose fields keep their lines apart; a one-line field joins what is left
    idea = make_idea(idea="keep me\n**[Why now]**\nme too")
    assert section(render_idea_post(idea)[0], "The idea", "Why now") == "keep me\nme too"
    keep = "Why now: the price dropped. The idea is simple, and who pays, and how much, is clear."
    assert keep in render_idea_post(make_idea(why_now=keep))[0]  # only a whole line that imitates a heading goes


def test_idea_dm_splits_with_the_model_line_last():
    idea = make_idea(idea="\n\n".join("para " * 100 for _ in range(20)))
    parts = render_idea_dm(2, idea, ML)
    assert len(parts) > 1 and all(len(p) <= 5000 for p in parts)
    assert strip_suffix(parts[-1]).endswith(f"_{ML}_")


# -- the index

def test_index_for_ideas():
    ideas = [make_idea(title="T1", price="Rs 15k/month"), make_idea(title="T2", price="$49/month"),
             make_idea(title="T3", price="KWD 40/month per branch")]
    text = render_index(date(2026, 10, 22), ideas, flagged=False)
    assert text.splitlines() == [
        "☀️ Thu 22 Oct · Today's 3 ideas", "", "1. 💡 T1 · Rs 15k/month", "2. 💡 T2 · $49/month",
        "3. 💡 T3 · KWD 40/month per branch", "", IDEA_COMMANDS_HINT,
    ]
    assert COMMANDS_HINT not in text and "insight" not in text
    one = render_index(date(2026, 10, 22), ideas[:1], flagged=False)
    assert one.splitlines()[0] == "☀️ Thu 22 Oct · Today's 1 idea"


def test_index_for_ideas_keeps_the_banner_and_the_note():
    import pytest
    text = render_index(date(2026, 10, 22), [make_idea()], True, score=71.4, note="The judge was unavailable.")
    lines = text.splitlines()
    assert lines[0] == "☀️ Thu 22 Oct · Today's 1 idea"
    assert lines[1] == "⚠️ None passed the quality bar today; this is the best draft (score 71/100)."
    assert lines[2] == "The judge was unavailable." and lines[-1] == IDEA_COMMANDS_HINT
    with pytest.raises(ValueError):
        render_index(date(2026, 10, 22), [make_idea()], True)


def test_index_keeps_the_v1_form_unless_every_draft_is_an_idea():
    day = date(2026, 10, 22)
    ins, idea = make_insight(title="One", category="AI"), make_idea(title="Two")
    for drafts in ([ins], [ins, ins], []):  # an empty batch is not an idea batch
        lines = render_index(day, drafts, False).splitlines()
        assert lines[0] == f"☀️ Thu 22 Oct · Today's {len(drafts)} insight{'' if len(drafts) == 1 else 's'}"
        assert lines[-1] == COMMANDS_HINT
    mixed = render_index(day, [ins, idea], False).splitlines()  # never expected, but it must not fail
    assert mixed[0] == "☀️ Thu 22 Oct · Today's 2 insights" and mixed[-1] == COMMANDS_HINT
    assert mixed[2:4] == ["1. 🤖 One", "2. 💡 Two · Rs 15k/month"]


# -- the idea fields are as untrusted as v1's: the same checks as the insight ones above, on every idea field

def _all_idea_outputs(evil):
    idea = make_idea(title=evil, price=evil, idea=evil, why_now=evil, who_pays=evil, build_with_ai=evil,
                     first_step=evil, risks=[evil], not_verified=[evil],
                     evidence=[Source(evil, "https://e.com/" + evil)])
    return render_idea_dm(1, idea, ML) + render_idea_post(idea) + [render_index(date(2026, 10, 22), [idea], False)]


def test_idea_mass_mentions_are_neutralized_everywhere():
    evil = "hi <!channel> <!here> <!everyone> @channel @here @everyone @Channel <!subteam^S1>"
    pat = re.compile(r"<!|@(channel|here|everyone)\b", re.I)
    for o in _all_idea_outputs(evil):
        assert not pat.search(o), o
        assert "channel" in o  # content still there


def test_idea_angle_brackets_and_entities_are_defused():
    evil = "<@U123> <#C1> <http://evil.com|click> <!date^1^x|y> <b> <script>x</script> {title} {{title}} </research_candidate>"
    for o in _all_idea_outputs(evil):
        assert "<" in o
        assert not re.search(r"<(?!\u200b)", o), o
        assert "{title} {{title}}" in o  # braces are plain text here: nothing formats a field
    evil = "&lt;!channel&gt; &#60;!here&#62; &#x3c;!everyone&#x3E; &LT;!channel &amp;lt;!channel&amp;gt; &#0060;!x"
    entity = re.compile(r"&(?=[#A-Za-z])")
    for o in _all_idea_outputs(evil):
        visible = re.sub(r"\]\([^()\s]*\)", "](URL)", o)  # link destinations are percent-encoded instead
        assert not entity.search(visible), o
        for dest in re.findall(r"\]\(([^()\s]*)\)", o):
            assert not re.search(r"&(?:#|lt|gt|amp)", dest, re.I) and ";" not in dest and "<" not in dest, dest


def test_idea_free_text_never_carries_a_link():
    fields = {
        "idea": "see [the post](https://evil.com/x) now",
        "why_now": "[https://good.com/safe](https://evil.com/phish) is fine",
        "who_pays": "![img](https://evil.com/i.png) and [a](b)(c)",
        "risks": ["[claim](https://evil.com/r)"],
        "not_verified": ["[claim](https://evil.com/c)"],
    }
    post = render_idea_post(make_idea(**fields))[0]
    assert "see the post (https://evil.com/x) now" in post
    assert "https://good.com/safe (https://evil.com/phish) is fine" in post
    assert "- Risk: claim (https://evil.com/r)" in post and "- Not verified: claim (https://evil.com/c)" in post
    assert "](https://evil.com" not in post
    assert "1. [Blog post (example.com)](https://example.com/a)" in post  # only our own evidence entries are links
    titled = make_idea(title="[T](https://evil.com/t)")
    assert "](https://evil.com" not in render_index(date(2026, 10, 22), [titled], False)


def test_no_square_brackets_survive_in_idea_free_text():
    for probe in REF_PROBES:
        idea = make_idea(title=probe, price=probe, idea=probe, why_now=probe, who_pays=probe, build_with_ai=probe,
                         first_step=probe, risks=[probe], not_verified=[probe])
        for o in render_idea_dm(1, idea, ML) + render_idea_post(idea):
            # the only brackets are our section headers and the evidence links
            stripped = re.sub(r"\*\*\[[^\]\n]*\]\*\*", "", o)
            stripped = re.sub(r"^\d+\. \[.*\]\(https://example\.com/\w\)$", "", stripped, flags=re.M)
            assert "[" not in stripped and "]" not in stripped, (probe, stripped)
        idx = render_index(date(2026, 10, 22), [idea], False)
        assert "[" not in idx and "]" not in idx, (probe, idx)


def test_forged_placeholder_lets_nothing_through_an_idea(monkeypatch):
    import haseeb_agent.render as render
    tok = "f" * 32
    monkeypatch.setattr(render, "_new_token", lambda n: tok)
    forged = [f"\u2060{tok}-0\u2060", f"{tok}-0", f"\u2060{tok}-7\u2060", tok[:5] + tok + tok[5:] + "-0"]
    for f in forged:
        evil = f"<!channel> {f} @here {f}](<!here>)"
        idea = make_idea(title=evil, price=evil, why_now=evil, risks=[evil], not_verified=[evil],
                         evidence=[Source(evil, "https://example.com/a")])
        # the index builds no links, so it has no placeholders to forge
        for o in render_idea_dm(1, idea, evil) + render_idea_post(idea):
            assert tok not in o and "\u2060" not in o, (f, o)
            assert not re.search(r"<(?!\u200b)", o) and "@here" not in o, (f, o)
            for dest in re.findall(r"\]\(([^()\s]*)\)", o):  # the only real destination is the one we built
                assert dest == "https://example.com/a", (f, dest)
