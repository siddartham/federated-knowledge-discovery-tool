#!/usr/bin/env bash
#
# A/B test ANY config change end-to-end. Runs each question in a list twice —
# once with the default config (baseline), once with your override deep-merged in
# via DOSSIER_CONFIG (treatment) — captures each run as an eval sample, then judges
# both arms with the LLM-as-judge. Compare the two "Aggregate" blocks.
#
#   scripts/ab_config.sh <override.toml> <questions.txt>
#
# The override is a partial TOML with just the keys you're changing, e.g.:
#   confidence cutoff:   printf '[loop]\nconfidence_cutoff = 0.7\n'        > cut.toml
#   cheaper synthesis:   printf '[models]\nsynthesis = "claude-sonnet-4-6"\n' > synth.toml
#   looser admission:    printf '[evidence]\nscore_threshold_points = 2\n' > adm.toml
#   gate off:            printf '[evidence]\nrelevance_gated = false\n'    > gate.toml
#
# Needs ANTHROPIC_API_KEY (for `ask` and the judge) and any source credentials the
# questions touch. Read-only apart from files it writes under ./.ab/.
set -euo pipefail

OVERRIDE="${1:?usage: scripts/ab_config.sh <override.toml> <questions.txt>}"
QUESTIONS="${2:?usage: scripts/ab_config.sh <override.toml> <questions.txt>}"
CLI="python -m dossier.cli"
WORK=".ab"
mkdir -p "$WORK"
: > "$WORK/samples-baseline.jsonl"
: > "$WORK/samples-override.jsonl"

while IFS= read -r q || [ -n "$q" ]; do
  [ -z "${q// }" ] && continue
  echo ">> $q"
  $CLI ask "$q" --capture-eval "$WORK/samples-baseline.jsonl" >/dev/null
  DOSSIER_CONFIG="$OVERRIDE" $CLI ask "$q" --capture-eval "$WORK/samples-override.jsonl" >/dev/null
done < "$QUESTIONS"

echo
echo "================ arm A · baseline (default config) ================"
$CLI devtools eval "$WORK/samples-baseline.jsonl" | tail -6
echo
echo "================ arm B · override ($OVERRIDE) ================"
$CLI devtools eval "$WORK/samples-override.jsonl" | tail -6
echo
echo "Compare the two aggregates. For a scoring/selection change, read context_precision first."
