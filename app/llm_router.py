import logging
from typing import Optional, Dict, Any

from psycopg2.extras import RealDictCursor

from app.llm_clients import call_llm
from app.llm_config import (
    get_router_runtime_config,
    get_phase_config,
    enrich_config_with_key_status,
    resolve_task_llm_config,
    resolve_phase_llm_config,
)

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "llm_input_provider": "gemini",
    "llm_input_model": "gemini-3.1-flash-lite",
    "llm_primary_provider": "gemini",
    "llm_primary_model": "gemini-3.1-flash-lite",
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


def resolve_prompt_phase(phase: str) -> str:
    phase = (phase or "").strip().lower()

    if phase == "fallback":
        return "primary"

    if phase in {"input", "primary", "output"}:
        return phase

    raise ValueError(f"Unknown phase for prompt resolution: {phase}")


def resolve_prompt_key(phase: str) -> str:
    prompt_phase = resolve_prompt_phase(phase)

    mapping = {
        "input": "Input",
        "primary": "Primary",
        "output": "Output",
    }
    return mapping[prompt_phase]


def pick_llm(config: Optional[Dict[str, Any]], phase: str) -> Dict[str, str]:
    cfg = get_llm_config(config)
    phase = (phase or "").strip().lower()

    if phase == "input":
        return {
            "provider": cfg["llm_input_provider"],
            "model": cfg["llm_input_model"],
        }

    if phase == "primary":
        return {
            "provider": cfg["llm_primary_provider"],
            "model": cfg["llm_primary_model"],
        }

    if phase == "output":
        return {
            "provider": cfg["llm_output_provider"],
            "model": cfg["llm_output_model"],
        }

    if phase == "fallback":
        return {
            "provider": cfg["llm_fallback_provider"],
            "model": cfg["llm_fallback_model"],
        }

    raise ValueError(f"Unknown phase: {phase}")


def _phase_row_to_legacy_config(row: dict, phase: str) -> Dict[str, Any]:
    if not row:
        raise RuntimeError(f"No hi ha configuració LLM per a la fase '{phase}'")

    provider = row.get("provider")
    model = row.get("model")
    enabled = bool(row.get("enabled", True))

    if not enabled:
        raise RuntimeError(f"La fase LLM '{phase}' està deshabilitada")

    if not provider:
        raise RuntimeError(f"Provider no configurat per a la fase '{phase}'")

    if not model:
        raise RuntimeError(f"Model no configurat per a la fase '{phase}'")

    return {
        f"llm_{phase}_provider": provider,
        f"llm_{phase}_model": model,
    }


def load_runtime_config(get_connection) -> Dict[str, Any]:
    conn = get_connection()
    try:
        runtime = get_router_runtime_config(conn)

        merged = dict(DEFAULT_CONFIG)

        for phase in ("input", "primary", "output", "fallback"):
            row = runtime.get("phases", {}).get(phase)
            if row and row.get("enabled", True):
                merged[f"llm_{phase}_provider"] = row["provider"]
                merged[f"llm_{phase}_model"] = row["model"]

        return merged
    finally:
        conn.close()


def load_phase_settings(get_connection, phase: str) -> Dict[str, Any]:
    conn = get_connection()
    try:
        row = get_phase_config(conn, phase)
        if not row:
            raise RuntimeError(f"No hi ha configuració LLM per a la fase '{phase}'")

        row = enrich_config_with_key_status(row)

        if not row.get("enabled", True):
            raise RuntimeError(f"La fase LLM '{phase}' està deshabilitada")

        if not row.get("provider"):
            raise RuntimeError(f"Provider no configurat per a la fase '{phase}'")

        if not row.get("model"):
            raise RuntimeError(f"Model no configurat per a la fase '{phase}'")

        if not row.get("key_configured", False):
            raise RuntimeError(
                f"Falta la API key requerida per al provider '{row['provider']}' a la fase '{phase}'"
            )

        return row
    finally:
        conn.close()


def load_prompt_template(get_connection, phase: str) -> str:
    prompt_key = resolve_prompt_key(phase)

    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(
            """
            SELECT key, value, updated_at
            FROM public.prompts
            WHERE key = %s
            LIMIT 1
            """,
            (prompt_key,),
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"Prompt no trobat: {prompt_key}")
        return row["value"]
    finally:
        cur.close()
        conn.close()


