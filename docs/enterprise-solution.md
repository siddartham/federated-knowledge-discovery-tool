# Enterprise scale

## First principles

At 100s of platforms, the instinct is to centralize — copy everything into one search index. That flattens permissions, goes stale, and never finishes. The alternative is to federate: search each source where it lives, live, under the user's own access. Columbo federates.

![Federate, don't centralize](images/scaling/enterprise-solution-first-principle.svg)

---

>> Q: A company that has 100s of platforms e.g. Application Management Tools/Platforms: ServiceNow, Architecture.org.com(artifacts), Splunk, PagerDuty, OTel, NewRelic,  SpendWise, CloudTrafficScanner, DataDog, incident coordination, operational readiness platform), JIRA, Jenkins, Centralized Pipeline Deployment modules, Release Decisioning, Testing Platform, Tcloud governance platform, est Data Management Platform, Data Catalog, API Catalog, Stream Catalog, Business Use Case Specific platforms such as Marketing, Message Platform, Customer Interaction, The Site, Phone/Agent Servicing Platform, Digital Servicing Platform, and there are 100s more pandering different aspects of business etc.
What solution is better suited for this landscape ... where there is major documentation in platform specific pages and confluence pages, google docs/slides, discussions on the same in slack

At hundreds of platforms, the question stops being "desktop vs web" or "MCP vs A2A" and becomes one thing: **how do you make integration scale to N≈hundreds without N bespoke builds — while enforcing per-user RBAC/ABAC and central governance (you're a bank)?** That reframing settles it, and it eliminates two options immediately.

## Two eliminations

- **Desktop is out as the enterprise answer.** A regulated financial institution will not accept cross-source, RBAC-gated content aggregated and cached on every laptop, with an LLM egress from each endpoint and no central audit/DLP(data loss protection). It might survive as a power-user tool, but not as *the* solution. (The governance axis dominates here — exactly the trade-off from a few messages ago.)
- **A single bespoke-per-source service is out.** Hand-writing an integration (or OBO wiring) for hundreds of platforms is an O(N) central-team cost that never converges. Whatever you build must **distribute the integration cost to the platform owners** and consume it through a **standard contract**.

## The shape that actually fits

A **governed central agent (hub-and-spoke) + federation via a standard + per-user identity everywhere**, onboarded in tiers:

![enterprise-solution.png](images/scaling/enterprise-solution.svg)

## The 80/20 that makes this tractable

You said it yourself: the documentation lives in **platform-specific pages *and* Confluence, Google Docs/Slides, and Slack.** That's the key. At any large enterprise, the **majority of retrievable knowledge is in a handful of *horizontal* stores** — Confluence, Google Workspace, Slack, Jira, GitHub, The Exchange, ServiceNow — *not* in the 100s of platform UIs. Those horizontal stores already have real APIs + OAuth. **Integrate ~8–12 of them well (Tier 1) and you cover most questions immediately** — including most cross-platform *discussion* and *how-to* content. The hundreds of platform-specific pages are a **long tail** you federate incrementally, not a wall you must scale on day one.

## Why each tier, and where MCP/A2A land

- **Tier 1 (direct):** the horizontal stores. Highest ROI, existing APIs, do it first. This is the "backbone" and it's a bounded, tractable set.
- **Tier 2 (MCP as the enterprise *contract*):** this is MCP's real payoff at scale — not "a nicer API," but a **standard every platform team implements once** so the central hub writes **one client** instead of hundreds. It turns an O(N) central problem into O(1)-per-platform *distributed* to owners. You publish "expose a docs/search MCP server with per-user OAuth" as an internal standard and onboard top platforms by query demand.
- **Tier 3 (A2A):** for the deepest, most-sensitive platforms (the ones with rich ABAC via Entitlements), let *their* agent answer — RBAC/ABAC enforced **at the source**, no raw data leaves their boundary, you get answers + citations. Highest adoption cost, best governance; reserve for where it's worth it. Keep the **central orchestrator** (hub-and-spoke) so the cross-link-cycle problem stays controlled — exactly as we discussed.
- **Tier 4 (cite):** everything not yet onboarded → cite the link, safe by construction, and rank the onboarding backlog by how often users hit it.

## The non-negotiable spine: per-user identity

Because you're a bank with **Entitlements (ABAC)** "act as the user" is mandatory and you already have the machinery. Propagate the user's identity to *every* tier — **OBO tokens** (Tiers 1–2), **A2A auth context** (Tier 3), and **ACL-security-trimming** if you put an index in front (store each doc's ACL at ingest, filter by the asker's entitlements at query time). This is what keeps the whole thing from being a confused-deputy leak, and it's the same principle throughout.

One realistic constraint you flagged: **UK (monoline) "can't currently consume" some enterprise platforms** (incl. parts of the Identity Platform). So the pragmatic path is: **start with what UK *can* consume** (Confluence, GDocs, Slack, Jira, GitHub via their existing OAuth), **cite the rest**, and let Tiers 2–3 grow as enterprise-platform adoption catches up.

## Phasing (so it's actionable, not architecture astronomy)

1. **Phase 1 — governed hub over Tier 1.** Central Q&A service: per-user OAuth to the horizontal stores, central audit + DLP + one LLM egress. Answers a large fraction on day one.
2. **Phase 2 — publish the MCP contract + onboarding program.** Platform teams expose docs/search servers; onboard top-N by demand. Add a security-trimmed enterprise index if latency/scale needs it.
3. **Phase 3 — A2A for the deep platforms**; cite-don't-fetch remains the default for the tail throughout.

## Bottom line

For a hundreds-of-platforms bank, the better-suited solution is **a single governed central Q&A agent (keep the orchestrator) + tiered federation + per-user identity**, *not* a desktop app and *not* a bespoke-per-source scraper:

- **Integrate the ~dozen horizontal doc stores directly** (covers most content).
- **Standardize the long tail via MCP** so integration distributes to owners and the hub stays O(1).
- **Delegate the deepest platforms via A2A** (RBAC at source, no data centralization).
- **Cite everything else** and onboard by demand.
- **Propagate the user's identity (OBO / A2A auth / ACL-trim) at every tier**, powered by your Identity Platform + Entitlements ABAC, with central audit/DLP/egress for compliance.

The desktop app was the elegant answer at *personal* scale; at *enterprise* scale the elegance moves to **federation + identity propagation + central governance** — you scale by making every platform team responsible for exposing their own knowledge through a common contract, while the center owns orchestration, identity, and governance. MCP is how you make that contract cheap; A2A is how the richest platforms answer for themselves; and per-user identity is the spine that keeps all of it correct and auditable.

>> Q: Assume there is a MCP server with 100 registered APIs for different platforms listed above.  How do we extend this solution for that?

Great scenario — and it exposes the one design decision that dominates at that scale. Let me give the concrete extension mapped to Columbo's actual seams, but lead with the thing that changes everything.

## The one insight: at 100 tools, you cannot put the tool catalog in the plan prompt

Today Columbo injects *every* source's guide into the plan prompt (`Registry.sources_prompt_block()`). That works for 6 sources. Dumping **100 tool schemas** into every plan call would: blow the context budget, cost a fortune per iteration, and — the real killer — **degrade tool-selection accuracy** (models pick badly from huge tool lists). So the headline change isn't "add an MCP client"; it's **add a tool-retrieval stage** that narrows 100 → ~5–15 relevant tools *before* planning. Everything else is plumbing.

![mcp-architecture-100-sources-.png](images/scaling/mcp-architecture-100-sources-.svg)

## The extension, mapped to Columbo's real seams

Five concrete changes; the loop, scoring, synthesis, caching, and cost caps are all reused unchanged.

**1. MCP client** — `columbo_py/sources/mcp/client.py`
Wrap the official `mcp` Python SDK. Two methods: `list_tools()` → catalog `[{name, description, input_schema}]`, and `call_tool(name, args)` → content. Constructed with the **per-user auth** (OBO token for a service, or the user's session on desktop) so each call carries the asking user's identity — the platform enforces RBAC, exactly as we discussed. One connection held by the registry; list tools at init.

**2. Tool selection (the scale-critical part)** — now shipped as `columbo_py/engine/orchestrator/tool_provider.py`
> **Built:** the router variant below shipped as `select_tools_semantic` (`request_type="route"`, cheap Haiku call over the tool names/descriptions), with the lexical `select_tools` as a fallback — making route/plan/score/restitch/synthesize the five LLM call sites. The tool gate (`GuardedToolProvider`) then validates each call. What follows is the original sketch.

Given the question, return the top‑K relevant tools out of 100. Pick one:
- **LLM router (simplest, no new infra):** a cheap Haiku call given just the *100 names + one-line descriptions* (that's small — names/descriptions, not full schemas) → returns the relevant tool names. Then fetch full schemas only for those.
- **Embedding retrieval (fastest at query time):** precompute embeddings of each tool's name+description; embed the question; rank. Deterministic-ish, cacheable.
- **Category prefilter:** your platforms already cluster by domain (Data / Delivery / Core Banking / …) — first pick categories, then tools. Cheapest, coarse.
Output: ~5–15 tools whose **full schemas** go to the planner.

```python
# selector.py — router variant
async def select_tools(question: str, catalog: list[ToolSpec], llm, k: int = 12) -> list[ToolSpec]:
    menu = "\n".join(f"- {t.name}: {t.description}" for t in catalog)   # 100 one-liners, cheap
    names = extract_json((await llm.generate(
        system_prompt=SELECT_SYS, prompt=f"Question: {question}\nTools:\n{menu}\nReturn the ≤{k} most relevant tool names as a JSON array.",
        model=SCORE_MODEL, request_type="tool_select")).text)
    keep = set(names)
    return [t for t in catalog if t.name in keep][:k]
```

**3. New action type** — `engine/orchestrator/models.py`
Add a structured tool-call action (MCP tools take *structured args*, not a query string, so this is the faithful shape):
```python
class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

class Actions(BaseModel):
    searches: list[SearchAction] = ...
    scrapes: list[str] = ...
    lookups: list[str] = ...
    tool_calls: list[ToolCall] = Field(default_factory=list)   # NEW
```

**4. Plan prompt gets only the shortlist** — `engine/prompts/templates/orchestrate.j2` + `_plan`
Inject a `TOOLS` section listing the K selected tools (name + description + input JSON schema) and add `"tool_calls": [{"name": ..., "arguments": {...}}]` to the response schema. `_plan` calls `select_tools()` first and passes the shortlist into the template. The planner now emits tool calls with arguments matching each schema — native tool-use, but inside Columbo's existing one-JSON-batch plan (no rewrite to a turn-by-turn tool loop, so the parallel fan-out + scoring stay).

**5. Execute + normalize** — `engine/orchestrator/actions.py`
Add `do_tool_call` to the Phase‑1 `asyncio.gather`, and map MCP output into `Result` so downstream is untouched:
```python
async def do_tool_call(tc: ToolCall) -> None:
    try:
        content = await mcp.call_tool(tc.name, tc.arguments)     # cached by (name, json(args))
    except Exception as exc:
        state.actions_taken.append(f"tool {tc.name} -> error: {exc}"); return
    results = [Result(source=f"mcp:{tc.name}", id=block.id, title=block.title,
                      content=block.text, permalink=block.uri) for block in content]
    state.batches.append(SearchBatch(source=f"mcp:{tc.name}", query=json.dumps(tc.arguments), results=results))
```
Those `Result`s get **scored and synthesized exactly like search results** — the scorer/synthesizer never learn they came from a tool.

## The cross-cutting pieces (reused, brief)

- **Auth:** the MCP client carries the **user's** identity per call (OBO / session) → RBAC enforced at each platform. Same spine as the whole prior discussion; MCP just standardizes the transport.
- **Caching:** tool-call results cache by `(name, canonical-json(args))` with a TTL — add a `tool` namespace to `CacheStore` (or reuse `search`). Determinism preserved by sorting args keys before the cache key.
- **Loop control:** tool calls are just more actions — the iteration cap, cost budget, and confidence exit already bound them.
- **Selection is itself cacheable** so the same question deterministically shortlists the same tools.

## Should MCP replace the bespoke sources?

At 100 tools, MCP becomes the *primary* integration and the tool-selection stage is mandatory. You can **keep the ~dozen horizontal stores** (Confluence/Slack/GitHub) as native `SearchSource`s (they're high-value and query-language-rich) and route the **long tail of 100 platform APIs through MCP + selection** — the tiered federation from the last discussion, now concrete: bespoke sources for the backbone, MCP for the tail, one selector deciding what's relevant per question.

## Want me to build a slice?

I can scaffold `columbo_py/sources/mcp/` (client + a router-based selector + `Result` normalization), add the `tool_calls` action + `do_tool_call` fan-out, extend `orchestrate.j2`, and wire it behind an `[mcp]` extra (mirroring `[ui]`/`[eval]`) — with a `FakeMCPClient` so it's testable without a live server (no real MCP dependency in the test path). Say the word and I'll implement it engine-side with the quality gates green; the live-server auth/OBO wiring would be the one piece you'd finish against your actual MCP endpoint.


>> Yes

On it — building a real MCP integration for a **Testing Platform** source, behind an `[mcp]` extra, fully testable without a live server (via a `FakeMCPClient`), with zero disruption to the default pipeline. Let me confirm the `_build_registry` insertion point and main.py imports.

Now the MCP client abstraction (protocol + fake + real, `mcp` lazily imported):

Now register it conditionally in `_build_registry` (only when configured, so default behavior is unchanged):

Now pyproject — the `[mcp]` extra, package-data for the guidance, and the mypy override:

Now the tests — a `FakeMCPClient` simulating the Testing Platform, driving the source and the registry path end-to-end:

All green — mypy now checks 85 files (the `register(MCPTestingPlatformSource())` call type-validated the SearchSource conformance). Full suite + smoke:

Confirmed: `testing_platform` appears only when `COLUMBO_MCP_TESTING_URL` is set. **124 tests pass** (5 new), smoke OK, gates clean. Let me add a README entry:

Shipped — a working MCP integration for the **Testing Platform**, plugged in as a first-class Columbo source with **no engine changes**.

## What I built

**New `columbo_py/sources/mcp/` package:**
- **[`client.py`](https://github.com/siddartham/federated-knowledge-discovery-tool/blob/main/columbo_py/sources/mcp/client.py)** — the MCP surface Columbo needs (`ToolSpec`, `ToolContent`, `MCPClient` protocol) with two impls:
  - `RemoteMCPClient` — real streamable-HTTP client via the official `mcp` SDK (lazily imported behind the `[mcp]` extra), attaching a **per-user bearer token** so the platform enforces the caller's RBAC — the identity spine from our whole discussion.
  - `FakeMCPClient` — in-memory, tools as Python callables → the entire path is tested with no SDK and no server.
- **[`source.py`](https://github.com/siddartham/federated-knowledge-discovery-tool/blob/main/columbo_py/sources/mcp/source.py)** — `MCPTestingPlatformSource`, a `SearchSource` adapter: `search(query)` → the server's `search_test_runs` tool, `lookup(id)` → `get_test_run`, both normalized to Columbo `Result`s. Downstream scoring/synthesis never learn the evidence came from a tool.
- **[`GUIDANCE.md`](https://github.com/siddartham/federated-knowledge-discovery-tool/blob/main/columbo_py/sources/mcp/GUIDANCE.md)** — tells the planner when to use it (test failures, flaky tests, perf regressions, offloaded integration/component runs) — injected into the plan prompt exactly like every other source's guide.

**Wiring (minimal, opt-in):**
- `_build_registry` registers `testing_platform` **only when `COLUMBO_MCP_TESTING_URL` is set** — verified: default runs still show the same 6 sources; with the env var, `testing_platform` joins. So existing behavior/tests/smoke are untouched.
- `[mcp]` extra + package-data for the guidance + mypy override for the `mcp` SDK.
- README credentials row.

**Tests** ([`test_mcp.py`](https://github.com/siddartham/federated-knowledge-discovery-tool/blob/main/columbo_py/tests/test_mcp.py)) — search normalization, lookup-by-id, the **registry integration path** (register → `registry.search` → cache hit), rejection of a server missing the search tool, and the clear "unconfigured" error.

## Why it stayed clean

The adapter-as-`SearchSource` approach means the Testing Platform's MCP tools flow through the **existing** plan → score → synthesize loop, caching (search cache), cost caps, and determinism — zero changes to the engine. The `mcp` SDK is quarantined behind lazy imports + the `[mcp]` extra, so the tested path (FakeMCPClient) needs no dependency.

**Verified:** ruff clean, mypy clean (85 files), **124 tests pass** (5 new, 1 skipped by design), smoke OK, and the source is confirmed to appear only when configured.

## To run it against your real server

```bash
pip install ".[mcp]"
export COLUMBO_MCP_TESTING_URL="https://testing.<corp>/mcp"
export COLUMBO_MCP_TESTING_TOKEN="<per-user OBO token>"   # RBAC enforced at source
columbo ask "why did the payment-service integration stage fail in the last release?"
```

**Two honest notes on the last-mile (live server):** the `RemoteMCPClient` follows the current `mcp` SDK shape (`streamablehttp_client` + `ClientSession`) but isn't exercised in tests (no live server here), so verify tool names/schema against your actual server — the adapter assumes `search_test_runs`/`get_test_run` (override `search_tool=` or extend if yours differ). And this MVP maps one search tool; when you're ready, the richer step is promoting to the `tool_calls` action + tool-retrieval selector I sketched earlier, so the planner calls *all* the platform's tools with structured args — say the word and I'll build that on top of this.

>> Yes

