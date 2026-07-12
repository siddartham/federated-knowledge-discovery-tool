The *reliability* of each source's permalink varies, and there are two real per-source caveats. Here's the full picture:

| Source (method) | permalink value | Reliability |
|---|---|---|
| Jira ([:128](columbo_py/sources/jira/source.py:128)) | constructed `{base}/browse/{KEY}` | ✅ always valid |
| GitHub issues ([:124](columbo_py/sources/github/source.py:124)) | `item["html_url"]` | ✅ API always returns it |
| GitHub code search ([:138](columbo_py/sources/github/source.py:138)) | `item["html_url"]` | ✅ |
| GitHub code lookup ([:199](columbo_py/sources/github/source.py:199)) | `item["html_url"]` | ✅ |
| Slack search ([:164](columbo_py/sources/slack/source.py:164)) | `match.get("permalink")` | ⚠️ None-able if API omits it |
| Drive ([:149](columbo_py/sources/drive/source.py:149)) | `file.get("webViewLink")` | ⚠️ None-able for some file types |
| Confluence ([:135](columbo_py/sources/confluence/source.py:135)) | `{base}{_links.webui}` | ⚠️ **degrades to bare `base_url`** if `_links.webui` is missing |
| Slack channel lookup ([:145](columbo_py/sources/slack/source.py:145)) | constructed `app.slack.com/client/_/{id}` | ⚠️ present but a SPA deep-link, poor scrape target |

So where URLs actually get dropped, and it's not source-specific:

1. **The plan digest** ([`evidence_summary`](columbo_py/engine/orchestrator/state.py:61)) drops permalinks for **every** source — the top-evidence line prints `[score] source:id title` and never `e.permalink`. That's the gap from last message; it starves the scrape action equally for all sources.
2. **The score prompt** input ([`score_batch`](columbo_py/engine/orchestrator/actions.py:163)) builds `{id, title, source, content}` — no permalink. But that's fine; scoring doesn't need URLs.
3. **Synthesis keeps them** — [`_synthesize`](columbo_py/engine/orchestrator/loop.py:190) passes `permalink=e.permalink` and `Citation` carries it. So citations aren't affected.

Two genuine per-source issues the audit turned up (beyond the digest drop):

- **Confluence has a latent bug**: if `_links.webui` is absent, the permalink silently becomes just `base_url` — a URL that points at the wiki root, not the page. That's worse than `None` (it looks valid, scrapes the wrong thing). Should guard: emit `None` when `webui` is missing.
- **None-ability**: Slack-search and Drive permalinks can legitimately be `None`, so the digest line (and anything consuming these for scraping) must None-guard — which my proposed `if e.permalink` guard already does.

If you want, I'll apply a small bundle: (a) surface permalinks in the digest with a `None`-guard, (b) fix the Confluence degrade-to-root so a missing `webui` yields `None` instead of a misleading URL, and (c) a test asserting the digest carries a real permalink and skips it when absent. Want me to?


