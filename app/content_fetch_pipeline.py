"""
Integracio de l'obtencio de contingut al pipeline d'enriquiment (Fase 1B, opcio B).

NOMES s'importa (de manera mandrosa) quan CONTENT_FETCH_ENABLED esta encès: amb
l'interruptor apagat, crawler.py no el carrega mai i el comportament es l'anterior.

Aquest modul decideix, per a UNA entrada, quin text s'ha d'analitzar:
  - elegibilitat (rss, canonical_url http(s), text del feed < llindar, domini no omes,
    llista d'ids opcional);
  - historial d'intents a entry_content_fetches (reutilitzacio, reintents, espera d'1 hora);
  - obtencio (ContentFetcher del mòdul) amb COMMIT immediat de la fila i sense cap
    transaccio oberta durant la xarxa;
  - enllac entries.analyzed_fetch_id (UPDATE minim + COMMIT) abans que s'executi Input;
  - tall determinista del text: a INPUT_TEXT_MAX_CHARS per a Input i a PRIMARY_TEXT_MAX_CHARS per a Primary.
No escriu mai ready_for_primary ni discard_reason, ni toca raw_content/raw_snippet.
Qualsevol excepcio es "fail-open": log + continuar amb el text del feed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from app import content_fetch as cf
from app import content_fetch_store as store

logger = logging.getLogger("asimovwatch.content_fetch")

PRIMARY_TEXT_MAX_CHARS_ENV = "PRIMARY_TEXT_MAX_CHARS"
DEFAULT_PRIMARY_TEXT_MAX_CHARS = 20000
INPUT_TEXT_MAX_CHARS_ENV = "INPUT_TEXT_MAX_CHARS"
DEFAULT_INPUT_TEXT_MAX_CHARS = 6000
SKIP_DOMAINS_ENV = "CONTENT_FETCH_SKIP_DOMAINS"
DEFAULT_SKIP_DOMAINS = "openai.com,fra.europa.eu"
ENTRY_IDS_ENV = "CONTENT_FETCH_ENTRY_IDS"

MAX_TEMPORARY_ATTEMPTS = 3
RETRY_MIN_INTERVAL = timedelta(hours=1)
RETRY_MIN_INTERVAL_SECONDS = int(RETRY_MIN_INTERVAL.total_seconds())


def sufficient_text_chars() -> int:
    """Llindar de text suficient: el MATEIX valor que usa el modul per a 'text_too_short' (cf.MIN_TEXT_CHARS)."""
    return cf.MIN_TEXT_CHARS


# ---------------------------------------------------------------------------
# Configuracio per entorn
# ---------------------------------------------------------------------------

def primary_text_max_chars(environ: Optional[Mapping[str, str]] = None) -> int:
    environ = os.environ if environ is None else environ
    try:
        value = int(str(environ.get(PRIMARY_TEXT_MAX_CHARS_ENV, "")).strip())
    except ValueError:
        return DEFAULT_PRIMARY_TEXT_MAX_CHARS
    return value if value > 0 else DEFAULT_PRIMARY_TEXT_MAX_CHARS


def input_text_max_chars(environ: Optional[Mapping[str, str]] = None) -> int:
    """Limit del text que rep Input (per defecte 6000); mateixa logica d'entorn que PRIMARY_TEXT_MAX_CHARS."""
    environ = os.environ if environ is None else environ
    try:
        value = int(str(environ.get(INPUT_TEXT_MAX_CHARS_ENV, "")).strip())
    except ValueError:
        return DEFAULT_INPUT_TEXT_MAX_CHARS
    return value if value > 0 else DEFAULT_INPUT_TEXT_MAX_CHARS


def skip_domains(environ: Optional[Mapping[str, str]] = None) -> List[str]:
    environ = os.environ if environ is None else environ
    raw = environ.get(SKIP_DOMAINS_ENV)
    if raw is None:
        raw = DEFAULT_SKIP_DOMAINS
    return [d for d in (part.strip().lower() for part in raw.split(",")) if d]


