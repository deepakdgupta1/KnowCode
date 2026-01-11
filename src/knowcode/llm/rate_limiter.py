"""Rate limiter for LLM requests."""

import json
import time
from pathlib import Path
from typing import Dict, List

from knowcode.config import ModelConfig


class RateLimiter:
    """Tracks and limits LLM usage based on RPM and RPD."""

    def __init__(self, persistence_path: Path = Path.home() / ".knowcode" / "usage_stats.json"):
        """Initialize the rate limiter.
        
        Args:
            persistence_path: Path to the JSON file storing usage stats.
        """
        self.persistence_path = persistence_path
        self.usage_data: Dict[str, List[float]] = {}
        self._load()

    def _load(self) -> None:
        """Load usage data from disk."""
        if self.persistence_path.exists():
            try:
                with open(self.persistence_path, "r", encoding="utf-8") as f:
                    self.usage_data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                print(f"Warning: Failed to load usage stats, starting fresh. Error: {e}")
                self.usage_data = {}
        else:
            self.usage_data = {}

        # Ensure directory exists for future saves
        self.persistence_path.parent.mkdir(parents=True, exist_ok=True)

    def _save(self) -> None:
        """Save usage data to disk."""
        try:
            # Clean up old data before saving to keep file small
            self._cleanup()
            with open(self.persistence_path, "w", encoding="utf-8") as f:
                json.dump(self.usage_data, f)
        except OSError as e:
             print(f"Warning: Failed to save usage stats. Error: {e}")

    def _cleanup(self) -> None:
        """Remove timestamps older than 24 hours."""
        now = time.time()
        cutoff = now - 86400  # 24 hours
        
        cleaned_data = {}
        for model, timestamps in self.usage_data.items():
            valid = [t for t in timestamps if t > cutoff]
            if valid:
                cleaned_data[model] = valid
        
        self.usage_data = cleaned_data

    def check_availability(self, model_config: ModelConfig) -> bool:
        """Check if a model is within its rate limits.
        
        Args:
            model_config: The model configuration containing limits.
            
        Returns:
            True if available, False if limit exceeded.
        """
        timestamps = self.usage_data.get(model_config.name, [])
        now = time.time()
        
        # Check RPM (last 60 seconds)
        cutoff_min = now - 60
        last_minute_requests = [t for t in timestamps if t > cutoff_min]
        if len(last_minute_requests) >= model_config.rpm_free_tier_limit:
            print(f"  ⚠️ Limit Reached: {model_config.name} used {len(last_minute_requests)}/{model_config.rpm_free_tier_limit} RPM.")
            return False
            
        # Check RPD (last 24 hours)
        cutoff_day = now - 86400
        last_day_requests = [t for t in timestamps if t > cutoff_day]
        if len(last_day_requests) >= model_config.rpd_free_tier_limit:
            print(f"  ⚠️ Limit Reached: {model_config.name} used {len(last_day_requests)}/{model_config.rpd_free_tier_limit} RPD.")
            return False
            
        return True

    def record_usage(self, model_name: str) -> None:
        """Record a successful request."""
        if model_name not in self.usage_data:
            self.usage_data[model_name] = []
        
        self.usage_data[model_name].append(time.time())
        self._save()
