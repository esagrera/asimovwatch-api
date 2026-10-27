"""
Integracio de l'obtencio de contingut a run_entry_enrichment (opcio B), darrere CONTENT_FETCH_ENABLED.
Connexio de BD i sessio HTTP simulades: cap BD ni xarxa. La BD simulada NO es PostgreSQL: nomes emula les
sentencies que fa el pipeline.
"""

import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest
import requests

from app import content_fetch as cf
from app import content_fetch_pipeline as pipe
from app import content_fetch_store as store
from tests.fakes import FakeClock, FakeResponse, FakeSession, HTML_HEADERS, LONG_TEXT, ok_page, page
from tests.pipeline_imports import import_pipeline

crawler, main = import_pipeline()
import app.enrichment_queue as queue  # noqa: E402  (despres d'import_pipeline: app.db ja carregat sense .env)

T0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
URL = "https://news.example/a"
ROBOTS = "https://news.example/robots.txt"

ENV_KEYS = ("CONTENT_FETCH_ENABLED", "CONTENT_FETCH_SKIP_DOMAINS", "CONTENT_FETCH_ENTRY_IDS",
            "PRIMARY_TEXT_MAX_CHARS", "CONTENT_FETCH_MIN_TEXT_CHARS", "CONTENT_FETCH_USER_AGENT")

INPUT_JSON = {"input_relevance": "high", "input_relevance_reason": "r", "ready_for_primary": "yes",
              "clean_input_text": "TEXT NET D'INPUT", "input_summary": "resum", "input_quality": "high",
              "input_quality_notes": "", "source_language": "en"}
PRIMARY_JSON = {"summary_factual": "S", "why_it_matters": "W", "theme_tags": ["t"], "affected_principles": ["p"],
                "risk_level": "low", "debate_questions": ["q"], "confidence_notes": "",
                "human_protection_declared": "unknown", "human_protection_verifiable": "unknown",
                "human_protection_depth": "unknown", "human_protection_notes": "n", "entry_category": "other",
                "analyzed_provider": None, "analyzed_model": None, "bihp_directives": [],
                "relevance_score": "low", "relevance_reason": "x"}
OUTPUT_JSON = {"summary_factual_ca": "S", "summary_factual_en": "S", "why_it_matters_ca": "W",
               "why_it_matters_en": "W", "debate_questions_ca": ["q"], "debate_questions_en": ["q"],
               "human_protection_notes_ca": "n", "human_protection_notes_en": "n"}


def set_env(monkeypatch, **values):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


# ---------------------------------------------------------------------------
# BD simulada (nomes les sentencies del pipeline) i sessio HTTP amb ganxo
# ---------------------------------------------------------------------------

class FakeDB:
    def __init__(self, entry, fetches=None):
        self.entry = entry
        self.fetches = [dict(f) for f in (fetches or [])]
        self.next_id = 900
        self.events, self.executed, self.ready_writes, self.net_in_tx = [], [], [], []
        self.in_tx, self.commits, self.rollbacks = False, 0, 0

    def connection(self):
        return _Conn(self)

    def sql(self, fragment):
        return [(s, p) for s, p in self.executed if fragment in s]


class _Conn:
    def __init__(self, db):
        self.db = db

    def cursor(self, cursor_factory=None):
        return _Cur(self.db)

    def commit(self):
        self.db.in_tx = False
        self.db.commits += 1
        self.db.events.append(("commit",))

    def rollback(self):
        self.db.in_tx = False
        self.db.rollbacks += 1
        self.db.events.append(("rollback",))

    def close(self):
        pass


class _Cur:
    def __init__(self, db):
        self.db, self.result = db, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result

    def close(self):
        pass

    def execute(self, sql, params=None):
        db = self.db
        flat = " ".join(sql.split())
        db.in_tx = True
        db.executed.append((flat, params))
        self.result = None
        kind = "other"
        if flat.startswith("SELECT * FROM public.entries WHERE id = %s"):
            self.result, kind = db.entry, "load"
        elif flat.startswith("UPDATE public.entries"):
            kind = "link" if flat.startswith("UPDATE public.entries SET analyzed_fetch_id = %s WHERE id = %s") else "update"
            names = re.findall(r"(\w+) = (?:COALESCE\()?%s", flat)
            for name, value in zip(names, list(params or [])):
                if name == "id":
                    continue
                if f"{name} = COALESCE(%s, {name})" in flat:
                    if value is not None:
                        db.entry[name] = value
                else:
                    db.entry[name] = value
                if name == "ready_for_primary":
                    db.ready_writes.append(value)
            for name, literal in re.findall(r"(\w+) = '([^']*)'", flat):
                db.entry[name] = literal
                if name == "ready_for_primary":
                    db.ready_writes.append(literal)
        elif flat.startswith("SELECT id, fetch_status, http_status, fetched_at, text_length FROM public.entry_content_fetches"):
            rows = [r for r in db.fetches if r["entry_id"] == params[0]]
            rows.sort(key=lambda r: (r["fetched_at"], r["id"]), reverse=True)
            self.result = [(r["id"], r["fetch_status"], r["http_status"], r["fetched_at"], r["text_length"]) for r in rows]
            kind = "list_fetches"
        elif flat.startswith("INSERT INTO public.entry_content_fetches"):
            db.next_id += 1
            row = {"id": db.next_id, "entry_id": params[0], **dict(zip(store.INSERT_FETCH_COLUMNS, params[1:]))}
            db.fetches.append(row)
            self.result, kind = (db.next_id,), "insert_fetch"
        elif "md5(normalized_url) = md5(%s)" in flat:
            hits = [r for r in db.fetches if r["normalized_url"] == params[0] and r["fetch_status"] == "ok"]
            hits.sort(key=lambda r: (r["fetched_at"], r["id"]), reverse=True)
            self.result, kind = ((hits[0]["id"],) if hits else None), "find_reusable"
        elif flat.startswith("SELECT text_content, reused_from_fetch_id FROM public.entry_content_fetches WHERE id = %s"):
            row = next((r for r in db.fetches if r["id"] == params[0]), None)
            self.result = None if row is None else (row["text_content"], row["reused_from_fetch_id"])
            kind = "get_text"
        else:
            raise AssertionError(f"SQL no previst: {flat[:90]}")
        db.events.append(("sql", kind))


class HookedSession(FakeSession):
    def __init__(self, routes, clock, db):
        super().__init__(routes, clock)
        self.db = db

    def get(self, url, **kwargs):
        self.db.events.append(("http", url))
        self.db.net_in_tx.append(self.db.in_tx)
        return super().get(url, **kwargs)


def make_entry(feed_len=100, **overrides):
    text = "x" * feed_len
    entry = {
        "id": 42, "raw_content": text, "raw_snippet": text, "raw_snippet_original": None,
        "source_url": "https://news.example/feed", "canonical_url": URL, "source_domain": "news.example",
        "source_title": "Titol", "source_type": "company", "country_region": "UE", "institution_type": None,
        "published_date": None, "source_language": "en", "ingest_method": "rss", "raw_payload": {},
        "processing_status": "RAW", "processing_retries": 0, "analyzed_fetch_id": None,
    }
    entry.update(overrides)
    return entry


