"""
Preparatius per a la integracio de l'obtencio de contingut. Sense BD ni xarxa: connexions i LLM simulats.

A. DELETE /entries/{id}: 409 davant d'una violacio de FK.
B. Parametre text= als constructors de text: identitat amb text=None.
C. Interruptor content_fetch_enabled().
E. Desament de relevance_score / relevance_reason de Primary.
"""

import itertools
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import psycopg2.errors
import pytest
from fastapi import HTTPException

from tests.pipeline_imports import import_pipeline

crawler, main = import_pipeline()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")


# ---------------------------------------------------------------------------
# A. DELETE /entries/{id}
# ---------------------------------------------------------------------------

class DeleteConn:
    def __init__(self, found=True, delete_error=None):
        self.found, self.delete_error = found, delete_error
        self.executed, self.commits, self.rollbacks, self.closed = [], 0, 0, False

    def cursor(self, cursor_factory=None):
        return DeleteCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class DeleteCursor:
    def __init__(self, conn):
        self.conn, self.row, self.closed = conn, None, False

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(sql.split()), params))
        if sql.lstrip().upper().startswith("DELETE"):
            if self.conn.delete_error is not None:
                raise self.conn.delete_error
            self.row = {"id": params[0], "source_title": "T"}
        else:
            self.row = {"id": params[0], "source_title": "T"} if self.conn.found else None

    def fetchone(self):
        return self.row

    def close(self):
        self.closed = True


def delete_with(monkeypatch, conn, entry_id=7):
    monkeypatch.setattr(main, "get_connection", lambda: conn)
    return main.delete_entry(entry_id)


def test_delete_entry_success_is_unchanged(monkeypatch):
    conn = DeleteConn()
    assert delete_with(monkeypatch, conn) == {"status": "deleted", "item": {"id": 7, "source_title": "T"}}
    assert conn.commits == 1 and conn.rollbacks == 0 and conn.closed


def test_delete_entry_not_found_is_still_404_without_rollback(monkeypatch):
    conn = DeleteConn(found=False)
    with pytest.raises(HTTPException) as excinfo:
        delete_with(monkeypatch, conn)
    assert excinfo.value.status_code == 404 and excinfo.value.detail == "Entry not found"
    assert conn.rollbacks == 0 and conn.commits == 0 and conn.closed


def test_delete_entry_foreign_key_violation_is_409_with_rollback(monkeypatch):
    error = psycopg2.errors.ForeignKeyViolation(
        'update or delete on table "entries" violates foreign key constraint '
        '"entry_content_fetches_entry_id_fkey" on table "entry_content_fetches"')
    conn = DeleteConn(delete_error=error)
    with pytest.raises(HTTPException) as excinfo:
        delete_with(monkeypatch, conn)
    assert excinfo.value.status_code == 409
    assert "No es pot esborrar l'entrada" in excinfo.value.detail
    assert "intents d'obtenció de contingut" in excinfo.value.detail
    assert conn.rollbacks == 1 and conn.commits == 0 and conn.closed


def test_delete_entry_foreign_key_violation_does_not_delete_anything_else(monkeypatch):
    conn = DeleteConn(delete_error=psycopg2.errors.ForeignKeyViolation("fk"))
    with pytest.raises(HTTPException):
        delete_with(monkeypatch, conn)
    statements = [sql for sql, _ in conn.executed]
    assert statements == ["SELECT id, source_title FROM public.entries WHERE id = %s",
                          "DELETE FROM public.entries WHERE id = %s RETURNING id, source_title"]
    assert not any("entry_content_fetches" in s or "analyzed_fetch_id" in s for s in statements)


@pytest.mark.parametrize("error", [
    RuntimeError("boom"),
    psycopg2.errors.UniqueViolation("dup"),
    psycopg2.errors.NotNullViolation("nn"),
    psycopg2.errors.CheckViolation("ck"),
    psycopg2.OperationalError("down"),
])
def test_delete_entry_any_other_exception_is_still_500(monkeypatch, error):
    conn = DeleteConn(delete_error=error)
    with pytest.raises(HTTPException) as excinfo:
        delete_with(monkeypatch, conn)
    assert excinfo.value.status_code == 500
    assert excinfo.value.detail == f"Failed to delete entry: {error}"
    assert conn.rollbacks == 1 and conn.closed


