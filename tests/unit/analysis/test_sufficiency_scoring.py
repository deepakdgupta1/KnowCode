"""What a sufficiency score is measured against (BL-20).

The score routes questions: a bundle at or above
``max(sufficiency_threshold, routing_quality_floor)`` may be answered without
an LLM. So the denominator has to describe what the bundle *should* have
contained. Growing it only when a bonus was earned made it describe what the
bundle happened to contain instead, which is not a measurement.
"""

from unittest.mock import MagicMock

import pytest

from knowcode.analysis.context_synthesizer import TASK_TEMPLATES, ContextSynthesizer
from knowcode.data_models import Entity, EntityKind, Location, TaskType

#: The gate these scores are compared against at runtime -- ``max(0.8, 0.9)``.
ROUTING_GATE = 0.9

#: Task types whose template names ``source_code``. A bundle that omits what
#: its own template asked for must not be able to clear the gate.
WANTS_SOURCE = [
    t for t in TASK_TEMPLATES if "source_code" in TASK_TEMPLATES[t]["priority"]
]


def _synthesizer() -> ContextSynthesizer:
    """A synthesizer with no store reads -- only the formula is under test."""
    synthesizer = ContextSynthesizer.__new__(ContextSynthesizer)
    synthesizer.live_loader = None
    synthesizer.index_is_stale = False
    return synthesizer


def _entity(*, source: str | None) -> Entity:
    return Entity(
        id="m.py::alpha",
        kind=EntityKind.FUNCTION,
        name="alpha",
        qualified_name="m.alpha",
        location=Location("m.py", 1, 2),
        signature="def alpha() -> int",
        docstring=None,
        source_code=source,
    )


def _score(task_type: TaskType, *, with_source: bool) -> float:
    """Score a bundle holding every section its template asked for.

    ``with_source`` is the only thing that varies, so any difference between
    the two calls is attributable to the source bonus and nothing else.
    """
    priority = TASK_TEMPLATES[task_type]["priority"]
    included = {
        section: (with_source or section != "source_code") for section in priority
    }
    context_text = "x" * 500 + ("\n## Source Code\n" if with_source else "")
    return _synthesizer()._calculate_sufficiency(
        task_type,
        included,
        _entity(source="return 1" if with_source else None),
        context_text,
    )


@pytest.mark.parametrize("task_type", list(TASK_TEMPLATES))
def test_omitting_source_code_always_costs_a_bundle_something(
    task_type: TaskType,
) -> None:
    """Two bundles differing only in source must not score the same.

    ``locate`` scored 1.00 either way: source is not in its priority list, so
    the only term that could have separated them was the bonus -- and the
    bonus moved numerator and denominator together. A score that cannot tell
    those two bundles apart is not measuring anything about them.
    """
    assert _score(task_type, with_source=False) < _score(task_type, with_source=True)


@pytest.mark.parametrize("task_type", WANTS_SOURCE)
def test_a_bundle_missing_what_its_template_asked_for_cannot_route_locally(
    task_type: TaskType,
) -> None:
    """``extend`` scored 0.91 with no source at all and cleared the 0.9 gate."""
    assert _score(task_type, with_source=False) < ROUTING_GATE


def test_a_complete_bundle_still_clears_the_gate() -> None:
    """The floor has to stay reachable, or nothing could ever answer locally.

    Guards the over-correction: putting the bonuses in the denominator caps a
    bundle whose entity has no long docstring at 0.95-0.96, which must remain
    above the gate.
    """
    for task_type in TASK_TEMPLATES:
        assert _score(task_type, with_source=True) >= ROUTING_GATE


def test_a_bundle_whose_entity_has_no_docstring_does_not_score_perfectly() -> None:
    """1.00 has to mean "nothing is missing", or it means nothing.

    Every entity here has ``docstring=None``, so every bundle below is missing
    the docstring bonus -- and every one of them scored exactly 1.00, for all
    six task types, because the missing bonus was subtracted from the
    denominator too. This is the docstring half of BL-20; the source half is
    the two tests above.
    """
    for task_type in TASK_TEMPLATES:
        assert _score(task_type, with_source=True) < 1.0


def test_an_extend_bundle_with_no_source_blocks_end_to_end() -> None:
    """The same defect through the real synthesizer, not the formula alone.

    Pins that ``content_included`` and ``context_text`` really do reach
    ``_calculate_sufficiency`` the way the tests above assume: an EXTEND bundle
    holding signature, docstring, children and parent -- everything its
    template asks for except source -- scored 0.91 and routed locally.
    """
    store = MagicMock()
    entity = Entity(
        id="m.py::Widget",
        kind=EntityKind.CLASS,
        name="Widget",
        qualified_name="m.Widget",
        location=Location("m.py", 1, 20),
        signature="class Widget",
        docstring="A widget that does widget things, at some length.",
        source_code=None,
    )
    other = Entity(
        id="m.py::Other",
        kind=EntityKind.CLASS,
        name="Other",
        qualified_name="m.Other",
        location=Location("m.py", 30, 40),
    )
    store.get_entity.return_value = entity
    store.get_parent.return_value = other
    store.get_children.return_value = [other]
    store.get_callers.return_value = []
    store.get_callees.return_value = []

    bundle = ContextSynthesizer(store, max_tokens=2000).synthesize_with_task(
        entity.id, TaskType.EXTEND
    )

    assert bundle is not None
    assert "## Source Code" not in bundle.context_text
    assert bundle.sufficiency_score < ROUTING_GATE
