"""
Obtencio de contingut d'articles (Fase 1B, pas 1). Modul AILLAT.

No esta integrat al pipeline: no l'importa crawler.py ni main.py, i no escriu a
public.entries. Parteix de la logica del prototip
prova_obtencio/prova_obtencio_contingut_v2.py, amb aquests canvis:

- Vocabulari: nomes els 11 estats i els 3 valors de text_source que admet el
  CHECK de public.entry_content_fetches (res en catala).
- Camps sense valor: None (mai "" ni 0).
- content_hash: SHA-256 complet (64 hex en minuscula) del text desat.
- Text sense caracters NUL; text_length = len(text_content).
- truncated: True si el cos s'ha tallat per MAX_BYTES.
- normalize_url(): funcio pura que no llanca mai excepcions.
- robots.txt: 2xx s'aplica, 4xx = sense regles, 5xx/timeout/xarxa =
  'robots_unreachable' (no es baixa l'article). Redireccions manuals (maxim 5),
  amb robots.txt de CADA desti.
- Eleccio del contenidor de text (extract_page): si existeix <main> (el primer), el
  candidat es el seu text; si no, el <article> amb mes text; si no hi ha ni <main> ni
  <article>, <body>. Mai no es cau a <body> si existeix <main> o <article>, encara que
  el candidat sigui curt: amb text mes curt que el llindar l'estat es 'text_too_short'
  i es desa el text del candidat triat, amb error_detail
  'container=<main|article|body>;len=<n>;body_len=<n>'.
- Deteccio de repte anti-bot: segment de cami exactament 'challenge' o frase del
  cos (sempre), i titol de repte NOMES si el text extret es mes curt que el
  llindar de text curt (mai per subcadena al cami). No s'intenta resoldre ni
  esquivar.
- No es canvia l'User-Agent ni s'afegeixen capcaleres per esquivar blocs.
- URL demanada a la xarxa = requested_url amb NOMES els parametres utm_*
  eliminats (strip_utm_params); la resta es conserva intacta. normalize_url()
  nomes serveix per a normalized_url i per a la cerca de duplicats.
- Sense canonical_url NO hi ha cap alternativa (ni la URL de la font): el registre
  es 'error' amb error_detail='no_canonical_url'.
- Una resposta 2xx que no es text/html, application/xhtml+xml ni PDF es 'error'
  amb error_detail='unsupported_content_type:<valor>' i sense text.

La persistencia (INSERT / cerca de duplicats) viu a app/content_fetch_store.py.
Aquest modul no fa cap acces a BD: la cerca de duplicats arriba com un callable.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Tuple
from urllib import robotparser
from urllib.parse import parse_qsl, unquote_plus, urldefrag, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Contracte amb la BD (CHECK de public.entry_content_fetches). No modificar.
# ---------------------------------------------------------------------------

FETCH_STATUSES = frozenset({
    "ok",
    "robots_blocked",
    "robots_unreachable",
    "access_denied",
    "anti_bot_challenge",
    "text_too_short",
    "timeout",
    "http_error",
    "error",
    "pdf_unprocessed",
    "duplicate",
})

TEXT_SOURCES = frozenset({"html_article", "html_main", "html_body"})

# contenidor triat per extract_page -> text_source del CHECK
EXTRACT_METHOD_TO_TEXT_SOURCE = {
    "article": "html_article",
    "main": "html_main",
    "body": "html_body",
}

_CONTENT_HASH_RE = re.compile(r"[0-9a-f]{64}")

# ---------------------------------------------------------------------------
# Configuracio
# ---------------------------------------------------------------------------

DEFAULT_USER_AGENT = "AsimovWatch-ContentFetcher/1.0 (+https://asimovwatch.com)"
USER_AGENT_ENV = "CONTENT_FETCH_USER_AGENT"

# Llindar de text curt: el del prototip v2 (MIN_TEXT = 400 caracters).
DEFAULT_MIN_TEXT_CHARS = 400
MIN_TEXT_CHARS_ENV = "CONTENT_FETCH_MIN_TEXT_CHARS"


def _user_agent_from_env(environ: Optional[Mapping[str, str]] = None) -> str:
    environ = os.environ if environ is None else environ
    value = (environ.get(USER_AGENT_ENV) or "").strip()
    return value or DEFAULT_USER_AGENT


def _min_text_chars_from_env(environ: Optional[Mapping[str, str]] = None) -> int:
    environ = os.environ if environ is None else environ
    try:
        value = int(str(environ.get(MIN_TEXT_CHARS_ENV, "")).strip())
    except ValueError:
        return DEFAULT_MIN_TEXT_CHARS
    return value if value >= 0 else DEFAULT_MIN_TEXT_CHARS


# Una sola constant, configurable per variable d'entorn, desada a cada fila.
USER_AGENT = _user_agent_from_env()
MIN_TEXT_CHARS = _min_text_chars_from_env()

REQUEST_TIMEOUT = 20.0          # segons (prototip: TIMEOUT = 20)
ROBOTS_TIMEOUT = 10.0           # timeout acotat per a robots.txt
MAX_BYTES = 3_000_000           # prototip: MAX_BYTES = 3_000_000
ROBOTS_MAX_BYTES = 512 * 1024
DELAY_PER_HOST = 2.0            # segons minims entre peticions al mateix host
MAX_REDIRECTS = 5
ERROR_DETAIL_MAX = 500

# Mateix Accept que el prototip. No s'afegeix cap altra capcalera.
ACCEPT_HEADER = "text/html,application/pdf;q=0.8,*/*;q=0.5"

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

_CHALLENGE_TITLE_RE = re.compile(
    r"verification required|just a moment|attention required|are you a robot|captcha",
    re.IGNORECASE,
)
_CHALLENGE_BODY_PHRASE = "verify that requests are coming from real users"

# ---------------------------------------------------------------------------
# Registre d'un intent
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class FetchRecord:
    """Un intent d'obtencio: una fila de public.entry_content_fetches (sense id ni entry_id)."""

    requested_url: str
    normalized_url: str
    fetch_status: str
    user_agent: str
    fetched_at: datetime
    truncated: bool = False
    final_url: Optional[str] = None
    http_status: Optional[int] = None
    content_type: Optional[str] = None
    text_content: Optional[str] = None
    text_length: Optional[int] = None
    content_hash: Optional[str] = None
    text_source: Optional[str] = None
    error_detail: Optional[str] = None
    reused_from_fetch_id: Optional[int] = None


