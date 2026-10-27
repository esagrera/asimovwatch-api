"""
Tests de app/content_fetch_store.py amb una connexio falsa: cap BD ni xarxa.
"""

import inspect
import os
import re

import pytest

from app import content_fetch, content_fetch_store as store
from app.content_fetch import ContentFetcher, FetchRecord, FetchRecordError
from tests.fakes import FIXED_NOW, FakeConn, FakeResponse, make_fetcher, ok_page

ART = "https://example.com/a"
APP_DIR = os.path.dirname(content_fetch.__file__)


def make_record(**overrides):
    data = dict(
        requested_url=ART, normalized_url=ART, fetch_status="ok", user_agent="UA/1.0", fetched_at=FIXED_NOW,
        truncated=False, final_url=ART, http_status=200, content_type="text/html", text_content="hola",
        text_length=4, content_hash="b" * 64, text_source="html_main", error_detail=None,
        reused_from_fetch_id=None,
    )
    data.update(overrides)
    return FetchRecord(**data)


# ---------------------------------------------------------------------------
# insert_fetch
# ---------------------------------------------------------------------------

def test_insert_fetch_returns_the_new_id_and_binds_every_column():
    conn = FakeConn(results=[(42,)])
    new_id = store.insert_fetch(conn, 17, make_record())
    assert new_id == 42
    sql, params = conn.executed[0]
    assert "INSERT INTO public.entry_content_fetches" in sql
    assert re.search(r"RETURNING id\s*$", sql.strip())
    assert sql.count("%s") == 16 and len(params) == 16
    assert params[0] == 17
    assert dict(zip(store.INSERT_FETCH_COLUMNS, params[1:])) == {
        "requested_url": ART, "normalized_url": ART, "final_url": ART, "fetch_status": "ok",
        "http_status": 200, "content_type": "text/html", "fetched_at": FIXED_NOW, "text_content": "hola",
        "text_length": 4, "truncated": False, "content_hash": "b" * 64, "text_source": "html_main",
        "error_detail": None, "user_agent": "UA/1.0", "reused_from_fetch_id": None,
    }


def test_insert_fetch_column_list_matches_the_table_columns():
    columns = re.search(r"\((.*?)\)\s*VALUES", store.INSERT_FETCH_SQL, re.S).group(1)
    names = [c.strip() for c in columns.split(",")]
    assert names == ["entry_id", *store.INSERT_FETCH_COLUMNS]
    assert "id" not in names  # bigserial: l'assigna la BD


def test_insert_fetch_does_not_commit_or_rollback():
    conn = FakeConn(results=[(1,)])
    store.insert_fetch(conn, 1, make_record())
    assert conn.commits == 0 and conn.rollbacks == 0


def test_insert_fetch_accepts_dict_rows():
    assert store.insert_fetch(FakeConn(results=[{"id": 5}]), 1, make_record()) == 5


def test_insert_fetch_validates_before_touching_the_connection():
    conn = FakeConn(results=[(1,)])
    with pytest.raises(FetchRecordError):
        store.insert_fetch(conn, 1, make_record(content_hash="a" * 16))
    with pytest.raises(FetchRecordError):
        store.insert_fetch(conn, 1, make_record(fetch_status="acces_denegat"))
    assert conn.cursors_opened == 0 and conn.executed == []


@pytest.mark.parametrize("entry_id", [None, "1", 1.5, True])
def test_insert_fetch_rejects_a_bad_entry_id(entry_id):
    conn = FakeConn(results=[(1,)])
    with pytest.raises(TypeError):
        store.insert_fetch(conn, entry_id, make_record())
    assert conn.executed == []


def test_insert_fetch_fails_loudly_when_nothing_is_returned():
    with pytest.raises(RuntimeError):
        store.insert_fetch(FakeConn(results=[None]), 1, make_record())


# ---------------------------------------------------------------------------
# find_reusable_fetch
# ---------------------------------------------------------------------------

def test_find_reusable_fetch_query_and_result():
    conn = FakeConn(results=[(9,)])
    assert store.find_reusable_fetch(conn, ART) == 9
    sql, params = conn.executed[0]
    flat = " ".join(sql.split())
    assert "FROM public.entry_content_fetches" in flat
    assert "WHERE md5(normalized_url) = md5(%s) AND normalized_url = %s AND fetch_status = 'ok'" in flat
    assert "ORDER BY fetched_at DESC" in flat and flat.endswith("LIMIT 1")
    assert params == (ART, ART)
    assert conn.commits == 0


