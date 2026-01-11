import pytest
from unittest.mock import MagicMock, patch
from google.api_core.exceptions import ResourceExhausted

from knowcode.config import AppConfig, ModelConfig
from knowcode.llm.agent import Agent

def test_agent_failover_logic():
    # Setup mock service and config
    mock_service = MagicMock()
    mock_service.search.return_value = [] # No context needed for this test
    
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
