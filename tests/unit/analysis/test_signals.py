"""Tests for Signal Ingestion."""

import pytest
from knowcode.analysis.signals import CoverageProcessor
from knowcode.data_models import EntityKind, RelationshipKind

@pytest.fixture
def coverage_xml(tmp_path):
    """Create a sample coverage.xml."""
    content = """<?xml version="1.0" ?>
    <coverage line-rate="0.5" branch-rate="0.0" lines-covered="10" lines-valid="20" timestamp="123456789">
        <packages>
            <package name="my_package" line-rate="0.5" branch-rate="0.0" complexity="0.0">
                <classes>
                    <class name="module_a" filename="module_a.py" line-rate="1.0" branch-rate="0.0" complexity="0.0" lines-covered="5" lines-valid="5">
                        <lines>
                            <line hits="1" number="1"/>
                            <line hits="1" number="2"/>
                        </lines>
                    </class>
                    <class name="module_b" filename="module_b.py" line-rate="0.0" branch-rate="0.0" complexity="0.0" lines-covered="0" lines-valid="5">
                         <lines>
                            <line hits="0" number="1"/>
                        </lines>
                    </class>
                </classes>
            </package>
        </packages>
    </coverage>
    """
    path = tmp_path / "coverage.xml"
    path.write_text(content, encoding="utf-8")
    return path

def test_process_cobertura(tmp_path, coverage_xml):
    """Test Cobertura XML processing."""
    # Create dummy files so they resolve
    (tmp_path / "module_a.py").touch()
    (tmp_path / "module_b.py").touch()
    
    processor = CoverageProcessor(tmp_path)
    result = processor.process_cobertura(coverage_xml)
    
    assert not result.errors
    
    # Check Report Entity
    reports = [e for e in result.entities if e.kind == EntityKind.COVERAGE_REPORT]
    assert len(reports) == 1
    report = reports[0]
    assert report.name == "Coverage Report (coverage.xml)"
    assert report.metadata["line-rate"] == "0.5"
    
    # Check Relationships
    # Report -> COVERS -> Module A
    covers_rels = [r for r in result.relationships if r.kind == RelationshipKind.COVERS]
    assert len(covers_rels) == 2 # module_a and module_b
    
    # Verify module IDs are correct (absolute path)
    targets = {r.target_id for r in covers_rels}
    assert any("module_a.py" in t for t in targets)
    assert any("module_b.py" in t for t in targets)
    
    # Check metadata on relationships
    rel_a = next(r for r in result.relationships if "module_a.py" in r.target_id and r.kind == RelationshipKind.COVERS)
    assert rel_a.metadata["line-rate"] == "1.0"
    assert rel_a.metadata["hits"] == "5/5"
