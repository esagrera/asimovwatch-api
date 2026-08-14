import os
from typing import Optional

from psycopg2.extras import RealDictCursor

VALID_SCOPE_TYPES = {"phase", "prompt"}

VALID_PHASE_KEYS = {
    "fallback",
}

VALID_PROMPT_SCOPE_KEY = "prompt"

SUPPORTED_PROVIDERS = (
    "claude",
    "gemini",
    "openai",
    "perplexity",
)

PROVIDER_ENV_MAP = {
    "gemini": "GEMINI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
}

PROVIDER_CLIENT_MAP = {
    "gemini": ("app.llm_clients.gemini_client", "call_gemini_client"),
    "claude": ("app.llm_clients.claude_client", "call_claude_client"),
    "openai": ("app.llm_clients.openai_client", "call_openai_client"),
    "perplexity": ("app.llm_clients.perplexity_client", "call_perplexity_client"),
}

DEFAULT_SCOPE_VALUES = {
    "provider": "gemini",
    "model": "gemini-3.1-flash-lite",
    "enabled": True,
    "use_fallback": True,
    "max_tokens": 2048,
    "temperature": 0.2,
    "timeout_secs": 60,
    "notes": None,
}

DEFAULT_PROMPT_KEYS = {
    ("phase", "fallback"): "Fallback",
}

def _normalize_row(row: dict) -> dict:
    if not row:
        return row

    return {
        "id": row.get("id"),
        "scope_type": row.get("scope_type"),
        "scope_key": row.get("scope_key"),
        "provider": row.get("provider"),
        "model": row.get("model"),
        "prompt_key": row.get("prompt_key"),
        "enabled": bool(row.get("enabled")),
        "use_fallback": bool(row.get("use_fallback")),
        "max_tokens": row.get("max_tokens"),
        "temperature": float(row["temperature"]) if row.get("temperature") is not None else None,
        "timeout_secs": row.get("timeout_secs"),
        "notes": row.get("notes"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _validate_scope(scope_type: str, scope_key: str) -> None:
    normalized_scope_type = (scope_type or "").strip().lower()
    normalized_scope_key = (scope_key or "").strip().lower()

    if normalized_scope_type not in VALID_SCOPE_TYPES:
        raise ValueError(
            f"scope_type invàlid: {normalized_scope_type}. "
            f"Valors permesos: {', '.join(sorted(VALID_SCOPE_TYPES))}"
        )

    if normalized_scope_type == "phase":
        if normalized_scope_key not in VALID_PHASE_KEYS:
            raise ValueError(
                f"scope_key invàlid per phase: {normalized_scope_key}"
            )
        return

    if normalized_scope_type == "prompt":
        if normalized_scope_key != VALID_PROMPT_SCOPE_KEY:
            raise ValueError(
                f"scope_key invàlid per prompt: {normalized_scope_key}"
            )
        return

def _validate_provider(provider: str) -> None:
    provider = (provider or "").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"provider no suportat: {provider}. "
            f"Disponibles: {', '.join(sorted(SUPPORTED_PROVIDERS))}"
        )


def _build_key_status(provider: str) -> dict:
    provider = (provider or "").strip().lower()
    env_name = PROVIDER_ENV_MAP.get(provider)
    configured = bool(env_name and os.getenv(env_name, "").strip())

    return {
        "provider": provider,
        "env_var": env_name,
        "key_configured": configured,
    }

    return {
        "provider": provider,
        "env_var": env_name,
        "key_configured": configured,
    }


def list_llm_runtime_config(conn) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                id,
                scope_type,
                scope_key,
                provider,
                model,
                prompt_key,
                enabled,
                use_fallback,
                max_tokens,
                temperature,
                timeout_secs,
                notes,
                created_at,
                updated_at
            FROM public.llm_runtime_config
            ORDER BY
                CASE scope_type
                    WHEN 'phase' THEN 1
                    WHEN 'prompt' THEN 2
                    ELSE 99
                END,
                CASE
                    WHEN scope_type = 'phase'
                    AND scope_key = 'fallback'
                        THEN 1
                    WHEN scope_type = 'prompt'
                    AND scope_key = 'prompt'
                        THEN 2
                    ELSE 99
                END,
                prompt_key,
                id
            """
        )
        rows = cur.fetchall()
        return [_normalize_row(dict(r)) for r in rows]


def get_llm_runtime_item(conn, scope_type: str, scope_key: str) -> Optional[dict]:
    _validate_scope(scope_type, scope_key)

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                id,
                scope_type,
                scope_key,
                provider,
                model,
                prompt_key,
                enabled,
                use_fallback,
                max_tokens,
                temperature,
                timeout_secs,
                notes,
                created_at,
                updated_at
            FROM public.llm_runtime_config
            WHERE scope_type = %s
              AND scope_key = %s
            LIMIT 1
            """,
            (scope_type, scope_key),
        )
        row = cur.fetchone()
        return _normalize_row(dict(row)) if row else None


