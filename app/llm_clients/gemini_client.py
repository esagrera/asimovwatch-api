import os
from functools import lru_cache
from typing import Optional

from google import genai
from google.genai import types


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