def test_find_reusable_fetch_returns_none_when_there_is_no_ok_row():
    assert store.find_reusable_fetch(FakeConn(results=[None]), ART) is None


@pytest.mark.parametrize("url", ["", None])
def test_find_reusable_fetch_ignores_empty_urls(url):
    conn = FakeConn()
    assert store.find_reusable_fetch(conn, url) is None
    assert conn.executed == []


# ---------------------------------------------------------------------------
# get_fetch_text
# ---------------------------------------------------------------------------

def selects_only(conn):
    for sql, _params in conn.executed:
        flat = " ".join(sql.split())
        assert flat.startswith("SELECT text_content, reused_from_fetch_id FROM public.entry_content_fetches WHERE id = %s")
        assert not re.search(r"\b(INSERT|UPDATE|DELETE)\b", flat)
    assert conn.commits == 0 and conn.rollbacks == 0


def test_get_fetch_text_returns_the_direct_text():
    conn = FakeConn(results=[("Text directe", None)])
    assert store.get_fetch_text(conn, 10) == "Text directe"
    assert [p for _, p in conn.executed] == [(10,)]
    selects_only(conn)


def test_get_fetch_text_resolves_a_duplicate_through_reused_from_fetch_id():
    conn = FakeConn(results=[(None, 10), ("Text de l'original", None)])
    assert store.get_fetch_text(conn, 11) == "Text de l'original"
    assert [p for _, p in conn.executed] == [(11,), (10,)]
    selects_only(conn)


def test_get_fetch_text_follows_a_chain_of_two():
    conn = FakeConn(results=[(None, 2), (None, 3), ("Final", None)])
    assert store.get_fetch_text(conn, 1) == "Final"
    assert [p for _, p in conn.executed] == [(1,), (2,), (3,)]


def test_get_fetch_text_allows_exactly_three_hops_and_no_more():
    three = FakeConn(results=[(None, 2), (None, 3), (None, 4), ("Quart", None)])
    assert store.get_fetch_text(three, 1) == "Quart"                       # 3 salts
    four = FakeConn(results=[(None, 2), (None, 3), (None, 4), (None, 5), ("Cinque", None)])
    assert store.get_fetch_text(four, 1) is None                            # 4 salts: massa llarg
    assert len(four.executed) == 4                                          # no consulta mes enlla del limit
    assert store.MAX_REUSE_HOPS == 3


def test_get_fetch_text_cycle_returns_none_without_raising():
    conn = FakeConn(results=[(None, 2), (None, 1)])
    assert store.get_fetch_text(conn, 1) is None
    assert len(conn.executed) == 2                                          # no torna a la fila 1


def test_get_fetch_text_self_reference_returns_none():
    assert store.get_fetch_text(FakeConn(results=[(None, 5)]), 5) is None


def test_get_fetch_text_missing_row_returns_none():
    conn = FakeConn(results=[None])
    assert store.get_fetch_text(conn, 99) is None
    assert store.get_fetch_text(FakeConn(results=[(None, 7), None]), 1) is None      # enllac a una fila que no existeix


def test_get_fetch_text_row_without_text_or_link_returns_none():
    assert store.get_fetch_text(FakeConn(results=[(None, None)]), 3) is None


def test_get_fetch_text_accepts_mapping_rows():
    conn = FakeConn(results=[{"text_content": None, "reused_from_fetch_id": 4}, {"text_content": "Mapa", "reused_from_fetch_id": None}])
    assert store.get_fetch_text(conn, 9) == "Mapa"


@pytest.mark.parametrize("fetch_id", [None, "1", 1.5, True])
def test_get_fetch_text_invalid_id_returns_none_without_querying(fetch_id):
    conn = FakeConn()
    assert store.get_fetch_text(conn, fetch_id) is None
    assert conn.executed == [] and conn.cursors_opened == 0


# ---------------------------------------------------------------------------
# fetch_and_store
# ---------------------------------------------------------------------------

def make_store_fetcher(routes):
    fetcher, session, _ = make_fetcher(routes)
    return fetcher, session


