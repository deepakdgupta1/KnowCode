"""Response profiles for retrieval: summary-first, source on demand (P3-2).

One definition for how much of an entity a retrieval response carries, so the
rule cannot drift between the query path and the context path. The roadmap
states the contract: profiles are "summary-first for exploratory work and
expose raw source only for explicit source requests or task types that need
it, such as debugging and review."

The profile table, per ``knowcode_retrieve`` action:

====================  ============  =========================================
Action                Default       Raw source
====================  ============  =========================================
query                 summary       at ``verbosity`` ≥ standard, or a
                                    source-hungry task type
context               summary       same rule as ``query``
search                summary       never — names, kinds, locations
trace                 summary       never — graph rows
semantic_search       source        always; it *is* the explicit source
                                    request, documented as a follow-up
====================  ============  =========================================

Escalation rides the ``verbosity`` ladder the ``query`` action already had —
``minimal`` summarizes, ``standard`` and above carry source — rather than a
second enum, because the tool schema is paid on every turn and a profile
parameter would cost tokens to say what an existing one already does.

The task-type floor is a floor, not a default: a debugging or review task
includes source *even at ``minimal``*, because ``minimal`` is the default and
a floor an explicit default could cancel is no floor at all. An agent that
wants less than the floor on a debug task has the ``max_tokens`` budget, which
caps everything the profile includes.
"""

from __future__ import annotations

#: Task types whose work reads the code itself. The roadmap names debugging
#: and review; membership is pinned by test because adding a type takes
#: source away from every default response of that kind.
SOURCE_HUNGRY_TASK_TYPES = frozenset({"debug", "review"})


def include_source(verbosity: str, task_type: str) -> bool:
    """Whether a retrieval response should carry raw source under its profile.

    An unrecognized verbosity or task type fails toward the summary — the
    cheaper default and the one that cannot leak a file's contents by
    accident.
    """
    if task_type in SOURCE_HUNGRY_TASK_TYPES:
        return True
    return verbosity not in ("", "minimal")


def summarize_context(verbosity: str, task_type: str) -> bool:
    """The synthesizer flag: whether the Source Code section is omitted."""
    return not include_source(verbosity, task_type)