# ---------------------------------------------------------------------------
# B. text= als constructors: identitat byte a byte amb text=None
# ---------------------------------------------------------------------------

def ref_input_overrides(entry):
    """Copia CONGELADA de _build_input_overrides d'abans del parametre text=."""
    content = entry.get("raw_content") or entry.get("raw_snippet") or ""
    return {
        "{source_url}": entry.get("source_url") or "",
        "{source_domain}": entry.get("source_domain") or "",
        "{source_title}": entry.get("source_title") or "",
        "{source_type}": entry.get("source_type") or "desconegut",
        "{country_region}": entry.get("country_region") or "desconegut",
        "{institution_type}": entry.get("institution_type") or "desconegut",
        "{published_date}": str(entry.get("published_date") or "desconeguda"),
        "{input_text}": content,
    }


def ref_primary_text(entry, input_result):
    """Copia CONGELADA de _build_primary_input_text d'abans del parametre text=."""
    if input_result is not None:
        clean_text = (
            input_result.get("clean_input_text")
            or entry.get("raw_content")
            or entry.get("raw_snippet")
            or ""
        )
        summary = input_result.get("input_summary") or ""
        source_note = (
            "Origen: ingesta RSS/Atom, processada prèviament per la fase Input."
        )
    else:
        raw_payload = entry.get("raw_payload") or {}
        search_result = raw_payload.get("search_result") or {}

        clean_text = (
            search_result.get("why_relevant")
            or entry.get("raw_content")
            or entry.get("raw_snippet")
            or ""
        )

        search_brief = raw_payload.get("search_brief") or ""

        if entry.get("ingest_method") == "web_search":
            summary = (
                f"Cerca temàtica motivada pel brief: {search_brief}"
                if search_brief
                else ""
            )
            source_note = (
                "Origen: cerca temàtica (web_search), sense pas previ per la fase Input."
            )
        else:
            summary = ""
            source_note = "Origen: entrada processada sense executar la fase Input."

    raw_source_language = (entry.get('source_language') or '').strip().lower()
    if raw_source_language not in ('ca', 'en'):
        primary_language = 'en'
    else:
        primary_language = raw_source_language

    return f"""Metadades de la peça:
source_url: {entry.get('source_url', '')}
source_domain: {entry.get('source_domain', '')}
source_title: {entry.get('source_title', '')}
source_type: {entry.get('source_type') or 'desconegut'}
country_region: {entry.get('country_region') or 'desconegut'}
institution_type: {entry.get('institution_type') or 'desconegut'}
published_date: {entry.get('published_date') or 'desconeguda'}
source_language: {raw_source_language or 'desconegut'}
primary_language: {primary_language}

{source_note}

Resum previ (si existeix):
{summary}

Contingut per analitzar:
{clean_text}"""


RAW_CONTENTS = [None, "", "   ", "Text del feed.\nAmb salts i accents: àéïòü ·", "x" * 5000]
RAW_SNIPPETS = [None, "", "Snippet del feed."]
LANGUAGES = [None, "ca", "en", "es", " CA "]
METHODS = [None, "rss", "web_search"]
PAYLOADS = [None, {}, {"search_result": {"why_relevant": "Per que es rellevant"}, "search_brief": "brief X"},
            {"search_brief": "només brief"}]


def entry_cases():
    for content, snippet, language, method, payload in itertools.product(
            RAW_CONTENTS, RAW_SNIPPETS, LANGUAGES, METHODS, PAYLOADS):
        yield {
            "raw_content": content, "raw_snippet": snippet, "source_language": language,
            "ingest_method": method, "raw_payload": payload,
            "source_url": "https://exemple.cat/feed", "source_domain": "exemple.cat", "source_title": "Títol",
            "source_type": None, "country_region": "UE", "institution_type": None,
            "published_date": datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        }


INPUT_RESULTS = [None, {}, {"clean_input_text": "Text net d'Input.", "input_summary": "Resum d'Input."},
                 {"clean_input_text": "", "input_summary": None}, {"input_summary": "Només resum"}]


