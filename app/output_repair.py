"""
AsimovWatch · Fase 5 · Peça 5.3 — Reparació de la fase Output («output-only»).

Per què existeix:
  run_entry_enrichment() s'aturava abans d'arribar a Output quan run_primary=False, així que el mode
  «output-only» de POST /api/batch/process mai no cridava el LLM (i el worker ho comptava com a èxit).

Què fa run_output_only():
  - Llegeix l'entrada i reconstrueix el «resultat de Primary» a partir del que ja hi ha desat.
  - Crida NOMÉS el prompt Output.
  - Omple NOMÉS els camps de traducció (_ca / _en) que són buits. Mai no toca Primary, l'estat, la
    revisió editorial ni cap camp de traducció que ja tingui text (respecta les edicions manuals).
  - Si Output torna a deixar camps buits, no desa res i retorna un error clar.

S'activa des de crawler.py amb 3 línies (vegeu la guia): run_entry_enrichment() delega aquí quan
run_output=True, run_primary=False i run_input=False.
"""
import json
from typing import Any, Dict, List

from psycopg2.extras import Json, RealDictCursor

from app.db import get_connection
from app.llm_config import call_llm_for_prompt

# Camps que Output tradueix; per a cadascun hi ha una columna _ca i una _en.
TRANSLATABLE = ("summary_factual", "why_it_matters", "debate_questions", "human_protection_notes")
JSON_FIELDS = {"debate_questions"}

# Camps de Primary que es reenvien a Output com a context (si l'entrada els té).
PRIMARY_KEYS = (
    "summary_factual", "why_it_matters", "theme_tags", "affected_principles", "risk_level",
    "debate_questions", "confidence_notes", "relevance_score", "relevance_reason",
    "human_protection_declared", "human_protection_verifiable", "human_protection_depth",
    "human_protection_notes", "entry_category", "analyzed_provider", "analyzed_model", "bihp_directives",
)


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, (list, dict)) and not value)


def _content_language(entry: Dict[str, Any]) -> Any:
    """
    Idioma real del text base. Les entrades antigues (esquema anterior) tenen el base en CATALÀ encara que la font
    fos anglesa: si el base és idèntic al `_ca`, és català. Altrament s'intenta detectar; si no és clar, es deixa
    que Output el detecti sol.
    """
    base = (entry.get("summary_factual") or "").strip()
    if base and base == (entry.get("summary_factual_ca") or "").strip():
        return "ca"
    try:
        from app.crawler import detect_language_fallback
        return detect_language_fallback(base)
    except Exception:
        return None


def run_output_only(entry_id: int, persist: bool = True) -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM public.entries WHERE id = %s", (entry_id,))
            entry = cur.fetchone()
        if not entry:
            return {"status": "error", "entry_id": entry_id, "detail": "Entry not found"}

        if _empty(entry.get("summary_factual")) and _empty(entry.get("why_it_matters")):
            return {
                "status": "error", "entry_id": entry_id,
                "detail": "L'entrada no té Primary desada (resum i rellevància buits): cal el mode «semifull» o «full».",
            }

        # Camps de traducció que cal omplir: només els buits, i només si hi ha text base per traduir.
        to_fill: List[str] = []
        for base in TRANSLATABLE:
            if _empty(entry.get(base)):
                continue
            for lang in ("ca", "en"):
                if _empty(entry.get(f"{base}_{lang}")):
                    to_fill.append(f"{base}_{lang}")
        if not to_fill:
            return {"status": "stopped", "entry_id": entry_id, "persisted": False,
                    "detail": "Cap camp de traducció buit: no cal fer res."}

        primary_like = {k: entry.get(k) for k in PRIMARY_KEYS if k in entry}
        language = _content_language(entry)
        if language:
            primary_like["content_language"] = language

        llm = call_llm_for_prompt(
            conn=conn,
            prompt_key="Output",
            prompt_overrides={"input_text": json.dumps(primary_like, ensure_ascii=False, default=str)},
        )
        from app.crawler import parse_json_output
        raw = parse_json_output(llm["output"], phase="Output")

        updates: Dict[str, Any] = {}
        missing: List[str] = []
        for col in to_fill:
            new_value = raw.get(col)
            if _empty(new_value):
                missing.append(col)
            else:
                updates[col] = new_value
        if missing:
            return {
                "status": "error", "entry_id": entry_id, "persisted": False,
                "detail": ("La resposta de la fase Output sembla truncada o ha superat max_tokens: camps buits ("
                           + ", ".join(missing) + "). No s'ha desat res."),
            }

        # Salvaguarda: si el base és català, l'anglès no pot ser una còpia del català.
        if language == "ca" and updates.get("summary_factual_en") and \
                updates["summary_factual_en"].strip() == (entry.get("summary_factual") or "").strip():
            return {"status": "error", "entry_id": entry_id, "persisted": False,
                    "detail": "Output ha copiat el text en català a la versió anglesa: no s'ha desat res."}

        if persist:
            sets = []
            values: List[Any] = []
            for col, value in updates.items():
                sets.append(f"{col} = %s")
                values.append(Json(value) if col.rsplit("_", 1)[0] in JSON_FIELDS else value)
            sets.append("updated_at = NOW()")
            values.append(entry_id)
            with conn.cursor() as cur:
                cur.execute(f"UPDATE public.entries SET {', '.join(sets)} WHERE id = %s", values)
            conn.commit()

        return {
            "status": "enriched", "entry_id": entry_id, "persisted": persist,
            "detail": {
                "filled_fields": sorted(updates),
                "phases": {"output": {
                    "provider_used": llm.get("provider_used"),
                    "model_used": llm.get("model_used"),
                    "used_fallback": llm.get("used_fallback"),
                    "result": raw,
                }},
            },
        }
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"status": "error", "entry_id": entry_id, "persisted": False, "detail": str(exc)[:2000]}
    finally:
        conn.close()
