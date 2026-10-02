# app/llm_clients/grok_client.py
import os
import time
from functools import lru_cache
from typing import Optional

from openai import OpenAI

# API d'xAI compatible amb l'SDK d'OpenAI (docs.x.ai/developers/quickstart)
XAI_BASE_URL = "https://api.x.ai/v1"

_MODELS_CACHE = {"data": None, "ts": 0}
_CACHE_TTL_SECS = 3600

# Models que no són de text (imatge, vídeo, veu, embeddings): no es mostren al catàleg.
_NON_CHAT_MARKERS = ("imagine", "image", "video", "voice", "embedding")


@lru_cache(maxsize=1)
def _get_grok_client() -> OpenAI:
    api_key = os.getenv("XAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("XAI_API_KEY no configurada")

    return OpenAI(
        api_key=api_key,
        base_url=XAI_BASE_URL,
    )


def list_available_models():
    """
    Llista de models disponibles per al provider Grok (xAI).

    Consulta el catàleg real via GET /v1/models. A diferència de NVIDIA,
    NO hi ha catàleg estàtic de reserva: si la crida falla (p. ex. clau no
    configurada) es propaga l'error perquè l'admin el mostri, i el resultat
    fallit no es guarda a la memòria cau.

    Cap model es marca com a default: l'elecció es fa des de l'admin
    (registry de public.llm_provider_models).
    """
    now = time.time()
    if _MODELS_CACHE["data"] is not None and (now - _MODELS_CACHE["ts"]) < _CACHE_TTL_SECS:
        return _MODELS_CACHE["data"]

    try:
        client = _get_grok_client()
        response = client.models.list()
    except Exception as e:
        raise RuntimeError(f"Grok no ha pogut llistar els models: {str(e)}") from e

    models = []
    for m in response.data:
        model_id = getattr(m, "id", "") or ""
        if not model_id:
            continue
        low = model_id.lower()
        if any(marker in low for marker in _NON_CHAT_MARKERS):
            continue
        is_stable = "preview" not in low and "beta" not in low
        models.append({
            "name": model_id,
            "stable": is_stable,
            "is_default": False,
        })

    if not models:
        raise RuntimeError("Cap model de text trobat a l'API de Grok")

    models.sort(key=lambda item: item["name"])
    _MODELS_CACHE["data"] = models
    _MODELS_CACHE["ts"] = now
    return models


def call_grok_client(
    model: str,
    prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout_secs: Optional[int] = 60,
    **kwargs,
) -> str:
    """
    Crida simple (no streaming) a un model Grok via l'API d'xAI.

    Manté la mateixa signatura que call_openai_client / call_nvidia_client
    perque el dispatcher generic (app/llm_clients/__init__.py) el resolgui
    via PROVIDER_CLIENT_MAP.

    Usa chat.completions, igual que la resta de clients del projecte. xAI el
    documenta com a endpoint legacy/deprecat; la interfície principal és
    /v1/responses. Migrar-hi com a tasca futura.
    """
    client = _get_grok_client()

    request_kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=[
            {"role": "user", "content": prompt}
        ],
    )
    if timeout_secs:
        request_kwargs["timeout"] = timeout_secs

    try:
        response = client.chat.completions.create(**request_kwargs)
    except Exception as e:
        raise RuntimeError(f"Grok request failed: {str(e)}") from e

    if not response.choices:
        raise RuntimeError("Grok ha retornat una resposta buida (choices buit)")

    final_text = (response.choices[0].message.content or "").strip()

    if not final_text:
        raise RuntimeError("Grok ha retornat contingut buit")

    return final_text