def get_llm_runtime_map(conn) -> dict[str, dict]:
    rows = list_llm_runtime_config(conn)
    return {
        f"{row['scope_type']}:{row['scope_key']}": row
        for row in rows
    }


def get_phase_config(conn, phase: str) -> Optional[dict]:
    return get_llm_runtime_item(conn, "phase", phase)

def get_prompt_runtime_config(
    conn,
    prompt_key: str,
) -> Optional[dict]:
    prompt_key = (prompt_key or "").strip()

    if not prompt_key:
        raise ValueError("prompt_key buit")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                id,
                scope_type,
                scope_key,
                provider,
                model,
                prompt_key,
                enabled,
                use_fallback,
                max_tokens,
                temperature,
                timeout_secs,
                notes,
                created_at,
                updated_at
            FROM public.llm_runtime_config
            WHERE scope_type = 'prompt'
              AND scope_key = 'prompt'
              AND prompt_key = %s
            LIMIT 1
            """,
            (prompt_key,),
        )

        row = cur.fetchone()

    return _normalize_row(dict(row)) if row else None

def update_llm_runtime_item(
    conn,
    scope_type: str,
    scope_key: str,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    enabled: Optional[bool] = None,
    use_fallback: Optional[bool] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    timeout_secs: Optional[int] = None,
    notes: Optional[str] = None,
) -> dict:
    _validate_scope(scope_type, scope_key)

    existing = get_llm_runtime_item(conn, scope_type, scope_key)
    if not existing:
        raise ValueError(f"No existeix configuració per {scope_type}:{scope_key}")

    fields = []
    values = []

    if provider is not None:
        provider = provider.strip().lower()
        _validate_provider(provider)
        fields.append("provider = %s")
        values.append(provider)

    if model is not None:
        model = model.strip()
        if not model:
            raise ValueError("model no pot estar buit")
        fields.append("model = %s")
        values.append(model)

    if enabled is not None:
        fields.append("enabled = %s")
        values.append(bool(enabled))

    if use_fallback is not None:
        fields.append("use_fallback = %s")
        values.append(bool(use_fallback))

    if max_tokens is not None:
        if max_tokens <= 0:
            raise ValueError("max_tokens ha de ser > 0")
        fields.append("max_tokens = %s")
        values.append(int(max_tokens))

    if temperature is not None:
        if temperature < 0 or temperature > 2:
            raise ValueError("temperature ha d'estar entre 0 i 2")
        fields.append("temperature = %s")
        values.append(float(temperature))

    if timeout_secs is not None:
        if timeout_secs <= 0:
            raise ValueError("timeout_secs ha de ser > 0")
        fields.append("timeout_secs = %s")
        values.append(int(timeout_secs))

    if notes is not None:
        fields.append("notes = %s")
        values.append(notes.strip() or None)

    if not fields:
        return existing

    fields.append("updated_at = now()")
    values.extend([scope_type, scope_key])

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            UPDATE public.llm_runtime_config
            SET {", ".join(fields)}
            WHERE scope_type = %s
            AND scope_key = %s
            RETURNING
                id,
                scope_type,
                scope_key,
                provider,
                model,
                prompt_key,
                enabled,
                use_fallback,
                max_tokens,
                temperature,
                timeout_secs,
                notes,
                created_at,
                updated_at
            """,
            values,
        )
        row = cur.fetchone()

    conn.commit()
    return _normalize_row(dict(row))


def get_provider_key_status() -> list[dict]:
    providers = sorted(SUPPORTED_PROVIDERS)
    return [_build_key_status(provider) for provider in providers]


