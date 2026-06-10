#!/usr/bin/env python3
"""Measure KnowCode storage artifacts and generate a reproducible report.

Usage:
    python scripts/measure_storage.py [--store PATH] [--index PATH]

Defaults to knowcode_knowledge.json and knowcode_index/ in the repo root.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _sizeof(path: Path) -> int:
    """File size in bytes."""
    return path.stat().st_size if path.exists() else 0


def _approx_tokens(chars: int, ratio: float = 4.0) -> int:
    """Rough token estimate at ~4 chars/token for English + code."""
    return int(chars / ratio)


def measure_knowledge_store(store_path: Path) -> dict:
    """Measure the knowledge store JSON."""
    raw = store_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    entities = data.get("entities", {})
    relationships = data.get("relationships", [])

    source_chars = sum(len(e.get("source_code", "") or "") for e in entities.values())
    docstring_chars = sum(len(e.get("docstring", "") or "") for e in entities.values())
    sig_chars = sum(len(e.get("signature", "") or "") for e in entities.values())
    location_chars = sum(len(json.dumps(e.get("location", {}))) for e in entities.values())
    id_chars = sum(len(e.get("id", "")) for e in entities.values())
    name_chars = sum(
        len(e.get("name", "")) + len(e.get("qualified_name", ""))
        for e in entities.values()
    )

    by_kind: dict[str, int] = {}
    with_source_by_kind: dict[str, int] = {}
    for e in entities.values():
        k = e.get("kind", "unknown")
        by_kind[k] = by_kind.get(k, 0) + 1
        if e.get("source_code"):
            with_source_by_kind[k] = with_source_by_kind.get(k, 0) + 1

    # Simulate stripped variants
    no_source = json.loads(raw)
    for eid in no_source["entities"]:
        no_source["entities"][eid]["source_code"] = None
    no_source_size = len(json.dumps(no_source, indent=2))

    skeleton_entities = {}
    for eid, e in entities.items():
        skeleton_entities[eid] = {
            "id": e["id"],
            "kind": e["kind"],
            "name": e["name"],
            "qualified_name": e["qualified_name"],
            "signature": e.get("signature"),
            "location": e.get("location"),
        }
    skeleton = {
        "schema_version": data.get("schema_version", data.get("version")),
        "metadata": data.get("metadata"),
        "entities": skeleton_entities,
        "relationships": relationships,
    }
    skeleton_size = len(json.dumps(skeleton, indent=2))

    rels_size = len(json.dumps(relationships, indent=2))

    return {
        "file_bytes": _sizeof(store_path),
        "file_chars": len(raw),
        "total_entities": len(entities),
        "total_relationships": len(relationships),
        "entities_by_kind": by_kind,
        "entities_with_source_by_kind": with_source_by_kind,
        "source_code_chars": source_chars,
        "docstring_chars": docstring_chars,
        "signature_chars": sig_chars,
        "location_chars": location_chars,
        "id_name_chars": id_chars + name_chars,
        "relationships_chars": rels_size,
        "hypothetical_no_source_chars": no_source_size,
        "hypothetical_skeleton_chars": skeleton_size,
    }


def measure_semantic_index(index_path: Path) -> dict:
    """Measure the semantic index directory."""
    chunks_file = index_path / "chunks.json"
    vectors_file = index_path / "vectors.index"
    vectors_json = index_path / "vectors.json"
    manifest_file = index_path / "index_manifest.json"

    result: dict = {
        "chunks_json_bytes": _sizeof(chunks_file),
        "vectors_index_bytes": _sizeof(vectors_file),
        "vectors_json_bytes": _sizeof(vectors_json),
        "manifest_bytes": _sizeof(manifest_file),
        "total_bytes": sum(
            _sizeof(f)
            for f in [chunks_file, vectors_file, vectors_json, manifest_file]
        ),
    }

    if chunks_file.exists():
        data = json.loads(chunks_file.read_text(encoding="utf-8"))
        chunks = data.get("chunks", [])
        result["total_chunks"] = len(chunks)
        result["chunk_content_chars"] = sum(len(c.get("content", "")) for c in chunks)
        result["chunk_token_chars"] = sum(
            len(str(c.get("tokens", []))) for c in chunks
        )

    return result


def simulate_payloads(store_path: Path) -> dict:
    """Simulate minimal vs standard context payloads."""
    data = json.loads(store_path.read_text(encoding="utf-8"))
    entities = data.get("entities", {})

    # Pick 3 representative entities (methods with source + docstring + sig)
    samples = []
    for e in entities.values():
        if (
            e["kind"] == "method"
            and e.get("source_code")
            and e.get("docstring")
            and e.get("signature")
        ):
            samples.append(e)
        if len(samples) >= 3:
            break

    minimal_chars = 0
    standard_chars = 0
    entities_measured = []

    for e in samples:
        header = (
            f"# Method: `{e['qualified_name']}`\n"
            f"**File**: `{e['location']['file_path']}`\n"
            f"**Lines**: {e['location']['line_start']}-{e['location']['line_end']}"
        )
        desc = f"## Description\n\n{e['docstring']}" if e.get("docstring") else ""
        sig = f"## Signature\n\n```python\n{e['signature']}\n```" if e.get("signature") else ""
        src = (
            f"## Source Code\n\n```python\n{e['source_code']}\n```"
            if e.get("source_code")
            else ""
        )

        min_parts = [s for s in [header, desc, sig] if s]
        std_parts = [s for s in [header, desc, sig, src] if s]

        min_bundle = "\n\n---\n\n".join(min_parts)
        std_bundle = "\n\n---\n\n".join(std_parts)

        minimal_chars += len(min_bundle)
        standard_chars += len(std_bundle)

        entities_measured.append(
            {
                "name": e["name"],
                "minimal_chars": len(min_bundle),
                "standard_chars": len(std_bundle),
                "source_chars": len(e.get("source_code", "") or ""),
            }
        )

    return {
        "entities_measured": entities_measured,
        "total_minimal_chars": minimal_chars,
        "total_minimal_tokens": _approx_tokens(minimal_chars),
        "total_standard_chars": standard_chars,
        "total_standard_tokens": _approx_tokens(standard_chars),
        "source_pct_of_standard": (
            round((standard_chars - minimal_chars) / standard_chars * 100, 1)
            if standard_chars
            else 0
        ),
    }


def print_report(ks: dict, idx: dict, payloads: dict) -> None:
    """Print a human-readable measurement report."""
    print("=" * 70)
    print("KnowCode Storage Measurement Report")
    print("=" * 70)
    print()

    print("1. KNOWLEDGE STORE (knowcode_knowledge.json)")
    print(f"   File size:              {ks['file_bytes']:>10,} bytes ({ks['file_bytes']/1024:.1f} KB)")
    print(f"   Entities:               {ks['total_entities']:>10}")
    print(f"   Relationships:          {ks['total_relationships']:>10}")
    print()
    print("   Content breakdown (chars):")
    print(f"     Source code:           {ks['source_code_chars']:>10,} ({ks['source_code_chars']/ks['file_chars']*100:.1f}%)")
    print(f"     Relationships:        {ks['relationships_chars']:>10,} ({ks['relationships_chars']/ks['file_chars']*100:.1f}%)")
    print(f"     Locations:            {ks['location_chars']:>10,} ({ks['location_chars']/ks['file_chars']*100:.1f}%)")
    print(f"     IDs + names:          {ks['id_name_chars']:>10,} ({ks['id_name_chars']/ks['file_chars']*100:.1f}%)")
    print(f"     Docstrings:           {ks['docstring_chars']:>10,} ({ks['docstring_chars']/ks['file_chars']*100:.1f}%)")
    print(f"     Signatures:           {ks['signature_chars']:>10,} ({ks['signature_chars']/ks['file_chars']*100:.1f}%)")
    print()
    print("   Entities by kind:")
    for kind, count in sorted(ks["entities_by_kind"].items(), key=lambda x: -x[1]):
        with_src = ks["entities_with_source_by_kind"].get(kind, 0)
        print(f"     {kind:15s} {count:4d} entities, {with_src:4d} with source_code")
    print()
    print("   Hypothetical variants:")
    pct1 = (1 - ks["hypothetical_no_source_chars"] / ks["file_chars"]) * 100
    pct2 = (1 - ks["hypothetical_skeleton_chars"] / ks["file_chars"]) * 100
    print(f"     Without source_code:  {ks['hypothetical_no_source_chars']:>10,} chars ({pct1:.0f}% smaller)")
    print(f"     Skeleton only:        {ks['hypothetical_skeleton_chars']:>10,} chars ({pct2:.0f}% smaller)")
    print()

    print("2. SEMANTIC INDEX (knowcode_index/)")
    print(f"   chunks.json:            {idx.get('chunks_json_bytes', 0):>10,} bytes ({idx.get('chunks_json_bytes', 0)/1024:.1f} KB)")
    print(f"   vectors.index:          {idx.get('vectors_index_bytes', 0):>10,} bytes ({idx.get('vectors_index_bytes', 0)/1024:.1f} KB)")
    print(f"   vectors.json:           {idx.get('vectors_json_bytes', 0):>10,} bytes ({idx.get('vectors_json_bytes', 0)/1024:.1f} KB)")
    print(f"   Total:                  {idx.get('total_bytes', 0):>10,} bytes ({idx.get('total_bytes', 0)/1024:.1f} KB)")
    print(f"   Chunks:                 {idx.get('total_chunks', 0):>10}")
    print(f"   Chunk content chars:    {idx.get('chunk_content_chars', 0):>10,}")
    print()

    print("3. SOURCE CODE DUPLICATION")
    print(f"   In knowledge JSON:      {ks['source_code_chars']:>10,} chars")
    print(f"   In chunk content:       {idx.get('chunk_content_chars', 0):>10,} chars")
    print("   On disk (.py files):    ground truth (authoritative)")
    print()

    print("4. CONTEXT PAYLOAD SIMULATION (3 entities)")
    print("   Minimal (summarize=True, no source):")
    print(f"     Total chars:          {payloads['total_minimal_chars']:>10,} (~{payloads['total_minimal_tokens']} tokens)")
    print("   Standard (summarize=False, with source):")
    print(f"     Total chars:          {payloads['total_standard_chars']:>10,} (~{payloads['total_standard_tokens']} tokens)")
    print(f"   Source code as % of standard payload: {payloads['source_pct_of_standard']}%")
    print()
    print("   Per-entity breakdown:")
    for em in payloads["entities_measured"]:
        print(f"     {em['name']:30s}  minimal={em['minimal_chars']:5d}  standard={em['standard_chars']:5d}  source={em['source_chars']:5d}")
    print()

    total_artifacts = ks["file_bytes"] + idx.get("total_bytes", 0)
    print("5. TOTAL ARTIFACT FOOTPRINT")
    print(f"   Knowledge store:        {ks['file_bytes']:>10,} bytes")
    print(f"   Semantic index:         {idx.get('total_bytes', 0):>10,} bytes")
    print(f"   Total:                  {total_artifacts:>10,} bytes ({total_artifacts/1024/1024:.1f} MB)")
    print()
    print("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure KnowCode storage artifacts")
    parser.add_argument(
        "--store",
        default="knowcode_knowledge.json",
        help="Path to knowcode_knowledge.json",
    )
    parser.add_argument(
        "--index",
        default="knowcode_index",
        help="Path to knowcode_index/ directory",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON instead of human report",
    )
    args = parser.parse_args()

    store_path = Path(args.store)
    index_path = Path(args.index)

    if not store_path.exists():
        print(f"Error: {store_path} not found. Run `knowcode analyze` first.", file=sys.stderr)
        sys.exit(1)

    ks = measure_knowledge_store(store_path)
    idx = measure_semantic_index(index_path) if index_path.exists() else {}
    payloads = simulate_payloads(store_path)

    if args.json:
        print(json.dumps({"knowledge_store": ks, "semantic_index": idx, "payloads": payloads}, indent=2))
    else:
        print_report(ks, idx, payloads)


if __name__ == "__main__":
    main()
