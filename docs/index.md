# Dossier documentation

Dossier is a command-line agent that answers questions across an enterprise
knowledge base — Slack, Confluence, GitHub, Jira, and Google Drive. It runs a
`(route →) plan → execute → score → synthesize` loop, scoring every piece of
evidence and returning a cited answer, under the user's own credentials.

These pages are the design deep-dives. For install and quick-start, see the
`README.md` at the repository root. New here? Start with
**[First principles](first-principles.md)** — a one-page map of every design
decision to the principle behind it.

## Architecture & internals
- [Call tree](call_tree.md) — the full path from `question` to `answer`: the five LLM calls (route/plan/score/restitch/synthesize), the loop, and the two `asyncio.gather` fan-outs.
- [Request trace](request_simulation.md) — a worked question → answer walkthrough with example state, marking LLM calls vs. I/O.
- [Scoring & confidence](scoring-and-confidence.md) — from first principles: why the planner's confidence and the Haiku scorer use the dimensions they do, the soft spots, and how to harden them.
- [Evaluation & validation](evaluation.md) — the LLM-as-judge harness (faithfulness / answer-relevancy / context-precision as the RAG triad) and the three-layer validation stack.
- [A/B testing changes](ab-testing.md) — how to decide whether a scoring change helps: the offline counterfactual and the paired end-to-end test.
- [Libraries used](libraries-used.md) — the component → enabling-library map (why the orchestration core is mostly standard library).
- [Guardrails](guardrails.md) — a guard at every trust boundary: the request-flow diagram, per-guard mechanics, and config.
- [Permalinks](permalinks.md) — per-source permalink reliability notes.

## Auth
- [Auth from first principles](auth-basics.md) — OAuth, SSO, IdP, OBO / token-exchange, RBAC / ACL, cookies vs. bearer tokens.
- [Per-deployment auth flows](dossier-auth-flow.md) — the auth flow for each option (desktop, web service, delegated OBO, cite-don't-fetch).

## Scaling & integration
- [MCP / A2A adoption](mcp-a2a-adoption.md) — whether MCP or A2A are worth adopting here.
- [Enterprise scale](enterprise-solution.md) — federating across an org with 100s of platforms.
- [CLI → microservice](cli-to-microservice.md) — reusing the engine behind a FastAPI service, and what it costs.
