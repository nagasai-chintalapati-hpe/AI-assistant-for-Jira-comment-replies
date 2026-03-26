"""Unified LLM client — GitHub Copilot API or local llama.cpp."""

from __future__ import annotations

import logging
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)

_COPILOT_DEFAULT_BASE_URL = "https://api.githubcopilot.com"


class CopilotLLMClient:
    """LLM client supporting Copilot API and local llama.cpp."""

    def __init__(self) -> None:
        self._openai_client = None
        self._local_llm = None
        self._active_backend: str = "none"

        backend = settings.llm.backend.lower()
        if backend == "local":
            self._init_local()
        else:
            self._init_copilot()

    # Backend initialisation
    def _init_copilot(self) -> None:
        """Set up the GitHub Copilot API client (OpenAI-compatible SDK)."""
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
        """Load a local GGUF model via llama.cpp."""
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

    # Properties

    @property
    def enabled(self) -> bool:
        """True when any backend is available."""
        return self._openai_client is not None or self._local_llm is not None

    @property
    def backend(self) -> str:
        """Active backend identifier: ``'copilot'`` | ``'local'`` | ``'none'``."""
        return self._active_backend

    # Public API

    def complete(
        self,
        messages: list[dict],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> Optional[str]:
        """Generate a chat completion. Returns text or None on failure."""
        _max = max_tokens or settings.llm.max_tokens
        _temp = temperature if temperature is not None else settings.llm.temperature

        # Local model takes priority when loaded (fully on-prem)
        if self._local_llm is not None:
            return self._complete_local(messages, _max, _temp)
        if self._openai_client is not None:
            return self._complete_copilot(messages, _max, _temp)
        return None

    # Private dispatch

    def _complete_copilot(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> Optional[str]:
        try:
            resp = self._openai_client.chat.completions.create(  # type: ignore[union-attr]
                model=settings.copilot.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.warning("Copilot API completion failed: %s", exc)
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


# Module-level singleton

_singleton: Optional[CopilotLLMClient] = None


def get_llm_client() -> CopilotLLMClient:
    """Return the process-level LLM client singleton."""
    global _singleton
    if _singleton is None:
        _singleton = CopilotLLMClient()
    return _singleton
