import os
import time
from functools import lru_cache
from typing import Optional

from google import genai
from google.genai import types

FALLBACK_MODELS = [
    {"name": "gemini-3.1-flash-lite", "stable": True},
    {"name": "gemini-3.1-pro", "stable": True},
]

_MODELS_CACHE = {"data": None, "ts": 0}
_CACHE_TTL_SECS = 3600

def list_available_models():
    now = time.time()
    if _MODELS_CACHE["data"] is not None and (now - _MODELS_CACHE["ts"]) < _CACHE_TTL_SECS:
        return _MODELS_CACHE["data"]

    models = []
    try:
        client = _get_gemini_client()
        for m in client.models.list():
            name = getattr(m, "name", "") or ""
            short_name = name.split("/")[-1]
            actions = getattr(m, "supported_actions", []) or []
            if short_name.startswith("gemini") and "generateContent" in actions:
                is_stable = "exp" not in short_name and "preview" not in short_name
                models.append({"name": short_name, "stable": is_stable})
        if not models:
            raise RuntimeError("Cap model trobat a l'API de Gemini")
    except Exception:
        models = FALLBACK_MODELS

    _MODELS_CACHE["data"] = models
    _MODELS_CACHE["ts"] = now
    return models

@lru_cache(maxsize=1)
def _get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY no configurada")
    return genai.Client(api_key=api_key)

def call_gemini_client(
    model: str,
    prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout_secs: Optional[int] = 60,
    **kwargs,
) -> str:
    client = _get_gemini_client()

    config = types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_tokens,
    )

    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        )
    except Exception as e:
        raise RuntimeError(f"Gemini request failed: {str(e)}") from e

    text = getattr(response, "text", None)
    if not text or not text.strip():
        raise RuntimeError("Gemini ha retornat una resposta buida")

    return text.strip()