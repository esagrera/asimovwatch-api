import importlib
import logging
from typing import Any

from app.llm_config import SUPPORTED_PROVIDERS, PROVIDER_CLIENT_MAP

logger = logging.getLogger(__name__)


def get_supported_providers() -> list[str]:
    return sorted(SUPPORTED_PROVIDERS)


def _resolve_provider_callable(provider: str):
    provider = (provider or "").strip().lower()

    if provider not in PROVIDER_CLIENT_MAP:
        raise ValueError(
            f"provider no suportat: {provider}. "
            f"Disponibles: {', '.join(get_supported_providers())}"
        )

    module_path, func_name = PROVIDER_CLIENT_MAP[provider]
    module = importlib.import_module(module_path)
    return getattr(module, func_name)


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