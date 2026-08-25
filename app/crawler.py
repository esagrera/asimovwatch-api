import argparse
import hashlib
import html
import json
import logging
import os
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
        "source_language": source.get("language_default"),
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

# ──────────────────────────────────────────────────────────────────────────────
# CERCA TEMÀTICA
# ──────────────────────────────────────────────────────────────────────────────


def get_prompt_config(prompt_key: str) -> Optional[Dict[str, Any]]:
    """
    Carrega la configuració d'un prompt des de la base de dades.
    
    Retorna un dict amb:
    - key: la clau del prompt
    - value: el text del prompt
    - provider: el provider (ex: "perplexity")
    - model: el model (ex: "sonar-pro")
    - use_fallback, max_tokens, temperature, timeout_secs: configuració
    """
    conn = get_connection()

    try:
        with conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(
                """
                SELECT
                    p.key,
                    p.value,
                    c.provider,
                    c.model,
                    c.use_fallback,
                    c.max_tokens,
                    c.temperature,
                    c.timeout_secs
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

            row = cur.fetchone()

            if not row:
                return None

            return dict(row)
    finally:
        conn.close()


def call_llm(
    prompt_text: str,
    user_message: str,
    provider: str,
    model: str,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    timeout_secs: Optional[int] = None,
) -> str:
    """
    Crida un LLM utilitzant la configuració del prompt.
    
    Suporta:
    - Perplexity API
    - OpenAI API
    - Altres providers compatibles amb OpenAI SDK
    """
    import os
    from openai import OpenAI
    
    # Determinar API key i base_url segons el provider
    if provider == "perplexity":
        api_key = os.getenv("PERPLEXITY_API_KEY")
        base_url = "https://api.perplexity.ai"
    elif provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        base_url = "https://api.openai.com/v1"
    else:
        # Provider genèric compatible amb OpenAI SDK
        api_key = os.getenv(f"{provider.upper()}_API_KEY")
        base_url = os.getenv(f"{provider.upper()}_BASE_URL")
    
    if not api_key:
        raise ValueError(f"API key no configurada per al provider: {provider}")
    
    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout_secs or 30,
    )
    
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": prompt_text},
            {"role": "user", "content": user_message},
        ],
        max_tokens=max_tokens or 2000,
        temperature=temperature or 0.3,
    )
    
    return response.choices[0].message.content

# =========================================================================
# TITLE: Funcions auxiliars del pipeline d'enriquiment (Input -> Primary -> Output)
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


def _build_input_user_message(entry: Dict[str, Any]) -> str:
    """
    Construeix el user_message per al prompt Input a partir d'una entry RAW.
    Fa servir raw_content si existeix; sinó, cau a raw_snippet.
    """
    content = entry.get("raw_content") or entry.get("raw_snippet") or ""

    return f"""Metadades de la peça:
source_url: {entry.get('source_url', '')}
source_domain: {entry.get('source_domain', '')}
source_title: {entry.get('source_title', '')}
source_type: {entry.get('source_type') or 'desconegut'}
country_region: {entry.get('country_region') or 'desconegut'}
institution_type: {entry.get('institution_type') or 'desconegut'}
published_date: {entry.get('published_date') or 'desconeguda'}

Contingut rebut:
{content}
"""


def _build_primary_user_message(entry: Dict[str, Any], input_result: Optional[Dict[str, Any]]) -> str:
    """
    Construeix el user_message per al prompt Primary.
    - Si input_result no és None (via RSS + Input): usa clean_input_text/input_summary.
    - Si input_result és None (via cerca temàtica): usa raw_snippet/why_relevant + search_brief.
    """
    if input_result is not None:
        clean_text = input_result.get("clean_input_text") or entry.get("raw_content") or entry.get("raw_snippet") or ""
        summary = input_result.get("input_summary") or ""
        source_note = "Origen: ingesta RSS/Atom, processada prèviament per la fase Input."
    else:
        raw_payload = entry.get("raw_payload") or {}
        search_result = raw_payload.get("search_result") or {}
        why_relevant = search_result.get("why_relevant") or entry.get("raw_snippet") or ""
        search_brief = raw_payload.get("search_brief") or ""
        clean_text = why_relevant
        summary = f"Cerca temàtica motivada pel brief: {search_brief}" if search_brief else ""
        source_note = "Origen: cerca temàtica (web_search), sense pas previ per la fase Input."

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
{clean_text}
"""


def _parse_json_output(raw_text: str) -> Dict[str, Any]:
    """
    Neteja i parseja la sortida JSON d'un LLM, seguint el mateix patró
    ja usat a _perplexity_search_raw() per retirar embolcalls ```json.
    """
    cleaned = raw_text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]

    try:
        return json.loads(cleaned.strip())
    except json.JSONDecodeError as exc:
        logger.error(f"JSON invàlid retornat pel LLM: {exc}")
        logger.error(f"Contingut rebut: {cleaned[:500]}")
        raise ValueError("El model ha retornat JSON invàlid. Revisa els logs per més detalls.") from exc


