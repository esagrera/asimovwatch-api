import argparse
import hashlib
import html
import json
import logging
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import feedparser
import psycopg2.extras
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from app.db import get_connection
from app.llm_config import call_llm_for_prompt

load_dotenv()

REQUEST_TIMEOUT_SECONDS = 20
USER_AGENT = "AsimovWatch-EntriesCrawler/1.0 (+https://asimovwatch.com)"
MAX_ENTRIES_PER_SOURCE = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("asimovwatch.entries_crawler")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None

    text = html.unescape(str(value)).strip()
    if not text:
        return None

    if "<" not in text and ">" not in text:
        return " ".join(text.split())

    return " ".join(
        BeautifulSoup(text, "html.parser").get_text(" ").split()
    )

def entry_declared_language(entry: Any) -> Optional[str]:
    """
    Retorna el llenguatge declarat al feed per a una entry concreta.
    Mira:
    - entry.language
    - entry.content[0].language
    - entry.summary_detail.language
    """
    direct_language = entry.get("language")
    if direct_language:
        return str(direct_language).strip().lower()[:10]

    content_items = entry.get("content") or []
    for item in content_items:
        language = item.get("language")
        if language:
            return str(language).strip().lower()[:10]

    summary_detail = entry.get("summary_detail") or {}
    language = summary_detail.get("language")
    if language:
        return str(language).strip().lower()[:10]

    return None

def detect_language_fallback(text: Optional[str]) -> Optional[str]:
    """
    Detecció conservadora per a feeds sense language declarat.
    No substitueix una detecció lingüística completa.
    """
    if not text:
        return None

    lowered = text.lower()

    catalan_markers = (
        " el ", " la ", " els ", " les ", " una ", " amb ",
        " per ", " que ", " dels ", " sobre ",
    )
    spanish_markers = (
        " el ", " la ", " los ", " las ", " una ", " con ",
        " para ", " que ", " del ", " sobre ",
    )

    catalan_score = sum(lowered.count(marker) for marker in catalan_markers)
    spanish_score = sum(lowered.count(marker) for marker in spanish_markers)

    if catalan_score > spanish_score and catalan_score >= 2:
        return "ca"

    if spanish_score > catalan_score and spanish_score >= 2:
        return "es"

    english_markers = (
        " the ", " and ", " of ", " to ", " with ",
        " for ", " from ", " ai ", " research ",
    )
    english_score = sum(lowered.count(marker) for marker in english_markers)

    if english_score >= 2:
        return "en"

    return None

