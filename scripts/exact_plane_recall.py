#!/usr/bin/env python3
"""Measure what replacing the exact-match plane would cost, in recall and bytes.

`ExactQueryEngine` is a `LIKE '%pattern%'` scan over `chunks.content`. Phase F
removes that column, so anything that ships F has to say what answers a quoted
query afterwards. This script measures the two candidate answers against the
plane they would replace.

**Recall.** Two query families are drawn from the indexed corpus: fragments cut
out of the middle of an identifier, which is what a substring plane exists for,
and literal runs of a whole line. The `LIKE` answer is ground truth. The term
index is scored unbounded and again at the limit `ExactQueryEngine` actually
passes, because an OR-of-tokens query matches almost everything and the
ordering is what decides whether the true hit is seen.

**Bytes.** An FTS5 `trigram` index answers a substring query exactly, and is
sized here against the column it would replace, contentless and not.

Usage:
    python scripts/exact_plane_recall.py [--index PATH] [--generation ID]
"""

from __future__ import annotations

import argparse
import random
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from knowcode.utils.tokenizer import tokenize_code  # noqa: E402
from measure_storage import resolve_generation  # noqa: E402

SAMPLE_SIZE = 300
ENGINE_LIMIT = 10
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{13,}")


@dataclass(frozen=True)
class Recall:
    """One query family scored against the LIKE plane."""

    family: str
    answerable: int
    unbounded: float
    bounded: float
    empty: int
    true_hits: int
    term_hits: int


def _escape(pattern: str) -> str:
    """Escape a pattern so LIKE reads it literally."""
    return pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_hits(con: sqlite3.Connection, pattern: str) -> set[int]:
    """Rows whose content contains ``pattern`` as literal text."""
    return {
        rowid
        for (rowid,) in con.execute(
            "SELECT rowid FROM chunks WHERE content LIKE ? ESCAPE '\\'",
            (f"%{_escape(pattern)}%",),
        )
    }


def _term_hits(con: sqlite3.Connection, query: str, limit: int | None) -> set[int]:
    """Rows the shipped term index returns for ``query``, bm25-ordered."""
    tokens = [
        "".join(ch for ch in token if ch.isalnum()) for token in tokenize_code(query)
    ]
    tokens = [token for token in tokens if token]
    if not tokens:
        return set()
    sql = (
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
        "ORDER BY bm25(chunks_fts)"
    )
    args: tuple[object, ...] = (" OR ".join(tokens),)
    if limit is not None:
        sql += " LIMIT ?"
        args += (limit,)
    try:
        return {rowid for (rowid,) in con.execute(sql, args)}
    except sqlite3.OperationalError:
        return set()


def _draw_queries(corpus: list[str], rng: random.Random) -> dict[str, list[str]]:
    """Cut mid-identifier fragments and whole-line literals out of the corpus."""
    identifiers = sorted(
        {
            match
            for chunk in rng.sample(corpus, min(400, len(corpus)))
            for match in IDENTIFIER.findall(chunk)
        }
    )
    fragments = []
    for identifier in rng.sample(identifiers, min(SAMPLE_SIZE, len(identifiers))):
        start = rng.randint(1, 4)
        fragments.append(identifier[start : start + rng.randint(9, 13)])

    lines = []
    for chunk in rng.sample(corpus, min(400, len(corpus))):
        candidates = [line.strip() for line in chunk.split("\n")]
        candidates = [line for line in candidates if 20 < len(line) < 60]
        if candidates:
            lines.append(rng.choice(candidates))
    return {
        "mid-identifier fragment": fragments,
        "literal line run": rng.sample(lines, min(SAMPLE_SIZE, len(lines))),
    }


def score(con: sqlite3.Connection, family: str, queries: list[str]) -> Recall:
    """Score one query family against the LIKE plane as ground truth."""
    unbounded, bounded = [], []
    answerable = empty = true_hits = term_hits = 0
    for query in queries:
        truth = _like_hits(con, query)
        if not truth:
            continue
        answerable += 1
        every = _term_hits(con, query, None)
        top = _term_hits(con, query, ENGINE_LIMIT)
        true_hits += len(truth)
        term_hits += len(every)
        unbounded.append(len(truth & every) / len(truth))
        bounded.append(len(truth & top) / min(ENGINE_LIMIT, len(truth)))
        empty += not every & truth
    if not answerable:
        raise SystemExit(f"no {family} query matched anything; the sample is broken")
    return Recall(
        family,
        answerable,
        sum(unbounded) / answerable,
        sum(bounded) / answerable,
        empty,
        true_hits,
        term_hits,
    )


def size_planes(rows: list[tuple[int, str]], workdir: Path) -> None:
    """Size a trigram index against the column it would replace."""
    workdir.mkdir(parents=True, exist_ok=True)
    text = sum(len(content.encode()) for _, content in rows)
    print(f"\nreplacement plane cost, over {text:,} bytes of chunk text")
    planes = (
        ("column (today)", "CREATE TABLE t(rowid INTEGER PRIMARY KEY, content TEXT)"),
        ("trigram", "CREATE VIRTUAL TABLE t USING fts5(content, tokenize='trigram')"),
        (
            "trigram, contentless",
            "CREATE VIRTUAL TABLE t USING fts5(content, tokenize='trigram', content='')",
        ),
    )
    for label, ddl in planes:
        path = workdir / f"plane-{label.split()[0]}-{len(label)}.db"
        path.unlink(missing_ok=True)
        con = sqlite3.connect(path)
        try:
            con.execute(ddl)
        except sqlite3.OperationalError as exc:
            print(f"  {label:<24}unavailable: {exc}")
            continue
        with con:
            con.executemany("INSERT INTO t(rowid, content) VALUES (?, ?)", rows)
        con.execute("VACUUM")
        con.close()
        size = path.stat().st_size
        print(f"  {label:<24}{size / 2**20:>8.2f} MB{size / text:>8.2f}x text")


def main() -> None:
    """Score the term index against the LIKE plane and size the alternatives."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=Path("knowcode_index"))
    parser.add_argument("--generation", type=str, default=None)
    parser.add_argument("--seed", type=int, default=20260830)
    parser.add_argument("--workdir", type=Path, default=Path("."))
    args = parser.parse_args()

    generation = resolve_generation(args.index, args.generation)
    con = sqlite3.connect(f"file:{generation / 'chunks.db'}?mode=ro", uri=True)
    rows = con.execute("SELECT rowid, content FROM chunks").fetchall()
    if not rows:
        raise SystemExit(f"{generation} holds no chunks")

    print(f"generation {generation.name}, {len(rows)} chunks")
    header = f"{'query family':<26}{'n':>4}{'unbounded':>12}{f'at {ENGINE_LIMIT}':>10}"
    print(f"\n{header}{'empty':>8}{'true':>8}{'term':>10}")
    for family, queries in _draw_queries(
        [content for _, content in rows], random.Random(args.seed)
    ).items():
        r = score(con, family, queries)
        print(
            f"{r.family:<26}{r.answerable:>4}{r.unbounded:>11.1%}"
            f"{r.bounded:>10.1%}{r.empty:>8}{r.true_hits:>8}{r.term_hits:>10}"
        )
    con.close()
    size_planes(rows, args.workdir)


if __name__ == "__main__":
    main()