def _validate_and_normalize_primary_output(primary_result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Valida i normalitza la sortida de Primary abans de persistir-la.
    - entry_category ha de ser un dels 7 valors vàlids; si no, es força 'other'.
    - human_protection_declared/verifiable/depth han de ser green/yellow/red/unknown
      (constraint SQL real); si no, es força 'unknown'.
    """
    result = dict(primary_result)
    notes = result.get("confidence_notes") or ""

    category = result.get("entry_category")
    if category not in VALID_ENTRY_CATEGORIES:
        notes += f" [Avís: entry_category '{category}' no vàlid, forçat a 'other'.]"
        result["entry_category"] = "other"

    for field in ("human_protection_declared", "human_protection_verifiable", "human_protection_depth"):
        value = result.get(field)
        if value is not None and value not in VALID_BIHP_LABELS:
            notes += f" [Avís: {field}='{value}' no vàlid, forçat a 'unknown'.]"
            result[field] = "unknown"

    result["confidence_notes"] = notes.strip() or None
    return result


def run_entry_enrichment(entry_id: int, skip_input: bool = False) -> Dict[str, Any]:
    """
    Servei únic d'enriquiment per a qualsevol entry, independentment de l'origen.

    skip_input=False -> aplica el gate Input abans de Primary (ús: crawler RSS)
    skip_input=True  -> salta Input, va directe a Primary (ús: cerca temàtica)

    Retorna: {"status": "enriched" | "discarded" | "error", "entry_id": ..., "detail": ...}
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM public.entries WHERE id = %s", (entry_id,))
            entry = cur.fetchone()
            if not entry:
                return {"status": "error", "entry_id": entry_id, "detail": "Entry not found"}

            input_result = None

            if not skip_input:
                input_prompt_config = get_prompt_config("Input")
                if not input_prompt_config:
                    raise ValueError("Prompt 'Input' no trobat a la base de dades")

                user_message = _build_input_user_message(entry)
                raw_output = call_llm(
                    prompt_text=input_prompt_config["value"],
                    user_message=user_message,
                    provider=input_prompt_config["provider"],
                    model=input_prompt_config["model"],
                    max_tokens=int(input_prompt_config["max_tokens"]) if input_prompt_config.get("max_tokens") is not None else None,
                    temperature=float(input_prompt_config["temperature"]) if input_prompt_config.get("temperature") is not None else None,
                    timeout_secs=int(input_prompt_config["timeout_secs"]) if input_prompt_config.get("timeout_secs") is not None else None,
                )
                input_result = _parse_json_output(raw_output)

                ready = input_result.get("ready_for_primary")

                if ready == "no":
                    cur.execute("""
                        UPDATE public.entries SET
                            processing_status = 'DISCARDED',
                            input_relevance = %s,
                            input_relevance_reason = %s,
                            ready_for_primary = %s,
                            input_quality = %s,
                            input_quality_notes = %s,
                            raw_content = NULL,
                            raw_payload = NULL,
                            updated_at = NOW()
                        WHERE id = %s
                    """, (
                        input_result.get("input_relevance"),
                        input_result.get("input_relevance_reason"),
                        ready,
                        input_result.get("input_quality"),
                        input_result.get("input_quality_notes"),
                        entry_id,
                    ))
                    conn.commit()
                    return {"status": "discarded", "entry_id": entry_id, "detail": input_result}

                # yes / unclear -> guardar resultat d'Input i continuar
                cur.execute("""
                    UPDATE public.entries SET
                        input_relevance = %s,
                        input_relevance_reason = %s,
                        ready_for_primary = %s,
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
                    input_result.get("clean_input_text"),
                    input_result.get("input_summary"),
                    input_result.get("input_quality"),
                    input_result.get("input_quality_notes"),
                    input_result.get("source_language"),
                    entry_id,
                ))
                conn.commit()

            # Fase Primary (per a totes les entries que arriben aquí)
            primary_prompt_config = get_prompt_config("Primary")
            if not primary_prompt_config:
                raise ValueError("Prompt 'Primary' no trobat a la base de dades")

            primary_user_message = _build_primary_user_message(entry, input_result)
            raw_primary_output = call_llm(
                prompt_text=primary_prompt_config["value"],
                user_message=primary_user_message,
                provider=primary_prompt_config["provider"],
                model=primary_prompt_config["model"],
                max_tokens=int(primary_prompt_config["max_tokens"]) if primary_prompt_config.get("max_tokens") is not None else None,
                temperature=float(primary_prompt_config["temperature"]) if primary_prompt_config.get("temperature") is not None else None,
                timeout_secs=int(primary_prompt_config["timeout_secs"]) if primary_prompt_config.get("timeout_secs") is not None else None,
            )
            primary_result = _parse_json_output(raw_primary_output)

            # Fase Output: validació/normalització mínima abans de persistir
            output_result = _validate_and_normalize_primary_output(primary_result)

            cur.execute("""
                UPDATE public.entries SET
                    processing_status = 'ENRICHED',
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
                output_result.get("summary_factual"),
                output_result.get("why_it_matters"),
                output_result.get("theme_tags"),
                output_result.get("affected_principles"),
                output_result.get("risk_level"),
                output_result.get("debate_questions"),
                output_result.get("confidence_notes"),
                output_result.get("human_protection_declared"),
                output_result.get("human_protection_verifiable"),
                output_result.get("human_protection_depth"),
                output_result.get("human_protection_notes"),
                output_result.get("entry_category"),
                output_result.get("analyzed_provider"),
                output_result.get("analyzed_model"),
                psycopg2.extras.Json(output_result.get("bihp_directives") or []),
                primary_prompt_config["model"],
                entry_id,
            ))
            conn.commit()
            return {"status": "enriched", "entry_id": entry_id, "detail": output_result}

    except Exception as exc:
        conn.rollback()
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
        logger.exception(f"Error enriquint entry {entry_id}: {exc}")
        return {"status": "error", "entry_id": entry_id, "detail": str(exc)}
    finally:
        conn.close()

