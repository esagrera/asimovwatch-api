import os
from functools import lru_cache
from typing import Optional

import anthropic


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
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
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

    final_text = "\n".join(parts).strip()

    if not final_text:
        raise RuntimeError("Claude ha retornat contingut no textual o buit")

    return final_text