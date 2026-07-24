import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_supported_providers() -> list[str]:
    return ["claude", "gemini", "openai"]


def _resolve_provider_callable(provider: str):
    provider = (provider or "").strip().lower()

    if provider == "gemini":
        from app.llm_clients.gemini_client import call_gemini_client
        return call_gemini_client

    if provider == "claude":
        from app.llm_clients.claude_client import call_claude_client
        return call_claude_client

    if provider == "openai":
        from app.llm_clients.openai_client import call_openai_client
        return call_openai_client

    raise ValueError(
        f"provider no suportat: {provider}. "
        f"Disponibles: {', '.join(get_supported_providers())}"
    )


def call_llm(
    provider: str,
    model: str,
    prompt: str,
    **kwargs: Any,
) -> str:
    provider_normalized = (provider or "").strip().lower()

    if not provider_normalized:
        raise ValueError("provider buit o no informat")

    if not model or not model.strip():
        raise ValueError(f"model no configurat per provider '{provider_normalized}'")

    if not prompt or not prompt.strip():
        raise ValueError("prompt buit")

    logger.info(
        "LLM dispatch provider=%s model=%s prompt_chars=%s",
        provider_normalized,
        model,
        len(prompt),
    )

    fn = _resolve_provider_callable(provider_normalized)

    try:
        result = fn(model=model, prompt=prompt, **kwargs)
    except Exception as e:
        logger.exception("LLM provider error provider=%s model=%s", provider_normalized, model)
        raise RuntimeError(
            f"Error executant provider '{provider_normalized}' amb model '{model}': {str(e)}"
        ) from e

    if not result or not str(result).strip():
        raise RuntimeError(
            f"Resposta buida retornada per provider '{provider_normalized}' amb model '{model}'"
        )

    return result.strip()