def get_provider_key_status_map() -> dict[str, dict]:
    return {
        item["provider"]: item
        for item in get_provider_key_status()
    }


def enrich_config_with_key_status(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return row

    key_status = _build_key_status(row["provider"])
    enriched = dict(row)
    enriched["env_var"] = key_status["env_var"]
    enriched["key_configured"] = key_status["key_configured"]
    return enriched


def list_llm_runtime_config_enriched(conn) -> list[dict]:
    rows = list_llm_runtime_config(conn)
    return [enrich_config_with_key_status(row) for row in rows]


def get_effective_llm_for_phase(conn, phase: str) -> dict:
    row = get_phase_config(conn, phase)
    if not row:
        raise ValueError(f"No hi ha configuració per a la fase '{phase}'")
    return enrich_config_with_key_status(row)

def _resolve_runtime_config(row: Optional[dict], scope_type: str, scope_key: str) -> dict:
    row = row or {}

    prompt_key = row.get("prompt_key")
    if not prompt_key:
        prompt_key = DEFAULT_PROMPT_KEYS.get((scope_type, scope_key))

    return {
        "scope_type": scope_type,
        "scope_key": scope_key,
        "provider": row.get("provider") or DEFAULT_SCOPE_VALUES["provider"],
        "model": row.get("model") or DEFAULT_SCOPE_VALUES["model"],
        "prompt_key": prompt_key,
        "enabled": DEFAULT_SCOPE_VALUES["enabled"] if row.get("enabled") is None else bool(row.get("enabled")),
        "use_fallback": DEFAULT_SCOPE_VALUES["use_fallback"] if row.get("use_fallback") is None else bool(row.get("use_fallback")),
        "max_tokens": row.get("max_tokens") if row.get("max_tokens") is not None else DEFAULT_SCOPE_VALUES["max_tokens"],
        "temperature": float(row["temperature"]) if row.get("temperature") is not None else DEFAULT_SCOPE_VALUES["temperature"],
        "timeout_secs": row.get("timeout_secs") if row.get("timeout_secs") is not None else DEFAULT_SCOPE_VALUES["timeout_secs"],
        "notes": row.get("notes") if row.get("notes") is not None else DEFAULT_SCOPE_VALUES["notes"],
        "config_exists": bool(row),
        "env_var": _build_key_status(row.get("provider") or DEFAULT_SCOPE_VALUES["provider"])["env_var"],
        "key_configured": _build_key_status(row.get("provider") or DEFAULT_SCOPE_VALUES["provider"])["key_configured"],
    }

def resolve_phase_llm_config(conn, phase_name: str) -> dict:
    phase_key = (phase_name or "").strip().lower()
    if not phase_key:
        raise ValueError("phase_name buit")

    row = get_llm_runtime_item(conn, "phase", phase_key)
    return _resolve_runtime_config(row, "phase", phase_key)

def list_llm_provider_registry(conn) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM public.llm_provider_registry ORDER BY provider")
        return [dict(r) for r in cur.fetchall()]


def get_llm_provider_registry_item(conn, provider: str):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM public.llm_provider_registry WHERE provider = %s",
            (provider,)
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_llm_provider_models(conn, provider: str = None) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if provider:
            cur.execute(
                "SELECT * FROM public.llm_provider_models WHERE provider = %s ORDER BY priority",
                (provider,)
            )
        else:
            cur.execute("SELECT * FROM public.llm_provider_models ORDER BY provider, priority")
        return [dict(r) for r in cur.fetchall()]


def list_llm_provider_status(conn, provider: str = None) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if provider:
            cur.execute(
                "SELECT * FROM public.llm_provider_status WHERE provider = %s",
                (provider,)
            )
        else:
            cur.execute("SELECT * FROM public.llm_provider_status")
        return [dict(r) for r in cur.fetchall()]


def get_llm_provider_status_item(conn, provider: str, model: str):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM public.llm_provider_status WHERE provider = %s AND model = %s",
            (provider, model)
        )
        row = cur.fetchone()
        return dict(row) if row else None