def test_input_overrides_are_identical_with_text_none():
    count = 0
    for entry in entry_cases():
        expected = ref_input_overrides(entry)
        assert crawler._build_input_overrides(entry) == expected
        assert crawler._build_input_overrides(entry, text=None) == expected
        assert crawler._build_input_overrides(entry, None) == expected
        count += 1
    assert count == len(RAW_CONTENTS) * len(RAW_SNIPPETS) * len(LANGUAGES) * len(METHODS) * len(PAYLOADS)


def test_primary_text_is_byte_identical_with_text_none():
    for entry in entry_cases():
        for input_result in INPUT_RESULTS:
            expected = ref_primary_text(entry, input_result)
            assert crawler._build_primary_input_text(entry, input_result) == expected
            assert crawler._build_primary_input_text(entry, input_result, text=None) == expected
            assert crawler._build_primary_input_text(entry, input_result, None) == expected


def test_characterization_with_literal_expected_values():
    entry = {"raw_content": "Cos del feed", "raw_snippet": "Snippet", "source_url": "https://a.cat/x",
             "source_domain": "a.cat", "source_title": "Títol", "source_language": "ca",
             "published_date": "2026-10-01"}
    overrides = crawler._build_input_overrides(entry)
    assert overrides == {
        "{source_url}": "https://a.cat/x", "{source_domain}": "a.cat", "{source_title}": "Títol",
        "{source_type}": "desconegut", "{country_region}": "desconegut", "{institution_type}": "desconegut",
        "{published_date}": "2026-10-01", "{input_text}": "Cos del feed",
    }
    assert crawler._build_input_overrides({"raw_snippet": "Només snippet"})["{input_text}"] == "Només snippet"
    assert crawler._build_input_overrides({"raw_content": "", "raw_snippet": "Snippet"})["{input_text}"] == "Snippet"
    assert crawler._build_input_overrides({})["{input_text}"] == ""
    assert crawler._build_input_overrides({})["{published_date}"] == "desconeguda"

    text = crawler._build_primary_input_text(entry, {"clean_input_text": "NET", "input_summary": "RES"})
    assert text == (
        "Metadades de la peça:\n"
        "source_url: https://a.cat/x\nsource_domain: a.cat\nsource_title: Títol\nsource_type: desconegut\n"
        "country_region: desconegut\ninstitution_type: desconegut\npublished_date: 2026-10-01\n"
        "source_language: ca\nprimary_language: ca\n\n"
        "Origen: ingesta RSS/Atom, processada prèviament per la fase Input.\n\n"
        "Resum previ (si existeix):\nRES\n\nContingut per analitzar:\nNET"
    )
    assert crawler._build_primary_input_text({"raw_snippet": "Snip"}, {}).endswith("Contingut per analitzar:\nSnip")
    assert crawler._build_primary_input_text({}, {}).endswith("Contingut per analitzar:\n")


# --- amb text= : s'usa en lloc de raw_content / raw_snippet -----------------

def test_input_overrides_use_text_instead_of_raw_fields():
    entry = {"raw_content": "feed", "raw_snippet": "snippet", "source_url": "u"}
    overrides = crawler._build_input_overrides(entry, text="TEXT OBTINGUT")
    assert overrides["{input_text}"] == "TEXT OBTINGUT"
    assert {k: v for k, v in overrides.items() if k != "{input_text}"} == \
           {k: v for k, v in ref_input_overrides(entry).items() if k != "{input_text}"}


def test_empty_string_text_is_used_as_is_not_replaced_by_the_feed_text():
    entry = {"raw_content": "feed", "raw_snippet": "snippet"}
    assert crawler._build_input_overrides(entry, text="")["{input_text}"] == ""
    assert crawler._build_primary_input_text(entry, {"clean_input_text": ""}, text="").endswith("analitzar:\n")


