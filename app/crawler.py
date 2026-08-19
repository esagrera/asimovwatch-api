import json
import os
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from psycopg2.extras import RealDictCursor

from app.db import get_connection


USER_AGENT = "AsimovWatch/0.1 (+https://asimovwatch.com)"
REQUEST_TIMEOUT = 20
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
API_KEY = os.getenv("API_KEY", "")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value.strip()


def get_domain(value: Optional[str]) -> str:
    if not value:
        return ""
    return (urlparse(value).netloc or "").lower()


def parse_feed_date(entry: Dict[str, Any]) -> Optional[datetime]:
    value = entry.get("published") or entry.get("updated")
    if not value:
        return None

    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def get_entry_content(entry: Dict[str, Any]) -> str:
    content_items = entry.get("content") or []
    if content_items:
        value = content_items[0].get("value")
        if value:
            return str(value).strip()

    return str(entry.get("summary") or entry.get("description") or "").strip()


def html_to_text(html: str) -> str:
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "aside"]):
        tag.decompose()

    root = soup.find("article") or soup.find("main") or soup.body or soup
    return root.get_text(" ", strip=True)


def fetch_article_text(url: str) -> Dict[str, Any]:
    response = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        allow_redirects=True,
    )
    response.raise_for_status()

    content_type = (response.headers.get("content-type") or "").lower()
    text = html_to_text(response.text) if "html" in content_type else ""

    return {
        "url": response.url,
        "content_type": content_type,
        "text": text,
        "status_code": response.status_code,
    }