def route_set(article=None, robots=None, url=URL):
    host = url.split("/")[2]
    routes = {f"https://{host}/robots.txt": robots if robots is not None else FakeResponse(404)}
    if article is not None:
        routes[url] = article
    return routes


def words(n_chars):
    """Text d'exactament n_chars caracters, amb espais (paraules de 9 + espai)."""
    base = ("palabra99 " * (n_chars // 10 + 1))[:n_chars]
    return base.rstrip() if base.endswith(" ") else base


class Harness:
    def __init__(self, monkeypatch, entry=None, fetches=None, routes=None, input_ready="yes", now=T0, fetcher_opts=None):
        self.db = FakeDB(entry or make_entry(), fetches)
        self.clock = FakeClock()
        self.now = now
        self.input_ready = input_ready
        self.session = HookedSession(routes if routes is not None else {}, self.clock, self.db)
        self.fetcher = cf.ContentFetcher(self.session, sleep=self.clock.sleep, clock=self.clock,
                                         now=lambda: self.now, **(fetcher_opts or {}))
        self.overrides = {}
        monkeypatch.setattr(crawler, "get_connection", lambda: self.db.connection())
        monkeypatch.setattr(crawler, "call_llm_for_prompt", self.fake_llm)
        monkeypatch.setattr(pipe, "_utc_now", lambda: self.now)

    def fake_llm(self, conn, key, prompt_overrides=None):
        self.db.events.append(("llm", key))
        self.overrides[key] = prompt_overrides
        payload = {"Input": {**INPUT_JSON, "ready_for_primary": self.input_ready},
                   "Primary": PRIMARY_JSON, "Output": OUTPUT_JSON}[key]
        return {"output": json.dumps(payload), "provider_used": "f", "model_used": f"f-{key}", "used_fallback": False}

    def run(self, with_fetcher=True, **kwargs):
        if with_fetcher:
            kwargs.setdefault("content_fetcher", self.fetcher)
        return crawler.run_entry_enrichment(entry_id=42, **kwargs)

    @property
    def input_text(self):
        return self.overrides["Input"]["{input_text}"]

    @property
    def article_requests(self):
        return [e for e in self.db.events if e[0] == "http" and e[1] != ROBOTS]

    @property
    def http_requests(self):
        return [e for e in self.db.events if e[0] == "http"]

    def index(self, event):
        return self.db.events.index(event)


def assert_universal_invariants(h):
    sqls = " ".join(s for s, _ in h.db.executed)
    assert not re.search(r"raw_content\s*=|raw_snippet\s*=", sqls)                     # cap canvi a raw_*
    assert "processing_retries" not in sqls.replace("SELECT * FROM public.entries", "")   # cap increment
    assert not any(h.db.net_in_tx)                                                      # cap transaccio oberta durant la xarxa
    assert "'ERROR'" not in sqls


# ===========================================================================
# 1. Interruptor apagat: identic a l'actual
# ===========================================================================

ORIGINAL_INPUT_DISCARD_SQL = " ".join("""
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
""".split())

ORIGINAL_NO_CONTENT_SQL = " ".join("""
    UPDATE public.entries
    SET
        processing_status = 'DISCARDED',
        processing_error = NULL,
        input_relevance = 'unknown',
        input_relevance_reason = %s,
        ready_for_primary = 'no',
        input_quality = 'low',
        input_quality_notes = %s,
        updated_at = NOW()
    WHERE id = %s
""".split())


@pytest.mark.parametrize("env", [
    {}, {"CONTENT_FETCH_ENABLED": "0"}, {"CONTENT_FETCH_ENABLED": "false"},
    {"CONTENT_FETCH_ENABLED": "TRUE"},   # nomes minuscules activen
    {"CONTENT_FETCH_ENTRY_IDS": "42", "CONTENT_FETCH_SKIP_DOMAINS": "", "PRIMARY_TEXT_MAX_CHARS": "10"},
])
def test_switch_off_flow_is_the_current_one(monkeypatch, env):
    set_env(monkeypatch, **env)
    h = Harness(monkeypatch, entry=make_entry(100), routes=route_set(ok_page()))
    monkeypatch.setattr(cf, "ContentFetcher", lambda *a, **k: pytest.fail("no s'ha de crear cap ContentFetcher"))
    result = h.run(with_fetcher=False)
    assert result["status"] == "enriched"
    kinds = [k for e in h.db.events if e[0] == "sql" for k in [e[1]]]
    assert kinds == ["load", "update", "update"]                       # carrega, Input, final (com avui)
    assert [e for e in h.db.events if e[0] == "llm"] == [("llm", "Input"), ("llm", "Primary"), ("llm", "Output")]
    assert h.http_requests == [] and h.db.fetches == []
    assert not any(t in s for s, _ in h.db.executed for t in ("entry_content_fetches", "analyzed_fetch_id", "discard_reason"))
    assert h.overrides["Input"] == crawler._build_input_overrides(h.db.entry)           # text del feed
    assert h.overrides["Primary"]["{input_text}"] == crawler._build_primary_input_text(
        h.db.entry, {**INPUT_JSON})                                                       # sense canonical_url
    assert "canonical_url:" not in h.overrides["Primary"]["{input_text}"]
    assert h.db.commits == 2 and h.db.rollbacks == 0                                      # com avui: 2 commits


def test_switch_off_input_discard_and_no_content_sql_are_byte_identical(monkeypatch):
    set_env(monkeypatch)
    h = Harness(monkeypatch, entry=make_entry(100), input_ready="no")
    assert h.run(with_fetcher=False)["status"] == "discarded"
    (sql, params), = h.db.sql("processing_status = 'DISCARDED'")
    assert sql == ORIGINAL_INPUT_DISCARD_SQL and len(params) == 7
    assert "discard_reason" not in sql

    h2 = Harness(monkeypatch, entry=make_entry(0))
    assert h2.run(with_fetcher=False)["status"] == "discarded"
    (sql2, params2), = h2.db.sql("processing_status = 'DISCARDED'")
    assert sql2 == ORIGINAL_NO_CONTENT_SQL
    assert params2 == ("No hi ha contingut RSS analitzable.", "El feed no ha aportat summary, description ni content.", 42)
    assert h2.db.ready_writes == ["no"]                                                   # com avui


def test_switch_off_queue_selection_and_call_are_unchanged(monkeypatch):
    set_env(monkeypatch)
    captured = {}

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def execute(self, sql, params=None):
            captured["sql"], captured["params"] = " ".join(sql.split()), params

        def fetchall(self):
            return []

    class Conn:
        def cursor(self, cursor_factory=None):
            return Cur()

        def close(self):
            pass

    monkeypatch.setattr(queue, "get_connection", lambda: Conn())
    assert queue._load_candidate_entries(3) == []
    assert captured["params"] == (3,)
    assert "entry_content_fetches" not in captured["sql"] and "NOT EXISTS" not in captured["sql"]
    assert captured["sql"].endswith("ORDER BY detected_at ASC NULLS LAST, id ASC")

    calls = []
    monkeypatch.setattr(queue, "run_entry_enrichment", lambda **kw: calls.append(kw) or {"status": "enriched"})
    monkeypatch.setattr(cf, "ContentFetcher", lambda *a, **k: pytest.fail("no s'ha de crear cap ContentFetcher"))
    queue._run_selected_entries([{"id": 1}, {"id": 2}], timeout_seconds=180)
    assert calls == [dict(entry_id=i, skip_input=False, run_input=True, run_primary=True, run_output=True, persist=True)
                     for i in (1, 2)]


# ===========================================================================
# 2. Matriu: resultat de l'obtencio x longitud del feed
# ===========================================================================

def html_text(n_chars):
    return FakeResponse(200, page("T", f"<p>{words(n_chars)}</p>"), HTML_HEADERS)


# nom -> (rutes, text esperat si s'usa o None, tipus)
def scenario_routes(name):
    r404 = FakeResponse(404)
    return {
        "ok": route_set(ok_page()),
        "duplicate": route_set(),
        "tts300": route_set(html_text(300)),
        "tts50": route_set(html_text(50)),
        "denied403": route_set(FakeResponse(403)),
        "robots_blocked": route_set(ok_page(), robots=FakeResponse(200, "User-agent: *\nDisallow: /\n")),
        "anti_bot": route_set(FakeResponse(200, page("Just a moment", "<p>espera</p>"), HTML_HEADERS)),
        "pdf": route_set(FakeResponse(200, b"%PDF", {"Content-Type": "application/pdf"})),
        "unsupported_ct": route_set(FakeResponse(200, "{}", {"Content-Type": "application/json"})),
        "http404": route_set(r404),
        "timeout": route_set(requests.Timeout("lent")),
        "robots_503": route_set(ok_page(), robots=FakeResponse(503)),
        "http503": route_set(FakeResponse(503)),
        "http429": route_set(FakeResponse(429)),
    }[name]


USE_ALWAYS = {"ok", "duplicate"}
SHORT_TEXT_LEN = {"tts300": 300, "tts50": 50}
PERMANENT = {"denied403", "robots_blocked", "anti_bot", "pdf", "unsupported_ct", "http404"}
TEMPORARY = {"timeout", "robots_503", "http503", "http429"}
ALL_SCENARIOS = sorted(USE_ALWAYS | set(SHORT_TEXT_LEN) | PERMANENT | TEMPORARY)


def expected_outcome(scenario, feed_len):
    if feed_len >= 400:
        return "ineligible"
    if scenario in USE_ALWAYS:
        return "use"
    if scenario in SHORT_TEXT_LEN:
        return "use" if SHORT_TEXT_LEN[scenario] > feed_len else "fallback"
    return "fallback" if scenario in PERMANENT else "defer"


@pytest.mark.parametrize("feed_len", [0, 100, 399, 400, 5000])
@pytest.mark.parametrize("scenario", ALL_SCENARIOS)
def test_matrix_fetch_outcome_by_feed_length(monkeypatch, scenario, feed_len):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    fetches = []
    if scenario == "duplicate":      # ja hi ha un 'ok' d'una altra entrada amb la mateixa URL
        fetches = [dict(id=1, entry_id=7, requested_url=URL, normalized_url=URL, fetch_status="ok", http_status=200,
                        fetched_at=T0 - timedelta(days=1), text_content=LONG_TEXT, text_length=len(LONG_TEXT),
                        reused_from_fetch_id=None)]
    h = Harness(monkeypatch, entry=make_entry(feed_len), fetches=fetches, routes=scenario_routes(scenario))
    feed_text = "x" * feed_len
    outcome = expected_outcome(scenario, feed_len)

    result = h.run()
    assert_universal_invariants(h)
    assert "no" not in h.db.ready_writes                                # Input diu 'yes': cap 'no' per una fallada d'obtencio

    if outcome == "ineligible":
        assert h.http_requests == [] and not h.db.sql("entry_content_fetches")
        assert result["status"] == "enriched" and h.input_text == feed_text
        assert h.db.entry["analyzed_fetch_id"] is None
        return

    new_rows = [r for r in h.db.fetches if r["entry_id"] == 42]
    assert len(new_rows) == 1                                           # un intent, sempre registrat
    row = new_rows[0]

    if outcome == "use":
        expected_text = LONG_TEXT if scenario in USE_ALWAYS else words(SHORT_TEXT_LEN[scenario])
        assert result["status"] == "enriched"
        assert h.input_text == expected_text
        assert h.db.entry["analyzed_fetch_id"] == row["id"]
        assert row["fetch_status"] == ("duplicate" if scenario == "duplicate" else row["fetch_status"])
        assert "TEXT NET D'INPUT" not in h.overrides["Primary"]["{input_text}"]      # Primary rep el text obtingut
        assert h.overrides["Primary"]["{input_text}"].endswith(f"Contingut per analitzar:\n{expected_text}")
        assert f"canonical_url: {URL}\n" in h.overrides["Primary"]["{input_text}"]
        if scenario == "duplicate":
            assert h.article_requests == [] and ROBOTS not in [e[1] for e in h.http_requests]
    elif outcome == "fallback":
        assert h.db.entry["analyzed_fetch_id"] is None
        assert row["fetch_status"] != "ok"
        if feed_len == 0:                                               # res a analitzar: descart tecnic
            assert result["status"] == "discarded"
            assert h.db.entry["discard_reason"] == "fetch_failed"
            assert h.db.ready_writes == [] and h.db.entry["processing_status"] == "DISCARDED"
            assert not [e for e in h.db.events if e[0] == "llm"]
        else:                                                           # continua amb el feed, com avui
            assert result["status"] == "enriched" and h.input_text == feed_text
            assert "canonical_url:" not in h.overrides["Primary"]["{input_text}"]
    else:  # defer
        assert result["status"] == "skipped" and result["detail"]["reason"] == "content_fetch_deferred"
        assert h.db.entry["processing_status"] == "RAW"                  # es queda RAW
        assert not [e for e in h.db.events if e[0] == "llm"]
        assert not h.db.sql("UPDATE public.entries")                     # cap escriptura a entries
        assert h.db.entry["analyzed_fetch_id"] is None


# ===========================================================================
# 3. Cap fallada d'obtencio escriu ready_for_primary='no'
# ===========================================================================

@pytest.mark.parametrize("feed_len", [0, 100])
@pytest.mark.parametrize("scenario", sorted(PERMANENT | TEMPORARY | {"tts50"}))
def test_no_fetch_failure_ever_writes_ready_for_primary_no(monkeypatch, scenario, feed_len):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, entry=make_entry(feed_len), routes=scenario_routes(scenario))
    h.run()
    assert "no" not in h.db.ready_writes
    assert all(not re.search(r"ready_for_primary = 'no'", s) for s, _ in h.db.executed)


def test_the_only_ready_no_with_fetch_on_is_the_no_attempt_empty_feed_path(monkeypatch):
    """Sense intent d'obtencio (domini omes) i feed buit: es el camí 'no_content' d'avui (ready 'no')."""
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, entry=make_entry(0, source_domain="openai.com", canonical_url="https://openai.com/x"))
    assert h.run()["status"] == "discarded"
    assert h.db.entry["discard_reason"] == "no_content" and h.db.ready_writes == ["no"]
    assert h.http_requests == [] and h.db.fetches == []