def test_primary_text_with_text_replaces_only_the_raw_fallback():
    entry = {"raw_content": "feed", "raw_snippet": "snippet"}
    # Input va donar un text net: guanya (com abans)
    with_clean = crawler._build_primary_input_text(entry, {"clean_input_text": "NET"}, text="OBTINGUT")
    assert with_clean.endswith("Contingut per analitzar:\nNET")
    # sense text net: s'usa `text` i no el feed
    without_clean = crawler._build_primary_input_text(entry, {"clean_input_text": ""}, text="OBTINGUT")
    assert without_clean.endswith("Contingut per analitzar:\nOBTINGUT") and "feed" not in without_clean
    # sense Input (cerca tematica): why_relevant guanya; si no, `text`
    web = {"raw_content": "feed", "ingest_method": "web_search",
           "raw_payload": {"search_result": {"why_relevant": "WR"}, "search_brief": "B"}}
    assert crawler._build_primary_input_text(web, None, text="OBTINGUT").endswith("analitzar:\nWR")
    no_wr = {"raw_content": "feed", "ingest_method": "web_search", "raw_payload": {"search_brief": "B"}}
    assert crawler._build_primary_input_text(no_wr, None, text="OBTINGUT").endswith("analitzar:\nOBTINGUT")
    # la resta de la capcalera no canvia
    reference = ref_primary_text(entry, {"clean_input_text": ""})
    assert without_clean.replace("OBTINGUT", "feed") == reference


# ---------------------------------------------------------------------------
# C. Interruptor
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("yes", True),
    ("0", False), ("false", False), ("no", False), ("", False), ("TRUE", False), ("True", False),
    ("YES", False), ("Yes", False), ("on", False), ("2", False), (" 1", False), ("1 ", False), ("enabled", False),
])
def test_content_fetch_enabled_values(monkeypatch, value, expected):
    monkeypatch.setenv("CONTENT_FETCH_ENABLED", value)
    assert crawler.content_fetch_enabled() is expected


def test_content_fetch_enabled_defaults_to_false_and_is_read_on_every_call(monkeypatch):
    monkeypatch.delenv("CONTENT_FETCH_ENABLED", raising=False)
    assert crawler.content_fetch_enabled() is False
    monkeypatch.setenv("CONTENT_FETCH_ENABLED", "1")
    assert crawler.content_fetch_enabled() is True
    monkeypatch.setenv("CONTENT_FETCH_ENABLED", "false")
    assert crawler.content_fetch_enabled() is False
    monkeypatch.delenv("CONTENT_FETCH_ENABLED")
    assert crawler.content_fetch_enabled() is False


@pytest.mark.parametrize("env_value", [None, "0", "false", "1"])
def test_importing_crawler_never_imports_the_fetch_modules(env_value):
    """Fins i tot amb l'interruptor encès: el modul no s'importa en importar crawler (cap crida nova)."""
    env = {k: v for k, v in os.environ.items() if k != "CONTENT_FETCH_ENABLED"}
    env["PYTHONPATH"] = ROOT
    if env_value is not None:
        env["CONTENT_FETCH_ENABLED"] = env_value
    code = (
        "import dotenv, sys\n"
        "dotenv.load_dotenv = lambda *a, **k: False\n"
        "import app.crawler\n"
        "bad = [m for m in ('app.content_fetch', 'app.content_fetch_store') if m in sys.modules]\n"
        "print('LOADED', bad)\n"
    )
    result = subprocess.run([sys.executable, "-W", "ignore", "-c", code], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("LOADED []")


def test_content_fetch_enabled_is_not_called_anywhere_yet():
    definitions, calls = 0, []
    for name in sorted(os.listdir(APP_DIR)):
        if not name.endswith(".py"):
            continue
        for number, line in enumerate(open(os.path.join(APP_DIR, name), encoding="utf-8"), 1):
            if "content_fetch_enabled" in line:
                if re.match(r"\s*def content_fetch_enabled\(", line):
                    definitions += 1
                else:
                    calls.append((name, number, line.strip()))
    assert definitions == 1 and calls == []


@pytest.mark.parametrize("name", ["crawler.py", "main.py", "ingest_ops.py", "enrichment_queue.py"])
def test_pipeline_modules_have_no_import_of_the_fetch_modules(name):
    source = open(os.path.join(APP_DIR, name), encoding="utf-8").read()
    assert not re.search(r"^\s*(?:from\s+app(?:\.content_fetch\w*)?\s+import\s+.*content_fetch|"
                         r"from\s+app\.content_fetch\w*\s+import|import\s+app\.content_fetch)", source, re.M)


# ---------------------------------------------------------------------------
# E. relevance_score / relevance_reason
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("high", "high"), ("medium", "medium"), ("low", "low"), ("unknown", "unknown"),
    ("HIGH", "high"), ("  Medium  ", "medium"), ("\tLow\n", "low"), ("UnKnOwN", "unknown"),
    ("critical", None), ("", None), ("   ", None), ("high|medium", None), ("highest", None), ("hi gh", None),
    (None, None), (5, None), (1.5, None), (True, None), (["high"], None), ({"v": "high"}, None), (b"high", None),
])
def test_normalize_relevance_score(value, expected):
    assert crawler._normalize_relevance_score(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("Perque toca la supervisio humana", "Perque toca la supervisio humana"),
    ("  amb espais  ", "amb espais"),
    ("amb\x00nul", "ambnul"),
    ("línia 1\nlínia 2 àéï", "línia 1\nlínia 2 àéï"),
    ("", None), ("   ", None), ("\x00", None), (" \n\t ", None),
    (None, None), (5, None), (["x"], None), ({"r": "x"}, None), (b"bytes", None), (True, None),
])
def test_normalize_relevance_reason(value, expected):
    assert crawler._normalize_relevance_reason(value) == expected


