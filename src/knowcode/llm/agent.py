"""Agent module for KnowCode."""

import os
from typing import Optional, Union, Any
import time

from google import genai
from google.genai import types
from google.api_core.exceptions import ResourceExhausted
import openai

from knowcode.service import KnowCodeService
from knowcode.config import AppConfig, ModelConfig
from knowcode.llm.rate_limiter import RateLimiter
from knowcode.llm.query_classifier import classify_query, get_prompt_template
from knowcode.data_models import TaskType


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
            client = genai.Client(api_key=api_key)
        else:
            # Assume OpenAI-compatible (OpenAI, OpenRouter, Mistral, etc.)
            base_url = None
            if config.provider == "mistralai" or "openrouter" in config.provider:
                base_url = "https://openrouter.ai/api/v1"
            
            client = openai.OpenAI(
                api_key=api_key,
                base_url=base_url
            )
            
        self.clients[client_key] = client
        return client

    def answer(self, query: str) -> str:
        """Answer a question about the codebase.

        Args:
            query: User's question.

        Returns:
            The agent's answer.
            
        Raises:
            ValueError: If no API keys are set.
            Exception: If all models fail.
        """
        # 0. Classify query to determine task type and optimal prompt
        task_type, confidence = classify_query(query)
        print(f"  📋 Query type: {task_type.value} (confidence: {confidence:.0%})")
        
        # 1. Retrieve knowledge
        context_str = ""
        context_parts = []
        
        # Strategy A: Semantic Search (Preferred)
        # Check if index exists by peeking at the service's indexer path logic relative to store
        index_path = self.service.store_path.parent / "knowcode_index"
        if index_path.exists():
            try:
                print("  🔍 Using Semantic Search...")
                search_engine = self.service.get_search_engine()
                # Use a reasonable limit (e.g., 5 results)
                results = search_engine.search(query, limit=5)
                
                # Deduplicate based on content to save tokens
                seen_content = set()
                
                for res in results:
                    # SearchEngine returns SearchResult objects with score, entity, chunk
                    # We want the full context bundle for the entity if it's a high match
                    # or just the chunk text? 
                    # Let's try to get the full entity context for the top match, 
                    # and maybe just chunks for others to save space?
                    # For simplicity, let's grab context bundles for unique top entities.
                    
                    # Note: Agent doesn't import SearchResult, so we rely on duck typing or service methods if available.
                    # Service doesn't expose semantic search directly nicely for Agent yet without importing engine.
                    # Actually, KnowCodeService doesn't expose 'semantic_search' method, only 'get_search_engine'.
                    
                    # We can use the chunk text directly from the result
                    text = res.chunk.text
                    if text not in seen_content:
                        context_parts.append(f"Matching Code (Score: {res.score:.2f}):\n{text}")
                        seen_content.add(text)
                        
            except Exception as e:
                print(f"  ⚠️ Semantic search failed: {e}")

        # Strategy B: Lexical Search (Fallback / Supplement)
        # If semantic search yielded nothing (or index missing), try to find entities by name
        if not context_parts:
             print("  🔍 Using Lexical Search...")
             
             # 1. Try full query (unlikely to work but cheap)
             entities = self.service.search(query)
             
             # 2. Keyphrase extraction (Simple heuristic: finding "Words" that look like identifiers)
             if not entities:
                 import re
                 # Look for words like GraphBuilder, analyze, my_function (at least 3 chars)
                 # We exclude common instructions like "How", "does", "work" via a naive stoplist implicitly 
                 # by looking for specific code-like patterns or CapitalizedWords.
                 
                 # Pattern: 
                 # - CamelCase (GraphBuilder)
                 # - snake_case (build_from_directory, but avoid "does_it_work")
                 # - dot.separated (knowcode.cli)
                 potential_tokens = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_.]+\b', query)
                 
                 # Filter common stopwords (very basic)
                 stopwords = {"how", "what", "where", "when", "why", "who", "does", "is", "are", "can", "will", "the", "a", "an", "in", "on", "at", "for", "to", "of", "and", "or"}
                 keywords = [t for t in potential_tokens if t.lower() not in stopwords and len(t) > 3]
                 
                 for kw in keywords:
                     found = self.service.search(kw)
                     if found:
                         entities.extend(found)

             # Deduplicate entities by ID
             unique_entities = {e['id']: e for e in entities}.values()
             
             # 3. Retrieve Context for found entities
             for entity_meta in list(unique_entities)[:3]: # Limit to top 3
                try:
                    bundle = self.service.get_context(entity_meta['id'], max_tokens=2000)
                    context_parts.append(bundle["context_text"])
                except Exception:
                    continue

        if context_parts:
            context_str = "\n\n".join(context_parts)
        else:
            context_str = "No specific entities found in the codebase matching the query terms. Answer based on general software engineering principles if possible."

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
            
            except ResourceExhausted as e:
                 print(f"  ⚠️ Rate limit exceeded (Server) for {model_config.name}. Switching...")
                 last_error = e
                 continue
            except Exception as e:
                 print(f"  ❌ Error with {model_config.name}: {e}")
                 last_error = e
                 continue

        if last_error:
            raise last_error
        
        raise ValueError("No valid configuration found or all models skipped (check API keys or limits).")

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
        # 1. Classify query
        task_type, confidence = classify_query(query)
        print(f"  📋 Query type: {task_type.value} (confidence: {confidence:.0%})")
        
        # 2. Get context with task-specific prioritization
        context_parts = []
        sufficiency_scores = []
        
        # Try semantic search first
        index_path = self.service.store_path.parent / "knowcode_index"
        if index_path.exists():
            try:
                print("  🔍 Semantic search...")
                search_engine = self.service.get_search_engine()
                results = search_engine.search(query, limit=3)
                
                for res in results[:3]:
                    # Get task-specific context
                    bundle = self.service.get_context(
                        res.entity_id, 
                        max_tokens=2000,
                        task_type=task_type
                    )
                    context_parts.append(bundle["context_text"])
                    sufficiency_scores.append(bundle.get("sufficiency_score", 0.0))
            except Exception as e:
                print(f"  ⚠️ Semantic search failed: {e}")
        
        # Fallback to lexical search
        if not context_parts:
            import re
            potential_tokens = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_.]+\b', query)
            stopwords = {"how", "what", "where", "when", "why", "who", "does", "is", "are", "can", "will", "the", "a", "an", "in", "on", "at", "for", "to", "of", "and", "or"}
            keywords = [t for t in potential_tokens if t.lower() not in stopwords and len(t) > 3]
            
            for kw in keywords[:3]:
                entities = self.service.search(kw)
                if entities:
                    bundle = self.service.get_context(
                        entities[0]['id'],
                        max_tokens=2000,
                        task_type=task_type
                    )
                    context_parts.append(bundle["context_text"])
                    sufficiency_scores.append(bundle.get("sufficiency_score", 0.0))
                    break
        
        # Calculate overall sufficiency
        avg_sufficiency = sum(sufficiency_scores) / len(sufficiency_scores) if sufficiency_scores else 0.0
        context_str = "\n\n---\n\n".join(context_parts) if context_parts else ""
        
        threshold = self.config.sufficiency_threshold
        print(f"  📊 Sufficiency: {avg_sufficiency:.0%} (threshold: {threshold:.0%})")
        
        # 3. Decide: local answer or LLM
        if not force_llm and avg_sufficiency >= threshold and context_str:
            # Local-first: sufficient context found
            print("  ✅ Answering locally (sufficient context)")
            
            local_answer = self._format_local_answer(query, task_type, context_str)
            
            return {
                "answer": local_answer,
                "source": "local",
                "task_type": task_type.value,
                "sufficiency_score": avg_sufficiency,
                "context": context_str,
                "llm_tokens_saved": len(context_str.split()),  # Rough estimate
            }
        else:
            # Need LLM
            print(f"  🤖 Calling LLM (sufficiency below threshold or forced)")
            
            llm_answer = self.answer(query)
            
            return {
                "answer": llm_answer,
                "source": "llm",
                "task_type": task_type.value,
                "sufficiency_score": avg_sufficiency,
                "context": context_str,
                "llm_tokens_saved": 0,
            }

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

