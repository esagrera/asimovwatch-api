from app.llm_clients import call_llm


def call_gemini(
    prompt: str,
    model: str = "gemini-3.1-flash-lite",
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout_secs: int = 60,
    **kwargs,
) -> str:
    return call_llm(
        provider="gemini",
        model=model,
        prompt=prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_secs=timeout_secs,
        **kwargs,
    )