def levels(caplog, key):
    return [(r.levelno, r.getMessage()) for r in caplog.records if key in r.getMessage()]


def test_relevance_for_persist_valid_values_log_nothing(caplog):
    with caplog.at_level(logging.DEBUG, logger="asimovwatch.entries_crawler"):
        assert crawler._relevance_for_persist({"relevance_score": " HIGH ", "relevance_reason": "motiu"}, 3) == ("high", "motiu")
    assert caplog.records == []


@pytest.mark.parametrize("result", [{}, {"relevance_score": None, "relevance_reason": None}])
def test_relevance_for_persist_absent_or_none_is_debug_not_warning(caplog, result):
    with caplog.at_level(logging.DEBUG, logger="asimovwatch.entries_crawler"):
        assert crawler._relevance_for_persist(result, 4) == (None, None)
    assert {r.levelno for r in caplog.records} == {logging.DEBUG}
    assert len(caplog.records) == 2 and all("Entry 4" in r.getMessage() for r in caplog.records)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("score,reason", [
    (7, ["x"]), ("critical", "  "), ({"v": "high"}, ""), (True, 12), ("", b"bytes"), ("n/a", "\x00"),
])
def test_relevance_for_persist_present_but_invalid_is_a_warning(caplog, score, reason):
    with caplog.at_level(logging.DEBUG, logger="asimovwatch.entries_crawler"):
        assert crawler._relevance_for_persist({"relevance_score": score, "relevance_reason": reason}, 5) == (None, None)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2 and all("Entry 5" in r.getMessage() for r in warnings)
    assert any("relevance_score" in r.getMessage() for r in warnings)
    assert any("relevance_reason" in r.getMessage() for r in warnings)
    assert not [r for r in caplog.records if r.levelno == logging.DEBUG]


def test_relevance_for_persist_mixed_valid_and_absent(caplog):
    with caplog.at_level(logging.DEBUG, logger="asimovwatch.entries_crawler"):
        assert crawler._relevance_for_persist({"relevance_score": "low"}, 6) == ("low", None)
        assert crawler._relevance_for_persist({"relevance_score": "critical", "relevance_reason": "ok"}, 7) == (None, "ok")
    assert levels(caplog, "relevance_reason") == [(logging.DEBUG, "Entry 6: relevance_reason absent o None; es conserva el valor existent.")]
    assert [lvl for lvl, _ in levels(caplog, "Entry 7")] == [logging.WARNING]
    assert "critical" in levels(caplog, "Entry 7")[0][1]