_RECORD_FIELD_NAMES = tuple(f.name for f in fields(FetchRecord))


def record_as_dict(record: Any) -> Dict[str, Any]:
    if isinstance(record, Mapping):
        return dict(record)
    if is_dataclass(record) and not isinstance(record, type):
        return {name: getattr(record, name) for name in _RECORD_FIELD_NAMES}
    raise TypeError(f"Tipus de registre no suportat: {type(record).__name__}")


class FetchRecordError(ValueError):
    """El registre no compleix el contracte de la BD. `violations` llista cada incompliment."""

    def __init__(self, violations: List[str]):
        self.violations = list(violations)
        super().__init__("Registre d'obtencio invalid: " + "; ".join(self.violations))


def validate_record(record: Any, record_id: Optional[int] = None) -> None:
    """
    Reprodueix en Python els CHECK i NOT NULL de public.entry_content_fetches i
    falla amb FetchRecordError (llistant tots els incompliments) abans de
    qualsevol INSERT.

    Mes estricte que la BD en un punt, a proposit: els camps de text opcionals
    han de ser None o no buits (mai ""), i user_agent no pot ser buit.
    requested_url i normalized_url poden ser "" nomes si no hi havia cap URL.
    `record_id`, si es coneix, serveix per comprovar reused_from_fetch_id <> id.
    No comprova la coherencia entre camps (p. ex. text_length vs text_content).
    """
    data = record_as_dict(record)
    violations: List[str] = []

    for name in data:
        if name not in _RECORD_FIELD_NAMES:
            violations.append(f"camp desconegut: {name}")

    def get(name: str) -> Any:
        return data.get(name)

    # --- NOT NULL -----------------------------------------------------------
    for name in ("requested_url", "normalized_url", "user_agent", "fetch_status", "fetched_at", "truncated"):
        if get(name) is None:
            violations.append(f"{name} es NOT NULL")

    for name in ("requested_url", "normalized_url"):
        value = get(name)
        if value is not None and not isinstance(value, str):
            violations.append(f"{name} ha de ser text")

    user_agent = get("user_agent")
    if user_agent is not None and (not isinstance(user_agent, str) or not user_agent.strip()):
        violations.append("user_agent ha de ser text no buit")

    fetched_at = get("fetched_at")
    if fetched_at is not None:
        if not isinstance(fetched_at, datetime):
            violations.append("fetched_at ha de ser datetime")
        elif fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            violations.append("fetched_at ha de portar zona horaria (timestamptz)")

    truncated = get("truncated")
    if truncated is not None and not isinstance(truncated, bool):
        violations.append("truncated ha de ser boolea")

    # --- CHECK fetch_status -------------------------------------------------
    status = get("fetch_status")
    if status is not None and status not in FETCH_STATUSES:
        violations.append(f"fetch_status no admes pel CHECK: {status!r}")

    # --- CHECK text_source --------------------------------------------------
    text_source = get("text_source")
    if text_source is not None and text_source not in TEXT_SOURCES:
        violations.append(f"text_source no admes pel CHECK: {text_source!r}")

    # --- CHECK content_hash -------------------------------------------------
    content_hash = get("content_hash")
    if content_hash is not None and not (
        isinstance(content_hash, str) and _CONTENT_HASH_RE.fullmatch(content_hash)
    ):
        violations.append("content_hash ha de ser SHA-256 en hexadecimal minuscula (64 caracters) o None")

    # --- CHECK http_status 100..599 -----------------------------------------
    http_status = get("http_status")
    if http_status is not None and not (
        isinstance(http_status, int) and not isinstance(http_status, bool) and 100 <= http_status <= 599
    ):
        violations.append(f"http_status ha de ser None o un enter entre 100 i 599: {http_status!r}")

    # --- CHECK text_length >= 0 ---------------------------------------------
    text_length = get("text_length")
    if text_length is not None and not (
        isinstance(text_length, int) and not isinstance(text_length, bool) and text_length >= 0
    ):
        violations.append(f"text_length ha de ser None o un enter >= 0: {text_length!r}")

    # --- CHECK reused_from_fetch_id <> id i tipus ---------------------------
    reused = get("reused_from_fetch_id")
    if reused is not None:
        if not isinstance(reused, int) or isinstance(reused, bool):
            violations.append(f"reused_from_fetch_id ha de ser None o un enter: {reused!r}")
        elif record_id is not None and reused == record_id:
            violations.append("reused_from_fetch_id no pot apuntar a la mateixa fila")

    # --- Camps de text opcionals: None, mai "" ------------------------------
    for name in ("final_url", "content_type", "text_content", "error_detail"):
        value = get(name)
        if value is None:
            continue
        if not isinstance(value, str):
            violations.append(f"{name} ha de ser text o None")
        elif value == "":
            violations.append(f"{name} no pot ser una cadena buida (usa None)")

    if violations:
        raise FetchRecordError(violations)


