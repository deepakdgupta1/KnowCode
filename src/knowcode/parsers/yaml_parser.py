"""YAML configuration file parser."""

from pathlib import Path
from typing import Any

import yaml

from knowcode.models import (
    Entity,
    EntityKind,
    Location,
    ParseResult,
    Relationship,
    RelationshipKind,
)


class YamlParser:
    """Parses YAML files into configuration key entities."""

    def parse_file(self, file_path: str | Path) -> ParseResult:
        """Parse a YAML file.

        Args:
            file_path: Path to the YAML file.

        Returns:
            ParseResult with config key entities.
        """
        file_path = Path(file_path)
        errors: list[str] = []

        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            return ParseResult(
                file_path=str(file_path),
                entities=[],
                relationships=[],
                errors=[f"Failed to read file: {e}"],
            )

        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as e:
            return ParseResult(
                file_path=str(file_path),
                entities=[],
                relationships=[],
                errors=[f"YAML parse error: {e}"],
            )

        if data is None:
            data = {}

        entities: list[Entity] = []
        relationships: list[Relationship] = []
        lines = content.splitlines()

        # Create document entity
        doc_name = file_path.stem
        doc_id = f"{file_path}::{doc_name}"

        doc_entity = Entity(
            id=doc_id,
            kind=EntityKind.DOCUMENT,
            name=doc_name,
            qualified_name=doc_name,
            location=Location(
                file_path=str(file_path),
                line_start=1,
                line_end=len(lines),
            ),
        )
        entities.append(doc_entity)

        # Extract config keys recursively
        if isinstance(data, dict):
            self._extract_keys(
                data=data,
                file_path=file_path,
                parent_id=doc_id,
                prefix="",
                entities=entities,
                relationships=relationships,
                lines=lines,
            )

        return ParseResult(
            file_path=str(file_path),
            entities=entities,
            relationships=relationships,
            errors=errors,
        )

    def _extract_keys(
        self,
        data: dict[str, Any],
        file_path: Path,
        parent_id: str,
        prefix: str,
        entities: list[Entity],
        relationships: list[Relationship],
        lines: list[str],
    ) -> None:
        """Recursively extract configuration keys."""
        for key, value in data.items():
            qualified_name = f"{prefix}{key}" if prefix else key
            key_id = f"{file_path}::{qualified_name}"

            # Try to find line number for this key
            line_num = self._find_key_line(key, prefix, lines)

            # Determine value representation
            if isinstance(value, dict):
                value_repr = "{...}"
            elif isinstance(value, list):
                value_repr = f"[{len(value)} items]"
            else:
                value_repr = str(value)[:100]

            key_entity = Entity(
                id=key_id,
                kind=EntityKind.CONFIG_KEY,
                name=key,
                qualified_name=qualified_name,
                location=Location(
                    file_path=str(file_path),
                    line_start=line_num,
                    line_end=line_num,
                ),
                metadata={
                    "value_type": type(value).__name__,
                    "value_preview": value_repr,
                },
            )
            entities.append(key_entity)

            # Add contains relationship
            relationships.append(
                Relationship(
                    source_id=parent_id,
                    target_id=key_id,
                    kind=RelationshipKind.CONTAINS,
                )
            )

            # Recurse into nested dicts
            if isinstance(value, dict):
                self._extract_keys(
                    data=value,
                    file_path=file_path,
                    parent_id=key_id,
                    prefix=f"{qualified_name}.",
                    entities=entities,
                    relationships=relationships,
                    lines=lines,
                )

    def _find_key_line(
        self, key: str, prefix: str, lines: list[str]
    ) -> int:
        """Try to find the line number for a key."""
        # Simple heuristic: find first occurrence of "key:"
        search_pattern = f"{key}:"

        # Calculate expected indentation from prefix depth
        depth = prefix.count(".") if prefix else 0

        for i, line in enumerate(lines, start=1):
            stripped = line.lstrip()
            if stripped.startswith(search_pattern):
                # Check approximate indentation level
                indent = len(line) - len(stripped)
                expected_indent = depth * 2  # Assuming 2-space indent
                if abs(indent - expected_indent) <= 2:
                    return i

        return 1  # Default to line 1 if not found
