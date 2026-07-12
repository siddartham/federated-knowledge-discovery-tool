#!/usr/bin/env bash
#
# A/B the relevance-gate specifically: default (gated) vs the gate turned off.
# A thin convenience wrapper around ab_config.sh — see that script (and
# docs/ab-testing.md) for the general "A/B any config change" tool.
#
#   scripts/ab_gate.sh <questions.txt>
#
set -euo pipefail

QUESTIONS="${1:?usage: scripts/ab_gate.sh <questions.txt>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p .ab
printf '[evidence]\nrelevance_gated = false\n' > .ab/gate-off.toml
exec "$HERE/ab_config.sh" .ab/gate-off.toml "$QUESTIONS"
