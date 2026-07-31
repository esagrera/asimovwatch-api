# =============================================================================
# app/llm_admin.py
# Router d'administració per a configuració LLM
# =============================================================================

from typing import Optional, Literal
from io import BytesIO
from datetime import datetime, timezone

from psycopg2.extras import RealDictCursor

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.db import get_connection
from app.llm_clients import call_llm, get_supported_providers
from app.llm_config import (
    list_llm_runtime_config,
    get_llm_runtime_item,
    get_phase_config,
    update_llm_runtime_item,
    get_provider_key_status,
    _build_key_status,
)

router_llm_admin = APIRouter(prefix="/admin/llm", tags=["admin-llm"])

LLMPhase = Literal["input", "primary", "output", "fallback"]
LLMScopeType = Literal["task", "phase"]


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


VALID_PHASES = {"input", "primary", "output", "fallback"}


def _normalize_phase(phase: str) -> str:
    phase_norm = (phase or "").strip().lower()
    if phase_norm not in VALID_PHASES:
        raise HTTPException(
            status_code=400,
            detail="phase ha de ser input, primary, output o fallback"
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
        }
        for row in enriched:
            grouped[row["scope_type"]][row["scope_key"]] = row

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
def get_llm_runtime_item_detail(scope_type: LLMScopeType, scope_key: str):
    conn = None
    try:
        scope_type = (scope_type or "").strip().lower()
        scope_key = (scope_key or "").strip().lower()

        conn = get_connection()
        row = get_llm_runtime_item(conn, scope_type, scope_key)

        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"No hi ha configuració per a {scope_type}:{scope_key}"
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

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT provider, model
                FROM public.llm_runtime_config
                WHERE scope_type = 'phase'
                  AND scope_key = 'primary'
                LIMIT 1
            """)
            runtime = cur.fetchone()

            if not runtime:
                raise HTTPException(
                    status_code=404,
                    detail="No hi ha configuració LLM per a la fase primary",
                )

            provider = (runtime["provider"] or "").strip().lower()
            model = (runtime["model"] or "").strip()

            if not provider:
                raise HTTPException(status_code=400, detail="provider primary buit")
            if not model:
                raise HTTPException(status_code=400, detail="model primary buit")

            cur.execute(
                "SELECT key, category, value FROM public.prompts WHERE key = %s LIMIT 1",
                ("llm_model_advisor",),
            )
            prompt_row = cur.fetchone()

            if not prompt_row:
                raise HTTPException(
                    status_code=404,
                    detail="Prompt 'llm_model_advisor' no trobat. Crea'l primer a la vista Prompts amb categoria Sistema.",
                )

            prompt_template = prompt_row["value"] or ""

            summary_lines = []
            for p in get_supported_providers():
                try:
                    if p == "gemini":
                        from app.llm_clients.gemini_client import list_available_models
                    elif p == "claude":
                        from app.llm_clients.claude_client import list_available_models
                    elif p == "openai":
                        from app.llm_clients.openai_client import list_available_models
                    elif p == "perplexity":
                        from app.llm_clients.perplexity_client import list_available_models
                    else:
                        continue

                    models = list_available_models()
                    names = ", ".join(m["name"] for m in models)
                    summary_lines.append(f"{p}: {names}")
                except Exception:
                    continue

            models_list_text = "\n".join(summary_lines)
            final_prompt = prompt_template.replace("{{models_list}}", models_list_text)

            output = call_llm(
                provider=provider,
                model=model,
                prompt=final_prompt,
                max_tokens=2000,
                temperature=0.3,
                timeout_secs=60,
            )

            return {
                "status": "ok",
                "provider": provider,
                "model": model,
                "models_summary": models_list_text,
                "recommendation": output,
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()

@router_llm_admin.post("/test")
def test_llm_provider(payload: LLMTestRequest):
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
        raise HTTPException(status_code=503, detail=str(e))