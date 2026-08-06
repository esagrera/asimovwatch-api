from typing import Any, Dict, Optional


DEFAULT_CONFIG: Dict[str, Any] = {
    "llm_input_provider": "gemini",
    "llm_input_model": "gemini-3.1-flash-lite",
    "llm_primary_provider": "gemini",
    "llm_primary_model": "gemini-3.1-flash-lite",
    "llm_output_provider": "gemini",
    "llm_output_model": "gemini-3.1-flash-lite",
    "llm_fallback_provider": "gemini",
    "llm_fallback_model": "gemini-3.1-flash-lite",
}


def get_llm_config(
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)

    if config:
        merged.update(
            {
                key: value
                for key, value in config.items()
                if value is not None
            }
        )

    return merged


def pick_llm(
    config: Optional[Dict[str, Any]],
    phase: str,
) -> Dict[str, str]:
    cfg = get_llm_config(config)
    normalized_phase = (phase or "").strip().lower()

    phase_config = {
        "input": {
            "provider": cfg["llm_input_provider"],
            "model": cfg["llm_input_model"],
        },
        "primary": {
            "provider": cfg["llm_primary_provider"],
            "model": cfg["llm_primary_model"],
        },
        "output": {
            "provider": cfg["llm_output_provider"],
            "model": cfg["llm_output_model"],
        },
        "fallback": {
            "provider": cfg["llm_fallback_provider"],
            "model": cfg["llm_fallback_model"],
        },
    }

    if normalized_phase not in phase_config:
        raise ValueError(
            f"Unknown phase: {normalized_phase}"
        )

    return phase_config[normalized_phase]