"""
Persistencia de l'obtencio de contingut (Fase 1B, pas 1). Modul AILLAT.

Unic acces a public.entry_content_fetches. Aquest modul:
- NOMES escriu a public.entry_content_fetches (INSERT parametritzat).
- NO escriu a public.entries (ni discard_reason ni analyzed_fetch_id): aixo es
  un pas posterior.
- NO fa commit ni rollback: ho controla qui el crida.
- No s'importa des de crawler.py ni main.py.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

from app.content_fetch import (
    ContentFetcher,
    FetchRecord,
    record_as_dict,
    validate_record,
)

INSERT_FETCH_SQL = """
INSERT INTO public.entry_content_fetches (
    entry_id,
    requested_url,
    normalized_url,
    final_url,
    fetch_status,
    http_status,
    content_type,
    fetched_at,
    text_content,
    text_length,
    truncated,
    content_hash,
    text_source,
    error_detail,
    user_agent,
    reused_from_fetch_id
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
RETURNING id
"""

# Ordre dels parametres d'INSERT_FETCH_SQL despres d'entry_id
INSERT_FETCH_COLUMNS = (
    "requested_url",
    "normalized_url",
    "final_url",
    "fetch_status",
    "http_status",
    "content_type",
    "fetched_at",
    "text_content",
    "text_length",
    "truncated",
    "content_hash",
    "text_source",
    "error_detail",
    "user_agent",
    "reused_from_fetch_id",
)

# Una fila 'ok' amb la mateixa URL normalitzada. La comparacio amb md5() usa
# l'index d'expressio; la igualtat exacta es la que garanteix la coincidencia
# (el hash no es una prova d'identitat). id DESC nomes desempata fetched_at.
FIND_REUSABLE_SQL = """
SELECT id
FROM public.entry_content_fetches
WHERE md5(normalized_url) = md5(%s)
  AND normalized_url = %s
  AND fetch_status = 'ok'
ORDER BY fetched_at DESC, id DESC
LIMIT 1
"""


def _first_value(row: Any) -> Any:
    """Primer valor d'una fila, sigui tupla o mapping (RealDictCursor)."""
    if isinstance(row, Mapping):
        return next(iter(row.values()))
    return row[0]


def insert_fetch(conn: Any, entry_id: int, record: Any) -> int:
    """
    Insereix un intent a public.entry_content_fetches i retorna l'id (bigserial).
    Valida el registre abans (FetchRecordError si no compleix el contracte).
    No fa commit.
    """
    if not isinstance(entry_id, int) or isinstance(entry_id, bool):
        raise TypeError(f"entry_id ha de ser un enter: {entry_id!r}")

    validate_record(record)
    data = record_as_dict(record)
    params = (entry_id,) + tuple(data[name] for name in INSERT_FETCH_COLUMNS)

    with conn.cursor() as cur:
        cur.execute(INSERT_FETCH_SQL, params)
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING id no ha retornat cap fila")
    return int(_first_value(row))


def find_reusable_fetch(conn: Any, normalized_url: str) -> Optional[int]:
    """
    Id de l'intent 'ok' mes recent amb aquesta normalized_url, o None.
    Si l'original no es 'ok' no es reutilitza (es torna a intentar l'obtencio).
    """
    if not isinstance(normalized_url, str) or not normalized_url:
        return None
    with conn.cursor() as cur:
        cur.execute(FIND_REUSABLE_SQL, (normalized_url, normalized_url))
        row = cur.fetchone()
    return None if row is None else int(_first_value(row))


LIST_ENTRY_FETCHES_SQL = """
SELECT id, fetch_status, http_status, fetched_at, text_length
FROM public.entry_content_fetches
WHERE entry_id = %s
ORDER BY fetched_at DESC, id DESC
"""


def list_entry_fetches(conn: Any, entry_id: int) -> list:
    """Intents d'obtencio d'una entrada (mes recents primer), com a dicts. Nomes SELECT."""
    with conn.cursor() as cur:
        cur.execute(LIST_ENTRY_FETCHES_SQL, (entry_id,))
        rows = cur.fetchall()
    result = []
    for row in rows or []:
        if isinstance(row, Mapping):
            result.append(dict(row))
        else:
            result.append(dict(zip(("id", "fetch_status", "http_status", "fetched_at", "text_length"), row)))
    return result


# Una fila 'duplicate' no te text: apunta (reused_from_fetch_id) a la fila que el te.
GET_FETCH_SQL = """
SELECT text_content, reused_from_fetch_id
FROM public.entry_content_fetches
WHERE id = %s
"""

MAX_REUSE_HOPS = 3


def get_fetch_text(conn: Any, fetch_id: int) -> Optional[str]:
    """
    Text d'un intent d'obtencio, o None. Nomes fa SELECT.

    Si la fila te text_content, el retorna. Si no (una fila 'duplicate'), el resol
    seguint reused_from_fetch_id, com a maxim MAX_REUSE_HOPS (3) salts. Retorna None
    -- sense llancar excepcio -- si la fila no existeix, si no te text ni enllac, si hi
    ha un cicle o si la cadena es mes llarga que 3 salts.
    """
    if not isinstance(fetch_id, int) or isinstance(fetch_id, bool):
        return None

    seen = set()
    current: Optional[int] = fetch_id
    for hop in range(MAX_REUSE_HOPS + 1):
        if current is None or current in seen:
            return None
        seen.add(current)

        with conn.cursor() as cur:
            cur.execute(GET_FETCH_SQL, (current,))
            row = cur.fetchone()
        if row is None:
            return None

        if isinstance(row, Mapping):
            text, reused = row["text_content"], row["reused_from_fetch_id"]
        else:
            text, reused = row[0], row[1]

        if text is not None:
            return text
        if reused is None:
            return None
        current = int(reused)  # un salt mes; el bucle talla a MAX_REUSE_HOPS
    return None


def fetch_and_store(
    conn: Any,
    entry_id: int,
    canonical_url: Optional[str],
    *,
    fetcher: Optional[ContentFetcher] = None,
) -> Tuple[int, FetchRecord]:
    """
    Obte el contingut de canonical_url d'una entrada i desa l'intent. Retorna
    (id, registre). Sense canonical_url NO hi ha cap alternativa: es desa un
    registre 'error' amb error_detail='no_canonical_url'.
    Si ja hi ha una fila 'ok' per a la mateixa URL, desa una fila 'duplicate'
    que hi apunta (sense text). No escriu a entries ni fa commit.
    """
    fetcher = fetcher or ContentFetcher()
    record = fetcher.fetch_entry_content(
        canonical_url,
        find_reusable=lambda url: find_reusable_fetch(conn, url),
    )
    return insert_fetch(conn, entry_id, record), record
