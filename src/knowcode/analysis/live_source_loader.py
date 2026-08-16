"""Loader for authoritative source code reads from disk."""

from pathlib import Path
from knowcode.data_models import Entity


class LiveSourceLoader:
    """Safely reads and slices live source files from disk."""

    def __init__(self, root_dir: str | Path) -> None:
        """Initialize with repository root directory."""
        self.root_dir = Path(root_dir)

    def load_source(self, entity: Entity) -> str | None:
        """Load live source code from disk for the given entity.

        Args:
            entity: Target entity containing location data.

        Returns:
            The source code string sliced from the live file,
            or None if the file is missing or reading fails.
        """
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
