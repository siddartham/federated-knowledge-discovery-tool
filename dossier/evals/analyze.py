"""Offline analysis of Dossier run logs — validates the scoring / confidence design
against real runs, read-only.

Consumes the JSONL event logs written by ``EventEmitter`` (in ``~/.dossier/runs``)
and runs the three checks from docs/scoring-and-confidence.md:

1. dimension independence — a Spearman correlation matrix over the score (and
   confidence) dimensions, flagging pairs that move together (double-counting) or
   dimensions with no variance (no signal);
2. scorer vs. citations — whether the ids the answer cited rank high in the
   scorer's ordering (does the score predict what synthesis uses?);
3. confidence calibration — exit confidence vs. answer quality, when an evals file
   of ``{question, faithfulness}`` is supplied.

Pure stdlib (no numpy) so it has no runtime dependencies of its own.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from dossier.config import SETTINGS
from dossier.engine.orchestrator.models import MAX_SCORE, scored_composite

SCORE_DIMS = ("direct_relevance", "answer_potential", "context_value", "source_quality")
CONF_DIMS = ("explicit_evidence", "implicit_evidence", "evidence_consistency", "answer_specificity")

ADMIT_THRESHOLD = SETTINGS.evidence.score_threshold_points / MAX_SCORE
CORR_FLAG = 0.7  # |rho| at/above this flags a pair as too correlated
VAR_FLAG = 0.25  # dimension variance at/below this flags "no signal"

Event = dict[str, Any]


# ---- loading -----------------------------------------------------------------

def load_events(runs_dir: Path) -> list[Event]:
    events: list[Event] = []
    for path in sorted(runs_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def load_evals_index(path: Path) -> dict[str, float]:
    """Map question -> faithfulness from a JSONL of {question, faithfulness}."""
    index: dict[str, float] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if "question" in row and "faithfulness" in row:
            index[str(row["question"])] = float(row["faithfulness"])
    return index


def score_rows(events: list[Event]) -> list[dict[str, Any]]:
    """Every four-dimension score tuple, from search/lookup results
    (`score_complete`) and restitched scrape spans (`restitch_complete`)."""
    rows: list[dict[str, Any]] = []
    for e in events:
        if e.get("event") == "score_complete" and e.get("ok"):
            rows.extend(s for s in (e.get("scores") or []) if all(d in s for d in SCORE_DIMS))
        elif e.get("event") == "restitch_complete" and all(d in e for d in SCORE_DIMS):
            rows.append(e)
    return rows


def confidence_rows(events: list[Event]) -> list[Event]:
    return [e for e in events if e.get("event") == "plan_complete" and all(d in e for d in CONF_DIMS)]


# ---- statistics (pure) -------------------------------------------------------

def _avg_ranks(vals: list[float]) -> list[float]:
    """Ranks with ties averaged (so Spearman = Pearson on ranks handles ties)."""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1  # 1-based average rank across the tie block
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return math.nan
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return math.nan
    return num / (dx * dy)


def spearman(xs: list[float], ys: list[float]) -> float:
    return _pearson(_avg_ranks(xs), _avg_ranks(ys))


def _cols(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[str, list[float]]:
    return {k: [float(r[k]) for r in rows] for k in keys}


def correlation(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[str, float]:
    cols = _cols(rows, keys)
    out: dict[str, float] = {}
    for i, a in enumerate(keys):
        for b in keys[i:]:
            out[f"{a}|{b}"] = spearman(cols[a], cols[b])
    return out


def means(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[str, float]:
    n = len(rows) or 1
    return {k: sum(float(r[k]) for r in rows) / n for k in keys}


def variances(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> dict[str, float]:
    out: dict[str, float] = {}
    for k in keys:
        vals = [float(r[k]) for r in rows]
        n = len(vals) or 1
        m = sum(vals) / n
        out[k] = sum((v - m) ** 2 for v in vals) / n
    return out


def flags(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[str]:
    notes: list[str] = []
    corr = correlation(rows, keys)
    for pair, rho in corr.items():
        a, b = pair.split("|")
        if a != b and not math.isnan(rho) and abs(rho) >= CORR_FLAG:
            notes.append(f"{a} <-> {b}: rho={rho:+.2f} (moves together - candidate to merge / down-weight)")
    for k, v in variances(rows, keys).items():
        if v <= VAR_FLAG:
            notes.append(f"{k}: variance={v:.2f} (near-constant - little signal)")
    return notes


# ---- experiment 3: scorer vs. citations -------------------------------------

def citation_analysis(events: list[Event]) -> dict[str, Any]:
    by_run: dict[str, dict[str, Any]] = {}
    for e in events:
        rid = str(e.get("run_id"))
        d = by_run.setdefault(rid, {"scores": {}, "cited": []})
        if e.get("event") == "score_complete" and e.get("ok"):
            for s in e.get("scores") or []:
                d["scores"][s["id"]] = s.get("composite")
        elif e.get("event") == "restitch_complete":
            d["scores"][e["url"]] = e.get("composite")  # scrape evidence is keyed by url
        elif e.get("event") == "synthesize_complete":
            for c in e.get("cited") or []:
                d["cited"].append(c["id"] if isinstance(c, dict) else c)

    ranks: list[int] = []
    total = matched = below = 0
    for d in by_run.values():
        if not d["scores"] or not d["cited"]:
            continue
        ordered = sorted(d["scores"].items(), key=lambda kv: -(kv[1] or 0.0))
        rank_of = {cid: i + 1 for i, (cid, _) in enumerate(ordered)}
        for cid in d["cited"]:
            total += 1
            if cid in rank_of:
                matched += 1
                ranks.append(rank_of[cid])
                if (d["scores"].get(cid) or 0.0) < ADMIT_THRESHOLD:
                    below += 1
    median_rank = sorted(ranks)[len(ranks) // 2] if ranks else None
    return {
        "citations": total,
        "matched_to_a_score": matched,
        "median_rank_of_cited": median_rank,
        "cited_below_admit_threshold": below,
    }


# ---- experiment 2: calibration ----------------------------------------------

def calibration(events: list[Event], evals_index: dict[str, float]) -> list[dict[str, Any]]:
    question: dict[str, str] = {}
    conf: dict[str, float] = {}
    for e in events:
        rid = str(e.get("run_id"))
        if e.get("event") == "run_start":
            question[rid] = str(e.get("question"))
        elif e.get("event") == "run_end" and e.get("final_confidence") is not None:
            conf[rid] = float(e["final_confidence"])
    pairs = [(c, evals_index[question[rid]]) for rid, c in conf.items()
             if question.get(rid) in evals_index]
    bins = [(0.0, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]
    out: list[dict[str, Any]] = []
    for lo, hi in bins:
        sel = [f for c, f in pairs if lo <= c < hi]
        out.append({"confidence": f"[{lo:.1f}, {hi if hi <= 1 else 1.0:.1f})",
                    "n": len(sel),
                    "mean_faithfulness": (sum(sel) / len(sel)) if sel else None})
    return out


def _composite(s: dict[str, Any], *, gated: bool) -> float:
    return scored_composite(
        s["direct_relevance"], s["answer_potential"], s["context_value"], s["source_quality"],
        gated=gated,
    )


def _infer_arm(dims: list[dict[str, Any]]) -> str:
    """Guess which formula produced the logs by matching the logged composite to the
    two candidates — so the counterfactual column is read correctly."""
    mean_hits = gate_hits = 0
    for s in dims:
        logged = s.get("composite")
        if logged is None:
            continue
        if abs(logged - round(_composite(s, gated=False), 4)) < 1e-3:
            mean_hits += 1
        if abs(logged - round(_composite(s, gated=True), 4)) < 1e-3:
            gate_hits += 1
    if gate_hits > mean_hits:
        return "gated"
    if mean_hits > gate_hits:
        return "mean"
    return "unknown"


def gate_comparison(events: list[Event]) -> dict[str, Any]:
    """Offline counterfactual: recompute the mean and gated composites from the logged
    per-dimension scores and compare where the *cited* items rank under each. No
    re-run. Caveat: the cited set came from the config that was live, so this is a
    sanity check (does the gate keep cited evidence ranked well?), not a verdict."""
    by_run: dict[str, dict[str, Any]] = {}
    for e in events:
        rid = str(e.get("run_id"))
        d = by_run.setdefault(rid, {"dims": {}, "cited": []})
        if e.get("event") == "score_complete" and e.get("ok"):
            for s in e.get("scores") or []:
                if all(k in s for k in SCORE_DIMS):
                    d["dims"][s["id"]] = s
        elif e.get("event") == "restitch_complete" and all(k in e for k in SCORE_DIMS):
            d["dims"][e.get("url")] = e
        elif e.get("event") == "synthesize_complete":
            for c in e.get("cited") or []:
                d["cited"].append(c["id"] if isinstance(c, dict) else c)

    ranks_mean: list[int] = []
    ranks_gated: list[int] = []
    below_mean = below_gated = promoted = demoted = total = 0
    all_dims: list[dict[str, Any]] = []
    for d in by_run.values():
        all_dims.extend(d["dims"].values())
        if not d["dims"] or not d["cited"]:
            continue
        cm = {i: _composite(s, gated=False) for i, s in d["dims"].items()}
        cg = {i: _composite(s, gated=True) for i, s in d["dims"].items()}
        rm = {i: r + 1 for r, (i, _) in enumerate(sorted(cm.items(), key=lambda kv: -kv[1]))}
        rg = {i: r + 1 for r, (i, _) in enumerate(sorted(cg.items(), key=lambda kv: -kv[1]))}
        for cid in d["cited"]:
            if cid not in cm:
                continue
            total += 1
            ranks_mean.append(rm[cid])
            ranks_gated.append(rg[cid])
            below_mean += cm[cid] < ADMIT_THRESHOLD
            below_gated += cg[cid] < ADMIT_THRESHOLD
            promoted += rg[cid] < rm[cid]
            demoted += rg[cid] > rm[cid]

    def _median(xs: list[int]) -> Any:
        return sorted(xs)[len(xs) // 2] if xs else None

    return {
        "logs_produced_under": _infer_arm(all_dims),
        "cited_evaluated": total,
        "median_rank_cited_mean": _median(ranks_mean),
        "median_rank_cited_gated": _median(ranks_gated),
        "cited_promoted_by_gate": promoted,
        "cited_demoted_by_gate": demoted,
        "cited_below_threshold_mean": below_mean,
        "cited_below_threshold_gated": below_gated,
    }


def termination(events: list[Event]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in events:
        if e.get("event") == "run_end":
            reason = str(e.get("terminated_reason", "unknown"))
            counts[reason] = counts.get(reason, 0) + 1
    return counts


# ---- report ------------------------------------------------------------------

def build_report(
    events: list[Event],
    evals_index: dict[str, float] | None = None,
    *,
    compare_gate: bool = False,
) -> dict[str, Any]:
    srows = score_rows(events)
    crows = confidence_rows(events)
    report: dict[str, Any] = {
        "runs": len({e.get("run_id") for e in events if e.get("run_id")}),
        "scored_items": len(srows),
        "plan_iterations": len(crows),
        "score_dims": {
            "means": means(srows, SCORE_DIMS),
            "correlation": correlation(srows, SCORE_DIMS),
            "flags": flags(srows, SCORE_DIMS),
        },
        "confidence_dims": {
            "means": means(crows, CONF_DIMS),
            "correlation": correlation(crows, CONF_DIMS),
            "flags": flags(crows, CONF_DIMS),
        },
        "citations": citation_analysis(events),
        "termination": termination(events),
        "calibration": calibration(events, evals_index) if evals_index else None,
    }
    if compare_gate:
        report["gate_comparison"] = gate_comparison(events)
    return report


def _matrix(keys: tuple[str, ...], corr: dict[str, float]) -> list[str]:
    short = {k: k.split("_")[0][:7] for k in keys}
    header = "  " + "".join(f"{short[k]:>9}" for k in keys)
    lines = [header]
    for a in keys:
        cells = []
        for b in keys:
            key = f"{a}|{b}" if f"{a}|{b}" in corr else f"{b}|{a}"
            v = corr.get(key, math.nan)
            cells.append("     -   " if math.isnan(v) else f"{v:>9.2f}")
        lines.append(f"{short[a]:>7} " + "".join(cells))
    return lines


def format_report(r: dict[str, Any]) -> str:
    out: list[str] = []
    out.append(f"Dossier run-log analysis — {r['runs']} run(s), "
               f"{r['scored_items']} scored item(s), {r['plan_iterations']} plan iteration(s)\n")

    out.append("1 · dimension independence (Spearman rho)")
    out.append("  score dimensions:")
    out += ["    " + ln for ln in _matrix(SCORE_DIMS, r["score_dims"]["correlation"])]
    for f in r["score_dims"]["flags"] or ["    (none flagged)"]:
        out.append(f"    ! {f}" if not f.startswith("(") else f"    {f}")
    out.append("  confidence dimensions:")
    out += ["    " + ln for ln in _matrix(CONF_DIMS, r["confidence_dims"]["correlation"])]
    for f in r["confidence_dims"]["flags"] or ["(none flagged)"]:
        out.append(f"    ! {f}" if not f.startswith("(") else f"    {f}")

    c = r["citations"]
    out.append("\n2 · scorer vs. citations")
    out.append(f"    {c['citations']} citation(s); {c['matched_to_a_score']} matched to a scored item")
    out.append(f"    median rank of a cited item: {c['median_rank_of_cited']}")
    out.append(f"    cited but scored below the admit bar (~{ADMIT_THRESHOLD:.2f}): "
               f"{c['cited_below_admit_threshold']}  (scorer under-ranked these)")

    out.append("\n3 · confidence calibration")
    if r["calibration"] is None:
        out.append("    needs answer-quality labels — pass --evals <faithfulness.jsonl>")
    else:
        for row in r["calibration"]:
            mf = "n/a" if row["mean_faithfulness"] is None else f"{row['mean_faithfulness']:.2f}"
            out.append(f"    confidence {row['confidence']}: n={row['n']:>3}  mean faithfulness={mf}")

    gc = r.get("gate_comparison")
    if gc is not None:
        out.append("\n4 · relevance-gate A/B (offline counterfactual, recomputed from the logged dims)")
        out.append(f"    logs produced under: {gc['logs_produced_under']}  "
                   "(the other column is the counterfactual)")
        out.append(f"    {gc['cited_evaluated']} cited item(s) compared")
        out.append(f"    median rank of a cited item — mean: {gc['median_rank_cited_mean']}  ·  "
                   f"gated: {gc['median_rank_cited_gated']}")
        out.append(f"    the gate moved cited items: {gc['cited_promoted_by_gate']} up, "
                   f"{gc['cited_demoted_by_gate']} down in rank")
        out.append(f"    cited below the admit bar — mean: {gc['cited_below_threshold_mean']}  ·  "
                   f"gated: {gc['cited_below_threshold_gated']}  (gating lowers all composites)")
        out.append("    caveat: the cited set came from the live config — circular; a sanity "
                   "check, not a verdict. For a verdict, A/B end-to-end (ask --capture-eval + devtools eval).")

    out.append("\n   termination reasons: "
               + (", ".join(f"{k}={v}" for k, v in r["termination"].items()) or "(none)"))
    return "\n".join(out)
