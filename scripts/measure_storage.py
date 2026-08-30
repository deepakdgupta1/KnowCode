#!/usr/bin/env python3
"""Measure the storage footprint of a published KnowCode index generation.

Answers the question "where did the bytes go, and which of them are
avoidable?" for the artifacts ``knowcode build`` writes: ``knowledge.db``,
``chunks.db``, and the vector plane (``vectors.lancedb`` / ``vectors.json``).

The report is layered:

1. **Artifacts** - on-disk size of every file in the generation.
2. **Tables** - per-table page usage inside each SQLite database (``dbstat``).
3. **Columns** - per-column payload bytes, which is what isolates derived and
   duplicated data from irreducible payload.
4. **Vector plane** - vector count, width, and the amplification ratio against
   the chunk text each vector describes.

For what each candidate optimization actually recovers, run the companion
``scripts/storage_simulate.py``, which applies every change to a throwaway
copy and measures the result.

Usage:
    python scripts/measure_storage.py [--index PATH] [--generation ID] [--json]

Defaults to the generation currently pointed at by ``knowcode_index/current.json``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import struct
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# Content thresholds projected in the embedding-planner savings table.
THRESHOLD_BANDS = (60, 100, 150, 250, 400)

CHUNK_SIZE_BUCKETS = (
    ("<100B", 0, 100),
    ("100-250B", 100, 250),
    ("250-500B", 250, 500),
    ("500-1000B", 500, 1000),
    (">=1000B", 1000, None),
)


# ----------------------------------------------------------------------
# Generation discovery
# ----------------------------------------------------------------------


def resolve_generation(index_path: Path, generation: Optional[str]) -> Path:
    """Return the generation directory to measure.

    Args:
        index_path: Path to the ``knowcode_index`` directory.
        generation: Explicit generation id, or ``None`` to follow the
            ``current.json`` pointer and fall back to the newest directory.

    Returns:
        Path to a generation directory.

    Raises:
        FileNotFoundError: When no generation directory can be resolved.
    """
    generations = index_path / "generations"
    if generation:
        candidate = generations / generation
        if not candidate.is_dir():
            raise FileNotFoundError(f"No such generation: {candidate}")
        return candidate

    pointer = index_path / "current.json"
    if pointer.is_file():
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        current = data.get("generation") or data.get("generation_id")
        if isinstance(current, str) and (generations / current).is_dir():
            return generations / current

    existing = sorted(p for p in generations.glob("*") if p.is_dir())
    if not existing:
        raise FileNotFoundError(
            f"No generations under {generations}. Run `knowcode build` first."
        )
    return existing[-1]


def _dir_bytes(path: Path) -> int:
    """Total bytes of every file under ``path``, or 0 when absent."""
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


# ----------------------------------------------------------------------
# Measurement records
# ----------------------------------------------------------------------


@dataclass
class Report:
    """Full storage measurement for one generation."""

    generation: str
    artifacts: dict[str, int] = field(default_factory=dict)
    tables: dict[str, int] = field(default_factory=dict)
    columns: dict[str, dict[str, int]] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    vector_plane: dict[str, Any] = field(default_factory=dict)
    chunk_profile: dict[str, Any] = field(default_factory=dict)
    source: dict[str, int] = field(default_factory=dict)

    @property
    def total_bytes(self) -> int:
        """Total on-disk bytes across all artifacts in the generation."""
        return sum(self.artifacts.values())


# ----------------------------------------------------------------------
# SQLite introspection
# ----------------------------------------------------------------------


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a read-only connection that never mutates the measured artifact."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


DBSTAT_QUERY = "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC"


def measure_tables(db_path: Path) -> dict[str, int]:
    """Return per-table (and per-index) page bytes via ``dbstat``.

    ``dbstat`` is a compile-time option. It is present in most ``sqlite3``
    CLI builds but absent from several bundled Python modules (notably the
    macOS system Python), so the CLI is used as a fallback before giving up.

    Args:
        db_path: Path to a SQLite database.

    Returns:
        Mapping of table/index name to bytes, largest first. Empty when
        neither the module nor the CLI exposes ``dbstat``.
    """
    if not db_path.is_file():
        return {}

    con = _connect(db_path)
    try:
        rows = con.execute(DBSTAT_QUERY).fetchall()
        return {name: int(size) for name, size in rows}
    except sqlite3.OperationalError:
        pass
    finally:
        con.close()

    try:
        out = subprocess.run(
            ["sqlite3", "-readonly", str(db_path), DBSTAT_QUERY],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {}

    sizes: dict[str, int] = {}
    for row in out.stdout.splitlines():
        name, _, size = row.rpartition("|")
        if name and size.isdigit():
            sizes[name] = int(size)
    return sizes


def _column_names(con: sqlite3.Connection, table: str) -> list[str]:
    """Return the column names of ``table``."""
    return [row[1] for row in con.execute(f"PRAGMA table_info({table})")]


def measure_columns(db_path: Path, table: str) -> tuple[dict[str, int], int]:
    """Return per-column payload bytes and the row count for ``table``.

    Payload bytes use SQLite's ``length()``, which counts characters for TEXT
    and bytes for BLOB. For ASCII-dominated code identifiers and paths the two
    coincide closely enough to attribute page usage to columns.

    Args:
        db_path: Path to a SQLite database.
        table: Table to profile.

    Returns:
        Tuple of (column name to bytes, row count).
    """
    if not db_path.is_file():
        return {}, 0
    with _connect(db_path) as con:
        try:
            columns = _column_names(con, table)
        except sqlite3.OperationalError:
            return {}, 0
        if not columns:
            return {}, 0
        selects = ", ".join(f"COALESCE(SUM(LENGTH({c})), 0)" for c in columns)
        row = con.execute(f"SELECT COUNT(*), {selects} FROM {table}").fetchone()
    count = int(row[0])
    sizes = {col: int(val) for col, val in zip(columns, row[1:])}
    return dict(sorted(sizes.items(), key=lambda kv: -kv[1])), count


# ----------------------------------------------------------------------
# Vector plane
# ----------------------------------------------------------------------


def measure_vector_plane(generation: Path) -> dict[str, Any]:
    """Profile the vector plane and its duplication across backends.

    Compares the durable float32 embedding BLOBs in ``chunks.db`` (ADR 0003)
    against the derived on-disk vector index, and reports how much of the
    vector directory is bookkeeping rather than vector data.

    Args:
        generation: Path to a generation directory.

    Returns:
        Mapping of vector-plane metrics.
    """
    chunks_db = generation / "chunks.db"
    info: dict[str, Any] = {}
    if not chunks_db.is_file():
        return info

    with _connect(chunks_db) as con:
        row = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(embedding)), 0), "
            "COALESCE(MAX(embedding_dim), 0) FROM chunks "
            "WHERE embedding IS NOT NULL"
        ).fetchone()
        sample = con.execute(
            "SELECT embedding, embedding_dim FROM chunks "
            "WHERE embedding IS NOT NULL LIMIT 1"
        ).fetchone()

    count, durable_bytes, dim = int(row[0]), int(row[1]), int(row[2])
    info["vector_count"] = count
    info["dimension"] = dim
    info["durable_fp32_bytes"] = durable_bytes

    if sample and sample[1]:
        values = struct.unpack(f"<{int(sample[1])}f", sample[0])
        info["sample_l2_norm"] = round(sum(v * v for v in values) ** 0.5, 6)

    lance_dir = generation / "vectors.lancedb" / "vectors.lance"
    if lance_dir.is_dir():
        data = _dir_bytes(lance_dir / "data")
        versions = _dir_bytes(lance_dir / "_versions")
        transactions = _dir_bytes(lance_dir / "_transactions")
        info["derived_index_bytes"] = _dir_bytes(generation / "vectors.lancedb")
        info["derived_data_bytes"] = data
        info["derived_bookkeeping_bytes"] = versions + transactions
        info["derived_fragments"] = sum(
            1 for _ in (lance_dir / "data").glob("*") if _.is_file()
        )
        info["derived_versions"] = sum(
            1 for _ in (lance_dir / "_versions").glob("*") if _.is_file()
        )

    # Width alternatives for the derived ranking index. The durable fp32 record
    # is unaffected; these describe the ANN plane only.
    if count and dim:
        info["width_options_bytes"] = {
            "fp32": count * dim * 4,
            "fp16": count * dim * 2,
            "int8": count * dim,
            "binary": count * dim // 8,
        }
    return info


def measure_chunk_profile(generation: Path) -> dict[str, Any]:
    """Profile chunk text against vector cost to expose amplification.

    A chunk holding 40 bytes of content still carries a full-width vector, so
    the ratio of vector bytes to content bytes identifies which chunk classes
    are uneconomical to embed.

    Args:
        generation: Path to a generation directory.

    Returns:
        Mapping with size buckets, per-type amplification, duplicate counts,
        and projected vector-plane size at each content threshold.
    """
    chunks_db = generation / "chunks.db"
    if not chunks_db.is_file():
        return {}

    with _connect(chunks_db) as con:
        dim = int(
            (
                con.execute(
                    "SELECT COALESCE(MAX(embedding_dim), 0) FROM chunks"
                ).fetchone()
                or (0,)
            )[0]
        )
        vector_bytes = dim * 4 if dim else 0

        buckets = []
        for name, low, high in CHUNK_SIZE_BUCKETS:
            clause = f"LENGTH(content) >= {low}"
            if high is not None:
                clause += f" AND LENGTH(content) < {high}"
            n, content = con.execute(
                f"SELECT COUNT(*), COALESCE(SUM(LENGTH(content)), 0) "
                f"FROM chunks WHERE {clause}"
            ).fetchone()
            buckets.append(
                {
                    "bucket": name,
                    "chunks": int(n),
                    "content_bytes": int(content),
                    "vector_bytes": int(n) * vector_bytes,
                }
            )

        by_type = [
            {
                "type": row[0] or "(untyped)",
                "chunks": int(row[1]),
                "content_bytes": int(row[2]),
                "vector_bytes": int(row[1]) * vector_bytes,
            }
            for row in con.execute(
                "SELECT json_extract(metadata_json, '$.type'), COUNT(*), "
                "COALESCE(SUM(LENGTH(content)), 0) FROM chunks "
                "GROUP BY 1 ORDER BY 2 DESC"
            )
        ]

        redundant = con.execute(
            "SELECT COALESCE(SUM(c - 1), 0) FROM "
            "(SELECT COUNT(*) c FROM chunks GROUP BY content HAVING c > 1)"
        ).fetchone()[0]

        thresholds = []
        for threshold in THRESHOLD_BANDS:
            kept = con.execute(
                "SELECT COUNT(*) FROM chunks WHERE LENGTH(content) >= ?",
                (threshold,),
            ).fetchone()[0]
            thresholds.append(
                {
                    "min_content_bytes": threshold,
                    "chunks_kept": int(kept),
                    "vector_bytes": int(kept) * vector_bytes,
                }
            )

    return {
        "vector_bytes_per_chunk": vector_bytes,
        "size_buckets": buckets,
        "by_type": by_type,
        "redundant_duplicate_vectors": int(redundant),
        "threshold_projection": thresholds,
    }


# ----------------------------------------------------------------------
# Source baseline
# ----------------------------------------------------------------------


def measure_source(repo_root: Path) -> dict[str, int]:
    """Measure the indexed source tree, for artifact amplification ratios.

    Args:
        repo_root: Repository root to inspect via ``git ls-files``.

    Returns:
        Mapping with tracked-file and Python-only byte totals. Values are 0
        when ``repo_root`` is not a git repository.
    """

    def _bytes(patterns: list[str]) -> int:
        try:
            out = subprocess.run(
                ["git", "-C", str(repo_root), "ls-files", "-z", *patterns],
                capture_output=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return 0
        total = 0
        for name in out.stdout.split(b"\0"):
            if not name:
                continue
            path = repo_root / name.decode("utf-8", "replace")
            if path.is_file():
                total += path.stat().st_size
        return total

    return {"tracked_bytes": _bytes([]), "python_bytes": _bytes(["*.py"])}


# ----------------------------------------------------------------------
# Orchestration and rendering
# ----------------------------------------------------------------------


def build_report(index_path: Path, generation: Path, repo_root: Path) -> Report:
    """Measure every layer of one generation.

    Args:
        index_path: Path to the ``knowcode_index`` directory.
        generation: Generation directory to measure.
        repo_root: Repository root for source-amplification ratios.

    Returns:
        A populated report.
    """
    report = Report(generation=generation.name)

    for child in sorted(generation.iterdir()):
        report.artifacts[child.name] = _dir_bytes(child)
    pointer = index_path / "current.json"
    if pointer.is_file():
        report.artifacts["../current.json"] = pointer.stat().st_size

    knowledge_db = generation / "knowledge.db"
    chunks_db = generation / "chunks.db"
    report.tables = {
        **measure_tables(knowledge_db),
        **measure_tables(chunks_db),
    }
    for db, table in (
        (knowledge_db, "entities"),
        (knowledge_db, "relationships"),
        (chunks_db, "chunks"),
    ):
        columns, count = measure_columns(db, table)
        if columns:
            report.columns[table] = columns
            report.counts[table] = count

    report.vector_plane = measure_vector_plane(generation)
    report.chunk_profile = measure_chunk_profile(generation)
    report.source = measure_source(repo_root)
    return report


def _mb(value: int) -> str:
    """Format bytes as right-aligned megabytes."""
    return f"{value / 1048576:8.2f} MB"


def print_report(report: Report) -> None:
    """Render the human-readable report to stdout."""
    line = "=" * 78
    print(
        f"\n{line}\nKnowCode storage footprint - generation {report.generation}\n{line}"
    )

    print("\n1. ARTIFACTS")
    for name, size in sorted(report.artifacts.items(), key=lambda kv: -kv[1]):
        print(f"   {name:<34} {_mb(size)}")
    print(f"   {'TOTAL':<34} {_mb(report.total_bytes)}")

    src = report.source
    if src.get("python_bytes"):
        print(
            f"\n   vs source: {_mb(src['python_bytes'])} Python, "
            f"{_mb(src['tracked_bytes'])} tracked"
        )
        print(
            f"   amplification: {report.total_bytes / src['python_bytes']:.1f}x Python"
            f", {report.total_bytes / max(1, src['tracked_bytes']):.1f}x tracked"
        )

    print("\n2. TABLES AND INDEXES")
    for name, size in list(report.tables.items())[:14]:
        if size >= 65536:
            print(f"   {name:<34} {_mb(size)}")

    print("\n3. COLUMN PAYLOAD")
    for table, columns in report.columns.items():
        print(f"   {table} ({report.counts.get(table, 0):,} rows)")
        for col, size in columns.items():
            if size:
                print(f"      {col:<28} {_mb(size)}")

    vector = report.vector_plane
    if vector:
        print("\n4. VECTOR PLANE")
        print(
            f"   {vector.get('vector_count', 0):,} vectors x "
            f"{vector.get('dimension', 0)}d"
            + (
                f", L2 norm {vector['sample_l2_norm']}"
                if "sample_l2_norm" in vector
                else ""
            )
        )
        print(
            f"   durable fp32 BLOBs in chunks.db   {_mb(vector.get('durable_fp32_bytes', 0))}"
        )
        if "derived_index_bytes" in vector:
            print(
                f"   derived on-disk vector index      {_mb(vector['derived_index_bytes'])}"
            )
            print(
                f"      vector data                    {_mb(vector['derived_data_bytes'])}"
            )
            print(
                f"      bookkeeping                    "
                f"{_mb(vector['derived_bookkeeping_bytes'])}"
                f"  ({vector.get('derived_versions', 0)} versions, "
                f"{vector.get('derived_fragments', 0)} fragments)"
            )
        for width, size in vector.get("width_options_bytes", {}).items():
            print(f"   ANN index width {width:<6}            {_mb(size)}")

    profile = report.chunk_profile
    if profile:
        print("\n5. CHUNK TEXT vs VECTOR COST")
        print(
            f"   {'bucket':<12} {'chunks':>7} {'content':>11} {'vectors':>11}  amplif"
        )
        for bucket in profile["size_buckets"]:
            content = bucket["content_bytes"]
            amp = bucket["vector_bytes"] / content if content else 0
            print(
                f"   {bucket['bucket']:<12} {bucket['chunks']:>7,} "
                f"{content / 1024:>8.0f} KB {bucket['vector_bytes'] / 1048576:>8.2f} MB"
                f"  {amp:>5.0f}x"
            )
        print(
            f"\n   {'chunk type':<16} {'chunks':>7} {'content':>11} {'vectors':>11}  amplif"
        )
        for entry in profile["by_type"]:
            content = entry["content_bytes"]
            amp = entry["vector_bytes"] / content if content else 0
            print(
                f"   {entry['type']:<16} {entry['chunks']:>7,} "
                f"{content / 1024:>8.0f} KB {entry['vector_bytes'] / 1048576:>8.2f} MB"
                f"  {amp:>5.0f}x"
            )
        print("\n   vector plane at each minimum-content threshold:")
        for band in profile["threshold_projection"]:
            print(
                f"      >= {band['min_content_bytes']:>3} B  keep "
                f"{band['chunks_kept']:>6,} chunks  {_mb(band['vector_bytes'])}"
            )

    print(
        "\nFor measured savings per candidate optimization, run "
        "scripts/storage_simulate.py"
    )
    print(f"\n{line}\n")


def main() -> None:
    """Measure a KnowCode index generation and print or emit the report."""
    parser = argparse.ArgumentParser(
        description="Measure the storage footprint of a KnowCode index generation"
    )
    parser.add_argument(
        "--index",
        default="knowcode_index",
        help="Path to the knowcode_index/ directory",
    )
    parser.add_argument(
        "--generation",
        default=None,
        help="Generation id to measure (default: the current pointer)",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root, used for artifact-to-source amplification",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the human report",
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

    report = build_report(index_path, generation, Path(args.repo_root))

    if args.json:
        payload = asdict(report)
        payload["total_bytes"] = report.total_bytes
        print(json.dumps(payload, indent=2))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
