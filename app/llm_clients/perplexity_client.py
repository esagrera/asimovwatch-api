import os
from functools import lru_cache
from typing import Optional
from openai import OpenAI


@lru_cache(maxsize=1)
def _get_perplexity_client():
    api_key = os.getenv("PERPLEXITY_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("PERPLEXITY_API_KEY no configurada")
    return OpenAI(api_key=api_key, base_url="https://api.perplexity.ai")


def call_perplexity_client(
    model: str,
    prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout_secs: Optional[int] = 60,
    **kwargs,
) -> str:
    client = _get_perplexity_client()
    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout_secs,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        raise RuntimeError(f"Perplexity request failed: {e}") from e

    if not response.choices:
        raise RuntimeError("Perplexity ha retornat una resposta buida")

    final_text = response.choices[0].message.content.strip() if response.choices[0].message.content else ""
    if not final_text:
        raise RuntimeError("Perplexity ha retornat contingut buit")

    return final_text