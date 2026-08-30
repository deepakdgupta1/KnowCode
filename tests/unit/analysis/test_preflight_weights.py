"""Preflight weights are an invariant, not a suggestion (BL-21).

`docs/product/business-logic.md` says the weights "must sum to 1.0" and
``assess_codebase``'s own docstring repeats it. Nothing enforced it. The
composite divides by the sum, so a set summing to zero divided by a ``1e-9``
floor and clamped to 1.0: a codebase that parsed nothing was graded A and
cleared a ``min_score`` build gate, silently and maximally wrong.

Both entry points are covered. The parser is where a misconfiguration should
be caught, and ``assess_codebase`` is public and takes ``weights`` directly,
so neither guard makes the other redundant.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from knowcode.analysis.preflight import DEFAULT_WEIGHTS, assess_codebase
from knowcode.config import _DEFAULT_PREFLIGHT_WEIGHTS, AppConfig


def _assess(weights: dict[str, float] | None):  # type: ignore[no-untyped-def]
    """Grade a codebase that parsed nothing -- an unambiguous F."""
    return assess_codebase(
        entities={},
        relationships=[],
        scanned_files=[],
        parse_errors=["everything failed to parse"],
        weights=weights,
    )


def _load(weights_yaml: str) -> AppConfig:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "aimodels.yaml"
        path.write_text(f"preflight:\n  min_score: 0.9\n  weights:\n{weights_yaml}")
        return AppConfig.load(path)


def test_a_weight_set_summing_to_zero_is_rejected_not_graded_a() -> None:
    """The division that produced the A: every weight zero, so the sum is 0.0.

    ``max(weight_sum, 1e-9)`` turned that into a division by ``1e-9``, which
    clamped to ``overall_score = 1.0`` and ``overall_grade = A`` on a codebase
    whose ``parse_success_rate`` scored 0.0. No negative weight is needed to
    reach it -- this is the sum guard alone.
    """
    with pytest.raises(ValueError, match="sum to 1.0, not 0"):
        _assess(dict.fromkeys(DEFAULT_WEIGHTS, 0.0))


def test_a_negative_weight_is_rejected() -> None:
    """A dimension scoring well can never make a codebase worse.

    The audit's own reproduction lands here rather than on the sum guard:
    ``language_coverage`` 0.5 against ``documentation_density`` -0.5 sums to
    exactly 0.0, but the negative is the more specific fault and is named
    first.
    """
    zeroed = dict.fromkeys(DEFAULT_WEIGHTS, 0.0)
    zeroed["language_coverage"] = 0.5
    zeroed["documentation_density"] = -0.5

    with pytest.raises(ValueError, match="negative: documentation_density"):
        _assess(zeroed)


def test_a_weight_set_that_merely_misses_one_is_rejected() -> None:
    """Not just the pathological zero sum: any set that does not normalise.

    A partial override rebalances nothing, so ``{parse_success_rate: 0.5}``
    on top of the defaults sums to 1.30 and silently deflates every score.
    """
    weights = dict(DEFAULT_WEIGHTS)
    weights["parse_success_rate"] = 0.50

    with pytest.raises(ValueError, match="sum"):
        _assess(weights)


def test_the_config_parser_rejects_a_weight_set_that_does_not_sum_to_one() -> None:
    with pytest.raises(ValueError, match="sum"):
        _load("    parse_success_rate: 0.5\n")


def test_the_config_parser_rejects_a_negative_weight() -> None:
    with pytest.raises(ValueError, match="negative"):
        _load("    parse_success_rate: -0.2\n    language_coverage: 0.5\n")


def test_the_defaults_are_accepted_by_both_guards() -> None:
    """Over-rejection guard: the shipped configuration must still load."""
    assert _assess(None).overall_grade == "F"
    assert _assess(dict(DEFAULT_WEIGHTS)).overall_grade == "F"
    assert AppConfig.default().preflight.weights == _DEFAULT_PREFLIGHT_WEIGHTS


def test_a_rebalanced_custom_weight_set_is_accepted() -> None:
    """Customising weights stays possible -- it just has to add up."""
    rebalanced = dict.fromkeys(DEFAULT_WEIGHTS, 0.0)
    rebalanced["parse_success_rate"] = 0.6
    rebalanced["documentation_density"] = 0.4

    report = _assess(rebalanced)

    assert report.overall_score == 0.0
    assert report.overall_grade == "F"


def test_the_two_default_weight_tables_have_not_drifted() -> None:
    """``config`` and ``preflight`` each carry a copy of the same table.

    They are validated against the same invariant, so a drift between them
    would let the parser accept a set the assessor rejects -- or ship a
    default configuration that cannot load.
    """
    assert _DEFAULT_PREFLIGHT_WEIGHTS == DEFAULT_WEIGHTS
