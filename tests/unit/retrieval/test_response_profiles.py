"""Response profiles: summary-first, raw source only when asked for (P3-2).

``docs/mcp-contract.md`` defines the contract under "The source-hungry task
types". This module turns that section into one definition every retrieval
consumer answers to, so the rule cannot drift between the query path and the
context path.
"""

from __future__ import annotations

import pytest

from knowcode.retrieval.response_profiles import (
    SOURCE_HUNGRY_TASK_TYPES,
    include_source,
    summarize_context,
)


def test_minimal_omits_source_for_exploratory_work() -> None:
    """The default profile carries a summary, not the code."""
    for task in ("explain", "extend", "locate", "general"):
        assert not include_source("minimal", task), task
        assert summarize_context("minimal", task), task


@pytest.mark.parametrize("task", ["debug", "review"])
def test_minimal_keeps_source_for_tasks_that_need_the_code(task: str) -> None:
    """Debugging and reviewing read source; a summary cannot stand in."""
    assert include_source("minimal", task)
    assert not summarize_context("minimal", task)


def test_standard_and_above_always_include_source() -> None:
    """Escalating verbosity is the explicit source request."""
    for verbosity in ("standard", "verbose", "diagnostic"):
        for task in ("general", "debug", "review", "explain"):
            assert include_source(verbosity, task), (verbosity, task)


def test_the_source_hungry_set_is_exactly_what_the_contract_names() -> None:
    """Adding a task to this set takes source away from every default
    response of that type, so the membership is pinned, not open-ended."""
    assert SOURCE_HUNGRY_TASK_TYPES == frozenset({"debug", "review"})


def test_an_unresolved_task_is_exploratory() -> None:
    """An empty or unrecognized task type must fail toward the summary,
    which is the cheaper and safer default."""
    assert summarize_context("minimal", "")
    assert summarize_context("minimal", "not-a-task")
