"""Demo / profiling runner for the structure-aware prose chunker (P0 prototype).

Runs ``ProseChunker`` over a directory of markdown documents and prints corpus
statistics plus a sample document's chunks, so chunk quality can be eyeballed.

Usage::

    uv run python scripts/prose_chunk_demo.py [ROOT] [--show-doc PATH] [--limit N]

Example::

    uv run python scripts/prose_chunk_demo.py tests/test_sample-prose
"""

from __future__ import annotations

import statistics as st
from pathlib import Path

import click

from knowcode.indexing.prose_chunker import ProseChunker, ProseChunkingConfig


def _pct(values: list[int], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def _print_corpus_stats(chunker: ProseChunker, files: list[Path], root: Path) -> Path:
    """Print aggregate stats; return a clean representative doc to display."""
    max_tokens = chunker.config.max_tokens
    tokens: list[int] = []
    per_doc: list[int] = []
    flagged = total = 0
    sample: Path | None = None

    for path in files:
        chunks = chunker.chunk_file(path, doc_id=str(path.relative_to(root)))
        per_doc.append(len(chunks))
        total += len(chunks)
        for chunk in chunks:
            tokens.append(chunk.token_count)
            flagged += chunk.is_oversize
        if sample is None and 6 <= len(chunks) <= 14 and not any(c.is_oversize for c in chunks):
            sample = path

    in_band = sum(1 for t in tokens if 128 <= t <= max_tokens)
    click.echo(f"docs={len(files)}  total_chunks={total}")
    click.echo(
        f"chunks/doc: mean={st.mean(per_doc):.1f} p50={_pct(per_doc, .5)} "
        f"p90={_pct(per_doc, .9)} max={max(per_doc)}"
    )
    click.echo(
        f"chunk tokens: mean={st.mean(tokens):.0f} p50={_pct(tokens, .5)} "
        f"p90={_pct(tokens, .9)} max={max(tokens)}"
    )
    click.echo(
        f"within [128,{max_tokens}]: {in_band / len(tokens) * 100:.1f}%   "
        f"flagged-oversize: {flagged / total * 100:.1f}% (blob/code that could not split cleanly)"
    )
    return sample or files[0]


def _print_doc_chunks(chunker: ProseChunker, path: Path, root: Path, limit: int) -> None:
    chunks = chunker.chunk_file(path, doc_id=str(path.relative_to(root)))
    click.echo(f"\n=== {path.relative_to(root)}  ({len(chunks)} chunks) ===")
    for chunk in chunks[:limit]:
        crumb = " > ".join(chunk.heading_path) or "(preamble)"
        flag = " [OVERSIZE]" if chunk.is_oversize else ""
        click.echo(
            f"\n[L{chunk.level}] {crumb}  | lines {chunk.start_line}-{chunk.end_line} "
            f"| {chunk.token_count} tok{flag}"
        )
        click.echo(f"   ctx:    {chunk.context_header}")
        click.echo(f"   parent: {chunk.parent_id}")
        click.echo(f"   text:   {chunk.content[:140].replace(chr(10), ' ')}")


@click.command()
@click.argument("root", type=click.Path(exists=True, path_type=Path), default="tests/test_sample-prose")
@click.option("--show-doc", type=click.Path(path_type=Path), default=None, help="Display chunks for one document.")
@click.option("--limit", type=int, default=6, help="Chunks to display for the sample document.")
@click.option("--target-tokens", type=int, default=ProseChunkingConfig.target_tokens)
@click.option("--max-tokens", type=int, default=ProseChunkingConfig.max_tokens)
def main(root: Path, show_doc: Path | None, limit: int, target_tokens: int, max_tokens: int) -> None:
    chunker = ProseChunker(ProseChunkingConfig(target_tokens=target_tokens, max_tokens=max_tokens))
    if show_doc is not None:
        _print_doc_chunks(chunker, show_doc, show_doc.parent, limit)
        return
    files = sorted(p for p in root.rglob("*.md"))
    if not files:
        raise click.ClickException(f"no .md files under {root}")
    sample = _print_corpus_stats(chunker, files, root)
    _print_doc_chunks(chunker, sample, root, limit)


if __name__ == "__main__":
    main()