def _render_prompt_template(
    template: str,
    brief: str,
    date_range: Optional[str],
    source_scope: Optional[str],
    source_types: Optional[list[str]],
    max_results: int,
) -> str:
    """
    Substitueix els placeholders {{...}} del prompt pel valor real.
    
    Suporta:
    - {{brief}}
    - {{date_range}}
    - {{source_scope}}
    - {{source_types}}
    - {{max_results}}
    """
    rendered = template
    
    # {{brief}}
    rendered = rendered.replace("{{brief}}", brief or "")
    
    # {{date_range}}
    rendered = rendered.replace("{{date_range}}", date_range or "Últim any")
    
    # {{source_scope}}
    rendered = rendered.replace("{{source_scope}}", source_scope or "web_and_monitored_sources")
    
    # {{source_types}} (llista → string comma-separated)
    if source_types:
        source_types_str = ", ".join(source_types)
    else:
        source_types_str = "qualsevol"
    rendered = rendered.replace("{{source_types}}", source_types_str)
    
    # {{max_results}}
    rendered = rendered.replace("{{max_results}}", str(max_results))
    
    return rendered

def _perplexity_search_raw(
    prompt_text: str,
    provider: str,
    model: str,
    max_tokens: Optional[int],
    temperature: Optional[float],
    timeout_secs: Optional[int],
) -> List[Dict[str, Any]]:
    """
    Fa la cerca web utilitzant el prompt configurat (ja interpolat).
    
    Retorna una llista de resultats amb:
    - title, url, publisher, published_date, source_kind, language, why_relevant
    """
    import os
    from openai import OpenAI
    
    api_key = os.getenv("PERPLEXITY_API_KEY")
    
    logger.info("Iniciant cerca Perplexity amb prompt interpolat")
    logger.info("Provider: %s, Model: %s", provider, model)
    
    if not api_key:
        raise ValueError("PERPLEXITY_API_KEY no configurada")
    
    # Determinar base_url segons provider
    if provider == "perplexity":
        base_url = "https://api.perplexity.ai"
    elif provider == "openai":
        base_url = "https://api.openai.com/v1"
    else:
        base_url = os.getenv(f"{provider.upper()}_BASE_URL")
        if not base_url:
            raise ValueError(f"BASE_URL no configurada per al provider: {provider}")
    
    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout_secs or 30,
    )
    
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": prompt_text},
        ],
        max_tokens=max_tokens or 4000,
        temperature=temperature or 0.3,
    )
    
    content = response.choices[0].message.content
    
    # Parsejar JSON
    try:
        cleaned = content.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        
        parsed = json.loads(cleaned.strip())
        
        # Validar estructura mínima
        if not isinstance(parsed, dict) or "results" not in parsed:
            raise ValueError("El JSON no conté la clau 'results'")
        
        results = []
        for item in parsed.get("results", []):
            if not isinstance(item, dict):
                continue
            
            title = item.get("title")
            url = item.get("url")
            
            # Validar que tinguem almenys title i url
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
        
    except json.JSONDecodeError as exc:
        logger.error("JSON invàlid retornat per Perplexity: %s", exc)
        logger.error("Contingut rebut: %s", content[:500])
        raise ValueError(
            "El model ha retornat JSON invàlid. Revisa els logs per més detalls."
        ) from exc