def allowed_entry_ids(environ: Optional[Mapping[str, str]] = None) -> Optional[set]:
    """None = totes les entrades; un conjunt d'ids = nomes aquestes (valors no enters s'ignoren)."""
    environ = os.environ if environ is None else environ
    ids = set()
    for part in (environ.get(ENTRY_IDS_ENV) or "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids or None


# ---------------------------------------------------------------------------
# Funcions pures
# ---------------------------------------------------------------------------

def cap_text(text: str, max_chars: int) -> Tuple[str, bool]:
    """
    Talla `text` a com a molt `max_chars` caracters, DETERMINISTICAMENT: a l'ultim espai abans del
    limit (si n'hi ha); si no n'hi ha cap, al limit exacte. Retorna (text, s'ha_tallat).
    """
    if len(text) <= max_chars:
        return text, False
    head = text[:max_chars]
    cut = head.rfind(" ")
    return (head[:cut] if cut > 0 else head), True


def host_of(url: Any) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def domain_skipped(host: str, skip: List[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in skip if host)


def is_temporary_failure(fetch_status: Optional[str], http_status: Optional[int]) -> bool:
    """timeout, robots_unreachable, o http_error amb HTTP 5xx o 429. La resta (inclosos 4xx) no es reintenta."""
    if fetch_status in ("timeout", "robots_unreachable"):
        return True
    if fetch_status == "http_error":
        return isinstance(http_status, int) and (http_status == 429 or 500 <= http_status <= 599)
    return False


def feed_text_of(entry: Mapping[str, Any]) -> str:
    return (entry.get("raw_content") or entry.get("raw_snippet") or "").strip()


def eligibility(
    entry: Mapping[str, Any], environ: Optional[Mapping[str, str]] = None
) -> Tuple[bool, str]:
    """(elegible, motiu). Un domini omes no fa cap peticio ni cap fila: nomes s'enregistra al log."""
    environ = os.environ if environ is None else environ
    allowed = allowed_entry_ids(environ)
    if allowed is not None and entry.get("id") not in allowed:
        return False, "not_in_entry_ids"
    if entry.get("ingest_method") != "rss":
        return False, "not_rss"
    url = entry.get("canonical_url")
    if not isinstance(url, str) or not url.strip():
        return False, "no_canonical_url"
    try:
        parts = urlsplit(url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False, "canonical_url_not_http"
    except Exception:
        return False, "canonical_url_not_http"
    if len(feed_text_of(entry)) >= sufficient_text_chars():
        return False, "feed_text_sufficient"
    skip = skip_domains(environ)
    for domain in {host_of(url.strip()), (entry.get("source_domain") or "").strip().lower()}:
        if domain_skipped(domain, skip):
            logger.info("Entry %s: domini omes de l'obtencio de contingut (%s).", entry.get("id"), domain)
            return False, "domain_skipped"
    return True, "eligible"


# ---------------------------------------------------------------------------
# Decisio
# ---------------------------------------------------------------------------

@dataclass
class FetchDecision:
    """
    kind:
      'none'         no hi ha hagut obtencio (no elegible o desactivada)
      'use_fetched'  s'analitza `text` (capat); entries.analyzed_fetch_id = fetch_id ja desat i confirmat
      'defer'        fallada temporal amb intents restants: l'entrada es queda RAW (run_entry_enrichment -> 'skipped')
      'fallback'     continuar amb el text del feed
    failed: hi ha hagut un intent d'obtencio fallit (o un error del bloc) -> motiu de descart 'fetch_failed'
    """
    kind: str
    text: Optional[str] = None          # text per a Primary (capat a PRIMARY_TEXT_MAX_CHARS)
    input_text: Optional[str] = None    # text per a Input (capat a INPUT_TEXT_MAX_CHARS; mai mes llarg que `text`)
    fetch_id: Optional[int] = None
    reason: str = ""
    failed: bool = False
    detail: Dict[str, Any] = field(default_factory=dict)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _text_of(conn: Any, fetch_id: int) -> Optional[str]:
    try:
        return store.get_fetch_text(conn, fetch_id)
    finally:
        conn.rollback()  # lectura: no deixar cap transaccio oberta


def _link_entry(conn: Any, entry: Mapping[str, Any], fetch_id: int) -> None:
    """UPDATE minim d'analyzed_fetch_id + COMMIT (abans d'Input, perque l'enllac no es perdi)."""
    if entry.get("analyzed_fetch_id") == fetch_id:
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE public.entries SET analyzed_fetch_id = %s WHERE id = %s", (fetch_id, entry["id"]))
    conn.commit()


def _use(conn: Any, entry: Mapping[str, Any], text: str, fetch_id: int, environ: Mapping[str, str]) -> FetchDecision:
    primary_limit = primary_text_max_chars(environ)
    capped, cut = cap_text(text, primary_limit)
    if cut:
        logger.info(
            "Entry %s: text obtingut tallat de %d a %d caracters per a Primary (limit %d).",
            entry.get("id"), len(text), len(capped), primary_limit,
        )
    input_limit = input_text_max_chars(environ)
    input_text, input_cut = cap_text(capped, input_limit)     # Input mai rep mes que Primary
    if input_cut:
        logger.info(
            "Entry %s: text obtingut tallat de %d a %d caracters per a Input (limit %d).",
            entry.get("id"), len(text), len(input_text), input_limit,
        )
    _link_entry(conn, entry, fetch_id)
    return FetchDecision("use_fetched", text=capped, input_text=input_text, fetch_id=fetch_id, reason="fetched_text")


def _from_history(
    conn: Any, entry: Mapping[str, Any], rows: List[Dict[str, Any]], feed_len: int,
    now: datetime, environ: Mapping[str, str],
) -> Optional[FetchDecision]:
    """Decisio nomes amb els intents anteriors (sense xarxa). None = cal una obtencio nova."""
    threshold = sufficient_text_chars()

    for row in rows:                                    # text ja obtingut (ok / duplicate)
        if row["fetch_status"] in ("ok", "duplicate"):
            text = _text_of(conn, row["id"])
            if text and len(text) >= threshold:
                return _use(conn, entry, text, row["id"], environ)
    for row in rows:                                    # text curt, pero mes llarg que el del feed
        if row["fetch_status"] == "text_too_short":
            text = _text_of(conn, row["id"])
            if text and len(text) > feed_len:
                return _use(conn, entry, text, row["id"], environ)

    temporary = [r for r in rows if is_temporary_failure(r["fetch_status"], r.get("http_status"))]
    if len(temporary) < len(rows):                      # hi ha una fallada NO reintentable
        return FetchDecision("fallback", reason="previous_permanent_failure", failed=True)
    if len(temporary) >= MAX_TEMPORARY_ATTEMPTS:
        return FetchDecision("fallback", reason="temporary_attempts_exhausted", failed=True)
    if temporary:
        newest = max(r["fetched_at"] for r in temporary)
        if now - newest < RETRY_MIN_INTERVAL:
            return FetchDecision(
                "defer", reason="retry_too_soon",
                detail={"attempts": len(temporary), "next_attempt_after": (newest + RETRY_MIN_INTERVAL).isoformat()},
            )
    return None


def _from_record(
    conn: Any, entry: Mapping[str, Any], record: Any, fetch_id: int, previous_temporary: int,
    feed_len: int, environ: Mapping[str, str],
) -> FetchDecision:
    threshold = sufficient_text_chars()
    status = record.fetch_status

    if status in ("ok", "duplicate"):
        text = record.text_content if status == "ok" else _text_of(conn, fetch_id)
        if text and len(text) >= threshold:
            return _use(conn, entry, text, fetch_id, environ)
        return FetchDecision("fallback", reason="fetched_text_unusable", failed=True)
    if status == "text_too_short":
        if record.text_content and len(record.text_content) > feed_len:
            return _use(conn, entry, record.text_content, fetch_id, environ)
        return FetchDecision("fallback", reason="text_too_short_no_improvement", failed=True)
    if is_temporary_failure(status, record.http_status):
        attempts = previous_temporary + 1
        if attempts >= MAX_TEMPORARY_ATTEMPTS:
            return FetchDecision("fallback", reason="temporary_attempts_exhausted", failed=True,
                                 detail={"fetch_status": status})
        return FetchDecision("defer", reason="temporary_failure", detail={"attempts": attempts, "fetch_status": status})
    return FetchDecision("fallback", reason=f"permanent_failure:{status}", failed=True)


def prepare_analysis_text(
    conn: Any,
    entry: Mapping[str, Any],
    fetcher: Any = None,
    environ: Optional[Mapping[str, str]] = None,
    now: Optional[datetime] = None,
) -> FetchDecision:
    """
    Decideix el text d'analisi d'una entrada. FAIL-OPEN: qualsevol excepcio -> log + continuar amb el feed
    (kind='fallback', failed=True); mai posa l'entrada a ERROR ni incrementa processing_retries.
    """
    environ = os.environ if environ is None else environ
    eligible, reason = eligibility(entry, environ)
    if not eligible:
        return FetchDecision("none", reason=reason)

    try:
        now = now or _utc_now()
        feed_len = len(feed_text_of(entry))
        conn.rollback()                                  # tanca la lectura de l'entrada: res obert abans de la xarxa

        rows = store.list_entry_fetches(conn, entry["id"])
        conn.rollback()
        decision = _from_history(conn, entry, rows, feed_len, now, environ)
        if decision is not None:
            return decision
        previous_temporary = len(rows)                   # (si arribem aqui, totes son temporals)

        fetcher = fetcher or cf.ContentFetcher()

        def lookup(url: str) -> Optional[int]:
            found = store.find_reusable_fetch(conn, url)
            conn.rollback()                              # la xarxa que segueix no pot trobar una transaccio oberta
            return found

        record = fetcher.fetch_entry_content(entry["canonical_url"].strip(), find_reusable=lookup)
        fetch_id = store.insert_fetch(conn, entry["id"], record)
        conn.commit()                                    # visible per a altres processos
        return _from_record(conn, entry, record, fetch_id, previous_temporary, feed_len, environ)
    except Exception as exc:
        logger.warning("Entry %s: error al bloc d'obtencio de contingut; es continua amb el text del feed: %s",
                       entry.get("id"), exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return FetchDecision("fallback", reason="error", failed=True, detail={"error": type(exc).__name__})