# ---------------------------------------------------------------------------
# Funcions pures
# ---------------------------------------------------------------------------

_DISCARD_REASON_BY_STATUS = {
    "access_denied": "fetch_failed",
    "robots_blocked": "fetch_failed",
    "robots_unreachable": "fetch_failed",
    "anti_bot_challenge": "fetch_failed",
    "timeout": "fetch_failed",
    "http_error": "fetch_failed",
    "error": "fetch_failed",
    "text_too_short": "insufficient_text",
}


def discard_reason_for(fetch_status: Optional[str]) -> Optional[str]:
    """
    Motiu de descart (entries.discard_reason) associat a un estat d'obtencio.
    None per a ok, duplicate, pdf_unprocessed i qualsevol altre valor.
    Aquest modul no escriu mai discard_reason: nomes calcula el valor.
    """
    return _DISCARD_REASON_BY_STATUS.get(fetch_status)  # type: ignore[arg-type]


def normalize_url(url: Any) -> Any:
    """
    Normalitza una URL i NO llanca mai excepcions.

    Minuscules a esquema i host, treu el fragment, treu els parametres utm_*
    (sense distingir majuscules), ordena la resta de parametres i treu la barra
    final del cami. Si la URL no es pot normalitzar (malformada, esquema que no
    es http/https, sense host, port invalid, no es text...) retorna l'entrada
    tal com era.

    Notes: no resol redireccions, no treu el port ni les credencials, i
    re-codifica la query (un parametre sense '=' passa a 'clau=').
    """
    try:
        if not isinstance(url, str):
            return url
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https") or not parts.netloc:
            return url
        host = parts.hostname
        if not host:
            return url
        port = parts.port  # ValueError si el port no es valid
        host = host.lower()
        if ":" in host:  # IPv6
            host = f"[{host}]"
        userinfo = parts.netloc.rpartition("@")[0] + "@" if "@" in parts.netloc else ""
        netloc = f"{userinfo}{host}{f':{port}' if port is not None else ''}"
        path = parts.path.rstrip("/")
        pairs = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
        ]
        pairs.sort()
        return urlunsplit((scheme, netloc, path, urlencode(pairs), ""))
    except Exception:
        return url