def run_thematic_search(
    brief: str,
    date_range: Optional[str] = "Últim any",
    source_scope: Optional[str] = "web_and_monitored_sources",
    source_types: Optional[list[str]] = None,
    max_results: int = 10,
    run_enrichment: bool = True,
    dry_run: bool = False,
    requested_by: str = "admin-thematic-search",
    prompt_key: str = "Thematic entry discovery",
) -> Dict[str, Any]:
    """
    Executa una cerca temàtica web utilitzant el prompt especificat.
    
    Retorna un dict amb els comptadors i warnings.
    """
    started_at = utc_now()
    
    logger.info("Iniciant cerca temàtica per a: %s", brief)
    logger.info("Prompt key: %s", prompt_key)
    
    # Inicialitzar comptadors (amb llistes per duplicats)
    counters = {
        "sources_checked": 1,
        "items_found": 0,
        "items_created": 0,
        "items_duplicates": [],  # Llista d'IDs
        "items_duplicates_monitored": [],  # NOU: IDs de fonts promogudes
        "items_skipped": 0,
        "items_failed": 0,
    }
    # LOG TEMPORAL PER VALIDAR
    logger.info("DEBUG: counters inicials = %s", json.dumps(counters, ensure_ascii=False))

    
    warnings = []  # Per informar duplicats de fonts promogudes
    
    try:
        # 1. Carregar el prompt des de la base de dades
        prompt_config = get_prompt_config(prompt_key)
        if not prompt_config:
            raise ValueError(f"Prompt '{prompt_key}' no trobat a la base de dades")
        
        logger.info(
            "Carregat prompt: key=%s, provider=%s, model=%s",
            prompt_key,
            prompt_config["provider"],
            prompt_config["model"],
        )
        
        # 2. Validar capacitat de cerca web
        WEBSEARCH_CAPABLE_PROVIDERS = {"perplexity"}
        wants_web_search = source_scope in ("web_and_monitored_sources", "web_only")
        
        if wants_web_search and prompt_config["provider"].lower() not in WEBSEARCH_CAPABLE_PROVIDERS:
            if source_scope == "web_only":
                raise ValueError(
                    f"El provider '{prompt_config['provider']}' no té capacitat de cerca web real. "
                    "No es pot executar una cerca 'web_only' amb aquest model."
                )
            # web_and_monitored_sources: degradar, no fallar
            logger.warning(
                "Provider '%s' sense capacitat de cerca web. "
                "Cerca temàtica limitada (no es farà cerca web oberta).",
                prompt_config["provider"],
            )
            warnings.append("Cerca web omesa: el model configurat no té capacitat de cerca real.")
            # En aquest cas, no cal continuar - no hi ha res a cercar
            return {**counters, "warnings": warnings}
        
        # 3. Interpolar el prompt amb els valors reals
        prompt_text = _render_prompt_template(
            template=prompt_config["value"],
            brief=brief,
            date_range=date_range,
            source_scope=source_scope,
            source_types=source_types,
            max_results=max_results,
        )
        
        logger.info("Prompt interpolat preparat (%d caràcters)", len(prompt_text))
        
        # 4. Fer la cerca web amb el prompt interpolat
        search_results = _perplexity_search_raw(
            prompt_text=prompt_text,
            provider=prompt_config["provider"],
            model=prompt_config["model"],
            max_tokens=int(prompt_config["max_tokens"]) if prompt_config["max_tokens"] else None,
            temperature=float(prompt_config["temperature"]) if prompt_config["temperature"] else None,
            timeout_secs=int(prompt_config["timeout_secs"]) if prompt_config["timeout_secs"] else None,
        )
        
        counters["items_found"] = len(search_results)
        
        # 5. Processar cada resultat
        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                for result in search_results:
                    try:
                        # Construir payload "cru" (sense enriquiment)
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
                        
                        # 6. Generar dedup_key
                        dedup_key = build_dedup_key(payload)
                        
                        # 7. Verificar si ja existeix
                        existing_entry_id = entry_exists(cur, dedup_key)
                        if existing_entry_id is not None:
                            # Verificar si és una font promoguda
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
                        
                        # 8. Validar que la font sigui verificable
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
                        
                        # 9. Mode dry_run: validar i comptar, però no inserir
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
        finally:
            conn.close()
        
        logger.info(
            "Cerca temàtica finalitzada: %s",
            json.dumps(counters, ensure_ascii=False),
        )
        
        # Afegir warnings al resultat final
        return {**counters, "warnings": warnings}
    
    except Exception as exc:
        logger.exception("Error en cerca temàtica: %s", exc)
        counters["items_failed"] += 1
        return {**counters, "warnings": warnings, "error": str(exc)}


# ──────────────────────────────────────────────────────────────────────────────
# CRAWLER RSS
# ──────────────────────────────────────────────────────────────────────────────

def run(
    dry_run: bool = False,
    source_id: Optional[int] = None,
    limit_per_source: int = MAX_ENTRIES_PER_SOURCE,
) -> Dict[str, int]:
    started_at = utc_now()
    counters = {
        "sources_checked": 0,
        "items_found": 0,
        "items_created": 0,
        "items_duplicates": 0,
        "items_discarded_by_relevance": 0,
        "items_skipped": 0,
        "items_failed": 0,
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

                            new_entry_id = insert_entry(cur, payload, dedup_key)  # cal que retorni l'id (veure nota)
                            counters["items_created"] += 1
                            conn.commit()  # cal fer commit abans de cridar l'enriquiment perquè la fila existeixi

                            enrichment_result = run_entry_enrichment(entry_id=new_entry_id, skip_input=False)
                            if enrichment_result["status"] == "discarded":
                                counters["items_discarded_by_relevance"] = counters.get("items_discarded_by_relevance", 0) + 1
                            elif enrichment_result["status"] == "error":
                                counters["items_failed"] += 1

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