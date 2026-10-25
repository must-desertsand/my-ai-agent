"""What a post points at (haseeb_agent/site_review/targets.py): links from Slack markup and bare URLs, slack.com and file
links skipped, at most 2, and the HTML attachment. Plain parsing: no network."""
from __future__ import annotations

import pytest

from haseeb_agent.models import FileRef
from haseeb_agent.site_review.targets import (Target, file_label, find_html, find_links, find_targets, host_label,
                                              is_html_file)


def f(name, mime="application/octet-stream", fid="F1"):
    return FileRef(fid, name, mime)


# ---------------------------------------------------------------- links

@pytest.mark.parametrize("text,want", [
    ("check <http://reach.io|reach.io> out", ["http://reach.io"]),
    ("demo: <https://reach-poc.vercel.app|reach-poc.vercel.app>", ["https://reach-poc.vercel.app"]),
    ("no label <https://example.com/a/b>", ["https://example.com/a/b"]),
    ("amp <https://example.com/a?x=1&amp;y=2|here>", ["https://example.com/a?x=1&y=2"]),
    ("bare https://example.com/page and more", ["https://example.com/page"]),
    ("a sentence ends with https://example.com/page.", ["https://example.com/page"]),
    ("or with a comma https://example.com/page, then", ["https://example.com/page"]),
    ("(see https://example.com/y)", ["https://example.com/y"]),
    ("wiki https://en.wikipedia.org/wiki/Foo_(bar) ok", ["https://en.wikipedia.org/wiki/Foo_(bar)"]),
    ("quoted \"https://example.com/q\" and 'https://example.org/r'", ["https://example.com/q", "https://example.org/r"]),
    ("HTTPS://EXAMPLE.COM/Path", ["https://EXAMPLE.COM/Path"]),
    ("a fragment https://example.com/app#/client/view", ["https://example.com/app#/client/view"]),
    ("markdown [click](http://evil.example/x) here", ["http://evil.example/x"]),
    ("http://93.184.216.34/demo", ["http://93.184.216.34/demo"]),
    ("a port https://example.com:8443/x", ["https://example.com:8443/x"]),  # kept: the policy refuses it, with a reason
])
def test_links_are_found_in_slack_markup_and_bare_urls(text, want):
    assert find_links(text) == want


@pytest.mark.parametrize("text", [
    "reach-poc.vercel.app on its own is not a link", "see example.com", "mailto:someone@example.com",
    "ftp://example.com/x", "javascript:alert(1)", "data:text/html,<h1>x</h1>", "file:///etc/passwd",
    "<mailto:a@b.c|a@b.c>", "<tel:+123|call>", "https://", "http:///nohost", "",
    "https://example.com:99999/x",  # not a port
    "https://example.com/" + "a" * 2100,  # too long
])
def test_what_is_not_an_http_link_is_not_a_target(text):
    assert find_links(text) == []


@pytest.mark.parametrize("url", [
    "https://mustcompany.slack.com/archives/C0C62C128D8/p1792574403734689", "https://slack.com/help/articles/1",
    "https://app.slack.com/client/T/C", "https://files.slack.com/files-pri/T-F/x", "https://slack-edge.com/x.js",
    "https://slack-files.com/T-F-abc", "https://a.b.slack.com/x", "<https://mustcompany.slack.com/docs/T/F|Notes>",
])
def test_a_slack_link_is_not_a_site(url):
    assert find_links(url) == []


@pytest.mark.parametrize("url", [
    "https://example.com/report.pdf", "https://example.com/a/b.PNG", "https://example.com/x.zip?dl=1",
    "https://example.com/sheet.xlsx", "https://cdn.example.com/video.mp4", "https://example.com/data.csv",
    "https://example.com/x.docx#page=2", "https://example.com/notes.md", "https://example.com/pkg.dmg",
])
def test_a_link_to_a_file_is_not_a_site(url):
    assert find_links(f"look {url} here") == []


@pytest.mark.parametrize("url", [
    "https://example.com/demo.html", "https://example.com/index.php", "https://example.com/app/", "https://example.com",
    "https://example.com/v1.2/page", "https://example.com/a.b/c", "https://github.com/org/repo/blob/main/app.py",
    "https://example.com/page.html?file=x.pdf",
])
def test_a_link_to_a_page_is_a_site(url):
    assert find_links(url) == [url]


def test_at_most_two_links_in_the_order_the_post_mentions_them():
    text = "first https://a.example.com/1 then <https://b.example.com/2|b> and https://c.example.com/3"
    assert find_links(text) == ["https://a.example.com/1", "https://b.example.com/2"]
    assert find_links(text, limit=3)[2] == "https://c.example.com/3"


def test_slack_markup_and_bare_urls_are_ordered_by_where_they_sit_and_a_link_is_not_found_twice():
    text = "bare https://z.example.com/ first, <https://a.example.com/|a> second, <https://a.example.com/|again>"
    assert find_links(text, limit=5) == ["https://z.example.com/", "https://a.example.com/"]


