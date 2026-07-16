from typing import Optional, Dict, Any

DEFAULT_CONFIG = {
    "llm_input_provider": "gemini",
    "llm_input_model": "gemini-3.1-flash-lite",
    "llm_primary_provider": "gemini",
    "llm_primary_model": "gemini-3-flash",
    "llm_output_provider": "gemini",
    "llm_output_model": "gemini-3.1-flash-lite",
    "llm_fallback_provider": "gemini",
    "llm_fallback_model": "gemini-3.1-flash-lite",
}

def get_llm_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    if config:
        merged.update({k: v for k, v in config.items() if v is not None})
    return merged

def pick_llm(config: Optional[Dict[str, Any]], phase: str) -> Dict[str, str]:
    cfg = get_llm_config(config)
    phase = phase.lower()

    if phase == "input":
        return {"provider": cfg["llm_input_provider"], "model": cfg["llm_input_model"]}
    if phase == "primary":
        return {"provider": cfg["llm_primary_provider"], "model": cfg["llm_primary_model"]}
    if phase == "output":
        return {"provider": cfg["llm_output_provider"], "model": cfg["llm_output_model"]}
    if phase == "fallback":
        return {"provider": cfg["llm_fallback_provider"], "model": cfg["llm_fallback_model"]}

    raise ValueError(f"Unknown phase: {phase}")