PROVIDER_ERROR_SIGNATURES = {
    "gemini": {
        "quota_exceeded": ["resource_exhausted", "quota"],
        "rate_limited": ["429", "rate limit"],
    },
    "claude": {
        "quota_exceeded": ["credit balance", "insufficient", "quota"],
        "rate_limited": ["429", "rate_limit", "overloaded"],
    },
    "openai": {
        "quota_exceeded": ["insufficient_quota", "exceeded your current quota"],
        "rate_limited": ["429", "rate_limit_exceeded"],
    },
    "perplexity": {
        "quota_exceeded": ["insufficient credit", "quota"],
        "rate_limited": ["429", "rate limit"],
    },
}


def classify_llm_error(exc: Exception, provider: str) -> str:
    msg = str(exc).lower()
    signatures = PROVIDER_ERROR_SIGNATURES.get(provider, {})
    for error_type in ("quota_exceeded", "rate_limited"):
        for keyword in signatures.get(error_type, []):
            if keyword in msg:
                return error_type
    return "unknown_error"


def record_llm_provider_status(conn, provider: str, model: str, ok: bool, error_type: str = None, error_message: str = None):
    with conn.cursor() as cur:
        if ok:
            cur.execute(
                """
                INSERT INTO public.llm_provider_status (provider, model, last_checked_at, last_ok_at, updated_at)
                VALUES (%s, %s, now(), now(), now())
                ON CONFLICT (provider, model) DO UPDATE SET
                    last_checked_at = now(),
                    last_ok_at = now(),
                    updated_at = now()
                """,
                (provider, model)
            )
        else:
            cur.execute(
                """
                INSERT INTO public.llm_provider_status (provider, model, last_checked_at, last_error_type, last_error_message, last_error_at, updated_at)
                VALUES (%s, %s, now(), %s, %s, now(), now())
                ON CONFLICT (provider, model) DO UPDATE SET
                    last_checked_at = now(),
                    last_error_type = %s,
                    last_error_message = %s,
                    last_error_at = now(),
                    updated_at = now()
                """,
                (provider, model, error_type, error_message, error_type, error_message)
            )
    conn.commit()

def get_default_model(conn, provider: str) -> str:
    models = list_llm_provider_models(conn, provider)
    for m in models:
        if m["is_default"]:
            return m["model"]
    return models[0]["model"] if models else None


def get_recommended_models(conn, provider: str) -> list[dict]:
    models = list_llm_provider_models(conn, provider)
    return [m for m in models if m["visible_in_dropdown"] and m["enabled"]]


def get_fallback_candidate_models(conn, provider: str) -> list[dict]:
    models = list_llm_provider_models(conn, provider)
    return [m for m in models if m["is_fallback_candidate"]]        

def call_llm_for_prompt(
    conn,
    prompt_key: str,
    prompt_overrides: Optional[dict] = None,
) -> dict:
    from app.llm_clients import call_llm

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT value FROM public.prompts WHERE key = %s LIMIT 1",
            (prompt_key,),
        )
        prompt_row = cur.fetchone()

        if not prompt_row or not prompt_row.get("value"):
            raise ValueError(f"Prompt '{prompt_key}' no trobat")

        config_row = get_prompt_runtime_config(conn, prompt_key)

    if not config_row:
        raise ValueError(
            f"El prompt '{prompt_key}' no té configuració LLM "
            f"(llm_runtime_config)"
        )

    provider = (config_row.get("provider") or "").strip().lower()
    model = (config_row.get("model") or "").strip()
    use_fallback = bool(config_row.get("use_fallback"))

    if not provider or not model:
        raise ValueError(f"El prompt '{prompt_key}' no té provider/model configurats")

    prompt_text = prompt_row["value"]
    if prompt_overrides:
        for placeholder, value in prompt_overrides.items():
            prompt_text = prompt_text.replace(placeholder, value)

    call_kwargs = dict(
        max_tokens=config_row.get("max_tokens") or DEFAULT_SCOPE_VALUES["max_tokens"],
        temperature=float(config_row["temperature"]) if config_row.get("temperature") is not None else DEFAULT_SCOPE_VALUES["temperature"],
        timeout_secs=config_row.get("timeout_secs") or DEFAULT_SCOPE_VALUES["timeout_secs"],
    )

    try:
        output = call_llm(provider=provider, model=model, prompt=prompt_text, **call_kwargs)
        record_llm_provider_status(conn, provider, model, ok=True)
        return {
            "output": output,
            "provider_used": provider,
            "model_used": model,
            "used_fallback": False,
            "primary_error": None,
        }
    except Exception as primary_error:
        error_type = classify_llm_error(primary_error, provider)
        record_llm_provider_status(
            conn, provider, model,
            ok=False,
            error_type=error_type,
            error_message=str(primary_error),
        )

        if not use_fallback:
            raise

        fallback_row = get_phase_config(conn, "fallback")
        if not fallback_row or not fallback_row.get("provider") or not fallback_row.get("model"):
            raise RuntimeError(
                f"Crida primària a '{prompt_key}' ha fallat ({primary_error}) "
                f"i no hi ha fallback configurat"
            ) from primary_error

        fb_provider = fallback_row["provider"]
        fb_model = fallback_row["model"]

        try:
            output = call_llm(provider=fb_provider, model=fb_model, prompt=prompt_text, **call_kwargs)
            record_llm_provider_status(conn, fb_provider, fb_model, ok=True)
            return {
                "output": output,
                "provider_used": fb_provider,
                "model_used": fb_model,
                "used_fallback": True,
                "primary_error": str(primary_error),
            }
        except Exception as fallback_error:
            fb_error_type = classify_llm_error(fallback_error, fb_provider)
            record_llm_provider_status(
                conn, fb_provider, fb_model,
                ok=False,
                error_type=fb_error_type,
                error_message=str(fallback_error),
            )
            raise

