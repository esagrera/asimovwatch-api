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
