# app/llm_clients/nvidia_client.py
import os
import time
from functools import lru_cache
from typing import Optional

from openai import OpenAI

# Model per defecte si l'API de NVIDIA no respon o si list_available_models()
# no pot recuperar el catàleg real. Coincideix amb el model triat per a les
# primeres proves manuals (veure decisions_v1.md / bihp-comparativa).
DEFAULT_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"

FALLBACK_MODELS = [
    {"name": DEFAULT_MODEL, "stable": True},
]

_MODELS_CACHE = {"data": None, "ts": 0}
_CACHE_TTL_SECS = 3600


@lru_cache(maxsize=1)
def _get_nvidia_client() -> OpenAI:
    api_key = os.getenv("NVIDIA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("NVIDIA_API_KEY no configurada")

    return OpenAI(
        api_key=api_key,
        base_url="https://integrate.api.nvidia.com/v1",
    )


def list_available_models():
    """
    Llista de models disponibles per al provider NVIDIA.

    Segueix el mateix patró que list_available_models() a openai_client.py,
    gemini_client.py i claude_client.py: intenta consultar el catàleg real
    via l'API del provider (GET /v1/models a integrate.api.nvidia.com, que
    és compatible amb l'API d'OpenAI) i només cau al catàleg estàtic
    FALLBACK_MODELS si la crida falla (clau no configurada, error de xarxa,
    resposta buida, etc.).

    El model nvidia/nemotron-3.5-lightning-30b-a3b es marca com a "default"
    dins la llista retornada; no és l'únic model disponible, és el que es
    proposa per defecte a la UI mentre no hi hagi una selecció explícita
    feta des de l'admin (registry de public.llm_provider_models).
    """
    now = time.time()
    if _MODELS_CACHE["data"] is not None and (now - _MODELS_CACHE["ts"]) < _CACHE_TTL_SECS:
        return _MODELS_CACHE["data"]

    models = []
    try:
        client = _get_nvidia_client()
        response = client.models.list()
        for m in response.data:
            model_id = getattr(m, "id", "") or ""
            if not model_id:
                continue
            models.append({
                "name": model_id,
                "stable": True,
                "is_default": model_id == DEFAULT_MODEL,
            })
        if not models:
            raise RuntimeError("Cap model trobat a l'API de NVIDIA")
    except Exception:
        models = [
            {**m, "is_default": m["name"] == DEFAULT_MODEL}
            for m in FALLBACK_MODELS
        ]

    _MODELS_CACHE["data"] = models
    _MODELS_CACHE["ts"] = now
    return models


def call_nvidia_client(
    model: str,
    prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout_secs: Optional[int] = 60,
    **kwargs,
) -> str:
    """
    Crida simple (no streaming) a un model NVIDIA via l'API OpenAI-compatible
    de NVIDIA NIM (integrate.api.nvidia.com).

    Manté la mateixa signatura que call_openai_client / call_claude_client /
    call_gemini_client / call_perplexity_client perquè el dispatcher genèric
    (app/llm_clients/__init__.py) el pugui resoldre via PROVIDER_CLIENT_MAP
    sense necessitar cap cas especial.

    Pensat inicialment només per a proves manuals des de l'admin
    (/admin/llm/test), no per a fases o prompts en producció.
    """
    client = _get_nvidia_client()

    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "user", "content": prompt}
            ],
        )
    except Exception as e:
        raise RuntimeError(f"NVIDIA request failed: {str(e)}") from e

    if not response.choices:
        raise RuntimeError("NVIDIA ha retornat una resposta buida (choices buit)")

    final_text = (response.choices[0].message.content or "").strip()

    if not final_text:
        raise RuntimeError("NVIDIA ha retornat contingut buit")

    return final_text
