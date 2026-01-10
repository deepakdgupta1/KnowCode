"""Agent module for KnowCode."""

import os
from typing import Optional

from openai import OpenAI, OpenAIError

from knowcode.service import KnowCodeService


class Agent:
    """Agent that answers questions about the codebase using an LLM."""

    def __init__(self, service: KnowCodeService, model: str = "gpt-4o") -> None:
        """Initialize the agent.
        
        Args:
            service: KnowCodeService instance for context retrieval.
            model: OpenAI model to use.
        """
        self.service = service
        self.model = model
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
             # We allow initialization without key, but answer() will fail if not provided later or found.
             # This is to allow CLI to start up even if key is missing (until 'ask' is actually called).
             pass
        self.client = OpenAI(api_key=api_key) if api_key else None

    def answer(self, query: str) -> str:
        """Answer a question about the codebase.

        Args:
            query: User's question.

        Returns:
            The agent's answer.
            
        Raises:
            ValueError: If GOOGLE_API_KEY is not set.
            OpenAIError: If the API call fails.
        """
        if not self.client:
            api_key = os.environ.get("GOOGLE_API_KEY")
            if not api_key:
                raise ValueError("GOOGLE_API_KEY environment variable is not set.")
            self.client = OpenAI(api_key=api_key)

        # 1. Retrieve knowledge
        # Simple strategy: Search for keywords in the query to find relevant entities
        # then get context for the top match.
        # Ideally, we would have a vector store search here. For MVP, we use the graph search.
        search_results = self.service.search(query)
        
        context_str = ""
        if search_results:
            # Get up to 3 relevant entities
            top_entities = search_results[:3]
            context_parts = []
            for entity in top_entities:
                try:
                    # Limit tokens for each to fit in context window comfortably
                    bundle = self.service.get_context(entity.id, max_tokens=1500)
                    context_parts.append(bundle["context_text"])
                except Exception:
                    continue
            
            if context_parts:
                context_str = "\n\n".join(context_parts)
        else:
            context_str = "No specific entities found in the codebase matching the query terms."

        # 2. Construct Prompt
        system_prompt = (
            "You are an expert software engineering assistant. "
            "You have access to context from the user's codebase. "
            "Answer the user's question based strictly on the provided context. "
            "If the context doesn't contain the answer, say so, but try to be helpful based on the visible code structures."
        )
        
        user_message = f"Context:\n{context_str}\n\nQuestion: {query}"

        # 3. Call LLM
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.0,
        )

        return response.choices[0].message.content or "No response from LLM."