def strip_utm_params(url: Any) -> Any:
    """
    Elimina NOMES els parametres utm_* de la query (sense distingir majuscules ni
    percent-encoding de la clau). La resta de la URL -- ordre dels parametres,
    codificacio, barra final, fragment -- es conserva exactament. Si no hi ha cap
    utm_*, retorna la mateixa cadena. No llanca mai excepcions.
    """
    try:
        if not isinstance(url, str):
            return url
        head, hash_sep, fragment = url.partition("#")
        base, query_sep, query = head.partition("?")
        if not query_sep:
            return url
        kept = []
        removed = False
        for part in query.split("&"):
            if unquote_plus(part.partition("=")[0]).lower().startswith("utm_"):
                removed = True
            else:
                kept.append(part)
        if not removed:
            return url
        new_query = "&".join(kept)
        return base + (f"?{new_query}" if new_query else "") + hash_sep + fragment
    except Exception:
        return url


def clean_text(text: Optional[str]) -> str:
    """Treu NUL i surrogats no codificables, i normalitza els espais."""
    if not text:
        return ""
    text = text.replace("\x00", "")
    text = text.encode("utf-8", errors="ignore").decode("utf-8")
    return re.sub(r"\s+", " ", text).strip()


def content_hash_of(text: str) -> str:
    """SHA-256 complet (64 hex en minuscula) del text, codificat en UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PageExtract(NamedTuple):
    title: str
    page_text: str            # text de tota la pagina (per detectar reptes)
    text: str                 # text del contenidor triat (main > article amb mes text > body)
    method: Optional[str]     # contenidor triat ('main' | 'article' | 'body'); None si no hi ha text
    container: str            # contenidor triat, tambe quan el seu text es buit
    body_len: int             # caracters del text de <body> (despres de la neteja)


def extract_page(html: str) -> PageExtract:
    """
    Extreu titol i text. Eleccio del contenidor (NO es el mes llarg):
      1. si existeix <main> (el primer), el seu text;
      2. si no, el <article> amb mes text (el primer, si n'hi ha d'empatats);
      3. si no hi ha ni <main> ni <article>, <body>.
    Mai no es cau a <body> si existeix <main> o <article>, encara que el candidat sigui
    curt o buit; body_len informa de la mida del <body> per diagnosticar-ho.
    La neteja previa (script, style, noscript, nav, header, footer, aside, form, svg,
    iframe) es la mateixa de sempre.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    title = clean_text(soup.title.get_text(" ")) if soup.title else ""
    page_text = clean_text(soup.get_text(" "))

    for tag in soup(["nav", "header", "footer", "aside", "form", "svg", "iframe"]):
        tag.decompose()

    body = soup.body or soup
    body_text = clean_text(body.get_text(" "))

    main = soup.find("main")
    if main is not None:
        container, text = "main", clean_text(main.get_text(" "))
    else:
        articles = [clean_text(article.get_text(" ")) for article in soup.find_all("article")]
        if articles:
            container, text = "article", max(articles, key=len)
        else:
            container, text = "body", body_text

    return PageExtract(
        title=title,
        page_text=page_text,
        text=text,
        method=container if text else None,
        container=container,
        body_len=len(body_text),
    )


