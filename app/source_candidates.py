# =============================================================================
# IMPORTS
# =============================================================================
import json
from datetime import datetime
from typing import Optional, Literal

import psycopg2
import psycopg2.errors
import psycopg2.extras
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_connection
from app.llm_client import call_gemini
from app.llm_router import pick_llm, get_llm_config


# =============================================================================
# MODELS PYDANTIC — source_candidates
# =============================================================================
SourceCandidateStatus = Literal["PENDING", "APPROVED", "REJECTED"]
SourceCandidatePhase = Literal["1", "2", "3", "later"]

class SourceCandidateCreate(BaseModel):
    """Payload per crear un nou candidate."""
    name: str
    url: str
    domain: Optional[str] = None
    source_type: Optional[str] = None
    country_region: Optional[str] = None
    institution_type: Optional[str] = None
    proposed_phase: Optional[SourceCandidatePhase] = None
    human_protection_relevance: Optional[str] = None
    justification: Optional[str] = None
    proposed_by: Optional[str] = None


class SourceCandidateUpdate(BaseModel):
    """Payload per actualitzar un candidate (tots els camps opcionals)."""
    name: Optional[str] = None
    url: Optional[str] = None
    domain: Optional[str] = None
    source_type: Optional[str] = None
    country_region: Optional[str] = None
    institution_type: Optional[str] = None
    proposed_phase: Optional[SourceCandidatePhase] = None
    human_protection_relevance: Optional[str] = None
    justification: Optional[str] = None
    proposed_by: Optional[str] = None


class SourceCandidateReview(BaseModel):
    """Payload per revisar (aprovar/rebutjar) un candidate."""
    status: Literal["APPROVED", "REJECTED"]  # PENDING no és una decisió de revisió
    review_notes: Optional[str] = None


class SourcePromoteOverride(BaseModel):
    """
    Camps opcionals per personalitzar la source en la promoció.
    Si no s'especifiquen, s'hereten del candidate.
    """
    ingest_method: Optional[str] = None
    feed_url: Optional[str] = None
    language_default: Optional[str] = "ca"      # Asimovwatch: català per defecte
    crawl_frequency_minutes: Optional[int] = 60
    notes: Optional[str] = None
    created_by: Optional[str] = None

# =============================================================================
# MODELS PYDANTIC — source_candidates discovery
# =============================================================================

class SourceCandidateDiscoverRequest(BaseModel):
    """
    Request per executar descoberta de fonts candidates.

    - prompt_key: clau del prompt guardat a public.prompts
    - input_text: briefing concret de la cerca; si és buit o None, es farà
      servir source_discovery_default_brief de public.config
    - proposed_by: etiqueta d'origen per les files creades
    - dry_run: si True, no escriu a BD; només retorna els items detectats
    """
    prompt_key: str = "Source candidates discovery"
    input_text: Optional[str] = None
    proposed_by: Optional[str] = "ai-discovery"
    dry_run: bool = False

# =============================================================================
# ENDPOINTS CRUD — /source-candidates
# =============================================================================
# Afegir a main.py o importar com a router separat.
# Prerequisit: get_connection() ja definit al projecte.

from fastapi import APIRouter, HTTPException
import psycopg2
import psycopg2.extras
import psycopg2.errors
from app.db import get_connection

router_candidates = APIRouter(prefix="/source-candidates", tags=["source-candidates"])


@router_candidates.get("")
def list_source_candidates(
    status: Optional[str] = None,
    phase: Optional[str] = None,
):
    """
    Llista tots els candidates.
    Permet filtrar per status i/o proposed_phase.
    """
    conn = None
    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            query = "SELECT * FROM public.source_candidates WHERE 1=1"
            params = []
            if status:
                if status not in ("PENDING", "APPROVED", "REJECTED"):
                    raise HTTPException(
                        status_code=400,
                        detail="status ha de ser PENDING, APPROVED o REJECTED"
                    )
                query += " AND status = %s"
                params.append(status)
            if phase:
                query += " AND proposed_phase = %s"
                params.append(phase)
            query += " ORDER BY created_at DESC"
            cur.execute(query, params)
            rows = cur.fetchall()
        return {"status": "ok", "items": rows, "count": len(rows)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@router_candidates.get("/{candidate_id}")
def get_source_candidate(candidate_id: int):
    """Retorna un candidate pel seu id."""
    conn = None
    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM public.source_candidates WHERE id = %s",
                (candidate_id,)
            )
            row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Candidate no trobat")
        return {"status": "ok", "item": row}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@router_candidates.post("", status_code=201)
