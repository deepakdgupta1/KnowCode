"""Bracket a staged rewrite with a witness taken before it runs.

A generation manifest describes ``chunks.db`` and ``knowledge.db`` with counts
and id digests that ``read_chunk_ids``, ``read_entity_ids`` and
``count_durable_embeddings`` read out of those files at the end of the build.
Every number in it therefore derives from the artifact it is meant to check, so
a step that drops rows shrinks both and they agree (BL-8). Adding the two
databases to the manifest's byte digests does not help: that digest would be
computed from the damaged file for the same reason the counts are.

The fix is temporal rather than structural. Read the witness from the writer's
own connection before the rewrite and compare after, so the comparison cannot
be satisfied by the damage. Nothing is threaded in from the caller, which is
what makes this hold on every publication path: a full rebuild knows its corpus
total, an incremental build knows only the files it touched, and a watch batch
counts nothing at all.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Callable, Iterator, Mapping

from knowcode.errors import StagedRewriteError

Witness = Callable[[], Mapping[str, str]]


def digest_rows(rows: list[str]) -> str:
    """Digest a row set as a multiset, sorted so physical order does not count.

    Deliberately not :func:`knowcode.indexing.generations.digest_ids`, which
    digests a *set*. That is right for manifest parity, where the ids are
    unique by primary key and membership is the question. It is wrong here:
    ``relationships`` carries no unique constraint, so two identical edges are
    legal, and a set-based digest cannot see one of them go. A rewrite is
    lossless only when the multiset survives.
    """
    hasher = hashlib.sha256()
    for row in sorted(rows):
        hasher.update(row.encode("utf-8"))
        hasher.update(b"\0")
    return f"sha256:{hasher.hexdigest()}"


@contextmanager
def rows_preserved(witness: Witness, artifact: str) -> Iterator[None]:
    """Fail closed when the wrapped rewrite changes what ``witness`` reports.

    ``witness`` returns one digest per row set the rewrite promises to leave
    alone, keyed by a name the error can quote. It is called once before and
    once after, and both calls must materialise their rows: a cursor left open
    across the rewrite reads the wrong side of it.

    Raises:
        StagedRewriteError: A named row set did not survive.
    """
    before = dict(witness())
    yield
    after = dict(witness())

    changed = sorted(
        name
        for name in before.keys() | after.keys()
        if before.get(name) != after.get(name)
    )
    if changed:
        raise StagedRewriteError(artifact, changed)
