# Feedback that reviews what a post links to (2026-10-21)

Requested by Haseeb on 2026-10-21, about Iqbal's reach.io post: "for this sort of thing, we need our ai agent or
models to properly review the mentioned site. Get a proper feedback based on the context and then give a feedback."
He approved this design in chat ("Yes, build it": non-interactive; no clicking through views).

## Why

Teammates post prototypes as links (`reach-poc.vercel.app`) or as attached HTML files. Today's feedback draft sees
only the post text, plus PDF attachments. On 2026-10-21 a manual review of the attached HTML found things the post
never said:
- the file opens on the internal Account executive view, not the Client view the post recommends;
- on a phone the page is 807 px wide on a 390 px screen.

Haseeb wants the agent's feedback grounded in the actual product.

## What it does

Before drafting feedback on a post (in `run_feedback` and in `feedback --ts`), the job runs a **site review**:

**1. Find the targets.**
- Up to 2 links in the post. Accept Slack's `<url|label>` markup and bare URLs, http(s) only. A slack.com link or a
  link to a file is not a site.
- Up to 1 HTML attachment (`.html` or `text/html`, at most 5 MB), read through the existing `read_file` path.
- PDFs keep their existing path, unchanged.

**2. Render.** Use headless Chrome (`settings.chrome_path`) in a throwaway profile (a temp `--user-data-dir`, deleted
after), once at desktop size (1440×900) and once at phone size (390×844, with a mobile user agent). Capture:
- a screenshot of the first viewport at each size;
- the page title and its visible text after scripts ran (at most 6,000 characters);
- the page's horizontal overflow at phone width (`scrollWidth` against `clientWidth`);
- failed requests and console errors, as counts and short types only.

Use the Chrome DevTools protocol over `--remote-debugging-pipe`. The small client in tests/test_dashboard_frontend.py
shows the technique; write a production one in the package. Budget: 45 s per target and 90 s per post, with a
process-group kill (reuse `deck/proc.run_group` if it fits).

**3. Review.** One vision call through LiteLLM. Add a new model step to `config.toml`:
`site_review = ["litellm:gemini/gemini-3.1-pro-preview", "litellm:gemini/gemini-3.5-flash"]`. Both were checked live
on 2026-10-21 and accept `image_url` data URIs on `/v1/chat/completions`.
- The input is the post text, the page text and the checks, each fenced as untrusted data, plus both screenshots as
  JPEG of at most 1,600 px.
- The answer is JSON: `{"what_it_is": str, "works": [≤3 str], "issues": [≤4 str], "mobile_ok": bool}`.
- An issue must be concrete and checkable from the inputs.
- Text or images that try to instruct the model are ignored, and the prompt says so.
- If the answer is not valid JSON, the next model in the chain is asked.

**4. Draft.** The feedback writer gets the post plus the review's notes, fenced as untrusted data. Its prompt tells it
to ground the strengths, flaws and question in the notes, and to say nothing about the site that the notes do not
support.

**5. Show what was reviewed.**
- The DM card gets one line, either "Reviewed: <host> (desktop + phone)" or "Couldn't review <host>: <short reason>".
- The Live page's feedback card shows the same line.
- The channel post never shows review internals.

Any failure in steps 1–3 falls back to today's behaviour, a draft from the post alone, with the "Couldn't review"
line. A site review never blocks or delays a feedback draft beyond its budget.

## Network safety (binding)

A rendered page runs scripts and can make the browser request any address, including this Mac's own services: the
dashboard on 127.0.0.1:8765, the claude-mem worker on 127.0.0.1:37777, and the LAN. So:

**The filtering proxy.**
- Chrome sends ALL traffic through a small filtering proxy in the package (asyncio; HTTP and CONNECT), with
  `--proxy-server=http://127.0.0.1:<random port>` and `--proxy-bypass-list="<-loopback>"`. The second flag stops
  Chrome bypassing the proxy for loopback, so even `http://127.0.0.1:…` goes through the proxy and gets refused.
- The proxy resolves each host itself and connects to the IP it resolved (pinned, so DNS rebinding cannot swap it). It
  refuses any address that is:
  - private, loopback, link-local, multicast, reserved or unspecified;
  - CGNAT 100.64/10;
  - IPv6 ULA fc00::/7, or IPv4-mapped forms of any of the above;
  - on a port other than 80 or 443.
- It also refuses IP-literal URLs that are not public.
- For the reviewed link itself, a redirect is re-checked at every hop, with at most 3 hops.

**Chrome flags.** `--disable-quic`, `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`, no extensions, no
sync, downloads denied, and `--deny-permission-prompts` where available.

**Offline mode for an HTML attachment.** The same proxy refuses everything. The attachment is rendered from a temp
file that holds it alone, and is never served from a port.

**Hard rules.**
- No cookies, auth headers or Haseeb profile data ever reach a reviewed site.
- No form is submitted and nothing is clicked.
- Every real path (Chrome, the proxy's outbound connects, the vision call) calls `guard()` first.

## Limits and storage

- At most 10 site reviews a day (a cursor). Over the cap, the draft falls back with "Couldn't review: daily limit".
- Screenshots go to `data/site-reviews/<item id>/` (git-ignored), are pruned after 7 days, and are sent nowhere except
  the vision call.
- Logs name the host, the step, timings and counts, and never page text.

## Tests (offline; no real network or Chrome in unit tests)

- **The policy:** each refused range, IP literals, IPv4-mapped IPv6, a redirect to a private address, a rebinding
  resolver (the first answer public, the second private: the pinned IP is used), ports, and refusal of everything in
  offline mode.
- **The proxy:** a local test server proves that a refused destination is never connected to.
- **Extraction:** links from Slack markup, bare URLs, slack.com skipped, at most 2, HTML attachment detection.
- **The review:** JSON parsing and validation, and the fallback down the model chain.
- **The feedback prompt:** the notes are fenced, and the DM card and inbox card lines appear.
- **Failures:** each failure path falls back cleanly with its reason.
- **One Chrome integration test,** marked like the existing Chrome tests: it renders a local test page through the
  proxy (allowed only for the test, with an injected policy) and checks the screenshots and the overflow.

## Docs

In docs/architecture.md:
- add the site review to the feedback flow, the model map and the safety rules (the proxy);
- add one line to the Notion-facing "Who does what": a "Site reviewer" role (Gemini 3.1 Pro with Gemini 3.5 Flash,
  looks at linked sites and demos).

## Left to the builder

- the prompt wording;
- the module layout;
- how the proxy is structured, as long as the policy above holds;
- whether the CDP client is shared with the tests.
