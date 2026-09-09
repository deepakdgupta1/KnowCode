"""Resolve every relative link in the documentation set.

A moved document silently breaks the links its neighbours point at, and a
restructured document silently breaks anchors into its sections. Neither
failure shows up in ``mkdocs build``, which only warns, so this runs as its own
gate.

``docs/archive/`` is skipped for the same reason ``mkdocs.yml`` sets
``exclude_docs: archive/*``: those documents are frozen history, they are not
built, and their links describe a tree that has since moved. A living document
therefore may not live under ``docs/archive/``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_LINK = re.compile(
    r"\[[^\]]*\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\s*\)"
)
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
EXPLICIT_ID = re.compile(r"\{#([\w-]+)\}\s*$")
SKIP_SCHEME = re.compile(r"^(https?:|mailto:|ftp:|tel:|data:)", re.IGNORECASE)

ROOTS = ("README.md", "CONTRIBUTING.md")
EXCLUDED_DIRS = {"archive"}


def strip_inline_markup(text: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[`*_~]", "", text)
    return text


def slugify(heading: str) -> str:
    """GitHub's heading slug: the anchor MkDocs and GitHub both generate."""
    text = strip_inline_markup(heading).strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", "-", text).strip("-")


def anchors_of(path: Path) -> set[str]:
    """Every fragment a link into ``path`` may target."""
    found: set[str] = set()
    counts: dict[str, int] = {}
    for line in read_prose(path):
        match = HEADING.match(line)
        if not match:
            continue
        title = match.group(2)
        explicit = EXPLICIT_ID.search(title)
        if explicit:
            found.add(explicit.group(1))
            title = EXPLICIT_ID.sub("", title)
        slug = slugify(title)
        if not slug:
            continue
        seen = counts.get(slug, 0)
        counts[slug] = seen + 1
        found.add(slug if seen == 0 else f"{slug}-{seen}")
    return found


def read_prose(path: Path) -> list[str]:
    """Lines outside fenced code blocks. A link in a sample is not a link."""
    lines: list[str] = []
    fence: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        marker = FENCE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token == fence:
                fence = None
            continue
        if fence is None:
            lines.append(line)
    return lines


def documents(repo: Path) -> list[Path]:
    paths = [repo / name for name in ROOTS if (repo / name).is_file()]
    for path in sorted((repo / "docs").rglob("*.md")):
        if EXCLUDED_DIRS.isdisjoint(path.relative_to(repo / "docs").parts):
            paths.append(path)
    return paths


def failures_in(
    path: Path, repo: Path, anchor_cache: dict[Path, set[str]]
) -> tuple[list[str], int]:
    problems: list[str] = []
    resolved_count = 0
    for number, line in enumerate(read_prose(path), start=1):
        for raw in INLINE_LINK.findall(line):
            target = raw.strip("<>")
            if SKIP_SCHEME.match(target) or target.startswith("#"):
                continue
            file_part, _, fragment = target.partition("#")
            if not file_part:
                continue
            resolved_count += 1
            resolved = (path.parent / file_part).resolve()
            where = f"{path.relative_to(repo)}:{number}"
            if not resolved.exists():
                problems.append(f"{where}: unresolved path -> {target}")
                continue
            if not fragment or resolved.suffix != ".md":
                continue
            if resolved not in anchor_cache:
                anchor_cache[resolved] = anchors_of(resolved)
            if fragment not in anchor_cache[resolved]:
                problems.append(f"{where}: unresolved anchor -> {target}")
    return problems, resolved_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--min-links",
        type=int,
        default=120,
        help="Fail if fewer links were resolved than this. A checker that stops "
        "matching links passes everything, so the corpus size is itself an assertion.",
    )
    args = parser.parse_args()

    repo = args.repo.resolve()
    checked = documents(repo)
    anchor_cache: dict[Path, set[str]] = {}
    problems: list[str] = []
    total = 0
    for path in checked:
        found, count = failures_in(path, repo, anchor_cache)
        problems.extend(found)
        total += count

    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"checked {len(checked)} documents, {total} links, {len(problems)} broken")

    if total < args.min_links:
        print(
            f"only {total} links resolved, expected at least {args.min_links}; "
            "the link matcher is probably broken",
            file=sys.stderr,
        )
        return 1
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
