import os
from functools import lru_cache
from typing import Optional

from openai import OpenAI


@lru_cache(maxsize=1)
def _get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY no configurada")
    return OpenAI(api_key=api_key)


def call_openai_client(
    model: str,
    prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout_secs: Optional[int] = 60,
    **kwargs,
) -> str:
    client = _get_openai_client()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as e:
        raise RuntimeError(f"OpenAI request failed: {str(e)}") from e

    if not response.choices:
        raise RuntimeError("OpenAI ha retornat una resposta sense choices")

    text = response.choices[0].message.content
    if not text or not text.strip():
        raise RuntimeError("OpenAI ha retornat una resposta buida")

    return text.strip()