def create_source_candidate(payload: SourceCandidateCreate):
    """
    Crea un nou candidate amb status=PENDING per defecte.
    Retorna 409 si la URL ja existeix (constraint unique a la taula).
    """
    conn = None
    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO public.source_candidates
                    (name, url, domain, source_type, country_region,
                     institution_type, proposed_phase, human_protection_relevance,
                     justification, proposed_by, status, created_at)
                VALUES
                    (%s, %s, %s, %s, %s,
                     %s, %s, %s,
                     %s, %s, 'PENDING', NOW())
                RETURNING *
                """,
                (
                    payload.name, payload.url, payload.domain, payload.source_type,
                    payload.country_region, payload.institution_type,
                    payload.proposed_phase, payload.human_protection_relevance,
                    payload.justification, payload.proposed_by,
                )
            )
            row = cur.fetchone()
        conn.commit()
        return {"status": "created", "item": row}
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise HTTPException(status_code=409, detail="Ja existeix un candidate amb aquesta URL")
    except Exception as e:
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@router_candidates.patch("/{candidate_id}")
def update_source_candidate(candidate_id: int, payload: SourceCandidateUpdate):
    """
    Actualitza camps editables d'un candidate.
    NOTA: el status NO es modifica aquí; usar /review per a canvis d'estat.
    """
    conn = None
    try:
        updates = payload.model_dump(exclude_unset=True)
        if not updates:
            raise HTTPException(status_code=400, detail="Cap camp per actualitzar")

        set_clauses = ", ".join(f"{k} = %s" for k in updates.keys())
        values = list(updates.values()) + [candidate_id]

        conn = get_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"UPDATE public.source_candidates SET {set_clauses} WHERE id = %s RETURNING *",
                values
            )
            row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Candidate no trobat")
        conn.commit()
        return {"status": "updated", "item": row}
    except HTTPException:
        raise
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise HTTPException(status_code=409, detail="Ja existeix un candidate amb aquesta URL")
    except Exception as e:
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


@router_candidates.delete("/{candidate_id}", status_code=200)
def delete_source_candidate(candidate_id: int):
    """
    Esborra un candidate.
    SEGURETAT: es refusa si el candidate ja ha estat promocionat,
    per preservar la traçabilitat entre candidates i sources.
    """
    conn = None
    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, promoted_source_id FROM public.source_candidates WHERE id = %s",
                (candidate_id,)
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Candidate no trobat")
            if row["promoted_source_id"] is not None:
                raise HTTPException(
                    status_code=409,
                    detail="No es pot esborrar un candidate ja promocionat a source"
                )
            cur.execute(
                "DELETE FROM public.source_candidates WHERE id = %s",
                (candidate_id,)
            )
        conn.commit()
        return {"status": "deleted", "id": candidate_id}
    except HTTPException:
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()

# =============================================================================
# HELPERS — source_candidates discovery
# =============================================================================

def _get_prompt_value(cur, prompt_key: str) -> str:
    cur.execute(
        """
        SELECT value
        FROM public.prompts
        WHERE key = %s
        """,
        (prompt_key,)
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Prompt no trobat: {prompt_key}"
        )
    return row["value"]


def _get_default_discovery_brief(cur) -> Optional[str]:
    cur.execute(
        """
        SELECT value
        FROM public.config
        WHERE key = 'source_discovery_default_brief'
        """
    )
    row = cur.fetchone()
    return row["value"] if row else None


def _normalize_discovery_items(raw_items):
    """
    Valida i normalitza la llista retornada pel model.
    Exigeix com a mínim: name i url.
    """
    if not isinstance(raw_items, list):
        raise HTTPException(
            status_code=500,
            detail="La resposta del model no conté una llista 'items' vàlida"
        )

    normalized = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue

        name = (item.get("name") or "").strip()
        url = (item.get("url") or "").strip()

        if not name or not url:
            continue

        normalized.append({
            "name": name,
            "url": url,
            "domain": (item.get("domain") or "").strip() or None,
            "source_type": (item.get("source_type") or "").strip() or None,
            "country_region": (item.get("country_region") or "").strip() or None,
            "institution_type": (item.get("institution_type") or "").strip() or None,
            "proposed_phase": item.get("proposed_phase"),
            "human_protection_relevance": (item.get("human_protection_relevance") or "").strip() or None,
            "justification": (item.get("justification") or "").strip() or None,
        })

    return normalized

def _extract_json_candidate(text: str):
    if text is None:
        raise HTTPException(status_code=500, detail="La resposta del model és buida")

    if isinstance(text, dict):
        return text

    raw = text.strip()
    if not raw:
        raise HTTPException(status_code=500, detail="La resposta del model és buida")

    if raw.startswith("```"):
        raw = raw.replace("```json", "").replace("```JSON", "").replace("```", "").strip()

    first_obj = raw.find("{")
    last_obj = raw.rfind("}")
    first_arr = raw.find("[")
    last_arr = raw.rfind("]")

    candidates = []

    if first_obj != -1 and last_obj != -1 and last_obj > first_obj:
        candidates.append(raw[first_obj:last_obj + 1])

    if first_arr != -1 and last_arr != -1 and last_arr > first_arr:
        candidates.append(raw[first_arr:last_arr + 1])

    candidates.append(raw)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            continue

    raise HTTPException(status_code=500, detail="La resposta del model no és JSON vàlid")

# =============================================================================
# ENDPOINT DE DESCOBERTA — POST /source-candidates/discover
# =============================================================================

@router_candidates.post("/discover", status_code=201)
def discover_source_candidates(payload: SourceCandidateDiscoverRequest):
    """
    Executa una descoberta de fonts candidates via prompt + briefing.

    Comportament:
    - Llegeix el prompt de public.prompts segons prompt_key.
    - Si input_text és buit, usa source_discovery_default_brief de public.config.
    - Crida el model LLM.
    - Espera JSON amb forma: {"items": [...]}
    - Valida i normalitza items.
    - Deduplica contra public.source_candidates i public.sources per URL.
    - Insereix només noves files amb status='PENDING' (excepte dry_run=True).

    IMPORTANT:
    - No aprova ni promociona res.
    - La revisió humana continua sent obligatòria.
    """
    conn = None
    try:
        conn = get_connection()

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            prompt_value = _get_prompt_value(cur, payload.prompt_key)

            effective_input = (payload.input_text or "").strip()
            if not effective_input:
                effective_input = (_get_default_discovery_brief(cur) or "").strip()

            if not effective_input:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Falta input_text i no hi ha source_discovery_default_brief "
                        "configurat a public.config"
                    )
                )

            llm_input = f"""
            {prompt_value}

            [DISCOVERY BRIEF]
            {effective_input}

            [INSTRUCCIONS DE SORTIDA]
            Retorna exclusivament JSON vàlid amb aquesta estructura:
            {{
            "items": [
                {{
                "name": "Nom de la font",
                "url": "https://example.org",
                "domain": "example.org",
                "source_type": "website|rss|blog|news|government|company|ngo|academic|other",
                "country_region": "string o null",
                "institution_type": "string o null",
                "proposed_phase": "1|2|3|later|null",
                "human_protection_relevance": "string o null",
                "justification": "string o null"
                }}
            ]
            }}
            No escriguis text fora del JSON. No facis servir markdown.
            Si no tens prou dades, retorna exactament {{"items": []}}.
            """.strip()

            runtime_config = get_llm_config()
            llm_choice = pick_llm(runtime_config, "primary")

            print("DISCOVER: abans call_gemini", flush=True)
            llm_response = call_gemini(
                prompt=llm_input,
                model=llm_choice.get("model"),
                max_output_tokens=2048
            )
            print("DISCOVER: després call_gemini", flush=True)
            print("DISCOVER RESPONSE TYPE:", type(llm_response), flush=True)
            print("DISCOVER RAW RESPONSE:", flush=True)
            print(repr(llm_response), flush=True)

            if isinstance(llm_response, dict):
                parsed = llm_response
            elif isinstance(llm_response, list):
                parsed = {"items": llm_response}
            else:
                parsed = _extract_json_candidate(llm_response)

            if not isinstance(parsed, dict):
                raise HTTPException(
                    status_code=500,
                    detail="La resposta del model no és JSON vàlid"
                )

            items = _normalize_discovery_items(parsed.get("items"))
            detected = len(items)

            inserted = []
            skipped_existing = []

            for item in items:
                cur.execute(
                    """
                    SELECT id
                    FROM public.source_candidates
                    WHERE url = %s
                    """,
                    (item["url"],)
                )
                existing_candidate = cur.fetchone()

                if existing_candidate:
                    skipped_existing.append({
                        "url": item["url"],
                        "reason": "already_exists_in_source_candidates",
                        "id": existing_candidate["id"]
                    })
                    continue

                cur.execute(
                    """
                    SELECT id
                    FROM public.sources
                    WHERE url = %s
                    """,
                    (item["url"],)
                )
                existing_source = cur.fetchone()

                if existing_source:
                    skipped_existing.append({
                        "url": item["url"],
                        "reason": "already_exists_in_sources",
                        "id": existing_source["id"]
                    })
                    continue

                if payload.dry_run:
                    inserted.append({
                        "status": "dry_run",
                        "item": {
                            **item,
                            "status": "PENDING",
                            "proposed_by": payload.proposed_by
                        }
                    })
                    continue

                cur.execute(
                    """
                    INSERT INTO public.source_candidates
                    (name, url, domain, source_type, country_region,
                     institution_type, proposed_phase,
                     human_protection_relevance, justification,
                     proposed_by, status, created_at)
                    VALUES
                    (%s, %s, %s, %s, %s,
                     %s, %s,
                     %s, %s,
                     %s, 'PENDING', NOW())
                    RETURNING *
                    """,
                    (
                        item["name"],
                        item["url"],
                        item["domain"],
                        item["source_type"],
                        item["country_region"],
                        item["institution_type"],
                        item["proposed_phase"],
                        item["human_protection_relevance"],
                        item["justification"],
                        payload.proposed_by,
                    )
                )
                inserted.append(cur.fetchone())

            if not payload.dry_run:
                conn.commit()

            return {
                "status": "ok" if payload.dry_run else "created",
                "prompt_key": payload.prompt_key,
                "input_used": effective_input,
                "dry_run": payload.dry_run,
                "detected": detected,
                "inserted": len(inserted),
                "skipped_existing": len(skipped_existing),
                "items": inserted,
                "skipped": skipped_existing,
            }

    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except psycopg2.errors.UniqueViolation:
        if conn:
            conn.rollback()
        raise HTTPException(
            status_code=409,
            detail="S'ha detectat una URL duplicada durant la descoberta"
        )
    except Exception as e:
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()

# =============================================================================
# ENDPOINT DE REVISIÓ — POST /source-candidates/{id}/review
# =============================================================================

@router_candidates.post("/{candidate_id}/review")
def review_source_candidate(candidate_id: int, payload: SourceCandidateReview):
    """
    Canvia l'status d'un candidate a APPROVED o REJECTED.
    Registra la data de revisió i les notes.
    SUPERVISIÓ HUMANA: porta obligatòria abans de qualsevol promoció.
    """
    conn = None
    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE public.source_candidates
                SET status = %s,
                    review_notes = %s,
                    reviewed_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (payload.status, payload.review_notes, candidate_id)
            )
            row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Candidate no trobat")
        conn.commit()
        return {"status": "reviewed", "item": row}
    except HTTPException:
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


# =============================================================================
# ENDPOINT DE PROMOCIÓ — POST /source-candidates/{id}/promote
# =============================================================================

@router_candidates.post("/{candidate_id}/promote", status_code=201)
def promote_source_candidate(
    candidate_id: int,
        payload: Optional[SourcePromoteOverride] = None
):
    """
    Promou un candidate APPROVED a la taula public.sources.

    Comprovacions (per ordre):
      1. El candidate existeix.
      2. status == APPROVED (la revisió humana és obligatòria).
      3. promoted_source_id IS NULL (no s'ha promocionat abans).

    Operació atòmica (una sola transacció, un sol conn.commit()):
      a. INSERT a public.sources (amb created_from_candidate_id).
      b. UPDATE a public.source_candidates (amb promoted_source_id).

    Errors possibles:
      404  → candidate no trobat
      422  → candidate no aprovat
      409  → ja promocionat, o URL duplicada a sources
      500  → error intern
    """
    if payload is None:
        payload = SourcePromoteOverride()

    conn = None
    try:
        conn = get_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

            # -- Pas 1: existència --
            cur.execute(
                "SELECT * FROM public.source_candidates WHERE id = %s",
                (candidate_id,)
            )
            candidate = cur.fetchone()
            if not candidate:
                raise HTTPException(status_code=404, detail="Candidate no trobat")

            # -- Pas 2: status APPROVED --
            if candidate["status"] != "APPROVED":
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"El candidate ha d'estar APPROVED per ser promocionat "
                        f"(estat actual: {candidate['status']})"
                    )
                )

            # -- Pas 3: no promocionat prèviament --
            if candidate["promoted_source_id"] is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"El candidate ja ha estat promocionat "
                        f"(source id={candidate['promoted_source_id']})"
                    )
                )

            # -- Pas 4a: convertir proposed_phase a integer (o None si "later") --
            phase_value = candidate["proposed_phase"]
            if phase_value not in ("1", "2", "3", "later"):
                phase_value = None

            # -- Pas 4b: INSERT a public.sources --
            cur.execute(
                """
                INSERT INTO public.sources
                    (name, url, domain, source_type, country_region,
                     institution_type, phase,
                     ingest_method, feed_url, language_default,
                     crawl_frequency_minutes, notes,
                     created_from_candidate_id, created_by,
                     created_at, updated_at)
                VALUES
                    (%s, %s, %s, %s, %s,
                     %s, %s,
                     %s, %s, %s,
                     %s, %s,
                     %s, %s,
                     NOW(), NOW())
                RETURNING *
                """,
                (
                    candidate["name"],
                    candidate["url"],
                    candidate["domain"],
                    candidate["source_type"],
                    candidate["country_region"],
                    candidate["institution_type"],
                    phase_value,
                    payload.ingest_method,
                    payload.feed_url,
                    payload.language_default,
                    payload.crawl_frequency_minutes,
                    payload.notes or candidate.get("justification"),
                    candidate_id,            # created_from_candidate_id
                    payload.created_by,
                )
            )
            new_source = cur.fetchone()

            # -- Pas 4c: UPDATE source_candidates amb promoted_source_id --
            cur.execute(
                """
                UPDATE public.source_candidates
                SET promoted_source_id = %s
                WHERE id = %s
                """,
                (new_source["id"], candidate_id)
            )

        # -- Commit únic: tot o res --
        conn.commit()

        return {
            "status": "promoted",
            "source": new_source,
            "candidate_id": candidate_id,
        }

    except HTTPException:
        if conn:
            conn.rollback()
        raise
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise HTTPException(
            status_code=409,
            detail="Ja existeix una source amb aquesta URL. El candidate NO s'ha promocionat."
        )
    except Exception as e:
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()


# =============================================================================
# REGISTRE AL ROUTER PRINCIPAL — afegir al final de main.py
# =============================================================================
#
#   from candidates import router_candidates   # si és fitxer separat
#   app.include_router(router_candidates)
#
# =============================================================================