def test_relevance_for_persist_never_raises():
    class Exploding(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("get explota")

    assert crawler._relevance_for_persist(Exploding(), 1) == (None, None)
    assert crawler._relevance_for_persist(None, 1) == (None, None)


# --- Pipeline complet amb connexio i LLM simulats ---------------------------

def param_names(sql):
    """Noms de columna dels parametres %s d'un UPDATE, en ordre (accepta COALESCE(%s, col))."""
    return re.findall(r"(\w+) = (?:COALESCE\()?%s", sql)


class PipeCursor:
    def __init__(self, conn):
        self.conn, self.row = conn, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.conn.executed.append((flat, params))
        self.row = self.conn.entry if sql.lstrip().upper().startswith("SELECT * FROM PUBLIC.ENTRIES") else None
        if flat.startswith("UPDATE public.entries") and params:
            # Emula nomes les dues columnes de rellevancia sobre la fila en memoria:
            # COALESCE(%s, col) conserva el valor existent si el parametre es None.
            names = param_names(flat)
            for name, value in zip(names, params):
                if name in ("relevance_score", "relevance_reason"):
                    if f"{name} = COALESCE(%s, {name})" in flat:
                        if value is not None:
                            self.conn.entry[name] = value
                    else:
                        self.conn.entry[name] = value

    def fetchone(self):
        return self.row


class PipeConn:
    def __init__(self, entry):
        self.entry, self.executed, self.commits, self.rollbacks, self.closed = entry, [], 0, 0, False

    def cursor(self, cursor_factory=None):
        return PipeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


FEED_TEXT = "Text del feed " * 40


def make_entry(**overrides):
    entry = {
        "id": 42, "raw_content": FEED_TEXT, "raw_snippet": FEED_TEXT, "raw_snippet_original": None,
        "source_url": "https://exemple.cat/feed", "source_domain": "exemple.cat", "source_title": "Titol",
        "source_type": "company", "country_region": "UE", "institution_type": None, "published_date": None,
        "source_language": "en", "ingest_method": "rss", "raw_payload": {},
    }
    entry.update(overrides)
    return entry


INPUT_JSON = {"input_relevance": "high", "input_relevance_reason": "r", "ready_for_primary": "yes",
              "clean_input_text": "TEXT NET", "input_summary": "resum", "input_quality": "high",
              "input_quality_notes": "", "source_language": "en"}
PRIMARY_JSON = {"summary_factual": "S", "why_it_matters": "W", "theme_tags": ["t"], "affected_principles": ["p"],
                "risk_level": "low", "debate_questions": ["q"], "confidence_notes": "",
                "human_protection_declared": "unknown", "human_protection_verifiable": "unknown",
                "human_protection_depth": "unknown", "human_protection_notes": "n",
                "entry_category": "other", "analyzed_provider": None, "analyzed_model": None,
                "bihp_directives": []}
OUTPUT_JSON = {"summary_factual_ca": "S", "summary_factual_en": "S", "why_it_matters_ca": "W",
               "why_it_matters_en": "W", "debate_questions_ca": ["q"], "debate_questions_en": ["q"],
               "human_protection_notes_ca": "n", "human_protection_notes_en": "n"}


def run_pipeline(monkeypatch, primary_extra=None, run_output=True, entry=None, conn=None):
    conn = conn or PipeConn(entry or make_entry())
    seen = {}

    def fake_llm(c, key, prompt_overrides=None):
        seen[key] = prompt_overrides
        payload = {"Input": INPUT_JSON, "Primary": {**PRIMARY_JSON, **(primary_extra or {})}, "Output": OUTPUT_JSON}[key]
        return {"output": json.dumps(payload), "provider_used": "fake", "model_used": f"fake-{key}", "used_fallback": False}

    monkeypatch.setattr(crawler, "get_connection", lambda: conn)
    monkeypatch.setattr(crawler, "call_llm_for_prompt", fake_llm)
    result = crawler.run_entry_enrichment(entry_id=42, run_output=run_output)
    return result, conn, seen


def update_params(conn, marker):
    """{columna: valor} de l'UPDATE que conte `marker`."""
    for sql, params in conn.executed:
        if sql.startswith("UPDATE public.entries") and marker in sql:
            names = param_names(sql)
            assert len(names) == len(params)
            return dict(zip(names, params))
    raise AssertionError(f"cap UPDATE amb {marker!r}")


def final_update(conn):
    return update_params(conn, "summary_factual_ca")


def test_pipeline_defaults_run_the_same_flow_and_touch_no_new_table(monkeypatch):
    monkeypatch.delenv("CONTENT_FETCH_ENABLED", raising=False)
    result, conn, seen = run_pipeline(monkeypatch)
    assert result["status"] == "enriched"
    kinds = [re.match(r"(SELECT|UPDATE)", sql).group(1) for sql, _ in conn.executed]
    assert kinds == ["SELECT", "UPDATE", "UPDATE"]                 # carrega, Input, resultat final
    assert not any("entry_content_fetches" in sql or "analyzed_fetch_id" in sql or "discard_reason" in sql
                   for sql, _ in conn.executed)
    assert seen["Input"]["{input_text}"] == FEED_TEXT              # el text d'Input segueix sent el del feed
    assert seen["Primary"]["{input_text}"].endswith("Contingut per analitzar:\nTEXT NET")
    assert conn.rollbacks == 0 and conn.closed


EXISTING = {"relevance_score": "medium", "relevance_reason": "motiu anterior"}


def test_both_updates_use_coalesce_for_the_relevance_columns(monkeypatch):
    _, conn, _ = run_pipeline(monkeypatch, {"relevance_score": "low", "relevance_reason": "x"})
    updates = [sql for sql, _ in conn.executed if "processing_status = 'ENRICHED'" in sql]
    assert len(updates) == 1                                       # camí complet: un sol UPDATE final
    assert "relevance_score = COALESCE(%s, relevance_score)" in updates[0]
    assert "relevance_reason = COALESCE(%s, relevance_reason)" in updates[0]
    _, conn, _ = run_pipeline(monkeypatch, {"relevance_score": "low"}, run_output=False)
    interim = [sql for sql, _ in conn.executed if "processing_status = 'ENRICHED'" in sql]
    assert len(interim) == 1
    assert "relevance_score = COALESCE(%s, relevance_score)" in interim[0]
    assert "relevance_reason = COALESCE(%s, relevance_reason)" in interim[0]


def test_valid_relevance_overwrites_the_existing_value_normalized(monkeypatch):
    entry = make_entry(**EXISTING)
    result, conn, _ = run_pipeline(monkeypatch, {"relevance_score": " High ", "relevance_reason": "  Toca la supervisio.  "}, entry=entry)
    row = final_update(conn)
    assert result["status"] == "enriched"
    assert row["relevance_score"] == "high" and row["relevance_reason"] == "Toca la supervisio."
    assert (entry["relevance_score"], entry["relevance_reason"]) == ("high", "Toca la supervisio.")   # sobreescrit


@pytest.mark.parametrize("extra", [{}, {"relevance_score": None, "relevance_reason": None}])
def test_absent_or_none_relevance_keeps_the_existing_value_without_warning(monkeypatch, caplog, extra):
    entry = make_entry(**EXISTING)
    with caplog.at_level(logging.DEBUG, logger="asimovwatch.entries_crawler"):
        result, conn, _ = run_pipeline(monkeypatch, extra, entry=entry)
    row = final_update(conn)
    assert result["status"] == "enriched"
    assert row["relevance_score"] is None and row["relevance_reason"] is None          # parametres: COALESCE conserva
    assert (entry["relevance_score"], entry["relevance_reason"]) == ("medium", "motiu anterior")
    mine = [r for r in caplog.records if "relevance_" in r.getMessage()]
    assert mine and all(r.levelno == logging.DEBUG for r in mine)
    assert conn.rollbacks == 0 and not any("'ERROR'" in sql for sql, _ in conn.executed)


@pytest.mark.parametrize("extra", [
    {"relevance_score": 5, "relevance_reason": ["lista"]},
    {"relevance_score": {"v": "high"}, "relevance_reason": 12},
    {"relevance_score": "critical", "relevance_reason": "   "},
])
def test_invalid_relevance_keeps_the_existing_value_and_warns(monkeypatch, caplog, extra):
    entry = make_entry(**EXISTING)
    with caplog.at_level(logging.DEBUG, logger="asimovwatch.entries_crawler"):
        result, conn, _ = run_pipeline(monkeypatch, extra, entry=entry)
    assert result["status"] == "enriched"
    assert (entry["relevance_score"], entry["relevance_reason"]) == ("medium", "motiu anterior")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "relevance_" in r.getMessage()]
    assert len(warnings) == 2
    assert not any("'ERROR'" in sql for sql, _ in conn.executed)