def has_challenge_path(url: str) -> bool:
    """True si ALGUN SEGMENT del cami es exactament 'challenge' (mai per subcadena)."""
    try:
        return any(segment.lower() == "challenge" for segment in urlsplit(url).path.split("/"))
    except Exception:
        return False


def is_anti_bot_challenge(
    final_url: str, title: str, page_text: str, text_length: int, min_text_chars: int
) -> bool:
    """
    Repte anti-bot. Aplica SEMPRE: segment de cami exactament 'challenge' i frase
    de verificacio al cos. El titol de repte nomes aplica si el text extret
    (text_length) es mes curt que el llindar de text curt (min_text_chars): un
    article llarg amb 'captcha' al titol no es un repte.
    """
    if has_challenge_path(final_url):
        return True
    if _CHALLENGE_BODY_PHRASE in (page_text or "").lower():
        return True
    return text_length < min_text_chars and bool(_CHALLENGE_TITLE_RE.search(title or ""))


def _is_fetchable(url: str) -> bool:
    try:
        parts = urlsplit(url)
        _ = parts.port  # ValueError si no es valid
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except Exception:
        return False


def _origin(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme.lower()}://{host}{port}"


def _is_pdf(content_type: Optional[str], url: str) -> bool:
    if "pdf" in (content_type or "").lower():
        return True
    try:
        return urlsplit(url).path.lower().endswith(".pdf")
    except Exception:
        return False


_HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})