def fetch_rss_sources(source_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        query = """
            SELECT
                id,
                name,
                url,
                domain,
                source_type,
                institution_type,
                country_region,
                feed_url,
                ingest_method,
                language_default,
                crawl_frequency_minutes,
                priority
            FROM public.sources
            WHERE status = 'ACTIVE'
              AND LOWER(COALESCE(ingest_method, '')) = 'rss'
              AND feed_url IS NOT NULL
              AND TRIM(feed_url) <> ''
        """
        params: List[Any] = []

        if source_ids:
            query += " AND id = ANY(%s)"
            params.append(source_ids)

        query += " ORDER BY priority ASC, id ASC"

        cur.execute(query, params)
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


def update_source_success(source_id: int) -> None:
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            UPDATE public.sources
            SET
                last_checked_at = now(),
                last_success_at = now(),
                last_error_message = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (source_id,),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def update_source_error(source_id: int, message: str) -> None:
    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            UPDATE public.sources
            SET
                last_checked_at = now(),
                last_error_at = now(),
                last_error_message = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (message[:2000], source_id),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def build_entry_payload(
    source: Dict[str, Any],
    entry: Dict[str, Any],
    article_data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    link = normalize_url(entry.get("link"))
    guid = normalize_url(entry.get("id") or entry.get("guid"))
    canonical_url = (
        normalize_url(article_data.get("url"))
        if article_data
        else guid or link
    )

    rss_content = get_entry_content(entry)
    raw_content = (
        article_data.get("text", "").strip()
        if article_data and article_data.get("text")
        else html_to_text(rss_content)
    )
    raw_snippet = html_to_text(rss_content)[:2000]

    source_url = canonical_url or link or source["url"]
    source_domain = source.get("domain") or get_domain(source_url)

    published_dt = parse_feed_date(entry)

    payload = {
        "source_url": source_url,
        "source_domain": source_domain,
        "source_title": str(entry.get("title") or source["name"]).strip(),
        "source_type": source.get("source_type"),
        "source_language": source.get("language_default"),
        "ingest_method": "rss",
        "external_id": guid or link,
        "canonical_url": canonical_url,
        "published_date": published_dt.isoformat() if published_dt else None,
        "detected_at": utc_now().isoformat(),
        "country_region": source.get("country_region"),
        "institution_type": source.get("institution_type"),
        "raw_snippet": raw_snippet or None,
        "raw_content": raw_content or None,
        "raw_content_format": "html" if article_data and article_data.get("text") else "rss",
        "raw_payload": {
            "source_id": source["id"],
            "source_name": source["name"],
            "feed_url": source["feed_url"],
            "feed_title": entry.get("title"),
            "feed_link": link,
            "feed_guid": guid,
            "feed_categories": [tag.get("term") for tag in (entry.get("tags") or []) if tag.get("term")],
            "article_fetch": article_data,
        },
        "review_status": "NEW",
    }

    return payload


def post_entry(payload: Dict[str, Any], dry_run: bool) -> str:
    if dry_run:
        return "dry_run"

    if not API_KEY:
        raise RuntimeError("API_KEY no configurada per al crawler")

    response = requests.post(
        f"{API_BASE_URL}/entries",
        json=payload,
        timeout=REQUEST_TIMEOUT,
        headers={
            "User-Agent": USER_AGENT,
            "X-API-Key": API_KEY,
            "Content-Type": "application/json",
        },
    )

    if response.status_code == 409:
        return "duplicate"

    response.raise_for_status()
    return "created"


def run_entry_ingestion(
    source_ids: Optional[List[int]] = None,
    hours_back: int = 24,
    max_items_per_source: int = 20,
    dry_run: bool = True,
) -> Dict[str, Any]:
    since = utc_now() - timedelta(hours=hours_back)

    metrics = {
        "sources_checked": 0,
        "items_found": 0,
        "items_in_window": 0,
        "entries_created": 0,
        "duplicates": 0,
        "dry_run_items": 0,
        "items_failed": 0,
        "source_errors": 0,
        "article_fetch_successes": 0,
        "article_fetch_failures": 0,
        "errors": [],
    }

    sources = fetch_rss_sources(source_ids=source_ids)

    for source in sources:
        metrics["sources_checked"] += 1

        try:
            response = requests.get(
                source["feed_url"],
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                allow_redirects=True,
            )
            response.raise_for_status()

            content_type = (response.headers.get("content-type") or "").lower()
            # Accepta text/plain perquè molts feeds RSS es serveixen així des de GitHub/raw
            if not any(ct in content_type for ct in ["xml", "rss", "atom", "text/plain"]):
                raise RuntimeError(
                    f"Content-Type inesperat per a RSS: {content_type or 'absent'}"
                )

            feed = feedparser.parse(response.content)

            if getattr(feed, "bozo", False) and not feed.entries:
                exc = getattr(feed, "bozo_exception", None)
                raise RuntimeError(f"Feed XML invàlid: {exc or 'sense detall'}")

            if not feed.entries:
                raise RuntimeError("Feed sense entries; revisar feed_url o estructura")

            for entry in feed.entries[:max_items_per_source]:
                metrics["items_found"] += 1

                published_date = parse_feed_date(entry)
                if published_date and published_date < since:
                    continue

                metrics["items_in_window"] += 1
                article_data = None

                link = normalize_url(entry.get("link"))
                if link:
                    try:
                        article_data = fetch_article_text(link)
                        metrics["article_fetch_successes"] += 1
                    except Exception as article_error:
                        metrics["article_fetch_failures"] += 1
                        metrics["errors"].append(
                            {
                                "source_id": source["id"],
                                "url": link,
                                "stage": "article_fetch",
                                "error": str(article_error)[:500],
                            }
                        )

                try:
                    payload = build_entry_payload(source, entry, article_data)
                    result = post_entry(payload, dry_run=dry_run)

                    if result == "created":
                        metrics["entries_created"] += 1
                    elif result == "duplicate":
                        metrics["duplicates"] += 1
                    else:
                        metrics["dry_run_items"] += 1

                except Exception as entry_error:
                    metrics["items_failed"] += 1
                    metrics["errors"].append(
                        {
                            "source_id": source["id"],
                            "url": link,
                            "stage": "entry_create",
                            "error": str(entry_error)[:500],
                        }
                    )

            update_source_success(source["id"])

        except Exception as source_error:
            metrics["source_errors"] += 1
            message = str(source_error)
            update_source_error(source["id"], message)
            metrics["errors"].append(
                {
                    "source_id": source["id"],
                    "feed_url": source["feed_url"],
                    "stage": "feed_fetch",
                    "error": message[:500],
                }
            )

    return metrics