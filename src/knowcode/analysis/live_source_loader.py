"""Loader for authoritative source code reads from disk."""

import hashlib
import logging
from pathlib import Path
from knowcode.data_models import Entity
from knowcode.utils.entity_identity import (
    canonicalize_source_snippet,
    compute_entity_fallback_hash,
)

logger = logging.getLogger(__name__)


class LiveSourceLoader:
    """Safely reads and slices live source files from disk."""

    def __init__(self, root_dir: str | Path) -> None:
        """Initialize with repository root directory."""
        self.root_dir = Path(root_dir)

    def _slice(self, entity: Entity) -> str | None:
        """Read the entity's recorded line span from its file, or None."""
        if not entity.location or not entity.location.file_path:
            return None

        file_path = self.root_dir / entity.location.file_path
        if not file_path.exists():
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            start = entity.location.line_start - 1
            end = entity.location.line_end

            if start < 0:
                start = 0
            if end > len(lines):
                end = len(lines)

            if start >= len(lines):
                return None

            return "".join(lines[start:end])
        except Exception:
            return None

    def load_source(self, entity: Entity) -> str | None:
        """Load live source code from disk for the given entity.

        Args:
            entity: Target entity containing location data.

        Returns:
            The source code string sliced from the live file,
            or None if the file is missing or reading fails.
        """
        return self._slice(entity)

    def load_verified_source(self, entity: Entity) -> str | None:
        """Load an entity's source only if it still matches its digest.

        Storage plan D3's read side: the span is re-read from the working tree
        and served only when it canonicalizes and hashes to the ``content_hash``
        recorded at index time. Everything else fails closed to None — a
        missing file, an unreadable span, an edited body, a shifted span
        (anything inserted above the entity moves it off its recorded lines),
        or a row with no digest at all. None is always "the index cannot vouch
        for this text", never "the entity has no source". Entities that never
        carried a snippet (MODULE, DOCUMENT, and other label-bearing rows,
        whose digest covers identity fields instead of text) also return None,
        quietly — the stored copy was empty for those rows too.

        Args:
            entity: Target entity carrying location and content_hash metadata.

        Returns:
            The verified source string, or None on any mismatch.
        """
        expected = entity.metadata.get("content_hash")
        if not expected:
            logger.debug(
                "No stored content hash for %s; cannot verify live source.",
                entity.id,
            )
            return None

        snippet = self._slice(entity)
        if snippet is None:
            logger.debug(
                "Live source for %s unreadable (missing file or bad span at %s).",
                entity.id,
                entity.location.file_path if entity.location else "?",
            )
            return None

        canonical = canonicalize_source_snippet(snippet)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if digest == expected:
            return snippet
        if expected == compute_entity_fallback_hash(entity):
            # The entity never carried a snippet — its digest was computed
            # from identity fields at parse time. The stored copy was empty
            # for these rows too, so None is parity, not drift; stay quiet.
            logger.debug(
                "Entity %s has no source snippet; nothing to resolve.", entity.id
            )
            return None
        logger.warning(
            "Source drift for %s: %s no longer matches its indexed digest; "
            "serving nothing rather than stale text. Re-index to refresh.",
            entity.id,
            entity.location.file_path,
        )
        return None
