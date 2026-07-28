import os
from functools import lru_cache
from openai import OpenAI

@lru_cache(maxsize=1)
def _get_perplexity_client():
    api_key = os.getenv("PERPLEXITY_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("PERPLEXITY_API_KEY no configurada")
    return OpenAI(api_key=api_key, base_url="https://api.perplexity.ai")

def call_perplexity_client(prompt: str, model: str = "sonar", max_tokens: int = 2048) -> str:
    client = _get_perplexity_client()
    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content.strip()