"""Runtime signal processing (e.g., coverage, traces)."""

from __future__ import annotations
import xml.etree.ElementTree as ET
from pathlib import Path

from knowcode.data_models import (
    Entity,
    EntityKind,
    Location,
    ParseResult,
    Relationship,
    RelationshipKind,
)


class CoverageProcessor:
    """Process coverage reports."""

    def __init__(self, root_dir: str | Path) -> None:
        """Initialize coverage processor.

        Args:
            root_dir: Root directory of the codebase (for relative path resolution).
        """
        self.root_dir = Path(root_dir).resolve()

    def process_cobertura(self, xml_path: str | Path) -> ParseResult:
        """Process a Cobertura XML coverage report.

        Args:
            xml_path: Path to coverage.xml.

        Returns:
            ParseResult containing COVERAGE_REPORT entity and COVERS relationships.
        """
        xml_path = Path(xml_path)
        if not xml_path.exists():
            return ParseResult(
                file_path=str(xml_path),
                entities=[],
                relationships=[],
                errors=[f"Coverage file not found: {xml_path}"],
            )

        entities: list[Entity] = []
        relationships: list[Relationship] = []
        errors: list[str] = []

        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()

            # Create Report Entity
            report_id = f"coverage::{xml_path.name}"
            # Extract timestamp if available available in root attributes usually 'timestamp'
            timestamp = root.get("timestamp", str(xml_path.stat().st_mtime))
            
            report_entity = Entity(
                id=report_id,
                kind=EntityKind.COVERAGE_REPORT,
                name=f"Coverage Report ({xml_path.name})",
                qualified_name=xml_path.name,
                location=Location(str(xml_path), 0, 0),
                metadata={
                    "timestamp": timestamp,
                    "line-rate": root.get("line-rate", "0"),
                    "branch-rate": root.get("branch-rate", "0"),
                },
            )
            entities.append(report_entity)

            # Traverse packages -> classes -> lines
            # Structure: coverage -> packages -> package -> classes -> class -> lines -> line
            
            # We want to map files/classes to the report.
            # <class name="knowcode.models" filename="src/knowcode/models.py" line-rate="1.0" ...>
            
            for cls in root.findall(".//class"):
                filename = cls.get("filename")
                if not filename:
                    continue
                
                # Resolve file path to simple module ID
                # We assume standard module ID: /abs/path/to/file::filename_stem
                # filename in coverage.xml is usually relative to root
                abs_file_path = (self.root_dir / filename).resolve()
                module_name = abs_file_path.stem
                module_id = f"{abs_file_path}::{module_name}"
                
                line_rate = cls.get("line-rate", "0")
                
                # Relationship: REPORT -> COVERS -> MODULE
                relationships.append(
                    Relationship(
                        source_id=report_id,
                        target_id=module_id,
                        kind=RelationshipKind.COVERS,
                        metadata={
                            "line-rate": line_rate,
                            "hits": cls.get("lines-covered", "0") + "/" + cls.get("lines-valid", "0")
                        }
                    )
                )
                
                # We could map specific lines to entities if we had line ranges of entities loaded.
                # Since CoverageProcessor runs independently or after graph build, 
                # we usually just link to the File/Module level for MVP.
                # Detailed line mapping requires access to the full graph to find which entity covers line X.
                # For v1.4 MVP, linking to Module is sufficient.
                
                # Note: We can also add "EXECUTED_BY" from Module to Report
                relationships.append(
                    Relationship(
                        source_id=module_id,
                        target_id=report_id,
                        kind=RelationshipKind.EXECUTED_BY
                    )
                )

        except ET.ParseError as e:
            errors.append(f"Invalid XML format: {e}")
        except Exception as e:
            errors.append(f"Error processing coverage: {e}")

        return ParseResult(
            file_path=str(xml_path),
            entities=entities,
            relationships=relationships,
            errors=errors,
        )
