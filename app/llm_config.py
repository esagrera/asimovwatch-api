import os
from typing import Any, Optional

from psycopg2.extras import RealDictCursor

VALID_SCOPE_TYPES = {"task", "phase"}
VALID_PHASE_KEYS = {"input", "primary", "output", "fallback"}
VALID_TASK_KEYS = {"source_discovery", "source_evaluation"}
SUPPORTED_PROVIDERS = {"gemini", "claude", "openai"}

PROVIDER_ENV_MAP = {
    "gemini": "GEMINI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
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
    ("task", "source_discovery"): "Source candidates discovery",
    ("task", "source_evaluation"): "Source candidate evaluation",
    ("phase", "input"): "Input",
    ("phase", "primary"): "Primary",
    ("phase", "output"): "Output",
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
    if scope_type not in VALID_SCOPE_TYPES:
        raise ValueError(f"scope_type invàlid: {scope_type}")

    if scope_type == "phase" and scope_key not in VALID_PHASE_KEYS:
        raise ValueError(f"scope_key invàlid per phase: {scope_key}")

    if scope_type == "task" and scope_key not in VALID_TASK_KEYS:
        raise ValueError(f"scope_key invàlid per task: {scope_key}")


def _validate_provider(provider: str) -> None:
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"provider no suportat: {provider}. "
            f"Disponibles: {', '.join(sorted(SUPPORTED_PROVIDERS))}"
        )


def _build_key_status(provider: str) -> dict:
    env_name = PROVIDER_ENV_MAP.get(provider)
    configured = bool(env_name and os.getenv(env_name, "").strip())

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
                    WHEN 'task' THEN 1
                    WHEN 'phase' THEN 2
                    ELSE 99
                END,
                CASE scope_key
                    WHEN 'source_discovery' THEN 1
                    WHEN 'source_evaluation' THEN 2
                    WHEN 'input' THEN 10
                    WHEN 'primary' THEN 11
                    WHEN 'output' THEN 12
                    WHEN 'fallback' THEN 13
                    ELSE 99
                END,
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


def get_task_config(conn, task_name: str) -> Optional[dict]:
    return get_llm_runtime_item(conn, "task", task_name)


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


def get_router_runtime_config(conn) -> dict[str, Any]:
    """
    Retorna una estructura simple pensada per al futur llm_router.py.

    Exemple de sortida:
    {
        "phases": {
            "input": {...},
            "primary": {...},
            "output": {...},
            "fallback": {...}
        },
        "tasks": {
            "source_discovery": {...},
            "source_evaluation": {...}
        }
    }
    """
    rows = list_llm_runtime_config(conn)

    result = {
        "phases": {},
        "tasks": {},
    }

    for row in rows:
        if row["scope_type"] == "phase":
            result["phases"][row["scope_key"]] = row
        elif row["scope_type"] == "task":
            result["tasks"][row["scope_key"]] = row

    return result


def get_effective_llm_for_phase(conn, phase: str) -> dict:
    row = get_phase_config(conn, phase)
    if not row:
        raise ValueError(f"No hi ha configuració per a la fase '{phase}'")
    return enrich_config_with_key_status(row)


def get_effective_llm_for_task(conn, task_name: str) -> dict:
    row = get_task_config(conn, task_name)
    if not row:
        raise ValueError(f"No hi ha configuració per a la task '{task_name}'")
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


def resolve_task_llm_config(conn, task_name: str) -> dict:
    task_key = (task_name or "").strip().lower()
    if not task_key:
        raise ValueError("task_name buit")

    row = get_llm_runtime_item(conn, "task", task_key)
    return _resolve_runtime_config(row, "task", task_key)


def resolve_phase_llm_config(conn, phase_name: str) -> dict:
    phase_key = (phase_name or "").strip().lower()
    if not phase_key:
        raise ValueError("phase_name buit")

    row = get_llm_runtime_item(conn, "phase", phase_key)
    return _resolve_runtime_config(row, "phase", phase_key)