def test_fetch_and_store_reuses_an_ok_row_as_a_duplicate():
    conn = FakeConn(results=[(7,), (100,)])
    fetcher, session = make_store_fetcher({})
    new_id, record = store.fetch_and_store(conn, 3, ART + "/", fetcher=fetcher)
    assert new_id == 100
    assert record.fetch_status == "duplicate" and record.reused_from_fetch_id == 7
    assert session.calls == []  # no es torna a baixar
    select_sql, select_params = conn.executed[0]
    insert_sql, insert_params = conn.executed[1]
    assert "SELECT id" in select_sql and select_params == (ART, ART)  # URL normalitzada
    assert "INSERT INTO public.entry_content_fetches" in insert_sql
    row = dict(zip(store.INSERT_FETCH_COLUMNS, insert_params[1:]))
    assert insert_params[0] == 3
    assert row["fetch_status"] == "duplicate" and row["reused_from_fetch_id"] == 7
    assert row["text_content"] is None and row["text_length"] is None and row["content_hash"] is None
    assert conn.commits == 0 and conn.rollbacks == 0


def test_fetch_and_store_downloads_again_when_there_is_no_ok_row():
    routes = {"https://example.com/robots.txt": FakeResponse(404), ART: ok_page()}
    conn = FakeConn(results=[None, (101,)])
    fetcher, session = make_store_fetcher(routes)
    new_id, record = store.fetch_and_store(conn, 4, ART, fetcher=fetcher)
    assert new_id == 101 and record.fetch_status == "ok"
    assert ART in session.urls
    row = dict(zip(store.INSERT_FETCH_COLUMNS, conn.executed[1][1][1:]))
    assert row["fetch_status"] == "ok" and len(row["content_hash"]) == 64 and row["reused_from_fetch_id"] is None


def test_fetch_and_store_records_failures_too():
    routes = {"https://example.com/robots.txt": FakeResponse(404), ART: FakeResponse(403)}
    conn = FakeConn(results=[None, (102,)])
    fetcher, _ = make_store_fetcher(routes)
    new_id, record = store.fetch_and_store(conn, 5, ART, fetcher=fetcher)
    assert (new_id, record.fetch_status) == (102, "access_denied")


def test_fetch_and_store_without_canonical_url_stores_an_error_and_has_no_fallback():
    conn = FakeConn(results=[(55,)])
    fetcher, session = make_store_fetcher({})
    new_id, record = store.fetch_and_store(conn, 9, None, fetcher=fetcher)
    assert new_id == 55
    assert record.fetch_status == "error" and record.error_detail == "no_canonical_url"
    assert session.calls == []
    assert len(conn.executed) == 1  # cap cerca de duplicats: no hi ha URL
    row = dict(zip(store.INSERT_FETCH_COLUMNS, conn.executed[0][1][1:]))
    assert row["requested_url"] == "" and row["normalized_url"] == ""
    assert row["error_detail"] == "no_canonical_url"
    assert "source_url" not in inspect.signature(store.fetch_and_store).parameters


def test_fetch_and_store_only_touches_entry_content_fetches():
    routes = {"https://example.com/robots.txt": FakeResponse(404), ART: ok_page()}
    conn = FakeConn(results=[None, (1,)])
    fetcher, _ = make_store_fetcher(routes)
    store.fetch_and_store(conn, 1, ART, fetcher=fetcher)
    for sql, _params in conn.executed:
        assert "public.entry_content_fetches" in sql
        assert not re.search(r"public\.entries\b", sql)
        assert not re.search(r"\b(UPDATE|DELETE)\b", sql)


# ---------------------------------------------------------------------------
# Aillament del modul
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["content_fetch.py", "content_fetch_store.py"])
def test_modules_never_write_to_entries_or_commit(name):
    source = open(os.path.join(APP_DIR, name), encoding="utf-8").read()
    assert not re.search(r"(INSERT INTO|UPDATE|DELETE FROM)\s+public\.entries\b", source)
    assert not re.search(r"\.(commit|rollback)\(", source)
    assert "discard_reason =" not in source and "analyzed_fetch_id =" not in source


@pytest.mark.parametrize("name", ["crawler.py", "main.py", "ingest_ops.py", "enrichment_queue.py"])
def test_pipeline_modules_do_not_import_the_new_modules_at_module_level(name):
    # Els moduls nous nomes es poden importar de manera MANDROSA (dins de funcions, amb
    # sagnat): una importacio a nivell de modul els carregaria tambe amb l'interruptor apagat.
    source = open(os.path.join(APP_DIR, name), encoding="utf-8").read()
    assert not re.search(r"^(?:from\s+app\.content_fetch\w*\s+import|import\s+app\.content_fetch|"
                         r"from\s+app\s+import\s+.*content_fetch)", source, re.M)