@pytest.mark.parametrize("text", [
    "https://example.com/a https://example.com/a", "https://example.com/a https://example.com/a/",
    "<https://example.com/a|x> https://example.com/a", "https://example.com/a?q=1 https://example.com/a?q=1",
])
def test_the_same_link_twice_is_one_target(text):
    assert len(find_links(text, limit=5)) == 1


def test_different_queries_or_hosts_are_different_targets():
    assert len(find_links("https://example.com/a?q=1 https://example.com/a?q=2", limit=5)) == 2
    assert len(find_links("https://example.com/a https://example.org/a", limit=5)) == 2


def test_the_label_of_slack_markup_is_never_a_link_of_its_own():
    """<url|label>: the link is the url. A label that looks like another address does not send the review there."""
    text = "<https://real.example.com/x|https://fake-label.example.net/y> and <https://b.example.com|b.example.com>"
    assert find_links(text, limit=5) == ["https://real.example.com/x", "https://b.example.com"]


def test_a_post_full_of_slack_links_and_one_site():
    text = ("Here is the <https://mustcompany.slack.com/archives/C/p1|thread> and the <https://files.slack.com/x/y.pdf|deck>, "
            "and the demo: <https://reach-poc.vercel.app|reach-poc.vercel.app>")
    assert find_links(text) == ["https://reach-poc.vercel.app"]


# ---------------------------------------------------------------- labels

@pytest.mark.parametrize("url,want", [
    ("https://reach-poc.vercel.app/x", "reach-poc.vercel.app"), ("http://EXAMPLE.com:8080/", "example.com"),
    ("https://example.com./", "example.com"), ("http://93.184.216.34/", "93.184.216.34"),
    ("https://bücher.example/", "xn--bcher-kva.example"), ("http://[2001:4860:4860::8888]/", "a linked site"),
    ("http://exa mple.com/", "a linked site"), ("http:///x", "a linked site"), ("not a url", "a linked site"),
])
def test_a_host_label_is_ascii_letters_digits_dots_and_hyphens_or_a_plain_phrase(url, want):
    assert host_label(url) == want


@pytest.mark.parametrize("name,want", [
    ("reach-io-poc-21-oct.html", "reach-io-poc-21-oct.html"), ("../../etc/passwd<script>.html", "passwdscript.html"),
    ("C:\\Users\\x\\demo page.html", "demo page.html"), ("", "the attached page"), ("<<<>>>", "the attached page"),
    ("a" * 80 + ".html", "a" * 40), ("ünï.html", "n.html"),
])
def test_a_file_label_is_reduced_to_safe_characters(name, want):
    assert file_label(name) == want


# ---------------------------------------------------------------- the HTML attachment

@pytest.mark.parametrize("ref", [f("demo.html"), f("DEMO.HTM"), f("page", "text/html"), f("x.bin", "text/html; charset=utf-8"),
                                 f("x", "application/xhtml+xml"), f("reach-io-poc-21-oct.html", "text/html")])
def test_an_html_file_is_recognised_by_name_or_type(ref):
    assert is_html_file(ref)


@pytest.mark.parametrize("ref", [f("report.pdf", "application/pdf"), f("x.txt", "text/plain"), f("notes.md", "text/markdown"),
                                 f("htmlish.png", "image/png"), f("a.html.pdf", "application/pdf"), f("", "")])
def test_other_files_are_not_html(ref):
    assert not is_html_file(ref)


def test_the_content_type_the_read_reports_counts_too():
    assert is_html_file(f("download"), "text/html") and not is_html_file(f("download"), "application/pdf")


def test_only_the_first_html_attachment_is_taken_and_pdfs_are_left_alone():
    files = [f("deck.pdf", "application/pdf", "F1"), f("a.html", "text/html", "F2"), f("b.html", "text/html", "F3")]
    assert find_html(files).id == "F2" and find_html([files[0]]) is None and find_html([]) is None


def test_targets_are_the_links_then_the_html_attachment():
    files = [f("deck.pdf", "application/pdf", "F1"), f("reach-io-poc-21-oct.html", "text/html", "F2")]
    got = find_targets("the demo is <https://reach-poc.vercel.app|reach-poc.vercel.app> and a copy attached", files)
    assert got == [Target("link", "reach-poc.vercel.app", url="https://reach-poc.vercel.app"),
                   Target("html", "reach-io-poc-21-oct.html", ref=files[1])]


def test_a_post_with_nothing_to_look_at_has_no_targets():
    assert find_targets("just words, and a PDF", [f("deck.pdf", "application/pdf")]) == []
    assert find_targets("", []) == []


def test_two_links_and_an_attachment_make_three_targets():
    got = find_targets("https://a.example.com and https://b.example.com and https://c.example.com", [f("d.html")])
    assert [t.kind for t in got] == ["link", "link", "html"]
