#!/usr/bin/env python3
"""
Smoke test d'INSERT a public.entry_content_fetches. NOMES PER A UNA COPIA DE LA BD.

L'ha d'executar l'usuari. Dins d'UNA transaccio, i amb ROLLBACK al final EN TOTS
ELS CASOS (mai no fa COMMIT):
  1. tria una entrada existent (nomes la llegeix);
  2. insereix amb insert_fetch() un registre per cadascun dels 11 estats, incloent
     un 'duplicate' amb reused_from_fetch_id apuntant a una fila 'ok' (obtinguda
     amb find_reusable_fetch());
  3. els llegeix de tornada i comprova tipus i valors (None es None, mai "" ni 0);
  4. imprimeix un resum i verifica, despres del rollback, que la taula torna a
     tenir les files que tenia.
NO escriu a public.entries. Cap peticio de xarxa (la sessio HTTP esta prohibida).

Salvaguardes:
  - EXIGEIX el flag --i-know-this-is-a-copy; sense ell surt SENSE llegir l'entorn
    ni connectar-se (codi 2).
  - El DSN es llegeix de la variable d'entorn SMOKE_DATABASE_DSN (o la de --dsn-env).
    No carrega .env ni importa app.db: no pot agafar credencials de produccio per
    accident. No s'imprimeix mai el DSN ni la contrasenya.
  - Les seqüencies no es reverteixen: s'hi consumeixen 11 valors d'entry_content_fetches_id_seq.

Codis de sortida: 0 = tot correcte; 1 = discrepancies o error; 2 = salvaguarda.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import content_fetch as cf  # noqa: E402
from app.content_fetch import ContentFetcher, FetchRecord, content_hash_of  # noqa: E402
from app.content_fetch_store import find_reusable_fetch, insert_fetch  # noqa: E402

GUARD_FLAG = "--i-know-this-is-a-copy"
DSN_ENV_DEFAULT = "SMOKE_DATABASE_DSN"

# Ordre d'insercio: 'ok' primer, 'duplicate' al final (apunta a la fila 'ok').
STATUS_ORDER = [
    "ok", "text_too_short", "robots_blocked", "robots_unreachable", "access_denied",
    "anti_bot_challenge", "timeout", "http_error", "error", "pdf_unprocessed", "duplicate",
]

READ_BACK_SQL = """
SELECT id, entry_id, requested_url, normalized_url, final_url, fetch_status, http_status,
       content_type, fetched_at, text_content, text_length, truncated, content_hash,
       text_source, error_detail, user_agent, reused_from_fetch_id