def parse_date(value: Any) -> Optional[datetime]:
    if not value:
        return None

    try:
        parsed = parsedate_to_datetime(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def parsed_entry_date(entry: Any) -> Optional[datetime]:
    for field in ("published", "updated", "created"):
        parsed = parse_date(entry.get(field))
        if parsed:
            return parsed

    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(field)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except Exception:
                continue

    return None


def normalize_url(value: Any) -> Optional[str]:
    if not value:
        return None

    url = str(value).strip()
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None

    return url


def entry_link(entry: Any) -> Optional[str]:
    link = normalize_url(entry.get("link"))
    if link:
        return link

    for item in entry.get("links", []):
        href = normalize_url(item.get("href"))
        if href and item.get("rel", "alternate") == "alternate":
            return href

    return None


def entry_content(entry: Any) -> Optional[str]:
    content_items = entry.get("content") or []
    if content_items:
        text = clean_text(content_items[0].get("value"))
        if text:
            return text

    return clean_text(entry.get("summary") or entry.get("description"))


def get_active_rss_sources(
    source_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    conn = get_connection()

    try:
        with conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            query = """
                SELECT
                    id,
                    name,
                    url,
                    domain,
                    source_type,
                    country_region,
                    institution_type,
                    ingest_method,
                    feed_url,
                    language_default,
                    crawl_frequency_minutes
                FROM public.sources
                WHERE COALESCE(status, 'ACTIVE') = 'ACTIVE'
                  AND ingest_method = 'rss'
                  AND feed_url IS NOT NULL
                  AND BTRIM(feed_url) <> ''
            """
            params: List[Any] = []

            if source_id is not None:
                query += " AND id = %s"
                params.append(source_id)

            query += " ORDER BY id"

            cur.execute(query, params)
            return cur.fetchall()
    finally:
        conn.close()


def build_dedup_key(payload: Dict[str, Any]) -> str:
    base = {
        "source_url": payload["source_url"],
        "canonical_url": payload.get("canonical_url"),
        "source_title": payload["source_title"].strip(),
        "published_date": payload.get("published_date"),
        "external_id": payload.get("external_id"),
        "source_domain": payload["source_domain"].strip().lower(),
    }

    normalized = json.dumps(base, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def entry_exists(cur: Any, dedup_key: str) -> Optional[int]:
    """
    Retorna l'id de l'entry existent si el dedup_key ja existeix, None si no.
    """
    cur.execute(
        """
        SELECT id
        FROM public.entries
        WHERE dedup_key = %s
        LIMIT 1
        """,
        (dedup_key,),
    )

    row = cur.fetchone()
    return row["id"] if row else None


def entry_payload(source: Dict[str, Any], entry: Any) -> Optional[Dict[str, Any]]:
    canonical_url = entry_link(entry)
    title = clean_text(entry.get("title"))

    if not canonical_url or not title:
        return None

    summary = entry_content(entry)
    external_id = clean_text(entry.get("id") or entry.get("guid"))
    author = clean_text(entry.get("author"))
    published_date = parsed_entry_date(entry)

    source_url = normalize_url(source.get("url")) or source["feed_url"]
    source_domain = (
        clean_text(source.get("domain"))
        or urlparse(source_url).netloc.lower()
    )

    return {
        "source_url": source_url,
        "source_domain": source_domain,
        "source_title": title,
        "source_type": source.get("source_type") or "rss",
        "source_language": (
            source.get("language_default")
            or entry_declared_language(entry)
            or detect_language_fallback(summary)
        ),
        "ingest_method": "rss",
        "external_id": external_id,
        "author_name": author,
        "canonical_url": canonical_url,
        "published_date": (
            published_date.isoformat() if published_date else None
        ),
        "detected_at": utc_now().isoformat(),
        "country_region": source.get("country_region"),
        "institution_type": source.get("institution_type"),
        "raw_snippet": summary,
        "raw_content": summary,
        "raw_content_format": "text",
        "raw_payload": {
            "feed_url": source["feed_url"],
            "feed_entry": dict(entry),
        },
        "review_status": "NEW",
    }


def fetch_feed(feed_url: str) -> Any:
    response = requests.get(
        feed_url,
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT},
        allow_redirects=True,
    )

    response.raise_for_status()

    parsed = feedparser.parse(response.content)

    if getattr(parsed, "bozo", False) and not parsed.entries:
        raise ValueError(
            f"Feed invàlid: {getattr(parsed, 'bozo_exception', 'format desconegut')}"
        )

    return parsed


def update_source_success(cur: Any, source_id: int) -> None:
    cur.execute(
        """
        UPDATE public.sources
        SET last_checked_at = NOW(),
            last_success_at = NOW(),
            last_error_at = NULL,
            last_error_message = NULL,
            updated_at = NOW()
        WHERE id = %s
        """,
        (source_id,),
    )


def update_source_error(cur: Any, source_id: int, message: str) -> None:
    cur.execute(
        """
        UPDATE public.sources
        SET last_checked_at = NOW(),
            last_error_at = NOW(),
            last_error_message = %s,
            updated_at = NOW()
        WHERE id = %s
        """,
        (message[:1000], source_id),
    )


def insert_entry(cur: Any, payload: Dict[str, Any], dedup_key: str) -> int:
    cur.execute(
        """
        INSERT INTO public.entries (
            source_url,
            source_domain,
            source_title,
            source_type,
            source_language,
            ingest_method,
            external_id,
            author_name,
            canonical_url,
            published_date,
            detected_at,
            country_region,
            institution_type,
            raw_snippet,
            raw_content,
            raw_content_format,
            raw_payload,
            review_status,
            dedup_key,
            ingested_at,
            updated_at,
            ingest_status,
            processing_status
        ) VALUES (
            %(source_url)s,
            %(source_domain)s,
            %(source_title)s,
            %(source_type)s,
            %(source_language)s,
            %(ingest_method)s,
            %(external_id)s,
            %(author_name)s,
            %(canonical_url)s,
            %(published_date)s,
            %(detected_at)s,
            %(country_region)s,
            %(institution_type)s,
            %(raw_snippet)s,
            %(raw_content)s,
            %(raw_content_format)s,
            %(raw_payload)s,
            %(review_status)s,
            %(dedup_key)s,
            NOW(),
            NOW(),
            'ingested',
            'RAW'
        )
        RETURNING id
        """,
        {
            **payload,
            "raw_payload": psycopg2.extras.Json(payload["raw_payload"]),
            "dedup_key": dedup_key,
        },
    )

    row = cur.fetchone()
    return row["id"] if isinstance(row, dict) else row[0]


def record_crawler_log(
    cur: Any,
    sources_checked: int,
    items_found: int,
    items_relevant: int,
    items_enriched: int,
    items_failed: int,
    duration_seconds: float,
    notes: str,
) -> None:
    cur.execute(
        """
        INSERT INTO public.crawler_log (
            sources_checked,
            items_found,
            items_relevant,
            items_enriched,
            items_failed,
            duration_seconds,
            notes
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            sources_checked,
            items_found,
            items_relevant,
            items_enriched,
            items_failed,
            duration_seconds,
            notes,
        ),
    )


# =========================================================================
# TITLE: Funcions auxiliars del pipeline d'enriquiment (Input -> Primary -> Output)
#
# IMPORTANT - Convenció de placeholders (verificada contra els prompts reals
# guardats a public.prompts, no inferida):
#
# - "Input"   usa claudàtor SIMPLE: {source_url} {source_domain} {source_title}
#   {source_type} {country_region} {institution_type} {published_date}
#   {input_text}
# - "Primary" usa claudàtor SIMPLE: {input_text}  (únic placeholder)
# - "Output"  usa claudàtor SIMPLE: {input_text}  (únic placeholder; rep el
#   JSON complet retornat per Primary, no el contingut original de la notícia)
# - "Thematic entry discovery" usa claudàtor DOBLE: {{brief}} {{date_range}}
#   {{source_scope}} {{source_types}} {{max_results}}
#
# call_llm_for_prompt() fa un replace() literal de cada clau del
# prompt_overrides contra el text del prompt guardat a la BD. Per això cada
# prompt requereix el format exacte de claudàtor que ja té escrit, no un
# format unificat inventat.
#
# Totes les crides LLM d'aquest pipeline (Input, Primary, Output, Thematic
# entry discovery) passen per call_llm_for_prompt() (app.llm_config), que
# centralitza provider/model/fallback/registre d'errors per a qualsevol
# provider suportat (perplexity, openai, gemini, claude). Aquest fitxer no
# conté cap crida directa a SDKs de proveïdors ni cap lògica pròpia
# d'API key / base_url per provider.
# =========================================================================

VALID_ENTRY_CATEGORIES = {
    "ai_ethics",
    "regulation_frameworks",
    "safety_control_oversight",
    "digital_rights",
    "human_protection_bihp",
    "incidents",
    "other",
}

VALID_BIHP_LABELS = {"green", "yellow", "red", "unknown"}

ENTRY_CATEGORY_ALIASES = {
    "ai ethics": "ai_ethics",
    "ai-ethics": "ai_ethics",
    "ai_ethics": "ai_ethics",

    "regulation": "regulation_frameworks",
    "regulatory frameworks": "regulation_frameworks",
    "regulatory_frameworks": "regulation_frameworks",
    "regulation-frameworks": "regulation_frameworks",
    "regulation_frameworks": "regulation_frameworks",

    "safety": "safety_control_oversight",
    "safety control": "safety_control_oversight",
    "safety oversight": "safety_control_oversight",
    "safety-control-oversight": "safety_control_oversight",
    "safety control oversight": "safety_control_oversight",
    "safety_control_oversight": "safety_control_oversight",

    "digital rights": "digital_rights",
    "digital-rights": "digital_rights",
    "digital_rights": "digital_rights",

    "bihp": "human_protection_bihp",
    "human protection": "human_protection_bihp",
    "human-protection-bihp": "human_protection_bihp",
    "human_protection_bihp": "human_protection_bihp",

    "incident": "incidents",
    "incidents": "incidents",

    "other": "other",
}

def _build_input_overrides(entry: Dict[str, Any]) -> Dict[str, str]:
    """
    Prepara els prompt_overrides per al prompt "Input", que espera 8
    placeholders individuals de claudàtor simple (no un bloc de text únic).

    {input_text} és el contingut cru: raw_content si existeix, sinó raw_snippet.
    """
    content = entry.get("raw_content") or entry.get("raw_snippet") or ""

    return {
        "{source_url}": entry.get("source_url") or "",
        "{source_domain}": entry.get("source_domain") or "",
        "{source_title}": entry.get("source_title") or "",
        "{source_type}": entry.get("source_type") or "desconegut",
        "{country_region}": entry.get("country_region") or "desconegut",
        "{institution_type}": entry.get("institution_type") or "desconegut",
        "{published_date}": str(entry.get("published_date") or "desconeguda"),
        "{input_text}": content,
    }


def _build_primary_input_text(
    entry: Dict[str, Any],
    input_result: Optional[Dict[str, Any]],
) -> str:
    """
    Construeix el text que substitueix l'únic placeholder {input_text} del
    prompt "Primary".
    - Si input_result no és None (via RSS + Input): usa clean_input_text/input_summary.
    - Si input_result és None: usa el contingut disponible directament.
    """
    if input_result is not None:
        clean_text = (
            input_result.get("clean_input_text")
            or entry.get("raw_content")
            or entry.get("raw_snippet")
            or ""
        )
        summary = input_result.get("input_summary") or ""
        source_note = (
            "Origen: ingesta RSS/Atom, processada prèviament per la fase Input."
        )
    else:
        raw_payload = entry.get("raw_payload") or {}
        search_result = raw_payload.get("search_result") or {}

        clean_text = (
            search_result.get("why_relevant")
            or entry.get("raw_content")
            or entry.get("raw_snippet")
            or ""
        )

        search_brief = raw_payload.get("search_brief") or ""

        if entry.get("ingest_method") == "web_search":
            summary = (
                f"Cerca temàtica motivada pel brief: {search_brief}"
                if search_brief
                else ""
            )
            source_note = (
                "Origen: cerca temàtica (web_search), sense pas previ per la fase Input."
            )
        else:
            summary = ""
            source_note = "Origen: entrada processada sense executar la fase Input."

    return f"""Metadades de la peça:
source_url: {entry.get('source_url', '')}
source_domain: {entry.get('source_domain', '')}
source_title: {entry.get('source_title', '')}
source_type: {entry.get('source_type') or 'desconegut'}
country_region: {entry.get('country_region') or 'desconegut'}
institution_type: {entry.get('institution_type') or 'desconegut'}
published_date: {entry.get('published_date') or 'desconeguda'}

{source_note}

Resum previ (si existeix):
{summary}

Contingut per analitzar:
{clean_text}"""


def _parse_json_output(raw_text: str, phase: str = "unknown") -> Dict[str, Any]:
    if not raw_text or not raw_text.strip():
        raise ValueError(f"La fase {phase} ha retornat una resposta buida.")

    cleaned = raw_text.strip()

    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]

    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]

    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as first_exc:
        first_brace = cleaned.find("{")
        last_brace = cleaned.rfind("}")

        if first_brace != -1 and last_brace > first_brace:
            candidate = cleaned[first_brace:last_brace + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

        logger.error(
            "JSON invàlid retornat a la fase %s: %s; chars=%s",
            phase,
            first_exc,
            len(cleaned),
        )
        logger.error("Contingut rebut a %s: %s", phase, cleaned[:1000])

        likely_truncated = (
            cleaned.count("{") > cleaned.count("}")
            or "unterminated string" in str(first_exc).lower()
        )

        if likely_truncated:
            raise ValueError(
                f"La resposta de la fase {phase} sembla truncada o ha superat max_tokens. "
                "Augmenta max_tokens o redueix la mida de la sortida requerida."
            ) from first_exc

        raise ValueError(
            f"La fase {phase} ha retornat JSON invàlid. Revisa els logs per més detalls."
        ) from first_exc


def _validate_and_normalize_final_output(
    primary_result: Dict[str, Any],
    output_result: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Combina i valida el resultat final a persistir:
    - Els camps textuals/traduïts venen d'output_result (fase Output: neteja
      editorial + traducció al català).
    - entry_category, analyzed_provider, analyzed_model i bihp_directives
      només existeixen a primary_result (Output no els reemet segons el seu
      esquema), així que es prenen d'aquí.
    - Es valida entry_category i els 3 semàfors BIHP contra els valors
      permesos (constraint SQL real); si no són vàlids, es força a
      'other'/'unknown' i es deixa constància a confidence_notes.
     """
    result = dict(primary_result)

    # Output només pot sobreescriure els camps de traducció i les seves notes.
    # Si Output retorna null (contingut ja en català), es respecta el null.
    translatable_fields = (
        "translated_summary_ca",
        "translated_whyitmatters_ca",
        "translated_debatequestions_ca",
    )
    for field in translatable_fields:
        if field in output_result:
            result[field] = output_result.get(field)

    if output_result.get("postprocess_notes"):
        result["postprocess_notes"] = output_result["postprocess_notes"]

    # Camps exclusius de Primary, que Output no reemet
    result["entry_category"] = primary_result.get("entry_category")
    result["analyzed_provider"] = primary_result.get("analyzed_provider")
    result["analyzed_model"] = primary_result.get("analyzed_model")
    result["bihp_directives"] = primary_result.get("bihp_directives") or []

    notes = result.get("confidence_notes") or ""

    raw_category = str(result.get("entry_category") or "").strip().lower()
    normalized_category = ENTRY_CATEGORY_ALIASES.get(raw_category, raw_category)

    if normalized_category not in VALID_ENTRY_CATEGORIES:
        notes += (
            f" [Avís: entry_category '{raw_category}' no vàlid, "
            "forçat a 'other'.]"
        )
        result["entry_category"] = "other"
    else:
        result["entry_category"] = normalized_category

    for field in ("human_protection_declared", "human_protection_verifiable", "human_protection_depth"):
        value = result.get(field)
        if value is not None and value not in VALID_BIHP_LABELS:
            notes += f" [Avís: {field}='{value}' no vàlid, forçat a 'unknown'.]"
            result[field] = "unknown"

    result["confidence_notes"] = notes.strip() or None
    return result


def run_entry_enrichment(
    entry_id: int,
    skip_input: bool = False,
    run_input: bool = True,
    run_primary: bool = True,
    run_output: bool = True,
    persist: bool = True,
) -> Dict[str, Any]:
    """
    Executa Input -> Primary -> Output per a una entry.

    persist=True (defecte): comportament operatiu; persisteix estats, resultats
    i errors a public.entries.

    persist=False: mode de prova. No modifica cap camp de public.entries, no
    incrementa processing_retries i retorna les sortides de les fases per
    inspecció. És segur per provar canvis de prompt, provider, model i parser.
    """
    conn = get_connection()
    phase_results: Dict[str, Any] = {}

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM public.entries WHERE id = %s", (entry_id,))
            entry = cur.fetchone()
            if not entry:
                return {"status": "error", "entry_id": entry_id, "detail": "Entry not found"}

            input_result = None
            should_run_input = run_input and not skip_input

            if should_run_input:
                input_llm_result = call_llm_for_prompt(
                    conn,
                    "Input",
                    prompt_overrides=_build_input_overrides(entry),
                )
                input_result = _parse_json_output(input_llm_result["output"], phase="Input")
                phase_results["input"] = {
                    "provider_used": input_llm_result["provider_used"],
                    "model_used": input_llm_result["model_used"],
                    "used_fallback": input_llm_result["used_fallback"],
                    "result": input_result,
                }

                ready = input_result.get("ready_for_primary")
                if ready == "no":
                    if persist:
                        cur.execute("""
                            UPDATE public.entries SET
                                processing_status = 'DISCARDED',
                                processing_error = NULL,
                                input_relevance = %s,
                                input_relevance_reason = %s,
                                ready_for_primary = %s,
                                raw_snippet_original = COALESCE(raw_snippet_original, %s),
                                input_quality = %s,
                                input_quality_notes = %s,
                                updated_at = NOW()
                            WHERE id = %s
                        """, (
                            input_result.get("input_relevance"),
                            input_result.get("input_relevance_reason"),
                            ready,
                            entry.get("raw_snippet_original") or entry.get("raw_snippet") or entry.get("raw_content"),
                            input_result.get("input_quality"),
                            input_result.get("input_quality_notes"),
                            entry_id,
                        ))
                        conn.commit()

                    return {
                        "status": "discarded",
                        "entry_id": entry_id,
                        "persisted": persist,
                        "detail": phase_results,
                    }

                # Càlcul de detected_language per a source_language
                detected_language = (
                    input_result.get("source_language")
                    or entry.get("source_language")
                    or detect_language_fallback(
                        input_result.get("clean_input_text")
                        or entry.get("raw_content")
                        or entry.get("raw_snippet")
                    )
                )

                if persist:    
                    cur.execute("""
                        UPDATE public.entries SET
                            input_relevance = %s,
                            input_relevance_reason = %s,
                            ready_for_primary = %s,
                            raw_snippet_original = COALESCE(raw_snippet_original, %s),
                            clean_input_text = %s,
                            input_summary = %s,
                            input_quality = %s,
                            input_quality_notes = %s,
                            source_language = COALESCE(%s, source_language),
                            updated_at = NOW()
                        WHERE id = %s
                    """, (
                        input_result.get("input_relevance"),
                        input_result.get("input_relevance_reason"),
                        ready,
                        entry.get("raw_snippet_original") or entry.get("raw_snippet") or entry.get("raw_content"),
                        input_result.get("clean_input_text"),
                        input_result.get("input_summary"),
                        input_result.get("input_quality"),
                        input_result.get("input_quality_notes"),
                        detected_language,
                        entry_id,
                    ))
                    conn.commit()

            if not run_primary:
                return {
                    "status": "stopped",
                    "entry_id": entry_id,
                    "persisted": persist,
                    "detail": {
                        "stopped_after": "input" if should_run_input else "none",
                        "phases": phase_results,
                    },
                }

            primary_input_text = _build_primary_input_text(entry, input_result)
            primary_llm_result = call_llm_for_prompt(
                conn,
                "Primary",
                prompt_overrides={"{input_text}": primary_input_text},
            )
            primary_result = _parse_json_output(primary_llm_result["output"], phase="Primary")
            phase_results["primary"] = {
                "provider_used": primary_llm_result["provider_used"],
                "model_used": primary_llm_result["model_used"],
                "used_fallback": primary_llm_result["used_fallback"],
                "result": primary_result,
            }

            if not run_output:
                interim_result = _validate_and_normalize_final_output(primary_result, dict(primary_result))

                if persist:
                    cur.execute("""
                        UPDATE public.entries SET
                            processing_status = 'ENRICHED',
                            processing_error = NULL,
                            summary_factual = %s,
                            why_it_matters = %s,
                            theme_tags = %s,
                            affected_principles = %s,
                            risk_level = %s,
                            debate_questions = %s,
                            confidence_notes = %s,
                            human_protection_declared = %s,
                            human_protection_verifiable = %s,
                            human_protection_depth = %s,
                            human_protection_notes = %s,
                            entry_category = %s,
                            analyzed_provider = %s,
                            analyzed_model = %s,
                            bihp_directives = %s,
                            enriched_model = %s,
                            enriched_at = NOW(),
                            updated_at = NOW()
                        WHERE id = %s
                    """, (
                        interim_result.get("summary_factual"),
                        interim_result.get("why_it_matters"),
                        interim_result.get("theme_tags"),
                        interim_result.get("affected_principles"),
                        interim_result.get("risk_level"),
                        interim_result.get("debate_questions"),
                        interim_result.get("confidence_notes"),
                        interim_result.get("human_protection_declared"),
                        interim_result.get("human_protection_verifiable"),
                        interim_result.get("human_protection_depth"),
                        interim_result.get("human_protection_notes"),
                        interim_result.get("entry_category"),
                        interim_result.get("analyzed_provider"),
                        interim_result.get("analyzed_model"),
                        psycopg2.extras.Json(interim_result.get("bihp_directives") or []),
                        primary_llm_result["model_used"],
                        entry_id,
                    ))
                    conn.commit()

                return {
                    "status": "stopped",
                    "entry_id": entry_id,
                    "persisted": persist,
                    "detail": {
                        "stopped_after": "primary",
                        "phases": phase_results,
                        "normalized_result": interim_result,
                    },
                }

            output_llm_result = call_llm_for_prompt(
                conn,
                "Output",
                prompt_overrides={"{input_text}": json.dumps(primary_result, ensure_ascii=False)},
            )
            output_result_raw = _parse_json_output(output_llm_result["output"], phase="Output")
            phase_results["output"] = {
                "provider_used": output_llm_result["provider_used"],
                "model_used": output_llm_result["model_used"],
                "used_fallback": output_llm_result["used_fallback"],
                "result": output_result_raw,
            }

            final_result = _validate_and_normalize_final_output(primary_result, output_result_raw)

            if persist:
                cur.execute("""
                    UPDATE public.entries SET
                        processing_status = 'ENRICHED',
                        processing_error = NULL,
                        summary_factual = %s,
                        why_it_matters = %s,
                        theme_tags = %s,
                        affected_principles = %s,
                        risk_level = %s,
                        debate_questions = %s,
                        confidence_notes = %s,
                        human_protection_declared = %s,
                        human_protection_verifiable = %s,
                        human_protection_depth = %s,
                        human_protection_notes = %s,
                        entry_category = %s,
                        analyzed_provider = %s,
                        analyzed_model = %s,
                        bihp_directives = %s,
                        translated_summary_ca = %s,
                        translated_whyitmatters_ca = %s,
                        translated_debatequestions_ca = %s,
                        enriched_model = %s,
                        enriched_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                """, (
                    final_result.get("summary_factual"),
                    final_result.get("why_it_matters"),
                    final_result.get("theme_tags"),
                    final_result.get("affected_principles"),
                    final_result.get("risk_level"),
                    final_result.get("debate_questions"),
                    final_result.get("confidence_notes"),
                    final_result.get("human_protection_declared"),
                    final_result.get("human_protection_verifiable"),
                    final_result.get("human_protection_depth"),
                    final_result.get("human_protection_notes"),
                    final_result.get("entry_category"),
                    final_result.get("analyzed_provider"),
                    final_result.get("analyzed_model"),
                    psycopg2.extras.Json(final_result.get("bihp_directives") or []),
                    final_result.get("translated_summary_ca"),
                    final_result.get("translated_whyitmatters_ca"),
                    final_result.get("translated_debatequestions_ca"),
                    output_llm_result["model_used"],
                    entry_id,
                ))
                conn.commit()

            return {
                "status": "enriched",
                "entry_id": entry_id,
                "persisted": persist,
                "detail": {
                    "phases": phase_results,
                    "final_result": final_result,
                },
            }

    except Exception as exc:
        conn.rollback()
        if persist:
            try:
                with conn.cursor() as cur2:
                    cur2.execute("""
                        UPDATE public.entries SET
                            processing_status = 'ERROR',
                            processing_error = %s,
                            processing_retries = COALESCE(processing_retries, 0) + 1,
                            updated_at = NOW()
                        WHERE id = %s
                    """, (str(exc), entry_id))
                conn.commit()
            except Exception:
                conn.rollback()

        logger.exception("Error enriquint entry %s (persist=%s): %s", entry_id, persist, exc)
        return {
            "status": "error",
            "entry_id": entry_id,
            "persisted": persist,
            "detail": str(exc),
            "phases_completed": phase_results,
        }
    finally:
        conn.close()


def _build_thematic_search_overrides(
    brief: str,
    date_range: Optional[str],
    source_scope: Optional[str],
    source_types: Optional[list],
    max_results: int,
) -> Dict[str, str]:
    """
    Prepara els prompt_overrides per al prompt "Thematic entry discovery",
    que usa claudàtor DOBLE {{...}}.

    Suporta:
    - {{brief}}
    - {{date_range}}
    - {{source_scope}}
    - {{source_types}}
    - {{max_results}}
    """
    if source_types:
        source_types_str = ", ".join(source_types)
    else:
        source_types_str = "qualsevol"

    return {
        "{{brief}}": brief or "",
        "{{date_range}}": date_range or "Últim any",
        "{{source_scope}}": source_scope or "web_and_monitored_sources",
        "{{source_types}}": source_types_str,
        "{{max_results}}": str(max_results),
    }


def _parse_search_results(raw_text: str) -> List[Dict[str, Any]]:
    """
    Parseja la sortida JSON d'una cerca temàtica (prompt 'Thematic entry
    discovery'), independentment del provider que l'hagi generat.

    Retorna una llista de resultats amb:
    - title, url, publisher, published_date, source_kind, language, why_relevant
    """
    cleaned = raw_text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]

    try:
        parsed = json.loads(cleaned.strip())
    except json.JSONDecodeError as exc:
        logger.error("JSON invàlid retornat per la cerca temàtica: %s", exc)
        logger.error("Contingut rebut: %s", cleaned[:500])
        raise ValueError(
            "El model ha retornat JSON invàlid. Revisa els logs per més detalls."
        ) from exc

    if not isinstance(parsed, dict) or "results" not in parsed:
        raise ValueError("El JSON no conté la clau 'results'")

    results = []
    for item in parsed.get("results", []):
        if not isinstance(item, dict):
            continue

        title = item.get("title")
        url = item.get("url")

        if not title or not url:
            logger.warning("Resultat sense title o url, ignorat: %s", item)
            continue

        results.append({
            "title": title[:250],  # Respectar límit del prompt
            "url": url,
            "publisher": item.get("publisher"),
            "published_date": item.get("published_date"),
            "source_kind": item.get("source_kind", "other"),
            "language": item.get("language"),
            "why_relevant": (item.get("why_relevant") or "")[:280],
        })

    logger.info("S'han retornat %d resultats vàlids", len(results))
    return results


def run_thematic_search(
    brief: str,
    date_range: Optional[str] = "Últim any",
    source_scope: Optional[str] = "web_and_monitored_sources",
    source_types: Optional[list] = None,
    max_results: int = 10,
    run_enrichment: bool = True,
    dry_run: bool = False,
    requested_by: str = "admin-thematic-search",
    prompt_key: str = "Thematic entry discovery",
) -> Dict[str, Any]:
    """
    Executa una cerca temàtica web utilitzant el prompt especificat.

    Fa servir call_llm_for_prompt(), igual que la resta del sistema
    (llm_model_advisor, Source candidates discovery, Source candidate
    evaluation, Input, Primary, Output). Si el provider configurat per a
    aquest prompt no té capacitat de cerca web real, es degrada
    (web_and_monitored_sources) o falla explícitament (web_only).

    Retorna un dict amb els comptadors i warnings.
    """
    started_at = utc_now()

    logger.info("Iniciant cerca temàtica per a: %s", brief)
    logger.info("Prompt key: %s", prompt_key)

    counters = {
        "sources_checked": 1,
        "items_found": 0,
        "items_created": 0,
        "items_duplicates": [],  # Llista d'IDs
        "items_duplicates_monitored": [],  # IDs de fonts promogudes
        "items_skipped": 0,
        "items_failed": 0,
    }

    warnings: List[str] = []

    conn = get_connection()

    try:
        # 1. Comprovar provider configurat per a aquest prompt (per a la
        #    validació de capacitat de cerca web, abans de gastar la crida)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT c.provider
                FROM public.prompts p
                LEFT JOIN public.llm_runtime_config c
                    ON c.scope_type = 'prompt'
                    AND c.scope_key = 'prompt'
                    AND c.prompt_key = p.key
                WHERE p.key = %s
                LIMIT 1
                """,
                (prompt_key,),
            )
            prompt_row = cur.fetchone()

        if not prompt_row:
            raise ValueError(f"Prompt '{prompt_key}' no trobat a la base de dades")

        configured_provider = (prompt_row.get("provider") or "").strip().lower()

        logger.info("Carregat prompt: key=%s, provider=%s", prompt_key, configured_provider)

        # 2. Validar capacitat de cerca web
        WEBSEARCH_CAPABLE_PROVIDERS = {"perplexity"}
        wants_web_search = source_scope in ("web_and_monitored_sources", "web_only")

        if wants_web_search and configured_provider not in WEBSEARCH_CAPABLE_PROVIDERS:
            if source_scope == "web_only":
                raise ValueError(
                    f"El provider '{configured_provider}' no té capacitat de cerca web real. "
                    "No es pot executar una cerca 'web_only' amb aquest model."
                )

            logger.warning(
                "Provider '%s' sense capacitat de cerca web. "
                "Cerca temàtica limitada (no es farà cerca web oberta).",
                configured_provider,
            )
            warnings.append("Cerca web omesa: el model configurat no té capacitat de cerca real.")
            return {**counters, "warnings": warnings}

        # 3. Preparar els overrides del prompt (placeholders {{...}})
        prompt_overrides = _build_thematic_search_overrides(
            brief=brief,
            date_range=date_range,
            source_scope=source_scope,
            source_types=source_types,
            max_results=max_results,
        )

        # 4. Fer la cerca amb call_llm_for_prompt() (mateix camí que la resta del sistema)
        llm_result = call_llm_for_prompt(
            conn,
            prompt_key,
            prompt_overrides=prompt_overrides,
        )
        search_results = _parse_search_results(llm_result["output"])
        counters["items_found"] = len(search_results)

        # 5. Processar cada resultat
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for result in search_results:
                try:
                    payload = {
                        "source_url": result["url"],
                        "source_domain": urlparse(result["url"]).netloc.lower(),
                        "source_title": result["title"],
                        "source_type": "web_search",
                        "source_language": result.get("language") or "ca",
                        "ingest_method": "web_search",
                        "external_id": None,
                        "author_name": None,
                        "canonical_url": result["url"],
                        "published_date": result.get("published_date"),
                        "detected_at": utc_now().isoformat(),
                        "country_region": None,
                        "institution_type": None,
                        "raw_snippet": result.get("why_relevant", ""),
                        "raw_content": result.get("why_relevant", ""),
                        "raw_content_format": "text",
                        "raw_payload": {
                            "search_brief": brief,
                            "search_date_range": date_range,
                            "search_result": result,
                        },
                        "review_status": "NEW",
                    }

                    dedup_key = build_dedup_key(payload)

                    existing_entry_id = entry_exists(cur, dedup_key)
                    if existing_entry_id is not None:
                        cur.execute(
                            """
                            SELECT s.id, s.name, s.domain
                            FROM public.sources s
                            WHERE s.domain = %s
                              AND s.status = 'ACTIVE'
                            LIMIT 1
                            """,
                            (payload["source_domain"],)
                        )
                        monitored_source = cur.fetchone()

                        if monitored_source:
                            counters["items_duplicates_monitored"].append(existing_entry_id)
                            warnings.append(
                                f"Font ja monitoritzada ({monitored_source['domain']}): "
                                f"entry existent id={existing_entry_id}"
                            )
                        else:
                            counters["items_duplicates"].append(existing_entry_id)

                        continue

                    source_title = (payload.get("source_title") or "").strip()
                    source_url = (payload.get("source_url") or "").strip()

                    is_placeholder_title = source_title.lower().startswith("resultat de cerca sobre:")

                    if (
                        is_placeholder_title
                        or not source_url
                        or not source_url.startswith(("https://", "http://"))
                    ):
                        counters["items_skipped"] += 1
                        logger.warning(
                            "Entrada descartada: font no verificable o placeholder. "
                            "title=%r, url=%r",
                            source_title,
                            source_url,
                        )
                        continue

                    if dry_run:
                        counters["items_skipped"] += 1
                        continue

                    new_entry_id = insert_entry(cur, payload, dedup_key)
                    counters["items_created"] += 1
                    conn.commit()

                    enrichment_result = run_entry_enrichment(entry_id=new_entry_id, skip_input=True)
                    if enrichment_result["status"] == "error":
                        counters["items_failed"] += 1

                except Exception as exc:
                    counters["items_failed"] += 1
                    logger.exception(
                        "Error processant resultat de cerca temàtica: %s",
                        exc,
                    )

            if not dry_run:
                conn.commit()

        logger.info(
            "Cerca temàtica finalitzada: %s",
            json.dumps(counters, ensure_ascii=False),
        )

        return {**counters, "warnings": warnings}

    except Exception as exc:
        logger.exception("Error en cerca temàtica: %s", exc)
        counters["items_failed"] += 1
        return {**counters, "warnings": warnings, "error": str(exc)}
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# CRAWLER RSS
# ──────────────────────────────────────────────────────────────────────────────

def run(
    dry_run: bool = False,
    source_id: Optional[int] = None,
    limit_per_source: int = MAX_ENTRIES_PER_SOURCE,
) -> Dict[str, Any]:
    started_at = utc_now()
    counters = {
        "sources_checked": 0,
        "items_found": 0,
        "items_created": 0,
        "items_duplicates": 0,
        "items_discarded_by_relevance": 0,
        "items_skipped": 0,
        "items_failed": 0,
        "items_enriched": 0,
        "items_left_raw": 0,
    }

    sources = get_active_rss_sources(source_id=source_id)

    if not sources:
        logger.warning("No hi ha fonts actives amb ingest_method='rss' i feed_url.")
        return counters

    conn = get_connection()

    try:
        with conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            for source in sources:
                counters["sources_checked"] += 1
                source_name = source.get("name") or f"source:{source['id']}"

                try:
                    feed = fetch_feed(source["feed_url"])
                    entries = list(feed.entries)[:max(1, limit_per_source)]
                    counters["items_found"] += len(entries)

                    for entry in entries:
                        try:
                            payload = entry_payload(source, entry)

                            if not payload:
                                counters["items_skipped"] += 1
                                continue

                            dedup_key = build_dedup_key(payload)

                            if entry_exists(cur, dedup_key):
                                counters["items_duplicates"] += 1
                                continue

                            if dry_run:
                                logger.info(f"DRY RUN nova entry {source_name} {payload['source_title']}")
                                continue

                            new_entry_id = insert_entry(cur, payload, dedup_key)
                            counters["items_created"] += 1
                            conn.commit()

                            enrichment_result = run_entry_enrichment(
                                entry_id=new_entry_id,
                                skip_input=False,
                            )

                            enrichment_status = enrichment_result.get("status")

                            if enrichment_status == "enriched":
                                counters["items_enriched"] = counters.get("items_enriched", 0) + 1
                            elif enrichment_status == "discarded":
                                counters["items_discarded_by_relevance"] += 1
                            elif enrichment_status == "error":
                                counters["items_failed"] += 1
                            else:
                                counters["items_left_raw"] = counters.get("items_left_raw", 0) + 1
                                logger.error(
                                    "Entry %s ha acabat amb estat inesperat després de l'enriquiment: %s",
                                    new_entry_id,
                                    enrichment_status,
                                )

                        except Exception as exc:
                            counters["items_failed"] += 1
                            logger.exception(
                                "Error processant entry de %s: %s",
                                source_name,
                                exc,
                            )

                    if not dry_run:
                        update_source_success(cur, source["id"])
                        conn.commit()

                except Exception as exc:
                    counters["items_failed"] += 1
                    logger.exception(
                        "Error descarregant feed de %s (%s): %s",
                        source_name,
                        source["feed_url"],
                        exc,
                    )
                    if not dry_run:
                        conn.rollback()
                        update_source_error(cur, source["id"], str(exc))
                        conn.commit()

            duration_seconds = round(
                (utc_now() - started_at).total_seconds(),
                3,
            )

            if not dry_run:
                notes = (
                    "entries_rss "
                    f"created={counters['items_created']} "
                    f"duplicates={counters['items_duplicates']} "
                    f"skipped={counters['items_skipped']}"
                )
                record_crawler_log(
                    cur=cur,
                    sources_checked=counters["sources_checked"],
                    items_found=counters["items_found"],
                    items_relevant=counters["items_created"],
                    items_enriched=0,
                    items_failed=counters["items_failed"],
                    duration_seconds=duration_seconds,
                    notes=notes,
                )
                conn.commit()

            logger.info(
                "Crawler finalitzat: %s",
                json.dumps(counters, ensure_ascii=False),
            )

            return counters

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawler RSS/Atom d'entries d'AsimovWatch",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="No escriu entries ni actualitza fonts o logs.",
    )
    parser.add_argument(
        "--source-id",
        type=int,
        default=None,
        help="Executa només una source concreta.",
    )
    parser.add_argument(
        "--limit-per-source",
        type=int,
        default=MAX_ENTRIES_PER_SOURCE,
        help=f"Màxim d'entries per source (per defecte {MAX_ENTRIES_PER_SOURCE}).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    try:
        result = run(
            dry_run=args.dry_run,
            source_id=args.source_id,
            limit_per_source=max(1, args.limit_per_source),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        logger.exception("El crawler ha fallat: %s", exc)
        sys.exit(1)