def test_out_of_list_score_keeps_the_old_score_but_a_valid_reason_overwrites(monkeypatch):
    entry = make_entry(**EXISTING)
    result, conn, _ = run_pipeline(monkeypatch, {"relevance_score": "critical", "relevance_reason": "motiu nou"}, entry=entry)
    assert result["status"] == "enriched"
    assert (entry["relevance_score"], entry["relevance_reason"]) == ("medium", "motiu nou")


def test_entry_without_previous_value_and_without_relevance_stays_null(monkeypatch):
    entry = make_entry()
    run_pipeline(monkeypatch, entry=entry)
    assert entry.get("relevance_score") is None and entry.get("relevance_reason") is None


def test_nul_in_reason_cannot_break_the_update(monkeypatch):
    entry = make_entry(**EXISTING)
    _, conn, _ = run_pipeline(monkeypatch, {"relevance_score": "low", "relevance_reason": "a\x00b"}, entry=entry)
    assert final_update(conn)["relevance_reason"] == "ab" and entry["relevance_reason"] == "ab"


def test_relevance_is_also_persisted_when_the_pipeline_stops_after_primary(monkeypatch):
    entry = make_entry(**EXISTING)
    result, conn, _ = run_pipeline(monkeypatch, {"relevance_score": "LOW", "relevance_reason": "motiu"}, run_output=False, entry=entry)
    assert result["status"] == "stopped"
    interim = [(sql, p) for sql, p in conn.executed if sql.startswith("UPDATE public.entries")
               and "summary_factual_ca" not in sql and "processing_status = 'ENRICHED'" in sql]
    assert len(interim) == 1
    row = dict(zip(param_names(interim[0][0]), interim[0][1]))
    assert row["relevance_score"] == "low" and row["relevance_reason"] == "motiu"
    assert (entry["relevance_score"], entry["relevance_reason"]) == ("low", "motiu")


