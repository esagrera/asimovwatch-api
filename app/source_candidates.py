# =============================================================================
# MODELS PYDANTIC — source_candidates
# =============================================================================
from pydantic import BaseModel
from typing import Optional, Literal
from datetime import datetime

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
