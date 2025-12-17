"""Basic smoke tests for KnowCode."""

import tempfile
from pathlib import Path

import pytest

from knowcode.graph_builder import GraphBuilder
from knowcode.knowledge_store import KnowledgeStore
from knowcode.models import EntityKind


def test_python_parsing() -> None:
    """Test parsing a simple Python file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a test file
        test_file = Path(tmpdir) / "test.py"
        test_file.write_text("""
def hello(name: str) -> str:
    '''Say hello.'''
    return f"Hello, {name}!"

class Greeter:
    '''A greeter class.'''
    
    def greet(self, name: str) -> str:
        '''Greet someone.'''
        return hello(name)
""")

        # Build graph
        builder = GraphBuilder()
        builder.build_from_directory(tmpdir)

        # Verify entities
        assert len(builder.entities) > 0
        
        # Check for function
        functions = builder.get_entities_by_kind("function")
        assert len(functions) == 1
        assert functions[0].name == "hello"
        
        # Check for class
        classes = builder.get_entities_by_kind("class")
        assert len(classes) == 1
        assert classes[0].name == "Greeter"


def test_knowledge_store_persistence() -> None:
    """Test saving and loading knowledge store."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a test file
        test_file = Path(tmpdir) / "test.py"
        test_file.write_text("def foo(): pass")

        # Build and save
        builder = GraphBuilder()
        builder.build_from_directory(tmpdir)
        
        store = KnowledgeStore.from_graph_builder(builder)
        save_path = Path(tmpdir) / "knowledge.json"
        store.save(save_path)

        # Load and verify
        loaded_store = KnowledgeStore.load(save_path)
        assert len(loaded_store.entities) == len(store.entities)


def test_query_operations() -> None:
    """Test basic query operations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files
        test_file = Path(tmpdir) / "test.py"
        test_file.write_text("""
def caller():
    callee()

def callee():
    pass
""")

        # Build graph
        builder = GraphBuilder()
        builder.build_from_directory(tmpdir)
        store = KnowledgeStore.from_graph_builder(builder)

        # Search
        results = store.search("caller")
        assert len(results) > 0
        assert any(e.name == "caller" for e in results)

        # Get entity
        caller_entity = next(e for e in results if e.name == "caller")
        
        # Check callees
        callees = store.get_callees(caller_entity.id)
        assert len(callees) > 0
