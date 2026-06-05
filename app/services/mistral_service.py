"""
Mistral Cloud API integration.

Provides:
- chat completion (frontier / efficient tiers)
- structured JSON output
- text embeddings (for ChromaDB)

Uses tenacity for retries; logs token usage for observability.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from mistralai import Mistral
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings

logger = logging.getLogger(__name__)


class MistralService:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.MISTRAL_API_KEY
        if not self.api_key:
            logger.warning("MISTRAL_API_KEY not configured — LLM calls will fail.")
            self.client = None
        else:
            self.client = Mistral(api_key=self.api_key)

        self.model_frontier = settings.MISTRAL_MODEL_FRONTIER
        self.model_efficient = settings.MISTRAL_MODEL_EFFICIENT
        self.embed_model = settings.MISTRAL_EMBED_MODEL

    def _require_client(self):
        if not self.client:
            raise RuntimeError(
                "Mistral client not initialized — set MISTRAL_API_KEY in environment."
            )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def chat(
        self,
        messages: List[Dict[str, str]],
        tier: str = "frontier",  # 'frontier' | 'efficient'
        temperature: float = 0.2,
        max_tokens: int = 1500,
        response_format_json: bool = False,
    ) -> Dict[str, Any]:
        """
        Run a chat completion.
        Returns {content, usage, model}.
        """
        self._require_client()
        model = self.model_frontier if tier == "frontier" else self.model_efficient

        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format_json:
            kwargs["response_format"] = {"type": "json_object"}

        resp = self.client.chat.complete(**kwargs)

        content = resp.choices[0].message.content if resp.choices else ""
        usage = {}
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }
        logger.info(f"Mistral [{model}] usage={usage}")
        return {"content": content, "usage": usage, "model": model}

    def chat_json(
        self,
        messages: List[Dict[str, str]],
        tier: str = "frontier",
        temperature: float = 0.1,
        max_tokens: int = 1500,
    ) -> Dict[str, Any]:
        """Chat completion that forces a JSON response, with safe parsing."""
        result = self.chat(
            messages=messages,
            tier=tier,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format_json=True,
        )
        try:
            return json.loads(result["content"])
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Mistral JSON response: {e}; raw={result['content'][:300]}")
            # Best-effort: extract first JSON object
            text = result["content"]
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except Exception:
                    pass
            return {"_error": "json_parse_failed", "_raw": result["content"]}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def embed(self, texts: List[str]) -> List[List[float]]:
        """Embed a list of texts. Returns list of vectors."""
        self._require_client()
        if not texts:
            return []
        resp = self.client.embeddings.create(
            model=self.embed_model,
            inputs=texts,
        )
        return [item.embedding for item in resp.data]


# Singleton
mistral_service = MistralService()
