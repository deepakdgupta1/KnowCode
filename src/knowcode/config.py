"""Configuration management for KnowCode."""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)


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

    KNOWN_TOP_LEVEL_KEYS = {
        "models",
        "natural_language_models",
        "embedding_models",
        "reranking_models",
        "eval_models",
        "config",
    }
    KNOWN_CONFIG_KEYS = {"sufficiency_threshold"}

    models: list[ModelConfig] = field(default_factory=list)
    embedding_models: list[ModelConfig] = field(default_factory=list)
    reranking_models: list[ModelConfig] = field(default_factory=list)
    sufficiency_threshold: float = 0.8  # For local-first answering

    @classmethod
    def load(cls, config_path: Optional[str] = None, strict: bool = False) -> "AppConfig":
        """Load configuration from file or use defaults.
        
        Priority:
        1. Explicit config_path
        2. ./aimodels.yaml
        3. ~/.aimodels.yaml
        4. Defaults

        Args:
            config_path: Explicit config path to load.
            strict: If True, raise ValueError on invalid config instead of
                falling back to defaults.
        """
        if config_path:
            path = Path(config_path)
            if path.exists():
                return cls._load_from_yaml(path, strict=strict)
        
        local_config = Path("aimodels.yaml")
        if local_config.exists():
            return cls._load_from_yaml(local_config, strict=strict)
            
        home_config = Path.home() / ".aimodels.yaml"
        if home_config.exists():
            return cls._load_from_yaml(home_config, strict=strict)
            
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
            embedding_models=[
                ModelConfig(
                    name="voyage-code-3",
                    provider="voyageai",
                    api_key_env="VOYAGE_API_KEY_1",
                )
            ],
            sufficiency_threshold=0.8,
        )

    @classmethod
    def _load_from_yaml(cls, path: Path, strict: bool = False) -> "AppConfig":
        """Parse YAML file into AppConfig.
        
        Supports both old format (models: [...]) and new format 
        (natural_language_models, embedding_models, reranking_models, config).
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw_data = yaml.safe_load(f)
            data = raw_data or {}
            if not isinstance(data, dict):
                raise ValueError("Config root must be a YAML mapping/object.")

            cls._validate_keys(data, path=path, strict=strict)
            
            # Load LLM models (natural language)
            models: list[ModelConfig] = []
            model_list = data.get("natural_language_models", data.get("models", []))
            for m in (model_list or []):
                if not isinstance(m, dict):
                    raise ValueError("Each model entry must be an object.")
                models.append(ModelConfig(
                    name=m["name"],
                    provider=m.get("provider", "google"),
                    api_key_env=m.get("api_key_env", "GOOGLE_API_KEY"),
                    rpm_free_tier_limit=m.get("rpm_free_tier_limit", 10),
                    rpd_free_tier_limit=m.get("rpd_free_tier_limit", 1000),
                ))
            
            # Load embedding models
            embedding_models: list[ModelConfig] = []
            for m in (data.get("embedding_models") or []):
                if not isinstance(m, dict):
                    raise ValueError("Each embedding model entry must be an object.")
                embedding_models.append(ModelConfig(
                    name=m["name"],
                    provider=m.get("provider", "voyageai"),
                    api_key_env=m.get("api_key_env", "VOYAGE_API_KEY_1"),
                    tokens_free_tier_limit=m.get("tokens_free_tier_limit", 0),
                ))
            
            # Load reranking models
            reranking_models: list[ModelConfig] = []
            for m in (data.get("reranking_models") or []):
                if not isinstance(m, dict):
                    raise ValueError("Each reranking model entry must be an object.")
                reranking_models.append(ModelConfig(
                    name=m["name"],
                    provider=m.get("provider", "voyageai"),
                    api_key_env=m.get("api_key_env", "VOYAGE_API_KEY_1"),
                    tokens_free_tier_limit=m.get("tokens_free_tier_limit", 0),
                ))
            
            # Load config section
            config_section = data.get("config", {})
            if config_section is None:
                config_section = {}
            if not isinstance(config_section, dict):
                raise ValueError("'config' section must be an object.")
            sufficiency_threshold = config_section.get("sufficiency_threshold", 0.8)
            if not isinstance(sufficiency_threshold, (int, float)):
                raise ValueError("'config.sufficiency_threshold' must be a number.")
            
            if not models:
                models = cls.default().models
                
            return cls(
                models=models,
                embedding_models=embedding_models,
                reranking_models=reranking_models,
                sufficiency_threshold=sufficiency_threshold,
            )
        except Exception as e:
            message = f"Failed to load config from {path}: {e}"
            if strict:
                raise ValueError(message) from e
            logger.warning(message)
            return cls.default()

    @classmethod
    def _validate_keys(cls, data: dict[str, Any], *, path: Path, strict: bool) -> None:
        """Validate known configuration keys and handle unknown keys."""
        unknown_top_level = sorted(set(data.keys()) - cls.KNOWN_TOP_LEVEL_KEYS)
        if unknown_top_level:
            cls._handle_validation_issue(
                f"Unknown top-level config keys in {path}: {', '.join(unknown_top_level)}",
                strict=strict,
            )

        config_section = data.get("config")
        if config_section is None:
            return
        if not isinstance(config_section, dict):
            cls._handle_validation_issue(
                f"Invalid 'config' section in {path}: expected an object.",
                strict=strict,
            )
            return

        unknown_config_keys = sorted(set(config_section.keys()) - cls.KNOWN_CONFIG_KEYS)
        if unknown_config_keys:
            cls._handle_validation_issue(
                f"Unknown config keys in {path} config section: {', '.join(unknown_config_keys)}",
                strict=strict,
            )

    @staticmethod
    def _handle_validation_issue(message: str, *, strict: bool) -> None:
        """Warn or raise on configuration validation issues."""
        if strict:
            raise ValueError(message)
        logger.warning(message)
