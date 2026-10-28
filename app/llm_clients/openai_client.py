import os
import time
from functools import lru_cache
from typing import Optional

from openai import OpenAI

FALLBACK_MODELS = [
    {"name": "gpt-6-luna", "stable": True},
    {"name": "gpt-5.4-mini", "stable": True},
    {"name": "gpt-5-mini", "stable": True},
]

_MODELS_CACHE = {"data": None, "ts": 0}
_CACHE_TTL_SECS = 3600

def list_available_models():
    now = time.time()
    if _MODELS_CACHE["data"] is not None and (now - _MODELS_CACHE["ts"]) < _CACHE_TTL_SECS:
        return _MODELS_CACHE["data"]

    models = []
    try:
        client = _get_openai_client()
        response = client.models.list()
        for m in response.data:
            model_id = getattr(m, "id", "") or ""
            if model_id.startswith("gpt-"):
                is_stable = "preview" not in model_id and "exp" not in model_id
                models.append({"name": model_id, "stable": is_stable})
        if not models:
            raise RuntimeError("Cap model trobat a l'API d'OpenAI")
    except Exception:
        models = FALLBACK_MODELS

    _MODELS_CACHE["data"] = models
    _MODELS_CACHE["ts"] = now
    return models

@lru_cache(maxsize=1)
def _get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY no configurada")
    return OpenAI(api_key=api_key)

def _is_reasoning_family(model: str) -> bool:
    m = (model or "").strip().lower()
    return m.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))

def call_openai_client(
    model: str,
    prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout_secs: Optional[int] = 60,
    **kwargs,
) -> str:
    client = _get_openai_client()

    params = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt}
        ],
    }
    if _is_reasoning_family(model):
        # Els tokens de raonament compten dins max_completion_tokens
        params["max_completion_tokens"] = max(max_tokens, 1024)
    else:
        params["max_tokens"] = max_tokens
        params["temperature"] = temperature

    try:
        response = client.chat.completions.create(**params)
    except Exception as e:
        raise RuntimeError(f"OpenAI request failed: {str(e)}") from e

    if not response.choices:
        raise RuntimeError("OpenAI ha retornat una resposta buida")

    final_text = response.choices[0].message.content.strip() if response.choices[0].message.content else ""

    if not final_text:
        if getattr(response.choices[0], "finish_reason", None) == "length":
            raise RuntimeError(
                "OpenAI ha retornat contingut buit (finish_reason=length; "
                "possible consum de tokens de raonament)"
            )
        raise RuntimeError("OpenAI ha retornat contingut buit")

    return final_text