from typing import Optional, Dict, Any

from psycopg2.extras import RealDictCursor

from app.llm_client import call_gemini


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


def load_runtime_config(get_connection) -> Dict[str, Any]:
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT key, value FROM public.config")
        rows = cur.fetchall()
        db_config = {row["key"]: row["value"] for row in rows}
        return get_llm_config(db_config)
    finally:
        cur.close()
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
            (prompt_key,)
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


def call_provider(provider: str, prompt: str, model: str) -> str:
    provider = (provider or "").strip().lower()

    if provider == "gemini":
        return call_gemini(prompt=prompt, model=model)

    raise RuntimeError(f"Provider no suportat encara: {provider}")


def run_llm_phase(
    get_connection,
    phase: str,
    variables: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = get_llm_config(config) if config else load_runtime_config(get_connection)
    llm = pick_llm(cfg, phase)
    template = load_prompt_template(get_connection, phase)
    prompt = build_prompt(template, variables)

    output = call_provider(
        provider=llm["provider"],
        prompt=prompt,
        model=llm["model"],
    )

    return {
        "phase": phase,
        "prompt_key": resolve_prompt_key(phase),
        "provider": llm["provider"],
        "model": llm["model"],
        "prompt": prompt,
        "output": output,
    }


def run_llm_with_fallback(
    get_connection,
    primary_phase: str,
    fallback_phase: str = "fallback",
    variables: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = get_llm_config(config) if config else load_runtime_config(get_connection)

    try:
        return run_llm_phase(
            get_connection=get_connection,
            phase=primary_phase,
            variables=variables,
            config=cfg,
        )
    except Exception as primary_error:
        fallback_result = run_llm_phase(
            get_connection=get_connection,
            phase=fallback_phase,
            variables=variables,
            config=cfg,
        )
        fallback_result["fallback_from"] = primary_phase
        fallback_result["primary_error"] = str(primary_error)
        return fallback_result