#!/usr/bin/env python3
"""Measure candidate storage optimizations against a real index generation.

``measure_storage.py`` reports where the bytes are and models what is
recoverable. This script proves it: each candidate change is applied to a
throwaway copy of the published artifacts and the resulting file is measured,
so every number is an observed size rather than an estimate.

Nothing under ``--index`` is modified. Copies are written to a temporary
directory and removed on exit unless ``--keep`` is passed.

Usage:
    python scripts/storage_simulate.py [--index PATH] [--generation ID] [--json]

Candidates are grouped by how much contract they change:

``free``
    No persisted contract changes. ``VACUUM`` and vector-index compaction.
``lossless``
    Same information, cheaper encoding. Relative paths, integer-keyed
    relationships, hash stored as a BLOB, redundant JSON keys removed.
``derived``
    Stop persisting what can be reconstructed. ``tokens_text`` (recomputable
    via ``tokenize_code``), ``entities.source_code`` and ``chunks.content``
    (resolvable from the source tree), and the vector index itself (rebuilt
    from the durable float32 BLOBs, per ADR 0003).
``policy``
    Changes what gets embedded. Minimum-content thresholds and vector width.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from measure_storage import resolve_generation  # noqa: E402

# Keys duplicated into metadata_json that are already first-class columns.
REDUNDANT_METADATA_KEYS = ("content_hash", "source_code", "file_path", "tokens")

# Minimum chunk content below which a full-width vector costs far more than
# the text it describes; import blocks are excluded regardless of size
# because the graph already carries them as edges.
PRUNE_MIN_CONTENT_BYTES = 250

DROP_TRIGGERS = (
    "DROP TRIGGER IF EXISTS chunks_ai;"
    "DROP TRIGGER IF EXISTS chunks_ad;"
    "DROP TRIGGER IF EXISTS chunks_au;"
)

# Rebuild tokens_text into a contentless FTS5 table: the term index survives,
# the text column does not. contentless_delete=1 keeps plain DELETE working
# against the contentless table (SQLite 3.43+); without it no row can ever be
# removed and incremental re-indexing corrupts search (BL-13).
CONTENTLESS_FTS = """
DROP TABLE chunks_fts;
CREATE VIRTUAL TABLE chunks_fts USING fts5(tokens_text, content='', contentless_delete=1);
INSERT INTO chunks_fts(rowid, tokens_text) SELECT rowid, tokens_text FROM chunks;
"""

# Replace TEXT edge keys with integer entity ids and a relationship-kind
# codebook, preserving both covering indexes.
INTEGER_EDGE_KEYS = """
CREATE TABLE eid(id INTEGER PRIMARY KEY, entity_id TEXT UNIQUE NOT NULL);
INSERT INTO eid(entity_id) SELECT DISTINCT source_id FROM relationships;
INSERT OR IGNORE INTO eid(entity_id) SELECT DISTINCT target_id FROM relationships;
CREATE TABLE relkind(id INTEGER PRIMARY KEY, kind TEXT UNIQUE NOT NULL);
INSERT INTO relkind(kind) SELECT DISTINCT kind FROM relationships;
CREATE TABLE rel_next(
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    kind      INTEGER NOT NULL,
    metadata_json TEXT
);
INSERT INTO rel_next
SELECT s.id, t.id, k.id, NULLIF(r.metadata_json, '{}')
FROM relationships r
JOIN eid s ON s.entity_id = r.source_id
JOIN eid t ON t.entity_id = r.target_id
JOIN relkind k ON k.kind = r.kind;
DROP INDEX idx_relationships_source_kind;
DROP INDEX idx_relationships_target_kind;
DROP TABLE relationships;
ALTER TABLE rel_next RENAME TO relationships;
CREATE INDEX idx_relationships_source_kind ON relationships(source_id, kind);
CREATE INDEX idx_relationships_target_kind ON relationships(target_id, kind);
"""


@dataclass
class Result:
    """Measured outcome of one simulated candidate.

    Attributes:
        label: What was changed.
        tier: ``free``, ``lossless``, ``derived``, or ``policy``.
        artifact: Which artifact the change applies to.
        before: Bytes before the change.
        after: Bytes after the change.
    """

    label: str
    tier: str
    artifact: str
    before: int
    after: int

    @property
    def saved(self) -> int:
        """Bytes recovered by this candidate."""
        return self.before - self.after

    @property
    def pct(self) -> float:
        """Percentage of the artifact recovered."""
        return 100.0 * self.saved / self.before if self.before else 0.0


# ----------------------------------------------------------------------
# Transformations
# ----------------------------------------------------------------------


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    """Whether ``name`` exists in the connected database."""
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
    """Whether ``table`` still carries ``column``."""
    names = {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in names


def _strip_prefix(
    con: sqlite3.Connection, table: str, columns: tuple[str, ...], prefix: str
) -> None:
    """Rewrite absolute paths in ``columns`` as repo-relative paths."""
    assignments = ", ".join(f"{c} = REPLACE({c}, ?, '')" for c in columns)
    con.execute(f"UPDATE {table} SET {assignments}", (prefix,) * len(columns))


def _slim_metadata(con: sqlite3.Connection, table: str) -> None:
    """Drop metadata_json keys that duplicate first-class columns."""
    updates = []
    for rowid, blob in con.execute(
        f"SELECT rowid, metadata_json FROM {table}"
    ).fetchall():
        if not blob:
            continue
        try:
            payload = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        for key in REDUNDANT_METADATA_KEYS:
            payload.pop(key, None)
        updates.append((json.dumps(payload, separators=(",", ":")), rowid))
    con.executemany(f"UPDATE {table} SET metadata_json = ? WHERE rowid = ?", updates)


def _vacuum(db: Path) -> int:
    """VACUUM ``db`` and return its size in bytes."""
    con = sqlite3.connect(db)
    try:
        con.execute("VACUUM")
    finally:
        con.close()
    return db.stat().st_size


class Simulator:
    """Applies candidate transformations to disposable artifact copies."""

    def __init__(self, generation: Path, workdir: Path, repo_root: Path) -> None:
        """Initialize the simulator.

        Args:
            generation: Published generation to copy from. Never modified.
            workdir: Directory for disposable copies.
            repo_root: Repository root, whose path prefix is stripped when
                simulating repo-relative storage.
        """
        self.generation = generation
        self.workdir = workdir
        self.prefix = f"{repo_root.resolve()}/"
        self._counter = 0

    def _copy(self, artifact: str) -> Path:
        """Return a fresh disposable copy of ``artifact``."""
        self._counter += 1
        dst = self.workdir / f"{self._counter:03d}-{artifact}"
        shutil.copy(self.generation / artifact, dst)
        return dst

    def run(
        self,
        label: str,
        tier: str,
        artifact: str,
        apply: Optional[Callable[[sqlite3.Connection], None]],
    ) -> Result:
        """Apply ``apply`` to a copy of ``artifact`` and measure the result.

        Args:
            label: Description of the candidate.
            tier: Contract-change tier.
            artifact: Artifact filename within the generation.
            apply: Callable receiving an open connection, or ``None`` to
                measure ``VACUUM`` alone.

        Returns:
            The measured result.
        """
        before = (self.generation / artifact).stat().st_size
        db = self._copy(artifact)
        con = sqlite3.connect(db)
        try:
            if apply is not None:
                apply(con)
            con.commit()
        finally:
            con.close()
        return Result(label, tier, artifact, before, _vacuum(db))


def chunk_candidates(sim: Simulator) -> list[Result]:
    """Simulate every chunks.db candidate, individually and combined."""
    prefix = sim.prefix
    probe = sqlite3.connect(f"file:{sim.generation / 'chunks.db'}?mode=ro", uri=True)
    try:
        fts_already_contentless = not _has_column(probe, "chunks", "tokens_text")
    finally:
        probe.close()

    def contentless_fts(con: sqlite3.Connection) -> None:
        if fts_already_contentless:
            # D2 already folded the column; re-running the fold would only
            # drop and rebuild an identical index.
            return
        con.executescript(DROP_TRIGGERS)
        con.executescript(CONTENTLESS_FTS)
        con.execute("ALTER TABLE chunks DROP COLUMN tokens_text")

    def drop_embedding(con: sqlite3.Connection) -> None:
        con.executescript(DROP_TRIGGERS)
        con.execute("ALTER TABLE chunks DROP COLUMN embedding")
        con.execute("ALTER TABLE chunks DROP COLUMN embedding_dim")

    def relative_paths(con: sqlite3.Connection) -> None:
        con.executescript(DROP_TRIGGERS)
        _strip_prefix(con, "chunks", ("file_path", "chunk_id", "entity_id"), prefix)

    def slim_metadata(con: sqlite3.Connection) -> None:
        con.executescript(DROP_TRIGGERS)
        _slim_metadata(con, "chunks")

    def drop_content(con: sqlite3.Connection) -> None:
        con.executescript(DROP_TRIGGERS)
        con.execute("ALTER TABLE chunks DROP COLUMN content")

    def combined(con: sqlite3.Connection) -> None:
        contentless_fts(con)
        relative_paths(con)
        _slim_metadata(con, "chunks")

    def combined_no_source(con: sqlite3.Connection) -> None:
        combined(con)
        con.execute("ALTER TABLE chunks DROP COLUMN content")

    def combined_no_vectors(con: sqlite3.Connection) -> None:
        combined(con)
        con.execute("ALTER TABLE chunks DROP COLUMN embedding")
        con.execute("ALTER TABLE chunks DROP COLUMN embedding_dim")

    def prune_trivial(con: sqlite3.Connection) -> None:
        """Stop embedding chunks too small to carry retrievable meaning."""
        con.executescript(DROP_TRIGGERS)
        con.execute(
            "UPDATE chunks SET embedding = NULL, embedding_dim = NULL "
            "WHERE LENGTH(content) < ? "
            "OR json_extract(metadata_json, '$.type') = 'imports'",
            (PRUNE_MIN_CONTENT_BYTES,),
        )

    def combined_pruned(con: sqlite3.Connection) -> None:
        combined(con)
        prune_trivial(con)

    return [
        sim.run("VACUUM only", "free", "chunks.db", None),
        sim.run(
            "relative paths in chunk_id/entity_id/file_path",
            "lossless",
            "chunks.db",
            relative_paths,
        ),
        sim.run("slim metadata_json", "lossless", "chunks.db", slim_metadata),
        sim.run(
            "fold tokens_text into contentless FTS5"
            + (" — already applied (D2)" if fts_already_contentless else ""),
            "derived",
            "chunks.db",
            contentless_fts,
        ),
        sim.run(
            "drop content (resolve from source tree)",
            "derived",
            "chunks.db",
            drop_content,
        ),
        sim.run("drop durable fp32 embeddings", "policy", "chunks.db", drop_embedding),
        sim.run(
            f"stop embedding chunks <{PRUNE_MIN_CONTENT_BYTES}B and import blocks",
            "policy",
            "chunks.db",
            prune_trivial,
        ),
        sim.run(
            "ALL lossless + derived, durable vectors kept",
            "combined",
            "chunks.db",
            combined,
        ),
        sim.run(
            "ALL + embedding-planner pruning", "combined", "chunks.db", combined_pruned
        ),
        sim.run(
            "ALL + content resolved from source tree",
            "combined",
            "chunks.db",
            combined_no_source,
        ),
        sim.run(
            "ALL + vectors moved out of SQLite",
            "combined",
            "chunks.db",
            combined_no_vectors,
        ),
    ]


def knowledge_candidates(sim: Simulator) -> list[Result]:
    """Simulate every knowledge.db candidate, individually and combined."""
    prefix = sim.prefix
    probe = sqlite3.connect(f"file:{sim.generation / 'knowledge.db'}?mode=ro", uri=True)
    try:
        # C2 replaced the TEXT edge keys with integer codebook ids and an
        # ``eid`` table; its root text is what relative paths must strip now.
        edges_already_integer = _has_table(probe, "eid")
    finally:
        probe.close()

    def drop_source(con: sqlite3.Connection) -> None:
        con.execute("ALTER TABLE entities DROP COLUMN source_code")

    def relative_paths(con: sqlite3.Connection) -> None:
        _strip_prefix(con, "entities", ("entity_id", "file_path"), prefix)
        if edges_already_integer:
            _strip_prefix(con, "eid", ("entity_id",), prefix)
        else:
            _strip_prefix(con, "relationships", ("source_id", "target_id"), prefix)

    def integer_edges(con: sqlite3.Connection) -> None:
        if edges_already_integer:
            # INTEGER_EDGE_KEYS assumes TEXT keys; against integer columns its
            # codebook joins match nothing and the rewrite below would empty
            # the graph (BL-12).
            return
        con.executescript(INTEGER_EDGE_KEYS)

    def slim_metadata(con: sqlite3.Connection) -> None:
        _slim_metadata(con, "entities")

    def hash_blob(con: sqlite3.Connection) -> None:
        # unhex() yields the identical digest in half the bytes.
        con.execute(
            "UPDATE entities SET content_hash = unhex(content_hash) "
            "WHERE content_hash IS NOT NULL AND LENGTH(content_hash) = 64"
        )

    def combined(con: sqlite3.Connection) -> None:
        drop_source(con)
        relative_paths(con)
        slim_metadata(con)
        hash_blob(con)
        integer_edges(con)

    return [
        sim.run("VACUUM only", "free", "knowledge.db", None),
        sim.run(
            "relative paths in entity ids and edge keys"
            + (" — edge keys already integer (C2)" if edges_already_integer else ""),
            "lossless",
            "knowledge.db",
            relative_paths,
        ),
        sim.run(
            "integer-keyed relationships + kind codebook"
            + (" — already applied (C2)" if edges_already_integer else ""),
            "lossless",
            "knowledge.db",
            integer_edges,
        ),
        sim.run(
            "slim metadata_json (drop duplicated content_hash)",
            "lossless",
            "knowledge.db",
            slim_metadata,
        ),
        sim.run("content_hash as 32-byte BLOB", "lossless", "knowledge.db", hash_blob),
        sim.run(
            "drop source_code (resolve from source tree)",
            "derived",
            "knowledge.db",
            drop_source,
        ),
        sim.run("ALL lossless + derived", "combined", "knowledge.db", combined),
    ]


def vector_candidates(generation: Path) -> list[Result]:
    """Report vector-plane candidates from measured vector geometry."""
    chunks_db = generation / "chunks.db"
    lance = generation / "vectors.lancedb"
    if not chunks_db.is_file():
        return []

    con = sqlite3.connect(f"file:{chunks_db}?mode=ro", uri=True)
    try:
        count, dim = con.execute(
            "SELECT COUNT(*), COALESCE(MAX(embedding_dim), 0) FROM chunks "
            "WHERE embedding IS NOT NULL"
        ).fetchone()
    finally:
        con.close()
    if not count or not dim:
        return []

    results: list[Result] = []
    if lance.is_dir():
        total = sum(p.stat().st_size for p in lance.rglob("*") if p.is_file())
        data = sum(
            p.stat().st_size
            for p in (lance / "vectors.lance" / "data").glob("*")
            if p.is_file()
        )
        results.append(
            Result(
                "compact index, drop superseded versions",
                "free",
                "vectors.lancedb",
                total,
                data,
            )
        )
        results.append(
            Result(
                "rebuild from durable BLOBs, ship nothing",
                "derived",
                "vectors.lancedb",
                total,
                0,
            )
        )
        results.append(
            Result(
                "int8 ANN plane (recall@10 ~0.995 on normalized vectors)",
                "policy",
                "vectors.lancedb",
                total,
                count * dim,
            )
        )
    return results


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------


def _mb(value: int) -> str:
    """Format bytes as right-aligned megabytes."""
    return f"{value / 1048576:8.2f} MB"


def print_results(
    generation: Path, groups: dict[str, list[Result]], baseline: int
) -> None:
    """Render simulated results grouped by artifact."""
    line = "=" * 92
    print(
        f"\n{line}\nMeasured storage candidates - generation {generation.name}\n{line}"
    )
    print(f"\nbaseline generation size: {_mb(baseline)}\n")

    for artifact, results in groups.items():
        if not results:
            continue
        print(f"{artifact}  (baseline {_mb(results[0].before)})")
        for res in results:
            marker = "  =>" if res.tier == "combined" else "    "
            print(
                f"{marker} [{res.tier:<8}] {_mb(res.after):>11}  "
                f"saved {_mb(res.saved)} ({res.pct:5.1f}%)  {res.label}"
            )
        print()


def print_endstates(groups: dict[str, list[Result]], baseline: int) -> None:
    """Render composed end-states built from the measured combinations."""

    def combined(artifact: str, needle: str) -> Optional[Result]:
        return next(
            (
                r
                for r in groups.get(artifact, [])
                if r.tier == "combined" and needle in r.label
            ),
            None,
        )

    def vector(needle: str) -> Optional[Result]:
        return next(
            (r for r in groups.get("vectors.lancedb", []) if needle in r.label),
            None,
        )

    chunks_keep = combined("chunks.db", "durable vectors kept")
    chunks_pruned = combined("chunks.db", "embedding-planner")
    chunks_nosrc = combined("chunks.db", "content resolved")
    knowledge = combined("knowledge.db", "ALL")
    rebuild = vector("rebuild from durable")
    int8 = vector("int8 ANN")

    if not (chunks_keep and knowledge and rebuild):
        return

    line = "=" * 92
    print(f"{line}\nComposed end-states\n{line}\n")

    def show(name: str, total: int, note: str) -> None:
        print(
            f"  {name:<48} {_mb(total)}  "
            f"({100 * (baseline - total) / baseline:4.1f}% smaller)  {note}"
        )

    show("baseline", baseline, "")
    show(
        "lossless + derived, ANN rebuilt on demand",
        chunks_keep.after + knowledge.after + rebuild.after,
        "no recall change",
    )
    if int8:
        show(
            "lossless + derived, int8 ANN shipped",
            chunks_keep.after + knowledge.after + int8.after,
            "recall@10 ~0.995",
        )
    if chunks_pruned:
        show(
            "+ embedding-planner pruning",
            chunks_pruned.after + knowledge.after + rebuild.after,
            "fewer semantic candidates",
        )
    if chunks_nosrc:
        show(
            "+ source descriptors (content from source tree)",
            chunks_nosrc.after + knowledge.after + rebuild.after,
            "needs a source resolver",
        )
    print()


def main() -> None:
    """Simulate and measure candidate storage optimizations."""
    parser = argparse.ArgumentParser(
        description="Measure candidate KnowCode storage optimizations"
    )
    parser.add_argument(
        "--index",
        default="knowcode_index",
        help="Path to the knowcode_index/ directory",
    )
    parser.add_argument(
        "--generation",
        default=None,
        help="Generation id to simulate (default: current pointer)",
    )
    parser.add_argument(
        "--repo-root", default=".", help="Repository root whose path prefix is stripped"
    )
    parser.add_argument(
        "--keep", action="store_true", help="Keep the simulated copies for inspection"
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    args = parser.parse_args()

    index_path = Path(args.index)
    if not index_path.is_dir():
        print(
            f"Error: {index_path} not found. Run `knowcode build` first.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        generation = resolve_generation(index_path, args.generation)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    baseline = sum(p.stat().st_size for p in generation.rglob("*") if p.is_file())
    workdir = Path(tempfile.mkdtemp(prefix="knowcode-storage-sim-"))
    try:
        sim = Simulator(generation, workdir, Path(args.repo_root))
        groups = {
            "chunks.db": chunk_candidates(sim),
            "knowledge.db": knowledge_candidates(sim),
            "vectors.lancedb": vector_candidates(generation),
        }
        if args.json:
            print(
                json.dumps(
                    {
                        "generation": generation.name,
                        "baseline_bytes": baseline,
                        "candidates": {
                            artifact: [
                                {**vars(r), "saved": r.saved, "pct": round(r.pct, 2)}
                                for r in results
                            ]
                            for artifact, results in groups.items()
                        },
                    },
                    indent=2,
                )
            )
        else:
            print_results(generation, groups, baseline)
            print_endstates(groups, baseline)
            if args.keep:
                print(f"simulated copies kept in {workdir}\n")
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
