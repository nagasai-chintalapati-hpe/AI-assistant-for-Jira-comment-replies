"""Unified LLM client — GitHub Copilot API or local llama.cpp backend."""

from __future__ import annotations

import logging
import time
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)

_COPILOT_DEFAULT_BASE_URL = "https://api.githubcopilot.com"


class CopilotLLMClient:
    """LLM client supporting Copilot API and local llama.cpp."""

    _FALLBACK_MODELS = ["gpt-4o", "gpt-4o-mini", "gpt-4.1-mini", "gpt-4.1-nano"]

    def __init__(self) -> None:
        self._openai_client = None
        self._local_llm = None
        self._active_backend: str = "none"
        self._current_model: str = settings.copilot.model
        self._exhausted_models: set[str] = set()

        backend = settings.llm.backend.lower()
        if backend == "local":
            self._init_local()
        else:
            self._init_copilot()

    def _init_copilot(self) -> None:
        """Init Copilot client."""
        api_key = settings.copilot.api_key
        if not api_key:
            logger.info(
                "COPILOT_API_KEY not configured — Copilot LLM backend disabled"
            )
            return
        try:
            from openai import OpenAI  # GitHub Copilot SDK (OpenAI-compatible)

            base_url = settings.copilot.base_url or _COPILOT_DEFAULT_BASE_URL
            self._openai_client = OpenAI(
                api_key=api_key,
                base_url=base_url,
            )
            self._active_backend = "copilot"
            logger.info(
                "Copilot LLM client ready (model=%s, base_url=%s)",
                settings.copilot.model,
                base_url,
            )
        except Exception as exc:
            logger.warning("Failed to initialise Copilot LLM client: %s", exc)

    def _init_local(self) -> None:
        """Init local model."""
        model_path = settings.llm.model_path
        if not model_path:
            logger.warning(
                "LLM_MODEL_PATH not set for local backend — falling back to Copilot API"
            )
            self._init_copilot()
            return
        try:
            from llama_cpp import Llama  # type: ignore[import]

            self._local_llm = Llama(
                model_path=model_path,
                n_ctx=settings.llm.n_ctx,
                n_gpu_layers=settings.llm.n_gpu_layers,
                n_threads=settings.llm.n_threads,
                verbose=False,
            )
            self._active_backend = "local"
            logger.info(
                "Local LLM loaded: %s (n_ctx=%d, gpu_layers=%d)",
                model_path,
                settings.llm.n_ctx,
                settings.llm.n_gpu_layers,
            )
        except Exception as exc:
            logger.warning(
                "Failed to load local LLM from '%s': %s — falling back to Copilot API",
                model_path,
                exc,
            )
            self._init_copilot()

    @property
    def enabled(self) -> bool:
        """True when any LLM backend is available."""
        return self._openai_client is not None or self._local_llm is not None

    @property
    def backend(self) -> str:
        """Active backend identifier: ``'copilot'`` | ``'local'`` | ``'none'``."""
        return self._active_backend

    def complete(
        self,
        messages: list[dict],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> Optional[str]:
        """Generate a chat completion. Returns text or None on failure."""
        _max = max_tokens or settings.llm.max_tokens
        _temp = temperature if temperature is not None else settings.llm.temperature

        # Local model first
        if self._local_llm is not None:
            return self._complete_local(messages, _max, _temp)
        if self._openai_client is not None:
            return self._complete_copilot(messages, _max, _temp)
        return None

    def _complete_copilot(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> Optional[str]:
        """Copilot with fallback."""
        # Model priority list
        models_to_try = [self._current_model]
        for m in self._FALLBACK_MODELS:
            if m != self._current_model and m not in self._exhausted_models:
                models_to_try.append(m)

        for model in models_to_try:
            try:
                resp = self._openai_client.chat.completions.create(  # type: ignore[union-attr]
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                result = resp.choices[0].message.content.strip()
                if model != self._current_model:
                    logger.info("Model fallback: %s → %s (success)", self._current_model, model)
                    self._current_model = model
                return result
            except Exception as exc:
                exc_str = str(exc)
                is_rate_limit = "429" in exc_str or "RateLimit" in exc_str
                is_daily_limit = "exceeded" in exc_str.lower() and ("day" in exc_str.lower() or "86400" in exc_str)
                if is_daily_limit:
                    self._exhausted_models.add(model)
                    logger.warning(
                        "Model %s daily limit exhausted — trying next fallback",
                        model,
                    )
                    continue
                if is_rate_limit:
                    # Short rate limit — retry once
                    time.sleep(2)
                    try:
                        resp = self._openai_client.chat.completions.create(  # type: ignore[union-attr]
                            model=model,
                            messages=messages,
                            max_tokens=max_tokens,
                            temperature=temperature,
                        )
                        return resp.choices[0].message.content.strip()
                    except Exception:
                        logger.warning("Model %s rate-limited — trying next fallback", model)
                        continue
                # Other error — fail
                logger.warning("Copilot API completion failed (model=%s): %s", model, exc)
                return None

        logger.error("All LLM models exhausted — no fallback available")
        return None

    def _complete_local(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> Optional[str]:
        try:
            output = self._local_llm.create_chat_completion(  # type: ignore[union-attr]
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return output["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            logger.warning("Local LLM completion failed: %s", exc)
            return None


_singleton: Optional[CopilotLLMClient] = None


def get_llm_client() -> CopilotLLMClient:
    """Return the process-level LLM client singleton."""
    global _singleton
    if _singleton is None:
        _singleton = CopilotLLMClient()
    return _singleton
