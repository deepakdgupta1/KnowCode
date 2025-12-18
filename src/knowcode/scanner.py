"""File scanner with gitignore support."""

from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import pathspec


@dataclass
class FileInfo:
    """Information about a discovered file."""

    path: Path
    relative_path: str
    extension: str
    size_bytes: int


class Scanner:
    """Scans directories for source files with gitignore support."""

    SUPPORTED_EXTENSIONS = {".py", ".md", ".yaml", ".yml", ".js", ".ts", ".java"}

    def __init__(
        self,
        root_dir: str | Path,
        respect_gitignore: bool = True,
        additional_ignores: Optional[list[str]] = None,
    ) -> None:
        """Initialize scanner.

        Args:
            root_dir: Root directory to scan.
            respect_gitignore: Whether to respect .gitignore files.
            additional_ignores: Additional patterns to ignore.
        """
        self.root_dir = Path(root_dir).resolve()
        self.respect_gitignore = respect_gitignore
        self.additional_ignores = additional_ignores or []
        self._gitignore_spec: Optional[pathspec.PathSpec] = None

    def _load_gitignore(self) -> Optional[pathspec.PathSpec]:
        """Load .gitignore patterns from root directory."""
        gitignore_path = self.root_dir / ".gitignore"
        patterns: list[str] = []

        # Always ignore common directories
        patterns.extend([
            ".git/",
            "__pycache__/",
            "*.pyc",
            ".venv/",
            "venv/",
            "node_modules/",
            ".eggs/",
            "*.egg-info/",
        ])

        # Add additional ignores
        patterns.extend(self.additional_ignores)

        # Load .gitignore if it exists
        if gitignore_path.exists():
            with open(gitignore_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        patterns.append(line)

        return pathspec.PathSpec.from_lines("gitwildmatch", patterns)

    def _should_ignore(self, relative_path: str) -> bool:
        """Check if a path should be ignored."""
        if self._gitignore_spec is None:
            self._gitignore_spec = self._load_gitignore()

        if self._gitignore_spec:
            return self._gitignore_spec.match_file(relative_path)
        return False

    def scan(self) -> Iterator[FileInfo]:
        """Scan directory and yield file information.

        Yields:
            FileInfo for each discovered source file.
        """
        if not self.root_dir.exists():
            raise FileNotFoundError(f"Directory not found: {self.root_dir}")

        if not self.root_dir.is_dir():
            raise NotADirectoryError(f"Not a directory: {self.root_dir}")

        for root, dirs, files in os.walk(self.root_dir):
            root_path = Path(root)

            # Filter directories in-place to skip ignored ones
            dirs[:] = [
                d for d in dirs
                if not self._should_ignore(
                    str((root_path / d).relative_to(self.root_dir)) + "/"
                )
            ]

            for filename in files:
                file_path = root_path / filename
                relative_path = str(file_path.relative_to(self.root_dir))

                # Check if ignored
                if self._should_ignore(relative_path):
                    continue

                # Check extension
                extension = file_path.suffix.lower()
                if extension not in self.SUPPORTED_EXTENSIONS:
                    continue

                try:
                    size_bytes = file_path.stat().st_size
                except OSError:
                    continue

                yield FileInfo(
                    path=file_path,
                    relative_path=relative_path,
                    extension=extension,
                    size_bytes=size_bytes,
                )

    def scan_all(self) -> list[FileInfo]:
        """Scan and return all files as a list."""
        return list(self.scan())