def _media_type(content_type: Optional[str]) -> str:
    """Tipus MIME en minuscula, sense parametres (charset...)."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def _short(message: str) -> str:
    return message if len(message) <= ERROR_DETAIL_MAX else message[: ERROR_DETAIL_MAX - 3] + "..."


def _read_limited(response: Any, limit: int) -> Tuple[bytes, bool]:
    """Llegeix com a maxim `limit` bytes. Retorna (bytes, truncat)."""
    chunks: List[bytes] = []
    total = 0
    for chunk in response.iter_content(65536):
        if not chunk:
            continue
        remaining = limit - total
        if len(chunk) > remaining:
            if remaining > 0:
                chunks.append(chunk[:remaining])
            return b"".join(chunks), True
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), False


def _decode(raw: bytes, encoding: Optional[str]) -> str:
    try:
        return raw.decode(encoding or "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Obtencio
# ---------------------------------------------------------------------------

# Cerca de duplicats: donada una normalized_url, retorna l'id d'una fila 'ok' o None.
ReusableLookup = Callable[[str], Optional[int]]


class _RobotsEntry(NamedTuple):
    kind: str                                  # 'rules' | 'allow_all' | 'unreachable'
    parser: Optional[robotparser.RobotFileParser]
    detail: Optional[str]


class ContentFetcher:
    """
    Obte el contingut d'un article i retorna un FetchRecord. No toca la BD.

    `session` ha de tenir get(url, headers=, timeout=, allow_redirects=, stream=)
    com requests.Session. `sleep`, `clock` i `now` es poden injectar per als tests.
    La memoria cau de robots.txt i la cadencia per host viuen a la instancia
    (una execucio).
    """

    def __init__(
        self,
        session: Any = None,
        *,
        user_agent: Optional[str] = None,
        min_text_chars: Optional[int] = None,
        request_timeout: float = REQUEST_TIMEOUT,
        robots_timeout: float = ROBOTS_TIMEOUT,
        max_bytes: int = MAX_BYTES,
        delay_per_host: float = DELAY_PER_HOST,
        max_redirects: int = MAX_REDIRECTS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utc_now,
    ):
        self._session = session
        self.user_agent = (user_agent or "").strip() or USER_AGENT
        self.min_text_chars = MIN_TEXT_CHARS if min_text_chars is None else min_text_chars
        self.request_timeout = request_timeout
        self.robots_timeout = robots_timeout
        self.max_bytes = max_bytes
        self.delay_per_host = delay_per_host
        self.max_redirects = max_redirects
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._robots_cache: Dict[str, _RobotsEntry] = {}
        self._last_hit: Dict[str, float] = {}

    # -- API publica ---------------------------------------------------------

    def fetch_entry_content(
        self,
        canonical_url: Optional[str],
        *,
        find_reusable: Optional[ReusableLookup] = None,
    ) -> FetchRecord:
        """
        Obte el contingut de canonical_url. NO hi ha cap alternativa: sense
        canonical_url el registre es 'error' amb error_detail='no_canonical_url'.
        """
        return self.fetch(canonical_url, find_reusable=find_reusable)

    def fetch(self, requested_url: Optional[str], *, find_reusable: Optional[ReusableLookup] = None) -> FetchRecord:
        if not isinstance(requested_url, str) or not requested_url.strip():
            return self._record("", "", "error", error_detail="no_canonical_url")

        # normalized_url: nomes per desar-la i per a la cerca de duplicats.
        normalized = normalize_url(requested_url)
        # URL que es demana a la xarxa: requested_url amb nomes els utm_* eliminats.
        request_url = strip_utm_params(requested_url.strip())
        if not _is_fetchable(request_url):
            return self._record(
                requested_url, normalized, "error",
                error_detail=_short(f"URL no valida o no suportada (nomes http/https): {requested_url}"),
            )

        if find_reusable is not None:
            reused_id = find_reusable(normalized)  # un error de BD es propaga: no es un estat
            if reused_id is not None:
                return self._record(requested_url, normalized, "duplicate", reused_from_fetch_id=reused_id)

        return self._download(requested_url, normalized, request_url)

    # -- Intern: registres ---------------------------------------------------

    def _record(self, requested: str, normalized: str, status: str, **kwargs: Any) -> FetchRecord:
        return FetchRecord(
            requested_url=requested,
            normalized_url=normalized,
            fetch_status=status,
            user_agent=self.user_agent,
            fetched_at=self._now(),
            **kwargs,
        )

    @property
    def session(self) -> Any:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    # -- Intern: cadencia i robots -------------------------------------------

    def _wait_host(self, origin: str) -> None:
        last = self._last_hit.get(origin)
        if last is not None:
            delta = self._clock() - last
            if delta < self.delay_per_host:
                self._sleep(self.delay_per_host - delta)
        self._last_hit[origin] = self._clock()

    def _load_robots(self, origin: str) -> _RobotsEntry:
        url = f"{origin}/robots.txt"
        try:
            for _ in range(self.max_redirects + 1):
                self._wait_host(_origin(url))
                response = self.session.get(
                    url,
                    headers={"User-Agent": self.user_agent},
                    timeout=self.robots_timeout,
                    allow_redirects=False,
                    stream=True,
                )
                try:
                    code = response.status_code
                    if code in _REDIRECT_CODES:
                        location = (response.headers.get("Location") or "").strip()
                        if not location:
                            return _RobotsEntry("unreachable", None, f"HTTP {code} sense Location")
                        url = urldefrag(urljoin(url, location))[0]
                        if not _is_fetchable(url):
                            return _RobotsEntry("unreachable", None, "redireccio a una URL no valida")
                        continue
                    if 200 <= code < 300:
                        raw, _truncated = _read_limited(response, ROBOTS_MAX_BYTES)
                        parser = robotparser.RobotFileParser()
                        parser.parse(_decode(raw, response.encoding).splitlines())
                        return _RobotsEntry("rules", parser, None)
                    if 400 <= code < 500:
                        return _RobotsEntry("allow_all", None, f"HTTP {code}")
                    return _RobotsEntry("unreachable", None, f"HTTP {code}")
                finally:
                    response.close()
            return _RobotsEntry("unreachable", None, f"massa redireccions (>{self.max_redirects})")
        except requests.Timeout:
            return _RobotsEntry("unreachable", None, f"timeout ({self.robots_timeout}s)")
        except Exception as exc:
            return _RobotsEntry("unreachable", None, _short(f"{type(exc).__name__}: {exc}"))

    def _robots_verdict(self, url: str) -> Tuple[str, Optional[str]]:
        """('allowed' | 'blocked' | 'unreachable', detall)."""
        origin = _origin(url)
        entry = self._robots_cache.get(origin)
        if entry is None:
            entry = self._load_robots(origin)
            self._robots_cache[origin] = entry
        if entry.kind == "unreachable":
            return "unreachable", _short(f"No s'ha pogut llegir {origin}/robots.txt: {entry.detail}")
        if entry.kind == "allow_all":
            return "allowed", None
        assert entry.parser is not None
        if entry.parser.can_fetch(self.user_agent, url):
            return "allowed", None
        return "blocked", _short(f"robots.txt de {origin} no permet {url}")

    # -- Intern: descarrega --------------------------------------------------

    def _download(self, requested: str, normalized: str, request_url: str) -> FetchRecord:
        current = request_url
        redirects = 0
        final_url: Optional[str] = None
        http_status: Optional[int] = None
        content_type: Optional[str] = None

        def build(status: str, **kwargs: Any) -> FetchRecord:
            return self._record(
                requested, normalized, status,
                final_url=final_url, http_status=http_status, content_type=content_type, **kwargs,
            )

        try:
            while True:
                verdict, detail = self._robots_verdict(current)
                if verdict == "blocked":
                    return build("robots_blocked", error_detail=detail)
                if verdict == "unreachable":
                    return build("robots_unreachable", error_detail=detail)

                self._wait_host(_origin(current))
                response = self.session.get(
                    current,
                    headers={"User-Agent": self.user_agent, "Accept": ACCEPT_HEADER},
                    timeout=self.request_timeout,
                    allow_redirects=False,
                    stream=True,
                )
                try:
                    final_url = current
                    code = response.status_code
                    http_status = code
                    content_type = response.headers.get("Content-Type") or None

                    if code in _REDIRECT_CODES:
                        location = (response.headers.get("Location") or "").strip()
                        if not location:
                            return build("http_error", error_detail=f"HTTP {code} sense capcalera Location")
                        if redirects >= self.max_redirects:
                            return build("error", error_detail=f"Massa redireccions (>{self.max_redirects})")
                        target = urldefrag(urljoin(current, location))[0]
                        if not _is_fetchable(target):
                            return build("error", error_detail=_short(f"Redireccio a una URL no valida: {target}"))
                        redirects += 1
                        current = target
                        final_url = target  # desti assolit; encara no s'ha demanat
                        continue

                    if code in (401, 402, 403):
                        return build("access_denied", error_detail=f"HTTP {code}")
                    if code >= 400:
                        return build("http_error", error_detail=f"HTTP {code}")
                    if not 200 <= code < 300:
                        return build("http_error", error_detail=f"HTTP {code} inesperat")

                    if _is_pdf(content_type, current):
                        return build("pdf_unprocessed")

                    media_type = _media_type(content_type)
                    if media_type not in _HTML_MEDIA_TYPES:
                        return build(
                            "error",
                            error_detail=_short(f"unsupported_content_type:{media_type or 'missing'}"),
                        )

                    raw, truncated = _read_limited(response, self.max_bytes)
                    page = extract_page(_decode(raw, response.encoding))

                    if is_anti_bot_challenge(
                        current, page.title, page.page_text, len(page.text), self.min_text_chars
                    ):
                        return build(
                            "anti_bot_challenge", truncated=truncated,
                            error_detail="Pagina de verificacio anti-bot; no es processa.",
                        )

                    text = page.text
                    has_text = bool(text)
                    status = "ok" if has_text and len(text) >= self.min_text_chars else "text_too_short"
                    return build(
                        status,
                        truncated=truncated,
                        text_content=text if has_text else None,
                        text_length=len(text) if has_text else None,
                        content_hash=content_hash_of(text) if has_text else None,
                        text_source=EXTRACT_METHOD_TO_TEXT_SOURCE[page.method] if has_text else None,
                        error_detail=(
                            None if status == "ok"
                            else f"container={page.container};len={len(text)};body_len={page.body_len}"
                        ),
                    )
                finally:
                    response.close()
        except requests.Timeout:
            return build("timeout", error_detail=f"Timeout ({self.request_timeout}s)")
        except Exception as exc:
            return build("error", error_detail=_short(f"{type(exc).__name__}: {exc}"))