def test_stopped_pipeline_with_invalid_relevance_keeps_existing_values(monkeypatch):
    entry = make_entry(**EXISTING)
    run_pipeline(monkeypatch, {"relevance_score": "nope"}, run_output=False, entry=entry)
    assert (entry["relevance_score"], entry["relevance_reason"]) == ("medium", "motiu anterior")


def test_all_previous_columns_are_still_written_by_the_final_update(monkeypatch):
    _, conn, _ = run_pipeline(monkeypatch, {"relevance_score": "low", "relevance_reason": "x"})
    row = final_update(conn)
    for column in ("summary_factual", "why_it_matters", "theme_tags", "affected_principles", "risk_level",
                   "debate_questions", "confidence_notes", "human_protection_declared",
                   "human_protection_verifiable", "human_protection_depth", "human_protection_notes",
                   "entry_category", "analyzed_provider", "analyzed_model", "bihp_directives",
                   "summary_factual_ca", "summary_factual_en", "why_it_matters_ca", "why_it_matters_en",
                   "debate_questions_ca", "debate_questions_en", "human_protection_notes_ca",
                   "human_protection_notes_en", "enriched_model", "id"):
        assert column in row, column
    assert row["id"] == 42 and row["risk_level"] == "low"


def test_persist_false_writes_nothing_and_does_not_prepare_relevance(monkeypatch):
    conn = PipeConn(make_entry())
    monkeypatch.setattr(crawler, "get_connection", lambda: conn)

    def fake_llm(c, key, prompt_overrides=None):
        payload = {"Input": INPUT_JSON, "Primary": PRIMARY_JSON, "Output": OUTPUT_JSON}[key]
        return {"output": json.dumps(payload), "provider_used": "f", "model_used": "m", "used_fallback": False}

    monkeypatch.setattr(crawler, "call_llm_for_prompt", fake_llm)
    result = crawler.run_entry_enrichment(entry_id=42, persist=False)
    assert result["status"] == "enriched" and result["persisted"] is False
    assert [sql for sql, _ in conn.executed if sql.startswith("UPDATE")] == []


# ---------------------------------------------------------------------------
# main.py importa psycopg2.errors explicitament (necessari per a ForeignKeyViolation)
# ---------------------------------------------------------------------------

def test_main_imports_psycopg2_errors_explicitly_at_the_top():
    head = open(os.path.join(APP_DIR, "main.py"), encoding="utf-8").read().splitlines()[:40]
    assert "import psycopg2.errors" in [line.strip() for line in head]
    assert hasattr(psycopg2.errors, "ForeignKeyViolation")
