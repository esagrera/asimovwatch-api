import os
import time
from functools import lru_cache
from typing import Optional

import anthropic

FALLBACK_MODELS = [
    {"name": "claude-3.5-sonnet", "stable": True},
    {"name": "claude-3.5-haiku", "stable": True},
]

_MODELS_CACHE = {"data": None, "ts": 0}
_CACHE_TTL_SECS = 3600

def list_available_models():
    now = time.time()
    if _MODELS_CACHE["data"] is not None and (now - _MODELS_CACHE["ts"]) < _CACHE_TTL_SECS:
        return _MODELS_CACHE["data"]

    models = []
    try:
        client = _get_claude_client()
        response = client.models.list()
        for m in response.data:
            model_id = getattr(m, "id", "") or ""
            if model_id:
                is_stable = "latest" not in model_id
                models.append({"name": model_id, "stable": is_stable})
        if not models:
            raise RuntimeError("Cap model trobat a l'API de Claude")
    except Exception:
        models = FALLBACK_MODELS

    _MODELS_CACHE["data"] = models
    _MODELS_CACHE["ts"] = now
    return models

@lru_cache(maxsize=1)
def _get_claude_client():
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY no configurada")
    return anthropic.Anthropic(api_key=api_key)

def call_claude_client(
    model: str,
    prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout_secs: Optional[int] = 60,
    **kwargs,
) -> str:
    client = _get_claude_client()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise RuntimeError(f"Claude request failed: {str(e)}") from e

    if not response.content:
        raise RuntimeError("Claude ha retornat una resposta buida")

    parts = []
    for block in response.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)

    final_text = "".join(parts).strip()
    if not final_text:
        raise RuntimeError("Claude ha retornat contingut no textual o buit")

    return final_text