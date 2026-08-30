"""Configuration management for KnowCode."""

import logging
import os
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


# Default dimension weights for pre-flight assessment (must sum to ~1.0).
_DEFAULT_PREFLIGHT_WEIGHTS: dict[str, float] = {
    "parse_success_rate": 0.20,
    "language_coverage": 0.10,
    "documentation_density": 0.20,
    "naming_quality": 0.10,
    "structural_depth": 0.05,
    "relationship_density": 0.15,
    "type_annotation_coverage": 0.05,
    "complexity_distribution": 0.05,
    "behavior_analyzability": 0.05,
    "unresolved_references": 0.05,
}


@dataclass
class PreflightConfig:
    """Configuration for pre-flight codebase quality assessment."""

    enabled: bool = True
    min_score: float = 0.0  # Warn threshold; 0.0 = no warning
    weights: dict[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_PREFLIGHT_WEIGHTS)
    )


@dataclass
class AppConfig:
    """Global application configuration."""

    KNOWN_TOP_LEVEL_KEYS = {
        "models",
        "natural_language_models",
        "embedding_models",
        "prose_embedding_models",
        "reranking_models",
        "eval_models",
        "config",
        "preflight",
    }
    KNOWN_CONFIG_KEYS = {
        "sufficiency_threshold",
        "local_answer_task_types",
        "routing_quality_floor",
        "hybrid_alpha",
        "reranker_top_k_multiplier",
        "vector_backend",
        "entity_source",
    }
    SUPPORTED_TASK_TYPES = {
        "explain",
        "debug",
        "extend",
        "review",
        "locate",
        "general",
    }

    models: list[ModelConfig] = field(default_factory=list)
    embedding_models: list[ModelConfig] = field(default_factory=list)
    prose_embedding_models: list[ModelConfig] = field(default_factory=list)
    reranking_models: list[ModelConfig] = field(default_factory=list)
    sufficiency_threshold: float = 0.8  # For local-first answering
    # Fail closed: only a machine-verified artifact may populate this allowlist.
    local_answer_task_types: list[str] = field(default_factory=list)
    routing_quality_floor: float = 0.9
    hybrid_alpha: float = 0.2
    reranker_top_k_multiplier: int = 5
    vector_backend: str = "lancedb"
    # Storage plan D3: "disk" stops persisting entities.source_code and serves
    # it from the working tree, verified against the stored content hash and
    # failing closed on drift; "stored" keeps the persisted copy and its
    # possibly-stale reads.
    entity_source: str = "disk"
    preflight: PreflightConfig = field(default_factory=PreflightConfig)

    @classmethod
    def load(
        cls, config_path: Optional[str] = None, strict: bool = False
    ) -> "AppConfig":
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
                return cls._fail_closed(cls._load_from_yaml(path, strict=strict))

        local_config = Path("aimodels.yaml")
        if local_config.exists():
            return cls._fail_closed(cls._load_from_yaml(local_config, strict=strict))

        home_config = Path.home() / ".aimodels.yaml"
        if home_config.exists():
            return cls._fail_closed(cls._load_from_yaml(home_config, strict=strict))

        return cls._fail_closed(cls.default())

    @classmethod
    def _fail_closed(cls, config: "AppConfig") -> "AppConfig":
        """Prevent descriptive YAML fields from enabling local answers."""
        config.local_answer_task_types = []
        return config

    def apply_runtime_policy(self, *, source_root: Path) -> None:
        """Apply the explicitly checksum-pinned policy selected by the runtime."""
        artifact_path = os.environ.get("KNOWCODE_ROUTING_POLICY_ARTIFACT")
        if artifact_path:
            self.apply_machine_verification_artifact(
                Path(artifact_path),
                source_root=source_root,
                expected_sha256=os.environ.get("KNOWCODE_ROUTING_POLICY_SHA256"),
            )
        else:
            self.local_answer_task_types = []

    def apply_machine_verification_artifact(
        self,
        path: Path,
        *,
        source_root: Path,
        expected_sha256: str | None,
    ) -> None:
        """Apply a verified policy artifact; unverifiable artifacts stay closed.

        Any verification failure -- a missing or mismatched checksum, source
        drift, a version mismatch, or a malformed artifact -- fails closed:
        local answering stays disabled and the rest of the service keeps
        running LLM-only. "No local answering" is the secure state, so
        degrading is always safer than raising and taking the whole service
        down with an operator misconfiguration.
        """
        from knowcode.routing_policy import load_routing_policy

        # Fail closed by default; only a fully verified artifact re-enables this.
        self.local_answer_task_types = []
        try:
            policy = load_routing_policy(
                path,
                source_root=source_root,
                expected_sha256=expected_sha256,
            )
        except ValueError as exc:
            logger.warning(
                "Routing policy artifact %s rejected; local answering disabled: %s",
                path,
                exc,
            )
            return
        if policy is None:
            return
        self.sufficiency_threshold = policy.sufficiency_threshold
        self.local_answer_task_types = list(policy.local_answer_task_types)
        self.routing_quality_floor = policy.routing_quality_floor

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
            hybrid_alpha=0.2,
            reranker_top_k_multiplier=5,
            vector_backend="lancedb",
            entity_source="disk",
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
            for m in model_list or []:
                if not isinstance(m, dict):
                    raise ValueError("Each model entry must be an object.")
                models.append(
                    ModelConfig(
                        name=m["name"],
                        provider=m.get("provider", "google"),
                        api_key_env=m.get("api_key_env", "GOOGLE_API_KEY"),
                        rpm_free_tier_limit=m.get("rpm_free_tier_limit", 10),
                        rpd_free_tier_limit=m.get("rpd_free_tier_limit", 1000),
                    )
                )

            # Load embedding models
            embedding_models: list[ModelConfig] = []
            for m in data.get("embedding_models") or []:
                if not isinstance(m, dict):
                    raise ValueError("Each embedding model entry must be an object.")
                embedding_models.append(
                    ModelConfig(
                        name=m["name"],
                        provider=m.get("provider", "voyageai"),
                        api_key_env=m.get("api_key_env", "VOYAGE_API_KEY_1"),
                        tokens_free_tier_limit=m.get("tokens_free_tier_limit", 0),
                    )
                )

            # Load prose embedding models (SDLC documentation collateral)
            prose_embedding_models: list[ModelConfig] = []
            for m in data.get("prose_embedding_models") or []:
                if not isinstance(m, dict):
                    raise ValueError(
                        "Each prose embedding model entry must be an object."
                    )
                prose_embedding_models.append(
                    ModelConfig(
                        name=m["name"],
                        provider=m.get("provider", "voyageai"),
                        api_key_env=m.get("api_key_env", "VOYAGE_API_KEY_1"),
                        tokens_free_tier_limit=m.get("tokens_free_tier_limit", 0),
                    )
                )

            # Load reranking models
            reranking_models: list[ModelConfig] = []
            for m in data.get("reranking_models") or []:
                if not isinstance(m, dict):
                    raise ValueError("Each reranking model entry must be an object.")
                reranking_models.append(
                    ModelConfig(
                        name=m["name"],
                        provider=m.get("provider", "voyageai"),
                        api_key_env=m.get("api_key_env", "VOYAGE_API_KEY_1"),
                        tokens_free_tier_limit=m.get("tokens_free_tier_limit", 0),
                    )
                )

            # Load config section
            config_section = data.get("config", {})
            if config_section is None:
                config_section = {}
            if not isinstance(config_section, dict):
                raise ValueError("'config' section must be an object.")
            sufficiency_threshold = config_section.get("sufficiency_threshold", 0.8)
            if not isinstance(sufficiency_threshold, (int, float)):
                raise ValueError("'config.sufficiency_threshold' must be a number.")
            if not 0 <= float(sufficiency_threshold) <= 1:
                raise ValueError(
                    "'config.sufficiency_threshold' must be between 0 and 1."
                )

            local_answer_task_types = config_section.get("local_answer_task_types", [])
            if not isinstance(local_answer_task_types, list):
                raise ValueError("'config.local_answer_task_types' must be a list.")
            if not all(isinstance(item, str) for item in local_answer_task_types):
                raise ValueError(
                    "'config.local_answer_task_types' entries must be strings."
                )
            unsupported_task_types = sorted(
                set(local_answer_task_types) - cls.SUPPORTED_TASK_TYPES
            )
            if unsupported_task_types:
                raise ValueError(
                    "Unsupported task type(s) in "
                    "'config.local_answer_task_types': "
                    + ", ".join(unsupported_task_types)
                )

            routing_quality_floor = config_section.get("routing_quality_floor", 0.9)
            if not isinstance(routing_quality_floor, (int, float)):
                raise ValueError("'config.routing_quality_floor' must be a number.")
            if not 0 <= float(routing_quality_floor) <= 1:
                raise ValueError(
                    "'config.routing_quality_floor' must be between 0 and 1."
                )

            hybrid_alpha = config_section.get("hybrid_alpha", 0.2)
            if not isinstance(hybrid_alpha, (int, float)) or isinstance(
                hybrid_alpha, bool
            ):
                raise ValueError("'config.hybrid_alpha' must be a number.")
            if not 0.0 <= float(hybrid_alpha) <= 1.0:
                raise ValueError("'config.hybrid_alpha' must be between 0 and 1.")

            reranker_top_k_multiplier = config_section.get(
                "reranker_top_k_multiplier", 5
            )
            if not isinstance(reranker_top_k_multiplier, int) or isinstance(
                reranker_top_k_multiplier, bool
            ):
                raise ValueError(
                    "'config.reranker_top_k_multiplier' must be an integer."
                )
            if not 1 <= reranker_top_k_multiplier <= 100:
                raise ValueError(
                    "'config.reranker_top_k_multiplier' must be between 1 and 100."
                )

            vector_backend = config_section.get("vector_backend", "lancedb")
            if not isinstance(vector_backend, str) or vector_backend not in (
                "faiss",
                "lancedb",
            ):
                raise ValueError(
                    "'config.vector_backend' must be 'faiss' or 'lancedb'."
                )

            entity_source = config_section.get("entity_source", "disk")
            if not isinstance(entity_source, str) or entity_source not in (
                "disk",
                "stored",
            ):
                raise ValueError("'config.entity_source' must be 'disk' or 'stored'.")

            # Pre-flight assessment configuration
            preflight_config = cls._parse_preflight_section(data.get("preflight"))

            if not models:
                models = cls.default().models

            return cls(
                models=models,
                embedding_models=embedding_models,
                prose_embedding_models=prose_embedding_models,
                reranking_models=reranking_models,
                sufficiency_threshold=float(sufficiency_threshold),
                local_answer_task_types=list(dict.fromkeys(local_answer_task_types)),
                routing_quality_floor=float(routing_quality_floor),
                hybrid_alpha=hybrid_alpha,
                reranker_top_k_multiplier=reranker_top_k_multiplier,
                vector_backend=vector_backend,
                entity_source=entity_source,
                preflight=preflight_config,
            )
        except Exception as e:
            message = f"Failed to load config from {path}: {e}"
            # RF-001-D004: NEVER swallow errors silently.
            # If a config file is present but malformed, fail explicitly.
            raise ValueError(message) from e

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

    @classmethod
    def _parse_preflight_section(
        cls,
        raw: Any,
    ) -> PreflightConfig:
        """Parse the optional ``preflight:`` YAML section."""
        if raw is None:
            return PreflightConfig()
        if not isinstance(raw, dict):
            raise ValueError("'preflight' section must be an object.")

        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("'preflight.enabled' must be a boolean.")

        min_score = raw.get("min_score", 0.0)
        if not isinstance(min_score, (int, float)):
            raise ValueError("'preflight.min_score' must be a number.")
        if not 0.0 <= float(min_score) <= 1.0:
            raise ValueError("'preflight.min_score' must be between 0 and 1.")

        weights = dict(_DEFAULT_PREFLIGHT_WEIGHTS)
        raw_weights = raw.get("weights")
        if raw_weights is not None:
            if not isinstance(raw_weights, dict):
                raise ValueError("'preflight.weights' must be an object.")
            for key, value in raw_weights.items():
                if key not in _DEFAULT_PREFLIGHT_WEIGHTS:
                    raise ValueError(
                        f"Unknown preflight weight key: '{key}'. "
                        f"Valid keys: {', '.join(sorted(_DEFAULT_PREFLIGHT_WEIGHTS))}"
                    )
                if not isinstance(value, (int, float)):
                    raise ValueError(f"'preflight.weights.{key}' must be a number.")
                weights[key] = float(value)

        return PreflightConfig(
            enabled=enabled,
            min_score=float(min_score),
            weights=weights,
        )

    @staticmethod
    def _handle_validation_issue(message: str, *, strict: bool) -> None:
        """Warn or raise on configuration validation issues."""
        if strict:
            raise ValueError(message)
        logger.warning(message)