# ===========================================================================
# 4. analyzed_fetch_id, ordre i connexio
# ===========================================================================

def test_analyzed_fetch_id_is_written_and_committed_before_input(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, routes=route_set(ok_page()))
    assert h.run()["status"] == "enriched"
    events = h.db.events
    link = events.index(("sql", "link"))
    input_call = events.index(("llm", "Input"))
    assert link < input_call
    assert ("commit",) in events[link:input_call]                    # confirmat abans d'Input
    assert events.index(("sql", "insert_fetch")) < events.index(("commit",)) < link   # la fila, confirmada primer
    (sql, params), = h.db.sql("SET analyzed_fetch_id")
    assert sql == "UPDATE public.entries SET analyzed_fetch_id = %s WHERE id = %s" and params == (h.db.fetches[-1]["id"], 42)


def test_the_link_survives_an_input_failure(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, routes=route_set(ok_page()))

    def exploding_llm(conn, key, prompt_overrides=None):
        h.db.events.append(("llm", key))
        raise RuntimeError("Input fallit")

    monkeypatch.setattr(crawler, "call_llm_for_prompt", exploding_llm)
    result = h.run()
    assert result["status"] == "error"
    assert h.db.entry["analyzed_fetch_id"] == h.db.fetches[-1]["id"]       # l'enllac ja era confirmat
    link, input_call = h.db.events.index(("sql", "link")), h.db.events.index(("llm", "Input"))
    assert ("commit",) in h.db.events[link:input_call]


