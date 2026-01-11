import pytest
import time
from unittest.mock import MagicMock, patch
from pathlib import Path

from knowcode.config import AppConfig, ModelConfig
from knowcode.llm.agent import Agent
from knowcode.llm.rate_limiter import RateLimiter

def test_rate_limiter_integration(tmp_path):
    # Setup RateLimiter with temp file
    stats_file = tmp_path / "usage_stats.json"
    rate_limiter = RateLimiter(persistence_path=stats_file)
    
    # Config: Model A has RPM=1, Model B has RPM=10
    config = AppConfig(models=[
        ModelConfig(name="low-limit-model", api_key_env="TEST_KEY", rpm_free_tier_limit=1),
        ModelConfig(name="high-limit-model", api_key_env="TEST_KEY", rpm_free_tier_limit=10)
    ])
    
    agent = Agent(MagicMock(), config)
    agent.rate_limiter = rate_limiter # Inject test limiter
    
    # Mock clients
    with patch.object(agent, "_get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.models.generate_content.return_value = MagicMock(text="Success")
        
        with patch.dict("os.environ", {"TEST_KEY": "fake"}):
            # 1st Call: Should use low-limit-model
            print("\n--- Call 1 ---")
            agent.answer("query 1")
            
            # 2nd Call: low-limit-model should be skipped (used 1/1), should use high-limit-model
            print("\n--- Call 2 ---")
            agent.answer("query 2")
            
    # Verify persistence
    assert stats_file.exists()
    with open(stats_file) as f:
        print("\nStats File Content:", f.read())
