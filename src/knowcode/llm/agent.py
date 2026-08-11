"""Agent module for KnowCode."""

import os
from typing import Any, Optional

from knowcode.service import KnowCodeService
from knowcode.config import AppConfig, ModelConfig
from knowcode.llm.rate_limiter import RateLimiter
from knowcode.llm.query_classifier import get_prompt_template
from knowcode.data_models import TaskType


def _create_google_client(api_key: str) -> Any:
    """Create a Google GenAI client with an actionable dependency hint."""
    try:
        from google import genai
    except ImportError as exc:  # pragma: no cover - depends on environment extras
        raise ImportError("Install knowcode[llm] to use 'knowcode ask'.") from exc
    return genai.Client(api_key=api_key)


def _create_openai_client(api_key: str, base_url: str | None) -> Any:
    """Create an OpenAI-compatible client with an actionable dependency hint."""
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - depends on environment extras
        raise ImportError("Install knowcode[llm] to use 'knowcode ask'.") from exc
    return OpenAI(api_key=api_key, base_url=base_url)


class Agent:
    """Agent that answers questions about the codebase using an LLM (Gemini or OpenAI/OpenRouter)."""

    def __init__(self, service: KnowCodeService, config: AppConfig) -> None:
        """Initialize the agent.
        
        Args:
            service: KnowCodeService instance for context retrieval.
            config: Application configuration containing model priorities.
        """
        self.service = service
        self.config = config
        self.clients: dict[str, Any] = {}
        self.rate_limiter = RateLimiter()

    def _get_client(self, config: ModelConfig) -> Optional[Any]:
        """Get or create client for a specific model configuration."""
        client_key = f"{config.provider}_{config.api_key_env}"
        if client_key in self.clients:
            return self.clients[client_key]
            
        api_key = os.environ.get(config.api_key_env)
        if not api_key:
            return None
            
        if config.provider == "google":
            client = _create_google_client(api_key)
        else:
            # Assume OpenAI-compatible (OpenAI, OpenRouter, Mistral, etc.)
            base_url = None
            if config.provider == "mistralai" or "openrouter" in config.provider:
                base_url = "https://openrouter.ai/api/v1"
            elif config.provider in ("glm", "z-ai"):
                base_url = os.environ.get("GLM_BASE_URL")
            
            client = _create_openai_client(api_key=api_key, base_url=base_url)
            
        self.clients[client_key] = client
        return client

    def answer(
        self,
        query: str,
        *,
        retrieval: dict[str, Any] | None = None,
    ) -> str:
        """Answer a question about the codebase.

        Args:
            query: User's question.
            retrieval: Optional retrieval result to reuse. When omitted, the
                agent retrieves a minimal response with the task metadata
                required to select the prompt.

        Returns:
            The agent's answer.
            
        Raises:
            ValueError: If no API keys are set.
            Exception: If all models fail.
        """
        if retrieval is None:
            retrieval = self._retrieve_context(query)
        task_type = TaskType(retrieval.get("task_type", TaskType.GENERAL.value))
        confidence = float(retrieval.get("task_confidence", 0.0))
        print(f"  📋 Query type: {task_type.value} (confidence: {confidence:.0%})")

        context_str = retrieval.get("context_text", "")
        if not context_str:
            context_str = (
                "No specific entities found in the codebase matching the query terms. "
                "Answer based on general software engineering principles if possible."
            )

        # 2. Construct Prompt with task-specific system instructions
        system_instructions = get_prompt_template(task_type)
        
        prompt = f"{system_instructions}\n\nContext:\n{context_str}\n\nQuestion: {query}"

        # 3. Call LLM with Failover
        last_error = None
        
        for model_config in self.config.models:
            print(f"🤖 Trying model: {model_config.name} ({model_config.provider})...") 
            
            # Check Rate Limit (Client-side)
            if not self.rate_limiter.check_availability(model_config):
                # Warning already printed by check_availability
                continue

            client = self._get_client(model_config)
            
            if not client:
                print(f"  ⚠️ Skipping {model_config.name}: {model_config.api_key_env} not set.")
                continue

            try:
                response_text = ""
                if model_config.provider == "google":
                    response = client.models.generate_content(
                        model=model_config.name,
                        contents=prompt,
                    )
                    response_text = response.text or "No response from LLM."
                else:
                    # OpenAI / OpenRouter style
                    extra_headers = {}
                    if "openrouter" in model_config.provider or model_config.provider == "mistralai":
                         extra_headers = {
                             "HTTP-Referer": "https://github.com/deepakdgupta1/KnowCode",
                             "X-Title": "KnowCode",
                         }
                         
                    chat_completion = client.chat.completions.create(
                        model=model_config.name,
                        messages=[
                            {"role": "user", "content": prompt}
                        ],
                        extra_headers=extra_headers
                    )
                    response_text = chat_completion.choices[0].message.content or "No response from LLM."
                
                # Success! Record usage and return
                self.rate_limiter.record_usage(model_config.name)
                return response_text
            
            except Exception as e:
                if e.__class__.__name__ == "ResourceExhausted":
                    print(f"  ⚠️ Rate limit exceeded (Server) for {model_config.name}. Switching...")
                    last_error = e
                    continue
                print(f"  ❌ Error with {model_config.name}: {e}")
                last_error = e
                continue

        if last_error:
            raise last_error
        
        raise ValueError("No valid configuration found or all models skipped (check API keys or limits).")

    def _retrieve_context(
        self,
        query: str,
        *,
        max_tokens: int = 1500,
        limit_entities: int = 1,
        expand_deps: bool = False,
        verbosity: str = "minimal",
    ) -> dict[str, Any]:
        """Retrieve the minimal projection plus metadata used by the agent."""
        return self.service.retrieve_context_for_query(
            query,
            max_tokens=max_tokens,
            limit_entities=limit_entities,
            expand_deps=expand_deps,
            verbosity=verbosity,
            include_metadata=True,
        )

    def smart_answer(
        self,
        query: str,
        force_llm: bool = False,
    ) -> dict[str, Any]:
        """Smart answer with local-first mode.
        
        If context sufficiency >= threshold, returns local answer without LLM.
        Only calls external LLM when context is insufficient.
        
        Args:
            query: User's question.
            force_llm: If True, always use LLM regardless of sufficiency.
            
        Returns:
            Dict with:
                - answer: The response text
                - source: "local" or "llm"
                - task_type: Detected query type
                - sufficiency_score: Context quality score
                - context: The retrieved context
        """
        retrieval = self._retrieve_context(query)
        avg_sufficiency = float(retrieval.get("sufficiency_score", 0.0))
        context_str = retrieval.get("context_text", "")
        initial_task_type = TaskType(
            retrieval.get("task_type", TaskType.GENERAL.value)
        )
        can_route_locally = (
            initial_task_type.value in self.config.local_answer_task_types
        )

        threshold = self.config.sufficiency_threshold

        if (
            not force_llm
            and can_route_locally
            and (avg_sufficiency < threshold or not context_str)
        ):
            print("  🔎 Expanding local retrieval breadth...")
            retrieval = self._retrieve_context(
                query,
                max_tokens=3000,
                limit_entities=3,
                expand_deps=True,
            )
            avg_sufficiency = float(retrieval.get("sufficiency_score", 0.0))
            context_str = retrieval.get("context_text", "")

        if (
            not force_llm
            and can_route_locally
            and (avg_sufficiency < threshold or not context_str)
        ):
            print("  🔎 Requesting standard retrieval detail...")
            retrieval = self._retrieve_context(
                query,
                max_tokens=3000,
                limit_entities=3,
                expand_deps=True,
                verbosity="standard",
            )
            avg_sufficiency = float(retrieval.get("sufficiency_score", 0.0))
            context_str = retrieval.get("context_text", "")

        task_type = TaskType(retrieval.get("task_type", TaskType.GENERAL.value))
        routing_policy_allowed = (
            task_type.value in self.config.local_answer_task_types
        )
        confidence = float(retrieval.get("task_confidence", 0.0))
        print(f"  📋 Query type: {task_type.value} (confidence: {confidence:.0%})")
        print(f"  📊 Sufficiency: {avg_sufficiency:.0%} (threshold: {threshold:.0%})")
        
        # 3. Decide: local answer or LLM
        if (
            not force_llm
            and routing_policy_allowed
            and avg_sufficiency >= threshold
            and context_str
        ):
            # Local-first: sufficient context found
            print("  ✅ Answering locally (sufficient context)")
            
            local_answer = self._format_local_answer(query, task_type, context_str)
            
            res = {
                "answer": local_answer,
                "source": "local",
                "task_type": task_type.value,
                "sufficiency_score": avg_sufficiency,
                "context": context_str,
                "llm_tokens_saved": len(context_str.split()),  # Rough estimate
                "routing_policy_allowed": routing_policy_allowed,
            }
        else:
            # Need LLM
            print("  🤖 Calling LLM (sufficiency below threshold or forced)")
            
            llm_answer = self.answer(query, retrieval=retrieval)
            
            res = {
                "answer": llm_answer,
                "source": "llm",
                "task_type": task_type.value,
                "sufficiency_score": avg_sufficiency,
                "context": context_str,
                "llm_tokens_saved": 0,
                "routing_policy_allowed": routing_policy_allowed,
            }

        try:
            from knowcode.telemetry import log_event
            log_event(
                self.service.store_path,
                {
                    "event_type": "agent_decision",
                    "query": query,
                    "source": res["source"],
                    "sufficiency_score": avg_sufficiency,
                    "threshold": threshold,
                    "force_llm": force_llm,
                    "task_type": task_type.value,
                    "routing_policy_allowed": routing_policy_allowed,
                    "llm_tokens_saved": res["llm_tokens_saved"],
                }
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Ignored exception: %s", e)

        return res


    def _format_local_answer(
        self,
        query: str,
        task_type: TaskType,
        context: str,
    ) -> str:
        """Format a local answer without LLM.
        
        For simple queries like 'locate', we can answer directly from context.
        For more complex queries, we format the context nicely.
        """
        if task_type == TaskType.LOCATE:
            # Extract locations from context
            return f"**Found in codebase:**\n\n{context}"
        
        elif task_type == TaskType.EXPLAIN:
            return f"**Based on the codebase context:**\n\n{context}\n\n*Note: This is a local answer based on retrieved context. For more detailed explanations, use `force_llm=True`.*"
        
        else:
            # General format
            return f"**Relevant context from codebase:**\n\n{context}\n\n*Local answer based on high-confidence context match. Use `force_llm=True` for LLM-enhanced responses.*"