def build_prompt(template: str, variables: Optional[Dict[str, Any]] = None) -> str:
    variables = variables or {}
    prompt = template

    for key, value in variables.items():
        placeholder = "{" + str(key) + "}"
        prompt = prompt.replace(placeholder, "" if value is None else str(value))

    return prompt


def call_provider(
    provider: str,
    prompt: str,
    model: str,
    **kwargs,
) -> str:
    return call_llm(
        provider=provider,
        model=model,
        prompt=prompt,
        **kwargs,
    )


def run_llm_phase(
    get_connection,
    phase: str,
    variables: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    phase = (phase or "").strip().lower()

    cfg = get_llm_config(config) if config else load_runtime_config(get_connection)
    llm = pick_llm(cfg, phase)
    phase_settings = load_phase_settings(get_connection, phase)

    template = load_prompt_template(get_connection, phase)
    prompt = build_prompt(template, variables)

    output = call_provider(
        provider=llm["provider"],
        prompt=prompt,
        model=llm["model"],
        temperature=phase_settings.get("temperature", 0.2),
        max_tokens=phase_settings.get("max_tokens", 2048),
        timeout_secs=phase_settings.get("timeout_secs", 60),
    )

    if not output or not output.strip():
        raise RuntimeError(f"Resposta buida a la fase '{phase}'")

    return {
        "phase": phase,
        "prompt_key": resolve_prompt_key(phase),
        "provider": llm["provider"],
        "model": llm["model"],
        "prompt": prompt,
        "output": output,
        "settings": {
            "enabled": phase_settings.get("enabled"),
            "temperature": phase_settings.get("temperature"),
            "max_tokens": phase_settings.get("max_tokens"),
            "timeout_secs": phase_settings.get("timeout_secs"),
        },
    }


def run_llm_with_fallback(
    get_connection,
    primary_phase: str,
    fallback_phase: str = "fallback",
    variables: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = get_llm_config(config) if config else load_runtime_config(get_connection)

    primary_phase_settings = load_phase_settings(get_connection, primary_phase)
    use_fallback = bool(primary_phase_settings.get("use_fallback", True))

    try:
        return run_llm_phase(
            get_connection=get_connection,
            phase=primary_phase,
            variables=variables,
            config=cfg,
        )
    except Exception as primary_error:
        if not use_fallback:
            raise RuntimeError(
                f"Error a la fase '{primary_phase}' i fallback desactivat: {str(primary_error)}"
            ) from primary_error

        fallback_result = run_llm_phase(
            get_connection=get_connection,
            phase=fallback_phase,
            variables=variables,
            config=cfg,
        )
        fallback_result["fallback_from"] = primary_phase
        fallback_result["primary_error"] = str(primary_error)
        return fallback_result

def call_with_fallback(
    conn,
    scope_type: str,
    scope_key: str,
    prompt: str,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Executa una crida LLM per task o phase, i si falla activa el fallback global
    definit a phase:fallback.
    """
    scope_type = (scope_type or "").strip().lower()
    scope_key = (scope_key or "").strip().lower()

    if scope_type not in {"task", "phase"}:
        raise ValueError("scope_type ha de ser 'task' o 'phase'")
    if not scope_key:
        raise ValueError("scope_key buit")
    if not prompt or not str(prompt).strip():
        raise ValueError("prompt buit")

    if scope_type == "task":
        primary_cfg = resolve_task_llm_config(conn, scope_key)
    else:
        primary_cfg = resolve_phase_llm_config(conn, scope_key)

    if not primary_cfg["enabled"]:
        raise RuntimeError(f"Configuració LLM desactivada per {scope_type}:{scope_key}")

    if not primary_cfg.get("provider"):
        raise RuntimeError(f"Provider no configurat per {scope_type}:{scope_key}")

    if not primary_cfg.get("model"):
        raise RuntimeError(f"Model no configurat per {scope_type}:{scope_key}")

    if not primary_cfg.get("key_configured", False):
        raise RuntimeError(
            f"Falta la API key requerida per al provider '{primary_cfg['provider']}' a {scope_type}:{scope_key}"
        )

    primary_payload = {
        "provider": primary_cfg["provider"],
        "model": primary_cfg["model"],
        "prompt": prompt,
        "temperature": kwargs.get("temperature", primary_cfg["temperature"]),
        "max_tokens": kwargs.get("max_tokens", primary_cfg["max_tokens"]),
        "timeout_secs": kwargs.get("timeout_secs", primary_cfg["timeout_secs"]),
    }

    try:
        text = call_provider(**primary_payload)

        if not text or not text.strip():
            raise RuntimeError("Resposta buida del provider primari")

        return {
            "ok": True,
            "text": text,
            "used_fallback": False,
            "scope_type": scope_type,
            "scope_key": scope_key,
            "provider": primary_cfg["provider"],
            "model": primary_cfg["model"],
            "prompt_key": primary_cfg.get("prompt_key"),
            "attempts": [
                {
                    "step": "primary",
                    "scope_type": scope_type,
                    "scope_key": scope_key,
                    "provider": primary_cfg["provider"],
                    "model": primary_cfg["model"],
                    "status": "ok",
                }
            ],
        }

    except Exception as primary_error:
        logger.warning(
            "LLM primary failed for %s:%s provider=%s model=%s error=%s",
            scope_type,
            scope_key,
            primary_cfg.get("provider"),
            primary_cfg.get("model"),
            str(primary_error),
        )

        if not primary_cfg.get("use_fallback", True):
            raise RuntimeError(
                f"Error en {scope_type}:{scope_key} i fallback desactivat: {primary_error}"
            ) from primary_error

        fallback_cfg = resolve_phase_llm_config(conn, "fallback")

        if not fallback_cfg["enabled"]:
            raise RuntimeError(
                f"Error en {scope_type}:{scope_key} i phase:fallback desactivada: {primary_error}"
            ) from primary_error

        if not fallback_cfg.get("provider"):
            raise RuntimeError("Provider no configurat per a phase:fallback")

        if not fallback_cfg.get("model"):
            raise RuntimeError("Model no configurat per a phase:fallback")

        if not fallback_cfg.get("key_configured", False):
            raise RuntimeError(
                f"Falta la API key requerida per al provider '{fallback_cfg['provider']}' a phase:fallback"
            ) from primary_error

        fallback_payload = {
            "provider": fallback_cfg["provider"],
            "model": fallback_cfg["model"],
            "prompt": prompt,
            "temperature": kwargs.get("temperature", fallback_cfg["temperature"]),
            "max_tokens": kwargs.get("max_tokens", fallback_cfg["max_tokens"]),
            "timeout_secs": kwargs.get("timeout_secs", fallback_cfg["timeout_secs"]),
        }

        try:
            text = call_provider(**fallback_payload)

            if not text or not text.strip():
                raise RuntimeError("Resposta buida del provider fallback")

            return {
                "ok": True,
                "text": text,
                "used_fallback": True,
                "scope_type": scope_type,
                "scope_key": scope_key,
                "provider": fallback_cfg["provider"],
                "model": fallback_cfg["model"],
                "prompt_key": primary_cfg.get("prompt_key"),
                "attempts": [
                    {
                        "step": "primary",
                        "scope_type": scope_type,
                        "scope_key": scope_key,
                        "provider": primary_cfg["provider"],
                        "model": primary_cfg["model"],
                        "status": "error",
                        "error": str(primary_error),
                    },
                    {
                        "step": "fallback",
                        "scope_type": "phase",
                        "scope_key": "fallback",
                        "provider": fallback_cfg["provider"],
                        "model": fallback_cfg["model"],
                        "status": "ok",
                    },
                ],
            }

        except Exception as fallback_error:
            logger.error(
                "LLM fallback also failed for %s:%s fallback provider=%s model=%s error=%s",
                scope_type,
                scope_key,
                fallback_cfg.get("provider"),
                fallback_cfg.get("model"),
                str(fallback_error),
            )
            raise RuntimeError(
                f"Han fallat primary i fallback per {scope_type}:{scope_key}. "
                f"primary={primary_error}; fallback={fallback_error}"
            ) from fallback_error    