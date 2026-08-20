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


def entry_exists(cur: Any, dedup_key: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM public.entries
        WHERE dedup_key = %s
        LIMIT 1
        """,
        (dedup_key,),
    )
    return cur.fetchone() is not None


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


def insert_entry(cur: Any, payload: Dict[str, Any], dedup_key: str) -> None:
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
        """,
        {
            **payload,
            "raw_payload": psycopg2.extras.Json(payload["raw_payload"]),
            "dedup_key": dedup_key,
        },
    )


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
                                counters["items_created"] += 1
                                logger.info(
                                    "DRY RUN: nova entry %s — %s",
                                    source_name,
                                    payload["source_title"],
                                )
                                continue

                            insert_entry(cur, payload, dedup_key)
                            counters["items_created"] += 1

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