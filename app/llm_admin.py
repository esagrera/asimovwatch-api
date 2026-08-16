# =============================================================================
# app/llm_admin.py
# Router d'administració per a configuració LLM
# =============================================================================
from typing import Optional, Literal
from datetime import datetime, timezone

from psycopg2.extras import RealDictCursor

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import get_connection
from app.llm_clients import call_llm, get_supported_providers
from app.llm_config import (
    list_llm_runtime_config,
    get_llm_runtime_item,
    get_prompt_runtime_config,
    get_phase_config,
    update_llm_runtime_item,
    get_provider_key_status,
    _build_key_status,
    call_llm_for_prompt,
    list_llm_provider_registry,
    get_llm_provider_registry_item,
    list_llm_provider_models,
    list_llm_provider_status,
    replace_provider_models,
    get_default_model,
    get_recommended_models,
    get_fallback_candidate_models,
    record_llm_provider_status,
    classify_llm_error,
    save_model_advisor_report,
    get_latest_model_advisor_report,
)


router_llm_admin = APIRouter(prefix="/admin/llm", tags=["admin-llm"])

LLMPhase = Literal["fallback"]
LLMScopeType = Literal["phase", "prompt"]


class LLMPhaseConfigUpdate(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    enabled: Optional[bool] = None
    use_fallback: Optional[bool] = None
    max_tokens: Optional[int] = Field(default=None, ge=1, le=200000)
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    timeout_secs: Optional[int] = Field(default=None, ge=1, le=600)
    notes: Optional[str] = None


class LLMTestRequest(BaseModel):
    provider: str
    model: str
    prompt: str = "Respon només OK"
    max_tokens: int = Field(default=64, ge=1, le=4096)
    temperature: float = Field(default=0.0, ge=0, le=2)
    timeout_secs: int = Field(default=30, ge=1, le=120)

class LLMProviderModelSelection(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    is_default: bool = False
    is_fallback_candidate: bool = False
    visible_in_dropdown: bool = True
    priority: int = Field(default=100, ge=0, le=100000)


class LLMProviderModelsReplaceRequest(BaseModel):
    models: list[LLMProviderModelSelection] = Field(default_factory=list)


VALID_PHASES = {"fallback"}


def _normalize_phase(phase: str) -> str:
    phase_norm = (phase or "").strip().lower()
    if phase_norm not in VALID_PHASES:
        raise HTTPException(
            status_code=400,
            detail="phase ha de ser fallback"
        )
    return phase_norm

def _normalize_provider(provider: Optional[str]) -> Optional[str]:
    if provider is None:
        return None
    provider_norm = provider.strip().lower()
    if provider_norm not in get_supported_providers():
        raise HTTPException(
            status_code=400,
            detail=(
                f"provider no suportat: {provider_norm}. "
                f"Disponibles: {', '.join(get_supported_providers())}"
            )
        )
    return provider_norm


def _enrich_runtime_row(row: dict) -> dict:
    if not row:
        return row
    key_status = _build_key_status(row.get("provider"))
    enriched = dict(row)
    enriched["env_var"] = key_status.get("env_var")
    enriched["key_configured"] = key_status.get("key_configured")
    return enriched


@router_llm_admin.get("/providers")
def list_llm_providers():
    conn = None
    try:
        conn = get_connection()
        items = get_provider_key_status()
        return {
            "status": "ok",
            "count": len(items),
            "items": items,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()

@router_llm_admin.get("/providers/registry")
def list_providers_registry():
    conn = None
    try:
        conn = get_connection()
        registry = list_llm_provider_registry(conn)
        models = list_llm_provider_models(conn)
        status = list_llm_provider_status(conn)

        status_map = {(s["provider"], s["model"]): s for s in status}

        result = []
        for item in registry:
            provider = item["provider"]
            provider_models = [m for m in models if m["provider"] == provider]
            for m in provider_models:
                s = status_map.get((provider, m["model"]))
                m["last_checked_at"] = s["last_checked_at"] if s else None
                m["last_ok_at"] = s["last_ok_at"] if s else None
                m["last_error_at"] = s["last_error_at"] if s else None
                m["last_error_type"] = s["last_error_type"] if s else None
                m["last_error_message"] = s["last_error_message"] if s else None

            item["models"] = provider_models
            item["default_model"] = get_default_model(conn, provider)
            item["recommended_models"] = [m["model"] for m in get_recommended_models(conn, provider)]
            item["fallback_candidate_models"] = [m["model"] for m in get_fallback_candidate_models(conn, provider)]
            result.append(item)

        return {"status": "ok", "count": len(result), "items": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@router_llm_admin.get("/providers/registry/{provider}")
def get_provider_registry_detail(provider: str):
    conn = None
    try:
        provider = provider.strip().lower()
        conn = get_connection()
        item = get_llm_provider_registry_item(conn, provider)
        if not item:
            raise HTTPException(status_code=404, detail=f"Provider no trobat: {provider}")

        models = list_llm_provider_models(conn, provider)
        status = list_llm_provider_status(conn, provider)
        status_map = {s["model"]: s for s in status}

        for m in models:
            s = status_map.get(m["model"])
            m["last_checked_at"] = s["last_checked_at"] if s else None
            m["last_ok_at"] = s["last_ok_at"] if s else None
            m["last_error_at"] = s["last_error_at"] if s else None
            m["last_error_type"] = s["last_error_type"] if s else None
            m["last_error_message"] = s["last_error_message"] if s else None

        item["models"] = models
        item["default_model"] = get_default_model(conn, provider)
        item["recommended_models"] = [m["model"] for m in get_recommended_models(conn, provider)]
        item["fallback_candidate_models"] = [m["model"] for m in get_fallback_candidate_models(conn, provider)]

        return {"status": "ok", "item": item}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()

@router_llm_admin.put("/providers/registry/{provider}/models")
def replace_provider_models_admin(
    provider: str,
    payload: LLMProviderModelsReplaceRequest,
):
    conn = None

    try:
        provider = _normalize_provider(provider)

        conn = get_connection()

        registry_item = get_llm_provider_registry_item(conn, provider)
        if not registry_item:
            raise HTTPException(
                status_code=404,
                detail=f"Provider no trobat al registry: {provider}",
            )

        models = [item.dict() for item in payload.models]

        updated_models = replace_provider_models(
            conn,
            provider,
            models,
        )

        return {
            "status": "updated",
            "provider": provider,
            "count": len(updated_models),
            "items": updated_models,
        }

    except HTTPException:
        if conn:
            conn.rollback()
        raise

    except ValueError as exc:
        if conn:
            conn.rollback()

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        if conn:
            conn.rollback()

        raise HTTPException(
            status_code=500,
            detail=str(exc),
        ) from exc

    finally:
        if conn:
            conn.close()            

@router_llm_admin.get("/key-status")
def get_llm_key_status():
    try:
        items = get_provider_key_status()
        by_provider = {item["provider"]: item["key_configured"] for item in items}
        return {
            "status": "ok",
            "items": by_provider,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router_llm_admin.get("/config")
def get_llm_config():
    conn = None
    try:
        conn = get_connection()
        rows = list_llm_runtime_config(conn)
        enriched = [_enrich_runtime_row(row) for row in rows]

        grouped = {
            "task": {},
            "phase": {},
            "prompt": {},
        }

        for row in enriched:
            scope_type = row.get("scope_type")
            scope_key = row.get("scope_key")
            if scope_type not in grouped:
                grouped[scope_type] = {}
            grouped[scope_type][scope_key] = row

        return {
            "status": "ok",
            "count": len(enriched),
            "items": enriched,
            "grouped": grouped,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@router_llm_admin.get("/config/phases/{phase}")
def get_llm_phase(phase: LLMPhase):
    conn = None
    try:
        phase_norm = _normalize_phase(phase)
        conn = get_connection()

        row = get_phase_config(conn, phase_norm)
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"No hi ha configuració per a la fase '{phase_norm}'"
            )

        return {
            "status": "ok",
            "item": _enrich_runtime_row(row),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@router_llm_admin.patch("/config/phases/{phase}")
def patch_llm_phase(phase: LLMPhase, payload: LLMPhaseConfigUpdate):
    conn = None
    try:
        phase_norm = _normalize_phase(phase)
        updates = payload.model_dump(exclude_unset=True)

        if not updates:
            raise HTTPException(status_code=400, detail="Cap camp per actualitzar")

        if "provider" in updates:
            updates["provider"] = _normalize_provider(updates["provider"])

        if "model" in updates and updates["model"] is not None:
            updates["model"] = updates["model"].strip()
            if not updates["model"]:
                raise HTTPException(status_code=400, detail="model buit")

        conn = get_connection()

        existing = get_phase_config(conn, phase_norm)
        if not existing:
            raise HTTPException(
                status_code=404,
                detail=f"No hi ha configuració per a la fase '{phase_norm}'"
            )

        updated = update_llm_runtime_item(
            conn,
            "phase",
            phase_norm,
            provider=updates.get("provider"),
            model=updates.get("model"),
            enabled=updates.get("enabled"),
            use_fallback=updates.get("use_fallback"),
            max_tokens=updates.get("max_tokens"),
            temperature=updates.get("temperature"),
            timeout_secs=updates.get("timeout_secs"),
            notes=updates.get("notes"),
        )

        return {
            "status": "updated",
            "item": _enrich_runtime_row(updated),
        }
    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except ValueError as e:
        if conn:
            conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@router_llm_admin.get("/config/{scope_type}/{scope_key}")
def get_llm_runtime_item_detail(
    scope_type: LLMScopeType,
    scope_key: str,
    prompt_key: Optional[str] = None,
):
    conn = None
    try:
        scope_type = (scope_type or "").strip().lower()
        scope_key = (scope_key or "").strip().lower()

        if scope_type == "phase":
            if scope_key != "fallback":
                raise HTTPException(
                    status_code=400,
                    detail="La única phase vàlida és fallback",
                )

            conn = get_connection()
            row = get_phase_config(conn, "fallback")

        elif scope_type == "prompt":
            if scope_key != "prompt":
                raise HTTPException(
                    status_code=400,
                    detail="El scope_key d'un prompt ha de ser prompt",
                )

            clean_prompt_key = (prompt_key or "").strip()

            if not clean_prompt_key:
                raise HTTPException(
                    status_code=400,
                    detail="prompt_key és obligatori per consultar un prompt",
                )

            conn = get_connection()
            row = get_prompt_runtime_config(conn, clean_prompt_key)

        else:
            raise HTTPException(
                status_code=400,
                detail="scope_type ha de ser phase o prompt",
            )

        if not row:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No hi ha configuració per a "
                    f"{scope_type}:{scope_key}"
                    + (f":{prompt_key}" if prompt_key else "")
                ),
            )

        return {
            "status": "ok",
            "item": _enrich_runtime_row(row),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()

@router_llm_admin.get("/models/advisor/latest")
def get_latest_model_advisor():
    conn = None
    try:
        conn = get_connection()
        report = get_latest_model_advisor_report(conn)
        if not report:
            raise HTTPException(status_code=404, detail="Encara no s'ha generat cap informe.")
        return {"status": "ok", "item": report}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()

@router_llm_admin.get("/models/{provider}")
def list_provider_models(provider: str):
    provider = provider.strip().lower()

    if provider == "gemini":
        from app.llm_clients.gemini_client import list_available_models
    elif provider == "claude":
        from app.llm_clients.claude_client import list_available_models
    elif provider == "openai":
        from app.llm_clients.openai_client import list_available_models
    elif provider == "perplexity":
        from app.llm_clients.perplexity_client import list_available_models
    else:
        raise HTTPException(status_code=400, detail=f"provider no suportat: {provider}")

    return {
        "status": "ok",
        "provider": provider,
        "models": list_available_models(),
    }

@router_llm_admin.post("/models/advisor")
def get_model_advisor():
    conn = None
    try:
        conn = get_connection()

        # ── NOU: resum de l'estat real dels models segons el registry ──
        all_models = list_llm_provider_models(conn)
        all_status = list_llm_provider_status(conn)
        status_map = {(s["provider"], s["model"]): s for s in all_status}

        registry_summary_lines = []
        for m in all_models:
            status = status_map.get((m["provider"], m["model"]), {})
            state = "HABILITAT" if m.get("enabled") else "DESHABILITAT"
            stability = "estable" if m.get("stable") else "NO estable"
            error_info = ""
            if status.get("last_error_type"):
                error_info = f", últim error registrat: {status['last_error_type']}"
            registry_summary_lines.append(
                f"- {m['provider']}/{m['model']}: {state}, {stability}{error_info}"
            )
        registry_summary_text = "\n".join(registry_summary_lines) or "Cap model registrat."

        # ── Consulta existent de prompts (sense cap canvi) ──
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    p.key,
                    p.category,
                    p.value,
                    c.provider,
                    c.model,
                    c.max_tokens,
                    c.temperature,
                    c.timeout_secs,
                    c.use_fallback
                FROM public.prompts p
                LEFT JOIN public.llm_runtime_config c
                    ON c.prompt_key = p.key
                ORDER BY p.key
            """)
            prompts_rows = cur.fetchall()

        prompts_summary_lines = []
        for row in prompts_rows:
            snippet = (row.get("value") or "").strip().replace("\n", " ")
            if len(snippet) > 220:
                snippet = snippet[:220] + "..."

            prompts_summary_lines.append(
                f"- Prompt: {row['key']} (categoria: {row.get('category') or 'sense categoria'})\n"
                f"  Extracte del prompt: {snippet}\n"
                f"  Config actual: provider={row.get('provider') or '-'}, model={row.get('model') or '-'}, "
                f"max_tokens={row.get('max_tokens') or '-'}, temperature={row.get('temperature') if row.get('temperature') is not None else '-'}, "
                f"timeout_secs={row.get('timeout_secs') or '-'}, use_fallback={bool(row.get('use_fallback'))}"
            )

        prompts_summary_text = "\n".join(prompts_summary_lines)

        fallback_config = get_phase_config(conn, "fallback")
        
        if fallback_config:
            fallback_summary_text = (
                f"- provider={fallback_config.get('provider') or '-'}, "
                f"model={fallback_config.get('model') or '-'}, "
                f"enabled={bool(fallback_config.get('enabled'))}, "
                f"max_tokens={fallback_config.get('max_tokens') or '-'}, "
                f"temperature={fallback_config.get('temperature') if fallback_config.get('temperature') is not None else '-'}, "
                f"timeout_secs={fallback_config.get('timeout_secs') or '-'}"
            )
            if fallback_config.get("notes"):
                fallback_summary_text += f"\n  Notes: {fallback_config['notes']}"
        else:
            fallback_summary_text = "Cap configuració de fallback global trobada."

        # ── NOU: combinar totes dues seccions en un únic context ──
        full_context = (
            f"ESTAT REAL DELS MODELS SEGONS EL REGISTRY INTERN:\n"
            f"{registry_summary_text}\n\n"
            f"CONFIGURACIÓ ACTUAL DELS PROMPTS:\n"
            f"{prompts_summary_text}"
        )

        result = call_llm_for_prompt(
            conn,
            "llm_model_advisor",
            prompt_overrides={"{{prompts_config}}": full_context},
        )

        save_model_advisor_report(
            conn,
            content=result["output"],
            provider_used=result["provider_used"],
            model_used=result["model_used"],
            used_fallback=result["used_fallback"],
        )

        return {
            "status": "ok",
            "provider": result["provider_used"],
            "model": result["model_used"],
            "used_fallback": result["used_fallback"],
            "prompts_summary": full_context,
            "recommendation": result["output"],
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()

@router_llm_admin.post("/test")
def test_llm_provider(payload: LLMTestRequest):
    conn = None
    provider = None
    model = None
    try:
        provider = _normalize_provider(payload.provider)
        model = (payload.model or "").strip()
        prompt = (payload.prompt or "").strip()

        if not model:
            raise HTTPException(status_code=400, detail="model buit")
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt buit")

        output = call_llm(
            provider=provider,
            model=model,
            prompt=prompt,
            max_tokens=payload.max_tokens,
            temperature=payload.temperature,
            timeout_secs=payload.timeout_secs,
        )

        conn = get_connection()
        record_llm_provider_status(conn, provider, model, ok=True)

        return {
            "status": "ok",
            "provider": provider,
            "model": model,
            "output": output,
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        if provider and model:
            error_type = classify_llm_error(e, provider)
            try:
                if not conn:
                    conn = get_connection()
                record_llm_provider_status(conn, provider, model, ok=False, error_type=error_type, error_message=str(e))
            except Exception:
                pass
        raise HTTPException(status_code=503, detail=str(e))
    finally:
        if conn:
            conn.close()