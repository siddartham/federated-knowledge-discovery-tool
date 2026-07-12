"""Running dollar total across every LLM call in a run.

"Cost control on a system that wants to be agentic" only works if something
counts tokens as they're spent and is checked *before* the next expensive
call - not tallied up after the fact in a dashboard. Every LLM call type
(plan, score, restitch, synthesize) records here, so the budget guard sees
the full picture, not just the plan/synthesis calls the loop makes directly.
"""

from __future__ import annotations

from dossier.config import SETTINGS

# Rough per-million-token prices, USD, for cost tracking only - not billing.
# Configured in config/defaults.toml ([cost.*_per_mtok]); a production
# deployment could point these at the provider's pricing API instead - they
# drift. Keyed by model id.
_INPUT_COST_PER_MTOK = SETTINGS.cost.input_per_mtok
_OUTPUT_COST_PER_MTOK = SETTINGS.cost.output_per_mtok


class CostTracker:
    def __init__(self, max_cost_usd: float) -> None:
        self.max_cost_usd = max_cost_usd
        self.spent_usd = 0.0

    def record(self, model: str, input_tokens: int, output_tokens: int) -> None:
        self.spent_usd += (
            input_tokens / 1_000_000 * _INPUT_COST_PER_MTOK.get(model, 0.0)
            + output_tokens / 1_000_000 * _OUTPUT_COST_PER_MTOK.get(model, 0.0)
        )

    def exceeded(self) -> bool:
        return self.spent_usd >= self.max_cost_usd
