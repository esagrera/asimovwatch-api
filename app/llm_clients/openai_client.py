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
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "user", "content": prompt}
            ],
        )
    except Exception as e:
        raise RuntimeError(f"OpenAI request failed: {str(e)}") from e

    if not response.choices:
        raise RuntimeError("OpenAI ha retornat una resposta buida")

    final_text = response.choices[0].message.content.strip() if response.choices[0].message.content else ""

    if not final_text:
        raise RuntimeError("OpenAI ha retornat contingut buit")

    return final_text