def test_connection_is_never_in_a_transaction_during_the_network(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, routes=route_set(ok_page()))
    h.run()
    assert len(h.http_requests) == 2 and h.db.net_in_tx == [False, False]     # robots.txt i article


def test_connection_is_not_in_a_transaction_during_network_even_with_history_and_reuse_lookup(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    old = dict(id=5, entry_id=42, requested_url=URL, normalized_url=URL, fetch_status="timeout", http_status=None,
               fetched_at=T0 - timedelta(hours=2), text_content=None, text_length=None, reused_from_fetch_id=None)
    h = Harness(monkeypatch, fetches=[old], routes=route_set(ok_page()))
    h.run()
    assert h.db.net_in_tx and not any(h.db.net_in_tx)


# ===========================================================================
# 5. Capat de 20.000 i text directe a Primary
# ===========================================================================

def test_cap_text_unit():
    assert pipe.cap_text("abc def", 100) == ("abc def", False)
    assert pipe.cap_text("a" * 50, 50) == ("a" * 50, False)                         # exactament el limit: no es talla
    text = "aaaa bbbb cccc dddd"
    assert pipe.cap_text(text, 12) == ("aaaa bbbb", True)                           # ultim espai abans del limit
    assert pipe.cap_text(text, 13) == ("aaaa bbbb cccc", True) or pipe.cap_text(text, 13) == ("aaaa bbbb", True)
    assert pipe.cap_text("x" * 30, 10) == ("x" * 10, True)                          # sense espais: limit exacte
    assert pipe.cap_text(" " + "y" * 30, 10) == (" " + "y" * 9, True)               # l'espai inicial no deixa un text buit
    assert pipe.cap_text(text, 12) == pipe.cap_text(text, 12)                       # determinista
    out, _ = pipe.cap_text("p " * 20000, 20000)
    assert len(out) <= 20000 and out == out.rstrip() and out.endswith("p")


def test_primary_text_max_chars_default_and_environment():
    assert pipe.primary_text_max_chars({}) == 20000 == pipe.DEFAULT_PRIMARY_TEXT_MAX_CHARS
    assert pipe.primary_text_max_chars({"PRIMARY_TEXT_MAX_CHARS": "5000"}) == 5000
    for bad in ("", "abc", "0", "-5"):
        assert pipe.primary_text_max_chars({"PRIMARY_TEXT_MAX_CHARS": bad}) == 20000


def test_fetched_text_is_capped_at_20000_for_input_and_primary_and_the_cut_is_logged(monkeypatch, caplog):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    big = words(30000)
    h = Harness(monkeypatch, routes=route_set(FakeResponse(200, page("T", f"<p>{big}</p>"), HTML_HEADERS)))
    with caplog.at_level(logging.INFO, logger="asimovwatch.content_fetch"):
        assert h.run()["status"] == "enriched"
    assert len(h.input_text) <= 20000 and big.startswith(h.input_text)
    assert len(h.input_text) > 19000 and not h.input_text.endswith(" ")
    assert h.overrides["Primary"]["{input_text}"].endswith(f"Contingut per analitzar:\n{h.input_text}")
    assert [r.getMessage() for r in caplog.records if "tallat" in r.getMessage()] == [
        f"Entry 42: text obtingut tallat de {len(big)} a {len(h.input_text)} caracters (limit 20000)."]
    stored = h.db.fetches[-1]
    assert stored["text_content"] == big                                           # la BD conserva el text complet


def test_cap_limit_is_configurable_by_environment(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1", PRIMARY_TEXT_MAX_CHARS="500")
    h = Harness(monkeypatch, routes=route_set(ok_page()))
    h.run()
    assert len(h.input_text) <= 500


def test_primary_receives_the_fetched_text_not_the_clean_input_text(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, routes=route_set(ok_page()))
    h.run()
    primary = h.overrides["Primary"]["{input_text}"]
    assert LONG_TEXT in primary and "TEXT NET D'INPUT" not in primary
    assert f"source_url: https://news.example/feed\ncanonical_url: {URL}\nsource_domain: news.example" in primary
    assert "Resum previ (si existeix):\nresum" in primary                            # el resum d'Input es conserva


def test_primary_builder_difference_with_text_is_fetched():
    entry = {"raw_content": "feed", "source_url": "https://a.cat/feed", "canonical_url": "https://a.cat/p",
             "source_domain": "a.cat", "source_title": "T", "source_language": "ca"}
    result = {"clean_input_text": "NET", "input_summary": "RES"}
    default = crawler._build_primary_input_text(entry, result)
    fetched = crawler._build_primary_input_text(entry, result, text="OBTINGUT", text_is_fetched=True)
    assert default.endswith("Contingut per analitzar:\nNET")
    assert fetched.endswith("Contingut per analitzar:\nOBTINGUT")
    assert fetched == default.replace("source_url: https://a.cat/feed\n",
                                      "source_url: https://a.cat/feed\ncanonical_url: https://a.cat/p\n").replace(
        "\nNET", "\nOBTINGUT")
    assert crawler._build_primary_input_text(entry, result, text="X") .endswith("analitzar:\nNET")   # sense text_is_fetched


# ===========================================================================
# 6. Domini omes i CONTENT_FETCH_ENTRY_IDS
# ===========================================================================

@pytest.mark.parametrize("canonical,domain", [
    ("https://openai.com/index/x", "openai.com"),
    ("https://www.openai.com/index/x", "www.openai.com"),                           # subdomini
    ("https://cdn.openai.com/x", "openai.com"),
    ("https://fra.europa.eu/en/news/x", "fra.europa.eu"),
    ("https://sub.fra.europa.eu/x", "fra.europa.eu"),
    ("https://news.example/a", "openai.com"),                                        # source_domain omes
    ("https://openai.com/a", "other.example"),                                       # canonical omes
])
def test_default_skipped_domains_make_no_request_and_no_row(monkeypatch, caplog, canonical, domain):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, entry=make_entry(100, canonical_url=canonical, source_domain=domain))
    with caplog.at_level(logging.INFO, logger="asimovwatch.content_fetch"):
        assert h.run()["status"] == "enriched"
    assert h.http_requests == [] and h.db.fetches == [] and not h.db.sql("entry_content_fetches")
    assert any("domini omes" in r.getMessage() and "Entry 42" in r.getMessage() for r in caplog.records)
    assert h.input_text == "x" * 100


def test_skipped_domain_matching_does_not_catch_lookalikes(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    for canonical, domain in (("https://notopenai.com/x", "notopenai.com"), ("https://openai.com.evil.example/x", "evil.example")):
        h = Harness(monkeypatch, entry=make_entry(100, canonical_url=canonical, source_domain=domain),
                    routes=route_set(ok_page(), url=canonical))
        h.run()
        assert h.db.fetches, canonical


def test_skip_domains_environment_replaces_the_default(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1", CONTENT_FETCH_SKIP_DOMAINS="news.example")
    h = Harness(monkeypatch, routes=route_set(ok_page()))
    h.run()
    assert h.http_requests == [] and h.db.fetches == []
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1", CONTENT_FETCH_SKIP_DOMAINS="")        # buida = cap omes
    h = Harness(monkeypatch, entry=make_entry(100, canonical_url="https://openai.com/x", source_domain="openai.com"),
                routes=route_set(ok_page(), url="https://openai.com/x"))
    h.run()
    assert h.db.fetches


def test_env_parsers():
    assert pipe.skip_domains({}) == ["openai.com", "fra.europa.eu"]
    assert pipe.skip_domains({"CONTENT_FETCH_SKIP_DOMAINS": " A.com , ,b.org "}) == ["a.com", "b.org"]
    assert pipe.allowed_entry_ids({}) is None and pipe.allowed_entry_ids({"CONTENT_FETCH_ENTRY_IDS": " , "}) is None
    assert pipe.allowed_entry_ids({"CONTENT_FETCH_ENTRY_IDS": "1, 22 ,x,33"}) == {1, 22, 33}


def test_entry_ids_restrict_the_fetch_to_the_listed_entries(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1", CONTENT_FETCH_ENTRY_IDS="7, 8")
    h = Harness(monkeypatch, routes=route_set(ok_page()))
    assert h.run()["status"] == "enriched"
    assert h.http_requests == [] and h.db.fetches == [] and h.input_text == "x" * 100
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1", CONTENT_FETCH_ENTRY_IDS="7, 42")
    h = Harness(monkeypatch, routes=route_set(ok_page()))
    h.run()
    assert h.db.fetches and h.input_text == LONG_TEXT
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1", CONTENT_FETCH_ENTRY_IDS="")          # buida = totes
    h = Harness(monkeypatch, routes=route_set(ok_page()))
    h.run()
    assert h.db.fetches


# ===========================================================================
# 7. Elegibilitat
# ===========================================================================

@pytest.mark.parametrize("overrides,reason", [
    ({"ingest_method": "web_search"}, "not_rss"),
    ({"ingest_method": None}, "not_rss"),
    ({"canonical_url": None}, "no_canonical_url"),
    ({"canonical_url": "   "}, "no_canonical_url"),
    ({"canonical_url": "ftp://news.example/a"}, "canonical_url_not_http"),
    ({"canonical_url": "no-es-url"}, "canonical_url_not_http"),
])
def test_ineligible_entries_make_no_fetch(monkeypatch, overrides, reason):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    entry = make_entry(100, **overrides)
    assert pipe.eligibility(entry, {}) == (False, reason)
    h = Harness(monkeypatch, entry=entry, routes=route_set(ok_page()))
    h.run()
    assert h.http_requests == [] and h.db.fetches == []


def test_feed_length_threshold_is_the_modules_constant(monkeypatch):
    assert pipe.sufficient_text_chars() == cf.MIN_TEXT_CHARS == 400
    assert pipe.eligibility(make_entry(399), {})[0] is True
    assert pipe.eligibility(make_entry(400), {})[0] is False
    monkeypatch.setattr(cf, "MIN_TEXT_CHARS", 50)                                  # una sola constant: la mateixa per als dos usos
    assert pipe.sufficient_text_chars() == 50
    assert pipe.eligibility(make_entry(49), {})[0] is True and pipe.eligibility(make_entry(50), {})[0] is False


def test_feed_text_uses_raw_content_then_raw_snippet_and_strips():
    assert pipe.feed_text_of({"raw_content": "  abc ", "raw_snippet": "zzz"}) == "abc"
    assert pipe.feed_text_of({"raw_content": "", "raw_snippet": "snip"}) == "snip"
    assert pipe.feed_text_of({}) == ""
    entry = make_entry(0, raw_content="", raw_snippet="s" * 500)
    assert pipe.eligibility(entry, {})[0] is False                                  # el snippet ja es suficient


# ===========================================================================
# 8. Reintents
# ===========================================================================

def temp_failure_row(fetched_at, status="timeout", http_status=None, row_id=1):
    return dict(id=row_id, entry_id=42, requested_url=URL, normalized_url=URL, fetch_status=status,
                http_status=http_status, fetched_at=fetched_at, text_content=None, text_length=None,
                reused_from_fetch_id=None)


@pytest.mark.parametrize("status,http,expected", [
    ("timeout", None, True), ("robots_unreachable", None, True),
    ("http_error", 500, True), ("http_error", 503, True), ("http_error", 599, True), ("http_error", 429, True),
    ("http_error", 404, False), ("http_error", 403, False), ("http_error", 410, False), ("http_error", 499, False),
    ("http_error", None, False), ("http_error", 600, False),
    ("access_denied", 403, False), ("robots_blocked", None, False), ("anti_bot_challenge", 200, False),
    ("pdf_unprocessed", 200, False), ("error", None, False), ("text_too_short", 200, False),
    ("ok", 200, False), ("duplicate", None, False),
])
def test_is_temporary_failure(status, http, expected):
    assert pipe.is_temporary_failure(status, http) is expected


def test_three_attempts_in_different_runs_with_one_hour_between_them(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, routes=route_set(requests.Timeout("lent")))
    # --- intent 1
    assert h.run()["status"] == "skipped"
    assert len(h.db.fetches) == 1 and h.db.entry["processing_status"] == "RAW"
    # --- menys d'1 hora: res de xarxa ni de BD nova, es queda RAW
    h.now = T0 + timedelta(minutes=59)
    http_before = len(h.http_requests)
    result = h.run()
    assert result["status"] == "skipped" and result["detail"]["reason"] == "content_fetch_deferred"
    assert result["detail"]["fetch_reason"] == "retry_too_soon"
    assert len(h.db.fetches) == 1 and len(h.http_requests) == http_before
    # --- exactament 1 hora: ja es pot reintentar (intent 2)
    h.now = T0 + timedelta(hours=1)
    assert h.run()["status"] == "skipped"
    assert len(h.db.fetches) == 2 and len(h.http_requests) > http_before
    # --- menys d'1 hora des de l'intent 2
    h.now = T0 + timedelta(hours=1, minutes=30)
    assert h.run()["detail"]["fetch_reason"] == "retry_too_soon" and len(h.db.fetches) == 2
    # --- intent 3: falla i passa a NO reintentable: continua amb el feed
    h.now = T0 + timedelta(hours=2)
    result = h.run()
    assert len(h.db.fetches) == 3
    assert result["status"] == "enriched" and h.input_text == "x" * 100
    assert h.db.entry["analyzed_fetch_id"] is None and h.db.entry["processing_status"] == "ENRICHED"
    assert all(r["fetch_status"] == "timeout" for r in h.db.fetches)
    assert_universal_invariants(h)


def test_after_the_third_failed_attempt_there_are_no_more_fetches(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    rows = [temp_failure_row(T0 - timedelta(hours=h_), row_id=i) for i, h_ in enumerate((10, 8, 6), 1)]
    h = Harness(monkeypatch, fetches=rows, routes=route_set(ok_page()))
    result = h.run()
    assert result["status"] == "enriched" and h.input_text == "x" * 100
    assert h.http_requests == [] and len(h.db.fetches) == 3


def test_the_third_attempt_that_succeeds_is_used(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    rows = [temp_failure_row(T0 - timedelta(hours=5), row_id=1), temp_failure_row(T0 - timedelta(hours=3), row_id=2)]
    h = Harness(monkeypatch, fetches=rows, routes=route_set(ok_page()))
    assert h.run()["status"] == "enriched"
    assert h.input_text == LONG_TEXT and h.db.entry["analyzed_fetch_id"] == h.db.fetches[-1]["id"]


def test_third_temporary_failure_with_empty_feed_is_a_technical_discard(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    rows = [temp_failure_row(T0 - timedelta(hours=5), row_id=1), temp_failure_row(T0 - timedelta(hours=3), row_id=2)]
    h = Harness(monkeypatch, entry=make_entry(0), fetches=rows, routes=route_set(FakeResponse(503)))
    result = h.run()
    assert result["status"] == "discarded" and h.db.entry["discard_reason"] == "fetch_failed"
    assert h.db.ready_writes == [] and len(h.db.fetches) == 3


def test_previous_permanent_failure_is_not_retried(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    rows = [temp_failure_row(T0 - timedelta(hours=5), status="access_denied", http_status=403)]
    h = Harness(monkeypatch, fetches=rows, routes=route_set(ok_page()))
    assert h.run()["status"] == "enriched" and h.http_requests == [] and h.input_text == "x" * 100


def test_a_previous_ok_row_is_reused_without_new_requests_or_rows(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    ok_row = dict(id=3, entry_id=42, requested_url=URL, normalized_url=URL, fetch_status="ok", http_status=200,
                  fetched_at=T0 - timedelta(hours=1), text_content=LONG_TEXT, text_length=len(LONG_TEXT),
                  reused_from_fetch_id=None)
    h = Harness(monkeypatch, fetches=[ok_row], routes=route_set(ok_page()))
    assert h.run()["status"] == "enriched"
    assert h.input_text == LONG_TEXT and h.http_requests == [] and len(h.db.fetches) == 1
    assert h.db.entry["analyzed_fetch_id"] == 3
    # ja enllaçada: no es torna a escriure l'UPDATE
    h2 = Harness(monkeypatch, entry=make_entry(100, analyzed_fetch_id=3), fetches=[ok_row], routes=route_set(ok_page()))
    h2.run()
    assert not h2.db.sql("SET analyzed_fetch_id")


def test_previous_text_too_short_row_is_used_only_if_longer_than_the_feed(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    row = dict(id=4, entry_id=42, requested_url=URL, normalized_url=URL, fetch_status="text_too_short", http_status=200,
               fetched_at=T0 - timedelta(hours=1), text_content=words(300), text_length=300, reused_from_fetch_id=None)
    h = Harness(monkeypatch, entry=make_entry(100), fetches=[row])
    h.run()
    assert h.input_text == words(300) and h.db.entry["analyzed_fetch_id"] == 4 and h.http_requests == []
    h2 = Harness(monkeypatch, entry=make_entry(399), fetches=[row])
    h2.run()
    assert h2.input_text == "x" * 399 and h2.db.entry["analyzed_fetch_id"] is None


def test_pipeline_retries_after_an_error_do_not_refetch(monkeypatch):
    """processing_retries i els intents d'obtencio son independents: un reintent del pipeline reutilitza el text."""
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, routes=route_set(ok_page()))
    state = {"fail": True}
    original = h.fake_llm

    def flaky(conn, key, prompt_overrides=None):
        if state["fail"] and key == "Input":
            h.db.events.append(("llm", key))
            raise RuntimeError("LLM caigut")
        return original(conn, key, prompt_overrides)

    monkeypatch.setattr(crawler, "call_llm_for_prompt", flaky)
    assert h.run()["status"] == "error"
    first_requests = len(h.http_requests)
    state["fail"] = False
    assert h.run()["status"] == "enriched"
    assert len(h.http_requests) == first_requests and len(h.db.fetches) == 1
    assert h.input_text == LONG_TEXT


# ===========================================================================
# 9. Fail-open
# ===========================================================================

@pytest.mark.parametrize("target", ["insert_fetch", "list_entry_fetches", "find_reusable_fetch", "get_fetch_text"])
def test_fail_open_on_exceptions_inside_the_block(monkeypatch, caplog, target):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    ok_row = dict(id=3, entry_id=42, requested_url=URL, normalized_url=URL, fetch_status="ok", http_status=200,
                  fetched_at=T0 - timedelta(hours=1), text_content=LONG_TEXT, text_length=len(LONG_TEXT),
                  reused_from_fetch_id=None)
    fetches = [ok_row] if target == "get_fetch_text" else []
    h = Harness(monkeypatch, entry=make_entry(100), fetches=fetches, routes=route_set(ok_page()))

    def boom(*args, **kwargs):
        raise RuntimeError(f"{target} ha petat")

    monkeypatch.setattr(store, target, boom)
    with caplog.at_level(logging.WARNING, logger="asimovwatch.content_fetch"):
        result = h.run()
    assert result["status"] == "enriched"                                           # continua amb el feed
    assert h.input_text == "x" * 100 and h.db.entry["analyzed_fetch_id"] is None
    assert h.db.entry["processing_status"] == "ENRICHED"
    assert any(f"{target} ha petat" in r.getMessage() for r in caplog.records)
    assert_universal_invariants(h)
    assert h.db.entry["processing_retries"] == 0                                    # mai incrementa processing_retries


def test_fail_open_with_empty_feed_is_a_technical_discard_never_ready_no(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, entry=make_entry(0), routes=route_set(ok_page()))
    monkeypatch.setattr(store, "insert_fetch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("BD")))
    result = h.run()
    assert result["status"] == "discarded" and h.db.entry["discard_reason"] == "fetch_failed"
    assert h.db.ready_writes == []


def test_fail_open_when_the_integration_module_cannot_be_loaded(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, entry=make_entry(100), routes=route_set(ok_page()))
    import app as app_package
    monkeypatch.delattr(app_package, "content_fetch_pipeline", raising=False)
    monkeypatch.setitem(sys.modules, "app.content_fetch_pipeline", None)            # import -> ImportError
    result = h.run()
    assert result["status"] == "enriched" and h.input_text == "x" * 100
    assert h.http_requests == [] and h.db.fetches == []


def test_fail_open_when_the_integration_module_itself_raises(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, entry=make_entry(100), routes=route_set(ok_page()))
    monkeypatch.setattr(pipe, "prepare_analysis_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    result = h.run()
    assert result["status"] == "enriched" and h.input_text == "x" * 100
    h0 = Harness(monkeypatch, entry=make_entry(0))
    result0 = h0.run()                                                              # feed buit + error del bloc: tecnic, mai 'no'
    assert result0["status"] == "discarded" and h0.db.entry["discard_reason"] == "fetch_failed"
    assert h0.db.ready_writes == []


def test_the_fetcher_never_raises_into_the_pipeline(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, routes=route_set(requests.ConnectionError("xarxa")))
    result = h.run()
    assert result["status"] == "enriched" and h.input_text == "x" * 100               # 'error' no reintentable: feed


# ===========================================================================
# 10. Sense Input / persist=False: cap obtencio
# ===========================================================================

@pytest.mark.parametrize("kwargs", [
    {"skip_input": True},                                    # cerca tematica
    {"persist": False},
    {"run_input": False, "run_primary": True, "run_output": True},   # semifull / primary-only
    {"run_input": False, "run_primary": True, "run_output": False},  # primary-only
])
def test_no_fetch_without_input_or_without_persist(monkeypatch, kwargs):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, routes=route_set(ok_page()))
    monkeypatch.setattr(cf, "ContentFetcher", lambda *a, **k: pytest.fail("no s'ha de crear cap ContentFetcher"))
    h.run(**kwargs)
    assert h.http_requests == [] and h.db.fetches == [] and not h.db.sql("entry_content_fetches")
    assert not h.db.sql("analyzed_fetch_id")


def test_output_only_mode_does_not_fetch(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, routes=route_set(ok_page()))
    seen = []
    monkeypatch.setattr(crawler, "run_output_only", lambda **kw: seen.append(kw) or {"status": "enriched"})
    assert h.run(run_input=False, run_primary=False, run_output=True) == {"status": "enriched"}
    assert seen and h.http_requests == [] and h.db.fetches == []


def test_input_only_mode_does_fetch(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, routes=route_set(ok_page()))
    result = h.run(run_input=True, run_primary=False, run_output=False)
    assert result["status"] == "stopped" and h.input_text == LONG_TEXT


def test_local_fetcher_is_created_when_none_is_passed(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, routes=route_set(ok_page()))
    created = []
    monkeypatch.setattr(cf, "ContentFetcher", lambda *a, **k: created.append(1) or h.fetcher)
    assert h.run(with_fetcher=False)["status"] == "enriched"
    assert created == [1] and h.input_text == LONG_TEXT


# ===========================================================================
# 11. discard_reason (nomes amb l'interruptor encès)
# ===========================================================================

def test_input_no_on_real_text_is_out_of_scope_only_with_the_switch_on(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, entry=make_entry(100), routes=route_set(ok_page()), input_ready="no")
    assert h.run()["status"] == "discarded"
    (sql, params), = h.db.sql("processing_status = 'DISCARDED'")
    assert "discard_reason = 'out_of_scope'" in sql and h.db.entry["discard_reason"] == "out_of_scope"
    assert h.db.ready_writes == ["no"]                                               # decisio d'Input: permesa
    assert sql.replace("\n discard_reason = 'out_of_scope',", "") != ""              # forma estable
    base = ORIGINAL_INPUT_DISCARD_SQL.replace("input_quality_notes = %s,", "input_quality_notes = %s, discard_reason = 'out_of_scope',")
    assert sql == base and len(params) == 7                                          # mateixos 7 parametres


def test_input_no_on_fetched_text_is_also_out_of_scope_and_keeps_the_link(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, entry=make_entry(100), routes=route_set(ok_page()), input_ready="no")
    h.run()
    assert h.db.entry["discard_reason"] == "out_of_scope"
    assert h.db.entry["analyzed_fetch_id"] == h.db.fetches[-1]["id"]


def test_empty_feed_without_attempt_is_no_content_with_the_same_fields_as_today(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1", CONTENT_FETCH_ENTRY_IDS="7")     # aquesta entrada no es obté
    h = Harness(monkeypatch, entry=make_entry(0))
    assert h.run()["status"] == "discarded"
    (sql, params), = h.db.sql("processing_status = 'DISCARDED'")
    assert sql == ORIGINAL_NO_CONTENT_SQL.replace("input_quality_notes = %s,", "input_quality_notes = %s, discard_reason = 'no_content',")
    assert h.db.entry["discard_reason"] == "no_content" and h.db.entry["ready_for_primary"] == "no"
    assert params == ("No hi ha contingut RSS analitzable.", "El feed no ha aportat summary, description ni content.", 42)


def test_fetch_failed_discard_leaves_input_columns_untouched(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, entry=make_entry(0), routes=route_set(FakeResponse(403)))
    h.run()
    (sql, params), = h.db.sql("processing_status = 'DISCARDED'")
    assert sql == ("UPDATE public.entries SET processing_status = 'DISCARDED', processing_error = NULL, "
                   "discard_reason = 'fetch_failed', updated_at = NOW() WHERE id = %s")
    assert params == (42,)
    assert "ready_for_primary" not in sql and "input_relevance" not in sql
    assert h.db.entry.get("ready_for_primary") is None and h.db.entry.get("input_relevance") is None


def test_normal_enrichment_never_writes_discard_reason(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, routes=route_set(ok_page()))
    h.run()
    assert not [s for s, _ in h.db.executed if "discard_reason" in s]


# ===========================================================================
# 12. Cua
# ===========================================================================

class QueueCur:
    def __init__(self, store_):
        self.store = store_

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False

    def execute(self, sql, params=None):
        self.store["sql"], self.store["params"] = " ".join(sql.split()), params

    def fetchall(self):
        return []


class QueueConn:
    def __init__(self, store_):
        self.store = store_

    def cursor(self, cursor_factory=None):
        return QueueCur(self.store)

    def close(self):
        pass


def test_queue_excludes_entries_with_a_recent_temporary_failure_only_when_enabled(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    captured = {}
    monkeypatch.setattr(queue, "get_connection", lambda: QueueConn(captured))
    queue._load_candidate_entries(3)
    sql = captured["sql"]
    assert captured["params"] == (3, 3600) == (3, pipe.RETRY_MIN_INTERVAL_SECONDS)
    assert "NOT EXISTS" in sql and "public.entry_content_fetches f" in sql and "f.entry_id = public.entries.id" in sql
    assert "f.fetched_at > NOW() - (%s * INTERVAL '1 second')" in sql
    assert "f.fetch_status IN ('timeout', 'robots_unreachable')" in sql
    assert "f.fetch_status = 'http_error' AND (f.http_status = 429 OR f.http_status BETWEEN 500 AND 599)" in sql
    assert sql.index("processing_status = 'RAW'") < sql.index("NOT EXISTS") < sql.index("ORDER BY detected_at ASC")
    assert ("WHERE ( processing_status = 'RAW' OR ( processing_status = 'ERROR' AND "
            "COALESCE(processing_retries, 0) < %s ) ) AND NOT EXISTS (") in sql


def test_queue_exclusion_mirrors_the_python_classification():
    """Els valors del SQL (timeout, robots_unreachable, http_error 429/5xx) son els de is_temporary_failure."""
    for status in ("timeout", "robots_unreachable"):
        assert pipe.is_temporary_failure(status, None)
    assert pipe.is_temporary_failure("http_error", 429) and pipe.is_temporary_failure("http_error", 500)
    assert pipe.is_temporary_failure("http_error", 599) and not pipe.is_temporary_failure("http_error", 600)
    assert pipe.RETRY_MIN_INTERVAL == timedelta(hours=1) and pipe.MAX_TEMPORARY_ATTEMPTS == 3


def test_queue_creates_one_fetcher_per_run_and_passes_it_to_every_entry(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    instances = []

    class Spy:
        def __init__(self, *a, **k):
            instances.append(self)

    monkeypatch.setattr(cf, "ContentFetcher", Spy)
    calls = []
    monkeypatch.setattr(queue, "run_entry_enrichment", lambda **kw: calls.append(kw) or {"status": "enriched"})
    queue._run_selected_entries([{"id": 1}, {"id": 2}, {"id": 3}], timeout_seconds=180)
    assert len(instances) == 1
    assert [c["content_fetcher"] for c in calls] == [instances[0]] * 3
    assert [c["entry_id"] for c in calls] == [1, 2, 3]


def test_queue_counts_a_skipped_entry_without_failing_the_run(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    monkeypatch.setattr(cf, "ContentFetcher", lambda *a, **k: object())
    answers = iter([{"status": "skipped", "detail": {"reason": "retry_too_soon"}}, {"status": "enriched"}])
    monkeypatch.setattr(queue, "run_entry_enrichment", lambda **kw: next(answers))
    summary = queue._run_selected_entries([{"id": 1}, {"id": 2}], timeout_seconds=180)["summary"]
    assert summary["skipped"] == 1 and summary["enriched"] == 1 and summary["failed"] == 0 and summary["attempted"] == 2


def test_queue_falls_back_to_the_old_call_if_the_fetcher_cannot_be_created(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    monkeypatch.setattr(cf, "ContentFetcher", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    calls = []
    monkeypatch.setattr(queue, "run_entry_enrichment", lambda **kw: calls.append(kw) or {"status": "enriched"})
    queue._run_selected_entries([{"id": 1}], timeout_seconds=180)
    assert "content_fetcher" not in calls[0]


def test_deferred_entries_do_not_starve_the_cycle_with_the_exclusion(monkeypatch):
    """Model de la selecció: sense l'exclusio, 10 entrades 'skipped' ocupen els 10 llocs; amb ella, no."""
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    deferred = [{"id": i, "source_domain": f"d{i}.example", "detected_at": T0 - timedelta(days=5),
                 "processing_status": "RAW", "processing_retries": 0, "fetched": T0 - timedelta(minutes=10)}
                for i in range(1, 11)]
    fresh = [{"id": 100 + i, "source_domain": f"f{i}.example", "detected_at": T0 - timedelta(days=1),
              "processing_status": "RAW", "processing_retries": 0, "fetched": None} for i in range(5)]
    everything = deferred + fresh

    def loaded(with_exclusion):
        rows = [r for r in everything if not (with_exclusion and r["fetched"] and T0 - r["fetched"] < pipe.RETRY_MIN_INTERVAL)]
        return sorted(rows, key=lambda r: (r["detected_at"], r["id"]))

    monkeypatch.setattr(queue, "_load_candidate_entries", lambda retry_max: [dict(r) for r in loaded(True)])
    selected = queue.select_enrichment_candidates(max_per_run=10, max_per_source=2, retry_max=3)["selected_ids"]
    assert set(selected) == {100, 101, 102, 103, 104}
    monkeypatch.setattr(queue, "_load_candidate_entries", lambda retry_max: [dict(r) for r in loaded(False)])
    starved = queue.select_enrichment_candidates(max_per_run=10, max_per_source=2, retry_max=3)["selected_ids"]
    assert starved[:10] == list(range(1, 11))                                         # sense exclusio: tot per als 'skipped'


# ===========================================================================
# 13. Batch: 'skipped' no es una fallada
# ===========================================================================

def test_batch_counts_skipped_results_as_skipped(monkeypatch):
    updates = []
    monkeypatch.setattr(main, "get_connection", lambda: _Conn(FakeDB(make_entry())))
    monkeypatch.setattr(main, "get_entry_phase_state", lambda cur, entry_id: {"id": entry_id})
    monkeypatch.setattr(main, "update_batch_job", lambda batch_id, **kw: updates.append(kw))
    monkeypatch.setattr(main, "run_entry_enrichment",
                        lambda **kw: {"status": "skipped", "detail": {"reason": "retry_too_soon"}})
    options = {"timeout_per_entry_ms": 120000, "skip_existing": False, "persist": True, "on_error": "continue"}
    main.process_batch_job("b1", [42], "full", options)
    last = [u for u in updates if "finished_at" in u][-1]
    assert last["skipped"] == 1 and last["failed"] == 0 and last["succeeded"] == 0 and last["status"] == "COMPLETED"
    assert last["items"][0] == {**last["items"][0], "status": "skipped", "reason": "retry_too_soon"}


# ===========================================================================
# 14. Casos afegits per la prova de mutacio
# ===========================================================================

def test_prepare_analysis_text_is_fail_open_by_itself(monkeypatch):
    """El modul d'integracio es fail-open per ell mateix (no nomes el wrapper del crawler)."""
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, entry=make_entry(100), routes=route_set(ok_page()))
    monkeypatch.setattr(store, "list_entry_fetches", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("BD caiguda")))
    conn = h.db.connection()
    decision = pipe.prepare_analysis_text(conn, h.db.entry, h.fetcher, environ={})
    assert decision.kind == "fallback" and decision.failed and decision.reason == "error"
    assert decision.detail == {"error": "RuntimeError"} and h.db.rollbacks >= 1


def test_prepare_analysis_text_returns_none_kind_for_ineligible_entries_without_touching_the_db():
    db = FakeDB(make_entry(100, ingest_method="web_search"))
    decision = pipe.prepare_analysis_text(db.connection(), db.entry, None, environ={})
    assert decision.kind == "none" and decision.reason == "not_rss" and db.executed == [] and db.rollbacks == 0


def short_ok_row(row_id=3, entry_id=7, n=300):
    return dict(id=row_id, entry_id=entry_id, requested_url=URL, normalized_url=URL, fetch_status="ok", http_status=200,
                fetched_at=T0 - timedelta(hours=1), text_content=words(n), text_length=n, reused_from_fetch_id=None)


def test_a_duplicate_that_resolves_to_a_text_below_the_threshold_is_not_used(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    h = Harness(monkeypatch, entry=make_entry(100), fetches=[short_ok_row()], routes=route_set())
    result = h.run()
    assert result["status"] == "enriched" and h.input_text == "x" * 100          # continua amb el feed
    assert h.db.entry["analyzed_fetch_id"] is None
    new_row = [r for r in h.db.fetches if r["entry_id"] == 42][0]
    assert new_row["fetch_status"] == "duplicate" and h.http_requests == []


def test_a_previous_ok_row_below_the_threshold_is_not_reused(monkeypatch):
    set_env(monkeypatch, CONTENT_FETCH_ENABLED="1")
    row = short_ok_row(row_id=4, entry_id=42, n=300)
    h = Harness(monkeypatch, entry=make_entry(100), fetches=[row], routes=route_set(ok_page()))
    result = h.run()
    assert result["status"] == "enriched" and h.input_text == "x" * 100 and h.db.entry["analyzed_fetch_id"] is None
    assert h.http_requests == []                                                    # l'intent previ no es reintenta