FROM public.entry_content_fetches
WHERE id = ANY(%s)
ORDER BY id
"""

COMPARED_FIELDS = [
    "requested_url", "normalized_url", "final_url", "fetch_status", "http_status", "content_type",
    "fetched_at", "text_content", "text_length", "truncated", "content_hash", "text_source",
    "error_detail", "user_agent", "reused_from_fetch_id",
]


class _ForbiddenSession:
    """Sessio HTTP que falla si s'utilitza: el smoke test no pot fer xarxa."""

    def get(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("El smoke test no pot fer peticions de xarxa")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test d'INSERT a entry_content_fetches (copia de la BD).")
    parser.add_argument(GUARD_FLAG, dest="i_know_this_is_a_copy", action="store_true",
                        help="Obligatori: confirma que la BD de destinacio es una COPIA.")
    parser.add_argument("--dsn-env", default=DSN_ENV_DEFAULT,
                        help=f"Variable d'entorn amb el DSN (per defecte {DSN_ENV_DEFAULT})")
    parser.add_argument("--entry-id", type=int, default=None,
                        help="id d'una entrada existent (per defecte, la de menys id)")
    return parser.parse_args(argv)


def build_records(now: datetime, run_id: str) -> Dict[str, FetchRecord]:
    """Un registre per estat (excepte 'duplicate', que es construeix amb el fetcher). Valors realistes."""
    base = f"https://smoke-test.invalid/{run_id}"
    ua = cf.USER_AGENT
    text = ("Text de prova del smoke test. " * 20).strip()
    short = "Text curt."

    def rec(status: str, path: str, **kw: Any) -> FetchRecord:
        url = f"{base}/{path}"
        return FetchRecord(requested_url=url, normalized_url=url, fetch_status=status, user_agent=ua,
                           fetched_at=now, **kw)

    html = "text/html; charset=utf-8"
    return {
        "ok": rec("ok", "ok", final_url=f"{base}/ok", http_status=200, content_type=html, text_content=text,
                  text_length=len(text), content_hash=content_hash_of(text), text_source="html_article"),
        "text_too_short": rec("text_too_short", "short", final_url=f"{base}/short", http_status=200,
                              content_type=html, text_content=short, text_length=len(short), truncated=True,
                              content_hash=content_hash_of(short), text_source="html_body",
                              error_detail="Text de 10 caracters (minim 400)."),
        "robots_blocked": rec("robots_blocked", "robots-blocked",
                              error_detail="robots.txt no permet la URL"),
        "robots_unreachable": rec("robots_unreachable", "robots-unreachable",
                                  error_detail="No s'ha pogut llegir robots.txt: HTTP 503"),
        "access_denied": rec("access_denied", "denied", final_url=f"{base}/denied", http_status=403,
                             content_type=html, error_detail="HTTP 403"),
        "anti_bot_challenge": rec("anti_bot_challenge", "challenge-source",
                                  final_url="https://smoke-test.invalid/challenge", http_status=200,
                                  content_type=html, error_detail="Pagina de verificacio anti-bot; no es processa."),
        "timeout": rec("timeout", "timeout", error_detail="Timeout (20.0s)"),
        "http_error": rec("http_error", "http-error", final_url=f"{base}/http-error", http_status=500,
                          content_type=html, error_detail="HTTP 500"),
        "error": rec("error", "error", error_detail="ConnectionError: simulat"),
        "pdf_unprocessed": rec("pdf_unprocessed", "pdf", final_url=f"{base}/pdf", http_status=200,
                               content_type="application/pdf"),
    }


def compare(expected: FetchRecord, actual: Mapping[str, Any], entry_id: int) -> List[str]:
    """Discrepancies entre el registre inserit i la fila llegida. Cap = tot correcte."""
    problems: List[str] = []
    if not isinstance(actual.get("id"), int) or isinstance(actual.get("id"), bool):
        problems.append(f"id no es un enter: {actual.get('id')!r}")
    if actual.get("entry_id") != entry_id:
        problems.append(f"entry_id: esperat {entry_id}, llegit {actual.get('entry_id')!r}")
    for name in COMPARED_FIELDS:
        want, got = getattr(expected, name), actual.get(name)
        if want is None:
            if got is not None:
                problems.append(f"{name}: esperat NULL, llegit {got!r}")
        elif got != want or type(got) is not type(want):
            problems.append(f"{name}: esperat {want!r} ({type(want).__name__}), llegit {got!r} ({type(got).__name__})")
    fetched = actual.get("fetched_at")
    if not isinstance(fetched, datetime) or fetched.tzinfo is None:
        problems.append("fetched_at no es un datetime amb zona horaria")
    if actual.get("truncated") not in (True, False) or not isinstance(actual.get("truncated"), bool):
        problems.append("truncated no es boolea")
    chash = actual.get("content_hash")
    if chash is not None and not re.fullmatch(r"[0-9a-f]{64}", str(chash)):
        problems.append("content_hash no te 64 hexadecimals en minuscula")
    for name in ("final_url", "content_type", "text_content", "error_detail", "text_source", "content_hash"):
        if actual.get(name) == "":
            problems.append(f"{name} es una cadena buida (hauria de ser NULL)")
    for name in ("http_status", "text_length"):
        if actual.get(name) == 0:
            problems.append(f"{name} es 0 (hauria de ser NULL)")
    return problems


def _scalar(conn: Any, sql: str, params: Tuple = ()) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return None if row is None else row[0]


def run_checks(conn: Any, entry_id_arg: Optional[int], out: Any, state: Dict[str, Any]) -> List[str]:
    """
    Fa tota la prova dins la transaccio oberta. NO fa commit. Retorna la llista de
    problemes i deixa a state['before'] el recompte inicial de files de la taula.
    """
    problems: List[str] = []

    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '5s'")

    if not _scalar(conn, "SELECT to_regclass('public.entry_content_fetches') IS NOT NULL"):
        return ["La taula public.entry_content_fetches no existeix en aquesta BD."]

    if entry_id_arg is None:
        entry_id = _scalar(conn, "SELECT id FROM public.entries ORDER BY id LIMIT 1")
    else:
        entry_id = _scalar(conn, "SELECT id FROM public.entries WHERE id = %s", (entry_id_arg,))
    if entry_id is None:
        return ["No s'ha trobat cap entrada existent a public.entries per associar-hi els registres."]
    print(f"Entrada usada (nomes lectura): id={entry_id}", file=out)

    before = _scalar(conn, "SELECT count(*) FROM public.entry_content_fetches")
    state["before"] = before
    print(f"Files a entry_content_fetches abans: {before}", file=out)

    now = datetime.now(timezone.utc)
    records = build_records(now, uuid.uuid4().hex[:12])
    ids: Dict[str, int] = {}
    expected: Dict[str, FetchRecord] = {}

    ids["ok"] = insert_fetch(conn, entry_id, records["ok"])
    expected["ok"] = records["ok"]
    ok_url = records["ok"].normalized_url

    # find_reusable_fetch: troba la fila 'ok' i NO en troba cap per a una URL amb un estat que no es 'ok'
    for label in ("text_too_short", "robots_blocked", "robots_unreachable", "access_denied",
                  "anti_bot_challenge", "timeout", "http_error", "error", "pdf_unprocessed"):
        ids[label] = insert_fetch(conn, entry_id, records[label])
        expected[label] = records[label]

    found = find_reusable_fetch(conn, ok_url)
    if found != ids["ok"]:
        problems.append(f"find_reusable_fetch(ok) -> {found!r}, esperat {ids['ok']}")
    not_ok = find_reusable_fetch(conn, records["http_error"].normalized_url)
    if not_ok is not None:
        problems.append(f"find_reusable_fetch(http_error) -> {not_ok!r}, esperat None (nomes es reutilitzen 'ok')")

    # 'duplicate': el fetcher real, sense xarxa, a partir del resultat de find_reusable_fetch
    fetcher = ContentFetcher(_ForbiddenSession(), now=lambda: now)
    duplicate = fetcher.fetch(ok_url, find_reusable=lambda url: find_reusable_fetch(conn, url))
    if duplicate.fetch_status != "duplicate" or duplicate.reused_from_fetch_id != ids["ok"]:
        problems.append(f"El fetcher no ha generat un 'duplicate' que apunti a la fila 'ok': {duplicate!r}")
    ids["duplicate"] = insert_fetch(conn, entry_id, duplicate)
    expected["duplicate"] = duplicate

    with conn.cursor() as cur:
        cur.execute(READ_BACK_SQL, (list(ids.values()),))
        columns = [d[0] for d in cur.description]
        rows = {r[0]: dict(zip(columns, r)) for r in cur.fetchall()}

    after_insert = _scalar(conn, "SELECT count(*) FROM public.entry_content_fetches")
    print(f"Files a entry_content_fetches dins la transaccio: {after_insert}", file=out)
    if after_insert != before + len(STATUS_ORDER):
        problems.append(f"Recompte dins la transaccio: esperat {before + len(STATUS_ORDER)}, llegit {after_insert}")

    print("\n=== RESUM PER ESTAT ===", file=out)
    for label in STATUS_ORDER:
        row = rows.get(ids[label])
        if row is None:
            problems.append(f"{label}: no es pot llegir la fila id={ids[label]}")
            print(f"  [FALLA] {label:<20} id={ids[label]}  (fila no trobada)", file=out)
            continue
        issues = compare(expected[label], row, entry_id)
        if label == "duplicate":
            if row["reused_from_fetch_id"] != ids["ok"]:
                issues.append(f"reused_from_fetch_id: esperat {ids['ok']}, llegit {row['reused_from_fetch_id']!r}")
            for name in ("text_content", "text_length", "content_hash"):
                if row[name] is not None:
                    issues.append(f"{name} hauria de ser NULL en un duplicate")
        status = "OK   " if not issues else "FALLA"
        print(f"  [{status}] {label:<20} id={ids[label]}", file=out)
        for issue in issues:
            print(f"           - {issue}", file=out)
            problems.append(f"{label}: {issue}")
    return problems


def main(
    argv: Optional[Sequence[str]] = None,
    environ: Optional[Mapping[str, str]] = None,
    connect: Any = None,
    out: Any = None,
) -> int:
    out = out or sys.stdout
    args = parse_args(argv)

    if not args.i_know_this_is_a_copy:
        print(f"Cal el flag {GUARD_FLAG} per confirmar que la BD es una COPIA. No es fa res.", file=out)
        return 2

    environ = os.environ if environ is None else environ
    dsn = environ.get(args.dsn_env)
    if not dsn:
        print(f"Falta la variable d'entorn {args.dsn_env} (DSN de la COPIA). No es fa res.", file=out)
        return 2

    if connect is None:
        import psycopg2  # importacio tardana: sense el flag, no es toca la BD
        connect = psycopg2.connect

    try:
        conn = connect(dsn)
    except Exception as exc:
        print(f"No s'ha pogut connectar: {type(exc).__name__}", file=out)  # sense detalls: podrien incloure el DSN
        return 1

    problems: List[str] = []
    state: Dict[str, Any] = {}
    try:
        conn.autocommit = False
        try:
            params = conn.get_dsn_parameters()
            print("Destinacio: host={host} port={port} dbname={dbname} user={user}".format(
                **{k: params.get(k, "?") for k in ("host", "port", "dbname", "user")}), file=out)
        except Exception:
            pass
        problems = run_checks(conn, args.entry_id, out, state)
    except Exception as exc:
        problems.append(f"Error durant la prova: {type(exc).__name__}: {exc}")
        print(f"ERROR: {problems[-1]}", file=out)
    finally:
        try:
            conn.rollback()  # SEMPRE; aquest script no fa mai commit
        except Exception as exc:
            problems.append(f"Rollback fallit: {type(exc).__name__}")

    # Despres del rollback: la taula ha de tornar a l'estat inicial
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM public.entry_content_fetches")
            after_rollback = cur.fetchone()[0]
        conn.rollback()
        print(f"\nFiles a entry_content_fetches despres del ROLLBACK: {after_rollback}", file=out)
        if "before" in state and after_rollback != state["before"]:
            problems.append(f"Despres del ROLLBACK hi ha {after_rollback} files i abans n'hi havia {state['before']}")
    except Exception as exc:
        problems.append(f"No s'ha pogut verificar el rollback: {type(exc).__name__}")
        after_rollback = None
    finally:
        try:
            conn.close()
        except Exception:
            pass

    print("\nTransaccio revertida: no s'ha escrit res. (Els valors de la sequencia consumits no es reverteixen.)", file=out)
    if problems:
        print(f"\nRESULTAT: FALLA ({len(problems)} problemes)", file=out)
        for problem in problems:
            print(f"  - {problem}", file=out)
        return 1
    print("\nRESULTAT: CORRECTE (11 estats inserits, llegits i comparats; rollback fet)", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
