"""Tests for the KnowCode Agent."""

import os
from unittest.mock import MagicMock, patch

import pytest
from knowcode.llm.agent import Agent
from knowcode.models import Entity, EntityKind, Location


@pytest.fixture
def mock_service():
    service = MagicMock()
    return service


@pytest.fixture
def mock_openai():
    with patch("knowcode.llm.agent.OpenAI") as mock:
        yield mock


def test_agent_initialization(mock_service):
    """Test that agent initializes correctly."""
    # With API key
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
        agent = Agent(mock_service)
        assert agent.client is not None
        
    # Without API key
    with patch.dict(os.environ, {}, clear=True):
        agent = Agent(mock_service)
        assert agent.client is None


def test_agent_answer_no_key(mock_service):
    """Test error when answering without API key."""
    with patch.dict(os.environ, {}, clear=True):
        agent = Agent(mock_service)
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            agent.answer("test query")


def test_agent_answer_success(mock_service, mock_openai):
    """Test successful answer generation."""
    # Setup mocks
    mock_entity = Entity(
        id="file.py::MyClass",
        kind=EntityKind.CLASS,
        name="MyClass",
        qualified_name="pkg.MyClass",
        location=Location("file.py", 1, 10),
    )
    mock_service.search.return_value = [mock_entity]
    mock_service.get_context.return_value = {
        "context_text": "# MyClass Context",
        "total_tokens": 100,
    }
    
    mock_completion = MagicMock()
    mock_completion.choices[0].message.content = "This is the answer."
    mock_openai.return_value.chat.completions.create.return_value = mock_completion
    
    # Run test
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
        agent = Agent(mock_service)
        answer = agent.answer("How does MyClass work?")
        
        assert answer == "This is the answer."
        assert mock_service.search.called
        assert mock_service.get_context.called


def test_agent_answer_no_entities(mock_service, mock_openai):
    """Test answering when no relevant entities are found."""
    mock_service.search.return_value = []
    
    mock_completion = MagicMock()
    mock_completion.choices[0].message.content = "I don't know."
    mock_openai.return_value.chat.completions.create.return_value = mock_completion
    
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
        agent = Agent(mock_service)
        agent.answer("Unknown thing")
        
        # Verify prompt mentions no entities found
        call_args = mock_openai.return_value.chat.completions.create.call_args
        messages = call_args.kwargs["messages"]
        user_msg = messages[1]["content"]
        assert "No specific entities found" in user_msg
