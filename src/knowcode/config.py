"""Configuration management for KnowCode."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class ModelConfig:
    """Configuration for a single LLM model."""
    name: str
    provider: str = "google"
    api_key_env: str = "GOOGLE_API_KEY"
    rpm_free_tier_limit: int = 10
    rpd_free_tier_limit: int = 1000
    tokens_free_tier_limit: int = 0  # For embedding/reranking models


@dataclass
class AppConfig:
    """Global application configuration."""
    models: list[ModelConfig] = field(default_factory=list)
    embedding_models: list[ModelConfig] = field(default_factory=list)
    reranking_models: list[ModelConfig] = field(default_factory=list)
    sufficiency_threshold: float = 0.8  # For local-first answering

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "AppConfig":
        """Load configuration from file or use defaults.
        
        Priority:
        1. Explicit config_path
        2. ./aimodels.yaml
        3. ~/.aimodels.yaml
        4. Defaults
        """
        if config_path:
            path = Path(config_path)
            if path.exists():
                return cls._load_from_yaml(path)
        
        local_config = Path("aimodels.yaml")
        if local_config.exists():
            return cls._load_from_yaml(local_config)
            
        home_config = Path.home() / ".aimodels.yaml"
        if home_config.exists():
            return cls._load_from_yaml(home_config)
            
        return cls.default()

    @classmethod
    def default(cls) -> "AppConfig":
        """Return default configuration."""
        return cls(
            models=[
                ModelConfig(name="gemini-2.0-flash-lite"),
                ModelConfig(name="gemini-1.5-flash"),
                ModelConfig(name="gemini-1.5-pro"),
            ],
            sufficiency_threshold=0.8,
        )

    @classmethod
    def _load_from_yaml(cls, path: Path) -> "AppConfig":
        """Parse YAML file into AppConfig.
        
        Supports both old format (models: [...]) and new format 
        (natural_language_models, embedding_models, reranking_models, config).
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            
            # Load LLM models (natural language)
            models = []
            model_list = data.get("natural_language_models", data.get("models", []))
            for m in (model_list or []):
                models.append(ModelConfig(
                    name=m["name"],
                    provider=m.get("provider", "google"),
                    api_key_env=m.get("api_key_env", "GOOGLE_API_KEY"),
                    rpm_free_tier_limit=m.get("rpm_free_tier_limit", 10),
                    rpd_free_tier_limit=m.get("rpd_free_tier_limit", 1000),
                ))
            
            # Load embedding models
            embedding_models = []
            for m in (data.get("embedding_models") or []):
                embedding_models.append(ModelConfig(
                    name=m["name"],
                    provider=m.get("provider", "voyageai"),
                    api_key_env=m.get("api_key_env", "VOYAGE_API_KEY_1"),
                    tokens_free_tier_limit=m.get("tokens_free_tier_limit", 0),
                ))
            
            # Load reranking models
            reranking_models = []
            for m in (data.get("reranking_models") or []):
                reranking_models.append(ModelConfig(
                    name=m["name"],
                    provider=m.get("provider", "voyageai"),
                    api_key_env=m.get("api_key_env", "VOYAGE_API_KEY_1"),
                    tokens_free_tier_limit=m.get("tokens_free_tier_limit", 0),
                ))
            
            # Load config section
            config_section = data.get("config", {})
            sufficiency_threshold = config_section.get("sufficiency_threshold", 0.8)
            
            if not models:
                models = cls.default().models
                
            return cls(
                models=models,
                embedding_models=embedding_models,
                reranking_models=reranking_models,
                sufficiency_threshold=sufficiency_threshold,
            )
        except Exception as e:
            print(f"Warning: Failed to load config from {path}: {e}")
            return cls.default()
