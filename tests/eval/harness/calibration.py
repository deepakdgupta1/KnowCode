"""Sufficiency score calibration for KnowCode's local-first routing claim.

KnowCode's product claim: *"If sufficiency_score >= 0.8, answer locally with
zero external tokens."*  This module measures whether that claim is well-
calibrated — i.e. whether a sufficiency_score of X predicts answer correctness
with probability X.

Calibration concepts used here follow Guo et al. (2017) "On Calibration of
Modern Neural Networks": bin predicted confidences, compute actual accuracy per
bin, compute Expected Calibration Error (ECE).

Schema references: docs/GOLDEN_DATASET_PIPELINE.md §11.

"answer_correctness" in this module means the retrieved context contained every
``must_mention_fact`` from the golden record.  Because ``must_mention_facts``
checking currently requires an LLM judge (see scorer.score_narrative), the
``correct`` field on each record is an input — callers must populate it before
calling these functions.
"""

from __future__ import annotations

import math
from typing import Any


# ---------------------------------------------------------------------------
# Record type alias
# ---------------------------------------------------------------------------

# A "calibration record" is the output of scorer.score_record(), optionally
# augmented with a ``correct: bool`` field set by the narrative judge.
CalibrationRecord = dict[str, Any]


# ---------------------------------------------------------------------------
# Correctness gate
# ---------------------------------------------------------------------------

_SUFFICIENCY_THRESHOLD = 0.8  # mirrors AppConfig default


def passes_routing_gate(record: CalibrationRecord, threshold: float = _SUFFICIENCY_THRESHOLD) -> bool:
    """Return True if the system would route this query to local answering."""
    return float(record.get("sufficiency_score", 0.0)) >= threshold


def answer_correctness_at_threshold(
    records: list[CalibrationRecord],
    threshold: float = _SUFFICIENCY_THRESHOLD,
) -> dict[str, Any]:
    """Compute answer correctness for queries routed to local answering.

    This is the *operational* metric for the routing claim (§11):
    of all queries where sufficiency_score >= threshold, what fraction
    have ``correct == True``?

    Args:
        records:   List of calibration records.  Each must have a ``correct``
                   field (bool).  Records without ``correct`` are excluded.
        threshold: The sufficiency gate value.  Defaults to 0.8.

    Returns:
        Dict with:
        - ``threshold``: the gate value used
        - ``routed_count``: queries whose sufficiency_score >= threshold
        - ``judged_count``: subset with a ``correct`` value
        - ``correct_count``: subset that are both routed and correct
        - ``answer_correctness``: correct_count / judged_count (None if 0)
    """
    routed = [r for r in records if passes_routing_gate(r, threshold)]
    judged = [r for r in routed if r.get("correct") is not None]
    correct = [r for r in judged if r.get("correct") is True]
    correctness = len(correct) / len(judged) if judged else None
    return {
        "threshold": threshold,
        "routed_count": len(routed),
        "judged_count": len(judged),
        "correct_count": len(correct),
        "answer_correctness": correctness,
    }


# ---------------------------------------------------------------------------
# Calibration curve (reliability diagram data)
# ---------------------------------------------------------------------------


def build_calibration_curve(
    records: list[CalibrationRecord],
    n_bins: int = 10,
) -> dict[str, Any]:
    """Build the reliability diagram data for sufficiency_score.

    Bins [0, 1) into *n_bins* equal-width intervals.  For each bin reports
    the mean predicted sufficiency and the observed fraction of ``correct``
    answers.  Records without a ``correct`` field are excluded.

    Args:
        records: Calibration records with ``sufficiency_score`` and ``correct``.
        n_bins:  Number of equal-width bins.  Must be >= 1.

    Returns:
        Dict with:
        - ``n_bins``: number of bins requested
        - ``bins``: list of bin dicts, each with:
            - ``bin_lower``: lower edge of this bin
            - ``bin_upper``: upper edge
            - ``count``: records in this bin
            - ``mean_predicted``: mean sufficiency_score in bin
            - ``fraction_correct``: observed accuracy (None if count == 0)
        - ``ece``: Expected Calibration Error across all judged records
        - ``judged_count``: total records with a ``correct`` value
    """
    if n_bins < 1:
        raise ValueError("n_bins must be >= 1")

    judged = [r for r in records if r.get("correct") is not None]

    bin_width = 1.0 / n_bins
    bins: list[dict[str, Any]] = []

    for i in range(n_bins):
        lower = i * bin_width
        upper = lower + bin_width
        # Include upper boundary in the last bin to catch score == 1.0
        in_bin = [
            r for r in judged
            if lower <= float(r.get("sufficiency_score", 0.0)) < upper
            or (i == n_bins - 1 and float(r.get("sufficiency_score", 0.0)) == 1.0)
        ]
        mean_pred = (
            sum(float(r["sufficiency_score"]) for r in in_bin) / len(in_bin)
            if in_bin else None
        )
        frac_correct = (
            sum(1 for r in in_bin if r.get("correct")) / len(in_bin)
            if in_bin else None
        )
        bins.append({
            "bin_lower": round(lower, 4),
            "bin_upper": round(upper, 4),
            "count": len(in_bin),
            "mean_predicted": round(mean_pred, 4) if mean_pred is not None else None,
            "fraction_correct": round(frac_correct, 4) if frac_correct is not None else None,
        })

    ece = _compute_ece(bins, total=len(judged))

    return {
        "n_bins": n_bins,
        "bins": bins,
        "ece": round(ece, 4),
        "judged_count": len(judged),
    }


def _compute_ece(bins: list[dict[str, Any]], total: int) -> float:
    """Weighted mean absolute gap between predicted confidence and observed accuracy."""
    if total == 0:
        return 0.0
    error = 0.0
    for b in bins:
        if b["count"] == 0 or b["mean_predicted"] is None or b["fraction_correct"] is None:
            continue
        weight = b["count"] / total
        gap = abs(b["mean_predicted"] - b["fraction_correct"])
        error += weight * gap
    return error


# ---------------------------------------------------------------------------
# Over/under-confidence diagnosis
# ---------------------------------------------------------------------------


def diagnose_calibration(curve: dict[str, Any]) -> str:
    """Return a one-line diagnosis of calibration quality.

    Possible return values (§11 "Phase 1 reveals a finding"):
    - ``"well_calibrated"``   — ECE < 0.05
    - ``"over_confident"``    — system reports higher sufficiency than it delivers
    - ``"under_confident"``   — system is more correct than it claims
    - ``"insufficient_data"`` — fewer than 20 judged records

    Args:
        curve: Output of ``build_calibration_curve``.
    """
    if curve.get("judged_count", 0) < 20:
        return "insufficient_data"

    ece: float = curve.get("ece", 0.0)
    if ece < 0.05:
        return "well_calibrated"

    # Compute net bias: positive means over-confident
    bias_sum = 0.0
    weight_sum = 0.0
    for b in curve.get("bins", []):
        if b["count"] == 0 or b["mean_predicted"] is None or b["fraction_correct"] is None:
            continue
        bias_sum += b["count"] * (b["mean_predicted"] - b["fraction_correct"])
        weight_sum += b["count"]

    if weight_sum == 0:
        return "insufficient_data"

    net_bias = bias_sum / weight_sum
    return "over_confident" if net_bias > 0 else "under_confident"
