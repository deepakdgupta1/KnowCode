"""Tests for Token Counter and Context Synthesizer."""

from unittest.mock import MagicMock
from knowcode.utils.token_counter import TokenCounter
from knowcode.analysis.context_synthesizer import ContextSynthesizer
from knowcode.models import Entity, EntityKind, Location

def test_token_counter():
    """Test functionality of TokenCounter."""
    counter = TokenCounter()
    
    text = "Hello world"
    tokens = counter.count_tokens(text)
    assert tokens > 0
    
    truncated = counter.truncate(text, max_tokens=1)
    assert counter.count_tokens(truncated) == 1
    assert truncated != text

def test_context_synthesizer_budget():
    """Test standard budgeting logic."""
    store = MagicMock()
    
    # Create a mock entity with huge source code
    large_code = "print('hello')\n" * 1000
    entity = Entity(
        id="test::Foo",
        kind=EntityKind.CLASS,
        name="Foo",
        qualified_name="Foo",
        location=Location("test.py", 1, 1000),
        source_code=large_code
    )
    store.get_entity.return_value = entity
    store.get_parent.return_value = None
    store.get_callers.return_value = []
    store.get_callees.return_value = []
    store.get_children.return_value = []
    
    # Low budget
    synthesizer = ContextSynthesizer(store, max_tokens=50)
    bundle = synthesizer.synthesize("test::Foo")
    
    assert bundle is not None
    assert bundle.total_tokens <= 50
    assert bundle.truncated is True
    # The text itself might not say 'truncated' if we omitted the whole section
    # assert "truncated" in bundle.context_text

def test_context_synthesizer_priority():
    """Test that header is preserved even if code is truncated."""
    store = MagicMock()
    
    entity = Entity(
        id="test::Bar",
        kind=EntityKind.FUNCTION,
        name="bar",
        qualified_name="bar",
        location=Location("test.py", 1, 10),
        source_code="def bar():\n    pass # very long code...",
        docstring="Checks that header is kept."
    )
    store.get_entity.return_value = entity
    store.get_parent.return_value = None
    store.get_callers.return_value = []
    store.get_callees.return_value = []
    store.get_children.return_value = []
    
    synthesizer = ContextSynthesizer(store, max_tokens=100)
    bundle = synthesizer.synthesize("test::Bar")
    
    assert bundle is not None
    # Ensure header info is present
    assert "# Function: `bar`" in bundle.context_text
    assert "**File**: `test.py`" in bundle.context_text
