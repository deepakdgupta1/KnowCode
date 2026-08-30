#!/usr/bin/env python3
"""Project the corrected chunk corpus and measure what it costs.

``measure_storage.py`` reports where the bytes are. ``storage_simulate.py``
measures encoding and derived-data candidates against the corpus as it stands.
Neither can answer what happens when the *shape of the corpus itself* changes,
because that requires re-chunking.

This script closes that gap. It reconstructs the corpus the corrected chunker
would emit — described in ``docs/research/storage_optimization_2026_v4.md``
§6 — writes it into a throwaway copy of ``chunks.db``, and measures the file:

1. **Class shells.** ``Chunker._chunk_entity`` chunks a class and each of its
   methods, so method bodies are stored twice. The projection keeps a class
   chunk only up to its first member definition.
2. **Prose through :class:`ProseChunker`.** Markdown and reStructuredText go
   through the structure-aware chunker (already implemented and tested, but
   not wired into the indexer) instead of ``_extract_module_header``, which
   truncates a document at the first line beginning with ``import ``,
   ``from ``, ``class ``, or ``def ``.
3. **No heading-only or key-only micro-chunks.** ``section`` and
   ``config_key`` chunks carry a label with no body; the corrected chunker
   folds them into the unit that holds their content.

Retained chunks carry their published ``metadata_json`` through unchanged;
``tokens_text`` is recomputed with the real ``tokenize_code``. Embeddings are
placeholder BLOBs of the configured width -- SQLite does not compress BLOBs,
so the measured file size is representative even though the vector values are
not real.

Nothing under ``--index`` is modified.

Usage:
    python scripts/chunking_projection.py [--index PATH] [--generation ID]
                                          [--repo-root PATH] [--json]
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from measure_storage import resolve_generation  # noqa: E402
from storage_simulate import (  # noqa: E402
    CONTENTLESS_FTS,
    DROP_TRIGGERS,
    PRUNE_MIN_CONTENT_BYTES,
    _has_column,
    _slim_metadata,
    _strip_prefix,
)

from knowcode.indexing.prose_chunker import ProseChunker  # noqa: E402
from knowcode.utils.tokenizer import tokenize_code  # noqa: E402

PROSE_SUFFIXES = (".md", ".rst")

# Chunk kinds that carry a label with no body of their own.
LABEL_ONLY_KINDS = ("section", "config_key")

# First member definition inside a class body, across the supported languages.
MEMBER_DEF = re.compile(
    r"\n[ \t]+(?:@|async def |def |fn |pub fn |public |private |protected )"
)

# Minimum chunk content below which a full-width vector costs far more than
# the text it describes. Applied uniformly to code and prose: one corpus,
# one budget (v4 §2, DR-2).
POLICY_THRESHOLDS = (0, 100, 250, 400)
SKIP_KINDS = ("imports",)


@dataclass
class Chunk:
    """One projected chunk."""

    chunk_id: str
    entity_id: str
    file_path: str
    content: str
    kind: str
    metadata_json: str = "{}"


@dataclass
class Corpus:
    """A chunk corpus, measured."""

    label: str
    chunks: list[Chunk] = field(default_factory=list)

    @property
    def count(self) -> int:
        """Number of chunks."""
        return len(self.chunks)

    @property
    def text_bytes(self) -> int:
        """Total content bytes across all chunks."""
        return sum(len(c.content) for c in self.chunks)

    def vector_bytes(self, width: int) -> int:
        """Bytes the vector plane costs at ``width`` bytes per vector."""
        return self.count * width

    def by_kind(self) -> dict[str, tuple[int, int]]:
        """Chunk count and content bytes per kind."""
        counts: collections.Counter[str] = collections.Counter()
        sizes: collections.Counter[str] = collections.Counter()
        for c in self.chunks:
            counts[c.kind] += 1
            sizes[c.kind] += len(c.content)
        return {k: (counts[k], sizes[k]) for k in counts}


def _is_prose(file_path: str) -> bool:
    """Whether ``file_path`` is a prose document."""
    return file_path.endswith(PROSE_SUFFIXES)


def read_corpus(chunks_db: Path) -> Corpus:
    """Read the published corpus out of ``chunks_db``."""
    con = sqlite3.connect(f"file:{chunks_db}?mode=ro&immutable=1", uri=True)
    try:
        rows = con.execute(
            "SELECT chunk_id, entity_id, file_path, content, metadata_json FROM chunks"
        ).fetchall()
    finally:
        con.close()

    corpus = Corpus("published")
    for chunk_id, entity_id, file_path, content, metadata_json in rows:
        try:
            metadata = json.loads(metadata_json or "{}")
        except json.JSONDecodeError:
            metadata = {}
        kind = metadata.get("kind") or metadata.get("type") or "(untyped)"
        corpus.chunks.append(
            Chunk(
                chunk_id,
                entity_id,
                file_path,
                content or "",
                str(kind),
                metadata_json or "{}",
            )
        )
    return corpus


def _class_shell(text: str) -> str:
    """Return a class chunk trimmed to its shell, dropping member bodies."""
    match = MEMBER_DEF.search(text)
    return (text[: match.start()] if match else text).strip()


def project(published: Corpus, repo_root: Path) -> Corpus:
    """Return the corpus the corrected chunker would emit.

    Args:
        published: Corpus read out of the published generation.
        repo_root: Repository root, used to resolve prose files from disk.

    Returns:
        The projected corpus.
    """
    projected = Corpus("corrected chunker")

    # Class entities are re-assembled from their sliding windows first, so a
    # class split across several chunks yields one shell rather than several.
    class_text: dict[str, str] = collections.defaultdict(str)
    class_file: dict[str, str] = {}
    class_metadata: dict[str, str] = {}
    for chunk in published.chunks:
        if _is_prose(chunk.file_path):
            continue
        if chunk.kind == "class":
            class_text[chunk.entity_id] += chunk.content
            class_file[chunk.entity_id] = chunk.file_path
            class_metadata[chunk.entity_id] = chunk.metadata_json
        elif chunk.kind in LABEL_ONLY_KINDS:
            continue
        else:
            projected.chunks.append(chunk)

    for entity_id, text in class_text.items():
        shell = _class_shell(text)
        if shell:
            projected.chunks.append(
                Chunk(
                    f"{entity_id}::0",
                    entity_id,
                    class_file[entity_id],
                    shell,
                    "class",
                    class_metadata[entity_id],
                )
            )

    prose_files = sorted(
        {c.file_path for c in published.chunks if _is_prose(c.file_path)}
    )
    chunker = ProseChunker()
    for file_path in prose_files:
        path = Path(file_path)
        if not path.is_absolute():
            path = repo_root / path
        if not path.is_file():
            continue
        for prose in chunker.chunk_file(path):
            projected.chunks.append(
                Chunk(
                    prose.id,
                    prose.section_id,
                    file_path,
                    prose.content,
                    "prose",
                    json.dumps(
                        {
                            "type": "prose",
                            "content_hash": prose.content_hash,
                            "parent_id": prose.parent_id,
                            "context_header": prose.context_header,
                        },
                        separators=(",", ":"),
                    ),
                )
            )
    return projected


def prose_coverage(corpus: Corpus, repo_root: Path) -> tuple[int, int]:
    """Return (indexed prose bytes, prose bytes on disk)."""
    indexed = 0
    files: set[str] = set()
    for chunk in corpus.chunks:
        if _is_prose(chunk.file_path):
            indexed += len(chunk.content)
            files.add(chunk.file_path)
    on_disk = 0
    for file_path in files:
        path = Path(file_path)
        if not path.is_absolute():
            path = repo_root / path
        if path.is_file():
            on_disk += path.stat().st_size
    return indexed, on_disk


def write_projected(source: Path, target: Path, corpus: Corpus, width: int) -> int:
    """Write ``corpus`` into a copy of ``source`` and return its size.

    Args:
        source: Published ``chunks.db`` to copy. Never modified.
        target: Destination path for the disposable copy.
        corpus: Projected corpus to write.
        width: Embedding width in bytes, used for placeholder BLOBs.

    Returns:
        Size of the rewritten database after ``VACUUM``.
    """
    shutil.copy(source, target)
    con = sqlite3.connect(target)
    try:
        con.executescript(
            "DROP TRIGGER IF EXISTS chunks_ai;"
            "DROP TRIGGER IF EXISTS chunks_ad;"
            "DROP TRIGGER IF EXISTS chunks_au;"
        )
        con.execute("DELETE FROM chunks")
        blob = bytes(width)
        if _has_column(con, "chunks", "tokens_text"):
            con.executemany(
                "INSERT INTO chunks(chunk_id, entity_id, content, tokens_text, "
                "metadata_json, file_path, embedding, embedding_dim) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        c.chunk_id,
                        c.entity_id,
                        c.content,
                        " ".join(tokenize_code(c.content)),
                        c.metadata_json,
                        c.file_path,
                        blob,
                        width // 4,
                    )
                    for c in corpus.chunks
                ],
            )
            con.execute("DELETE FROM chunks_fts")
            con.execute(
                "INSERT INTO chunks_fts(rowid, tokens_text) "
                "SELECT rowid, tokens_text FROM chunks"
            )
        else:
            # D2 folded the column into a contentless index; the projected
            # rows carry their terms explicitly instead.
            con.executemany(
                "INSERT INTO chunks(chunk_id, entity_id, content, "
                "metadata_json, file_path, embedding, embedding_dim) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        c.chunk_id,
                        c.entity_id,
                        c.content,
                        c.metadata_json,
                        c.file_path,
                        blob,
                        width // 4,
                    )
                    for c in corpus.chunks
                ],
            )
            con.execute("DELETE FROM chunks_fts")
            con.executemany(
                "INSERT INTO chunks_fts(rowid, tokens_text) "
                "SELECT rowid, ? FROM chunks WHERE chunk_id = ?",
                [
                    (" ".join(tokenize_code(c.content)), c.chunk_id)
                    for c in corpus.chunks
                ],
            )
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()
    return target.stat().st_size


def compose(db: Path, prefix: str, *, prune: bool) -> int:
    """Apply the Phase C/D transforms to ``db`` in place and measure it.

    The transforms are imported from ``storage_simulate`` so the composed
    end-state is produced by exactly the code that measures them in isolation:
    repo-relative paths, ``metadata_json`` stripped of keys that duplicate a
    first-class column, and ``tokens_text`` folded into a contentless FTS5
    table. ``entities.metadata_json`` is retained by decision (v4 §3).

    Args:
        db: Chunk database to transform. Modified in place.
        prefix: Repository path prefix to strip from stored ids.
        prune: Whether to apply the embedding-selection threshold as well.

    Returns:
        Size of ``db`` after ``VACUUM``.
    """
    con = sqlite3.connect(db)
    try:
        con.executescript(DROP_TRIGGERS)
        if _has_column(con, "chunks", "tokens_text"):
            con.executescript(CONTENTLESS_FTS)
            con.execute("ALTER TABLE chunks DROP COLUMN tokens_text")
        _strip_prefix(con, "chunks", ("file_path", "chunk_id", "entity_id"), prefix)
        _slim_metadata(con, "chunks")
        if prune:
            con.execute(
                "UPDATE chunks SET embedding = NULL, embedding_dim = NULL "
                "WHERE LENGTH(content) < ? "
                "OR json_extract(metadata_json, '$.type') = 'imports'",
                (PRUNE_MIN_CONTENT_BYTES,),
            )
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()
    return db.stat().st_size


def policy_plane(corpus: Corpus, width: int) -> list[tuple[int, int, int]]:
    """Vectors surviving each minimum-content threshold.

    Returns:
        One ``(threshold, vectors kept, vector bytes)`` row per threshold.
    """
    rows = []
    for threshold in POLICY_THRESHOLDS:
        kept = [
            c
            for c in corpus.chunks
            if len(c.content) >= threshold and c.kind not in SKIP_KINDS
        ]
        rows.append((threshold, len(kept), len(kept) * width))
    return rows


def vector_width(generation: Path) -> int:
    """Bytes per vector, read from the published chunk rows."""
    con = sqlite3.connect(
        f"file:{generation / 'chunks.db'}?mode=ro&immutable=1", uri=True
    )
    try:
        row = con.execute(
            "SELECT LENGTH(embedding) FROM chunks WHERE embedding IS NOT NULL LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    return int(row[0]) if row and row[0] else 4096


def _mb(value: float) -> str:
    """Format bytes as right-aligned megabytes."""
    return f"{value / (1024 * 1024):8.2f} MB"


def _kb(value: float) -> str:
    """Format bytes as right-aligned kilobytes."""
    return f"{value / 1024:9.1f} KB"


def render(
    generation: Path,
    published: Corpus,
    projected: Corpus,
    width: int,
    sizes: dict[str, int],
    repo_root: Path,
) -> None:
    """Print the projection as a human-readable report."""
    rule = "=" * 88
    print(rule)
    print(f"Chunk corpus projection - generation {generation.name}")
    print(rule)

    print(
        f"\n{'':26}{'chunks':>8}{'content':>12}{'vector plane':>15}"
        f"{'amplification':>15}"
    )
    for corpus in (published, projected):
        amp = corpus.vector_bytes(width) / max(corpus.text_bytes, 1)
        print(
            f"{corpus.label:<26}{corpus.count:>8}{_kb(corpus.text_bytes):>12}"
            f"{_mb(corpus.vector_bytes(width)):>15}{amp:>14.1f}x"
        )

    pub_indexed, pub_disk = prose_coverage(published, repo_root)
    proj_indexed, proj_disk = prose_coverage(projected, repo_root)
    print("\nprose coverage (share of document bytes reachable by retrieval)")
    print(
        f"  published        {_kb(pub_indexed)} of {_kb(pub_disk)}"
        f"  ({pub_indexed / max(pub_disk, 1) * 100:3.0f}%)"
    )
    print(
        f"  corrected        {_kb(proj_indexed)} of {_kb(proj_disk)}"
        f"  ({proj_indexed / max(proj_disk, 1) * 100:3.0f}%)"
    )

    print("\ncorrected corpus by kind")
    for kind, (count, size) in sorted(
        projected.by_kind().items(), key=lambda kv: -kv[1][1]
    ):
        print(
            f"  {kind:<18}{count:>6} chunks {_kb(size)} "
            f"avg {size // max(count, 1):>5} B"
        )

    print("\nchunks.db measured against the projected corpus")
    print(f"  published                     {_mb(sizes['published'])}")
    for label, caption in (
        ("projected", "corrected chunker"),
        ("composed", "  + lossless + derived"),
        ("composed_pruned", "  + embedding policy"),
    ):
        print(
            f"  {caption:<30}{_mb(sizes[label])}"
            f"   saved {_mb(sizes['published'] - sizes[label])}"
        )

    print(
        "\nembedding-selection policy on the corrected corpus "
        "(one budget, code + prose)"
    )
    for threshold, kept, vector_bytes in policy_plane(projected, width):
        label = "no threshold" if threshold == 0 else f">= {threshold} B"
        print(f"  {label:<18}{kept:>6} vectors {_mb(vector_bytes)}")


def main() -> None:
    """Project the corrected chunk corpus and report what it costs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=Path("knowcode_index"))
    parser.add_argument("--generation", type=str, default=None)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--keep", action="store_true", help="keep the projected chunks.db copy"
    )
    args = parser.parse_args()

    generation = resolve_generation(args.index.resolve(), args.generation)
    repo_root = args.repo_root.resolve()
    width = vector_width(generation)

    published = read_corpus(generation / "chunks.db")
    projected = project(published, repo_root)

    workdir = Path(tempfile.mkdtemp(prefix="knowcode-chunk-projection-"))
    try:
        sizes = {
            "published": (generation / "chunks.db").stat().st_size,
            "projected": write_projected(
                generation / "chunks.db",
                workdir / "projected-chunks.db",
                projected,
                width,
            ),
        }
        prefix = f"{repo_root}/"
        for label, prune in (("composed", False), ("composed_pruned", True)):
            copy = workdir / f"{label}-chunks.db"
            write_projected(generation / "chunks.db", copy, projected, width)
            sizes[label] = compose(copy, prefix, prune=prune)
        if args.json:
            print(
                json.dumps(
                    {
                        "generation": generation.name,
                        "vector_width_bytes": width,
                        "published": {
                            "chunks": published.count,
                            "content_bytes": published.text_bytes,
                            "vector_bytes": published.vector_bytes(width),
                        },
                        "corrected": {
                            "chunks": projected.count,
                            "content_bytes": projected.text_bytes,
                            "vector_bytes": projected.vector_bytes(width),
                            "by_kind": projected.by_kind(),
                        },
                        "chunks_db_bytes": sizes,
                        "policy": [
                            {"min_content_bytes": t, "vectors": k, "vector_bytes": b}
                            for t, k, b in policy_plane(projected, width)
                        ],
                    },
                    indent=2,
                )
            )
        else:
            render(generation, published, projected, width, sizes, repo_root)
            if args.keep:
                print(f"\nprojected copy kept at {workdir}")
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