def replace_provider_models(
    conn,
    provider: str,
    models: list[dict],
) -> list[dict]:
    """
    Substitueix la selecció administrada de models d'un provider.
    No esborra registres existents: desactiva els que no apareixen
    al payload per mantenir l'historial i l'estat operatiu.
    """
    provider = (provider or "").strip().lower()
    _validate_provider(provider)

    normalized_models: list[dict] = []
    seen_models: set[str] = set()
    default_count = 0

    for index, item in enumerate(models or []):
        model = (item.get("model") or "").strip()

        if not model:
            raise ValueError(f"model buit a la posició {index + 1}")

        if model in seen_models:
            raise ValueError(f"model duplicat: {model}")

        seen_models.add(model)

        enabled = bool(item.get("enabled", True))
        is_default = bool(item.get("is_default", False))
        is_fallback_candidate = bool(
            item.get("is_fallback_candidate", False)
        )
        visible_in_dropdown = bool(
            item.get("visible_in_dropdown", enabled)
        )

        if is_default:
            default_count += 1

        if is_default and not enabled:
            raise ValueError(
                f"El model per defecte '{model}' ha d'estar activat"
            )

        if is_fallback_candidate and not enabled:
            raise ValueError(
                f"El fallback candidate '{model}' ha d'estar activat"
            )

        if visible_in_dropdown and not enabled:
            raise ValueError(
                f"El model visible '{model}' ha d'estar activat"
            )

        priority = item.get("priority", (index + 1) * 10)
        try:
            priority = int(priority)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"priority invàlid per al model '{model}'"
            ) from exc

        normalized_models.append(
            {
                "model": model,
                "enabled": enabled,
                "is_default": is_default,
                "is_fallback_candidate": is_fallback_candidate,
                "visible_in_dropdown": visible_in_dropdown,
                "priority": priority,
            }
        )

    if default_count > 1:
        raise ValueError(
            f"Només hi pot haver un model per defecte per a {provider}"
        )

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE public.llm_provider_models
            SET
                enabled = false,
                is_default = false,
                is_fallback_candidate = false,
                visible_in_dropdown = false,
                updated_at = now()
            WHERE provider = %s
            """,
            (provider,),
        )

        for item in normalized_models:
            cur.execute(
                """
                INSERT INTO public.llm_provider_models (
                    provider,
                    model,
                    enabled,
                    is_default,
                    is_fallback_candidate,
                    visible_in_dropdown,
                    priority,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (provider, model)
                DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    is_default = EXCLUDED.is_default,
                    is_fallback_candidate = EXCLUDED.is_fallback_candidate,
                    visible_in_dropdown = EXCLUDED.visible_in_dropdown,
                    priority = EXCLUDED.priority,
                    updated_at = now()
                """,
                (
                    provider,
                    item["model"],
                    item["enabled"],
                    item["is_default"],
                    item["is_fallback_candidate"],
                    item["visible_in_dropdown"],
                    item["priority"],
                ),
            )

    conn.commit()

    return list_llm_provider_models(conn, provider)