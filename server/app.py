# server/app.py  — nothing in columbo_py/ changes
import httpx
from fastapi import FastAPI
from pydantic import BaseModel
from columbo_py.engine.orchestrator import loop as orch
from columbo_py.engine.llm.claude import ClaudeClient
from columbo_py.engine.search.registry import Registry
from columbo_py.infra.cache.store import CacheStore
from columbo_py.infra.browser.fetcher import _html_to_text  # reuse the fallback parser
from columbo_py.infra.events.emitter import EventEmitter
from columbo_py.sources.github import GitHubCodeSource, GitHubIssuesSource
from columbo_py.sources.slack import SlackSource
from columbo_py.sources.confluence import ConfluenceSource
from columbo_py.sources.jira import JiraSource
from columbo_py.sources.drive import DriveSource
from columbo_py.sources.dictionary import DictionaryClient


class HttpxFetcher:  # ← satisfies the Fetcher Protocol, no browser
    def __init__(self): self._c = httpx.AsyncClient(follow_redirects=True, timeout=20.0)

    async def fetch(self, url: str) -> str:
        r = await self._c.get(url);
        r.raise_for_status()
        return _html_to_text(r.text)


app = FastAPI()
cache = CacheStore()  # diskcache: safe to share across requests
registry = Registry(cache)
for s in (GitHubCodeSource(), GitHubIssuesSource(),
          SlackSource(),  # no browser_session → uses env creds (xoxp/token)
          ConfluenceSource(), JiraSource(), DriveSource()):
    registry.register(s)
fetcher = HttpxFetcher()
dictionary = DictionaryClient()


class Ask(BaseModel): question: str


@app.post("/ask")
async def ask(req: Ask):
    emitter = EventEmitter(run_id=req.question[:32])  # per-request log
    llm = ClaudeClient(cache, emitter)  # cheap; wraps AsyncAnthropic + cache
    r = await orch.run(req.question, registry, fetcher, llm, emitter,
                       gate=None, dictionary=dictionary)
    return {"answer": r.answer, "citations": r.citations,
            "confidence": r.final_confidence, "iterations": r.iterations,
            "terminated": r.terminated_reason, "cost_usd": r.cost_usd}
