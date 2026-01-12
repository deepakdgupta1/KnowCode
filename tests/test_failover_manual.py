from unittest.mock import MagicMock, patch
from google.api_core.exceptions import ResourceExhausted
from pathlib import Path

from knowcode.config import AppConfig, ModelConfig
from knowcode.llm.agent import Agent

def test_agent_failover_logic():
    # Setup mock service and config
    mock_service = MagicMock()
    mock_service.store_path = Path(".")
    mock_service.retrieve_context_for_query.return_value = {
        "query": "test query",
        "task_type": "general",
        "task_confidence": 0.0,
        "retrieval_mode": "none",
        "context_text": "",
        "total_tokens": 0,
        "max_tokens": 6000,
        "truncated": False,
        "sufficiency_score": 0.0,
        "selected_entities": [],
        "evidence": [],
        "errors": [],
    }
    
    config = AppConfig(models=[
        ModelConfig(name="primary-model", api_key_env="TEST_KEY"),
        ModelConfig(name="backup-model", api_key_env="TEST_KEY")
    ])
    
    agent = Agent(mock_service, config)
    
    # Mock genai.Client
    with patch.object(agent, "_get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        
        # Simulate failover: first call raises ResourceExhausted, second succeeds
        mock_client.models.generate_content.side_effect = [
            ResourceExhausted("Quota exceeded"),
            MagicMock(text="Success from backup")
        ]
        
        # Execute
        with patch.dict("os.environ", {"TEST_KEY": "fake-key"}):
            answer = agent.answer("test query")
            
        # Verify
        print(f"Answer: {answer}")
        assert answer == "Success from backup"
        assert mock_client.models.generate_content.call_count == 2
