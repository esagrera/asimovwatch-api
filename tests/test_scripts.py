"""
Tests dels scripts de scripts/ amb dobles: cap xarxa ni BD. Els scripts no s'executen
com a programes; s'importen i es criden les seves funcions amb sessions i connexions falses.
"""

import csv
import importlib.util
import io
import os
import re
from datetime import datetime, timezone

import pytest

from app import content_fetch as cf
from app import content_fetch_store as store
from app.content_fetch import ContentFetcher, FETCH_STATUSES, validate_record
from tests.fakes import FIXED_NOW, FakeResponse, FakeSession, FakeClock, ok_page

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_script(name):
    path = os.path.join(ROOT, "scripts", f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"scripts_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


trial = load_script("trial_content_fetch")
smoke = load_script("smoke_insert_fetches")


def imports_of(source):
    """Linies d'import del script (les mencions a docstrings no compten)."""
    return re.findall(r"^\s*(?:import|from)\s+(\S+)", source, re.M)


def script_source(name):
    return open(os.path.join(ROOT, "scripts", f"{name}.py"), encoding="utf-8").read()


# ---------------------------------------------------------------------------
# trial_content_fetch.py
# ---------------------------------------------------------------------------

def sample_rows(openai=3, fra=7, other=8):
    rows = []
    for i in range(openai):
        rows.append({"id": f"o{i}", "source_domain": "openai.com", "canonical_url": f"https://openai.com/{i}"})
    for i in range(fra):
        rows.append({"id": f"f{i}", "source_domain": "fra.europa.eu", "canonical_url": f"https://fra.europa.eu/{i}"})
    for i in range(other):
        rows.append({"id": f"t{i}", "source_domain": "techcrunch.com", "canonical_url": f"https://techcrunch.com/{i}"})
    return rows


def test_trial_defaults_skip_openai_and_limit_fra_to_five():
    selected, skipped, over = trial.select_rows(sample_rows(), trial.parse_skip_domains(trial.DEFAULT_SKIP_DOMAINS), None)
    domains = [r["source_domain"] for r in selected]
    assert domains.count("openai.com") == 0 and skipped == {"openai.com": 3}
    assert domains.count("fra.europa.eu") == 5 and over == {"fra.europa.eu": 2}
    assert domains.count("techcrunch.com") == 8           # sense limit
    assert [r["id"] for r in selected if r["source_domain"] == "fra.europa.eu"] == ["f0", "f1", "f2", "f3", "f4"]


def test_trial_explicit_limit_replaces_the_fra_default_and_applies_to_all():
    selected, _, over = trial.select_rows(sample_rows(), ["openai.com"], 2)
    counts = {d: sum(1 for r in selected if r["source_domain"] == d) for d in ("fra.europa.eu", "techcrunch.com")}
    assert counts == {"fra.europa.eu": 2, "techcrunch.com": 2}
    selected, _, _ = trial.select_rows(sample_rows(), ["openai.com"], 7)
    assert sum(1 for r in selected if r["source_domain"] == "fra.europa.eu") == 7


def test_trial_skip_domains_option():
    assert trial.parse_skip_domains("") == []
    assert trial.parse_skip_domains(" OpenAI.com , a.cat ") == ["openai.com", "a.cat"]
    selected, skipped, _ = trial.select_rows(sample_rows(), [], None)
    assert sum(1 for r in selected if r["source_domain"] == "openai.com") == 3 and not skipped
    assert trial.is_skipped("www.openai.com", ["openai.com"]) and not trial.is_skipped("notopenai.com", ["openai.com"])
    args = trial.parse_args(["x.csv"])
    assert args.skip_domains == "openai.com" and args.limit_per_domain is None


def test_trial_row_has_empty_cells_for_missing_values_and_the_documented_columns():
    assert trial.CSV_FIELDS == ["id", "source_domain", "requested_url", "normalized_url", "fetch_status",
                                "http_status", "final_url", "text_source", "text_length", "content_hash",
                                "truncated", "error_detail", "durada_s", "boilerplate_ratio",
                                "text_source_candidates"]
    record = cf.FetchRecord(requested_url="https://a.cat/x", normalized_url="https://a.cat/x",
                            fetch_status="timeout", user_agent="UA", fetched_at=FIXED_NOW,
                            error_detail="Timeout (20.0s)")
    row = trial.record_to_row("7", "a.cat", record, 1.234)
    assert list(row) == trial.CSV_FIELDS
    assert row["http_status"] == row["final_url"] == row["text_source"] == row["text_length"] == row["content_hash"] == ""
    assert row["truncated"] == "false" and row["durada_s"] == "1.23" and row["error_detail"] == "Timeout (20.0s)"
    assert row["boilerplate_ratio"] == "" and row["text_source_candidates"] == ""


def make_sample_file(tmp_path, rows):
    path = tmp_path / "mostra.csv"
    fields = ["id", "grup", "source_domain", "source_title", "source_url", "canonical_url", "input_relevance", "content_len_db"]
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:       # amb BOM, com la mostra real
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({**{f: "" for f in fields}, **row})
    return str(path)


def make_trial_fetcher(routes):
    """(opcions per al fetcher del trial, sessio simulada)."""
    clock = FakeClock()
    session = FakeSession(routes, clock)
    return {"sleep": clock.sleep, "clock": clock, "now": lambda: FIXED_NOW}, session


def test_trial_main_end_to_end_with_a_fake_session(tmp_path):
    art = "https://example.com/a"
    routes = {
        "https://example.com/robots.txt": FakeResponse(404),
        art: ok_page(),
        "https://example.com/denied": FakeResponse(403),
    }
    sample = make_sample_file(tmp_path, [
        {"id": "1", "source_domain": "example.com", "canonical_url": art},
        {"id": "2", "source_domain": "example.com", "canonical_url": art + "/"},   # mateixa URL normalitzada
        {"id": "3", "source_domain": "openai.com", "canonical_url": "https://openai.com/x"},
        {"id": "4", "source_domain": "example.com", "canonical_url": ""},
        {"id": "5", "source_domain": "example.com", "canonical_url": "https://example.com/denied"},
    ])
    output = tmp_path / "out.csv"
    options, session = make_trial_fetcher(routes)
    out, err = io.StringIO(), io.StringIO()

    assert trial.main([sample, "--output", str(output)], session=session, fetcher_options=options,
                      out=out, err=err) == 0

    rows = list(csv.DictReader(open(output, newline="", encoding="utf-8")))
    assert list(rows[0].keys()) == trial.CSV_FIELDS
    assert [(r["id"], r["fetch_status"]) for r in rows] == [
        ("1", "ok"), ("2", "duplicate"), ("4", "error"), ("5", "access_denied")]
    ok, dup, err_row, denied = rows
    assert re.fullmatch(r"[0-9a-f]{64}", ok["content_hash"]) and ok["text_source"] == "html_article"
    assert ok["truncated"] == "false" and ok["http_status"] == "200"
    assert dup["text_length"] == dup["content_hash"] == dup["http_status"] == dup["final_url"] == ""
    assert err_row["error_detail"] == "no_canonical_url" and err_row["requested_url"] == "" == err_row["normalized_url"]
    assert denied["http_status"] == "403" and denied["error_detail"] == "HTTP 403"
    assert all(r["durada_s"] for r in rows)
    assert not any("openai.com" in u for u in session.urls)          # omes: ni es demana
    assert session.urls.count(art) == 1                              # el duplicat no es torna a baixar

    summary = out.getvalue()
    assert "Per fetch_status" in summary and "Per domini" in summary
    assert re.search(r"ok\s+1", summary) and re.search(r"duplicate\s+1", summary)
    assert "openai.com=1" in summary and "example.com" in summary


def test_trial_never_overwrites_without_the_flag(tmp_path):
    sample = make_sample_file(tmp_path, [{"id": "1", "source_domain": "example.com", "canonical_url": ""}])
    output = tmp_path / "out.csv"
    output.write_text("res", encoding="utf-8")
    options, session = make_trial_fetcher({})
    assert trial.main([sample, "--output", str(output)], session=session, fetcher_options=options,
                      out=io.StringIO(), err=io.StringIO()) == 2
    assert output.read_text(encoding="utf-8") == "res"
    assert trial.main([sample, "--output", str(output), "--overwrite"], session=session, fetcher_options=options,
                      out=io.StringIO(), err=io.StringIO()) == 0
    assert output.read_text(encoding="utf-8") != "res"


def test_trial_requires_the_input_columns(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("id,titol\n1,x\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        trial.read_sample(str(path))


def test_trial_script_does_not_touch_the_database_or_add_headers():
    source = script_source("trial_content_fetch")
    assert not {"psycopg2", "dotenv", "app.db"} & set(imports_of(source))
    assert "headers" not in source and "retries" not in source.lower()


# ---------------------------------------------------------------------------
# smoke_insert_fetches.py
# ---------------------------------------------------------------------------

class SpyEnviron(dict):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.reads = []

    def get(self, key, default=None):
        self.reads.append(key)
        return super().get(key, default)


def test_smoke_guard_flag_is_required_and_nothing_is_read_or_connected():
    calls, env = [], SpyEnviron({"SMOKE_DATABASE_DSN": "host=secret"})
    out = io.StringIO()
    code = smoke.main([], environ=env, connect=lambda dsn: calls.append(dsn), out=out)
    assert code == 2 and calls == [] and env.reads == []
    assert "--i-know-this-is-a-copy" in out.getvalue()


def test_smoke_missing_dsn_does_not_connect():
    calls = []
    code = smoke.main(["--i-know-this-is-a-copy"], environ={}, connect=lambda dsn: calls.append(dsn), out=io.StringIO())
    assert code == 2 and calls == []


def test_smoke_connect_error_does_not_leak_the_dsn():
    def boom(dsn):
        raise RuntimeError(f"no puc connectar a {dsn}")
    out = io.StringIO()
    code = smoke.main(["--i-know-this-is-a-copy"], environ={"SMOKE_DATABASE_DSN": "host=h password=SECRET"},
                      connect=boom, out=out)
    assert code == 1 and "SECRET" not in out.getvalue()


def test_smoke_uses_the_configured_env_variable():
    seen = []
    smoke.main(["--i-know-this-is-a-copy", "--dsn-env", "ALTRA"], environ={"ALTRA": "dsn-x"},
               connect=lambda dsn: seen.append(dsn) or (_ for _ in ()).throw(RuntimeError()), out=io.StringIO())
    assert seen == ["dsn-x"]


def test_smoke_records_cover_the_eleven_statuses_and_satisfy_the_contract():
    assert len(smoke.STATUS_ORDER) == 11 and set(smoke.STATUS_ORDER) == FETCH_STATUSES
    assert smoke.STATUS_ORDER[0] == "ok" and smoke.STATUS_ORDER[-1] == "duplicate"
    records = smoke.build_records(FIXED_NOW, "abc123")
    assert set(records) == FETCH_STATUSES - {"duplicate"}
    for status, record in records.items():
        validate_record(record)
        assert record.fetch_status == status
    ok = records["ok"]
    assert re.fullmatch(r"[0-9a-f]{64}", ok.content_hash) and ok.text_length == len(ok.text_content)
    urls = [r.normalized_url for r in records.values()]
    assert len(set(urls)) == len(urls)                                  # cap URL repetida
    assert all(u.startswith("https://smoke-test.invalid/") for u in urls)


class SmokeFakeDB:
    """Emula nomes les sentencies que fa el smoke test. NO es PostgreSQL."""

    COLS = ["id", "entry_id", "requested_url", "normalized_url", "final_url", "fetch_status", "http_status",
            "content_type", "fetched_at", "text_content", "text_length", "truncated", "content_hash",
            "text_source", "error_detail", "user_agent", "reused_from_fetch_id"]

    def __init__(self, preexisting=3, corrupt=None, rollback_clears=True, entries=True):
        self.preexisting, self.tx, self.next_id = preexisting, {}, 100
        self.corrupt, self.rollback_clears, self.entries = corrupt or {}, rollback_clears, entries
        self.executed, self.rollbacks, self.closed, self.autocommit = [], 0, False, True

    def cursor(self):
        return _SmokeCursor(self)

    def commit(self):
        raise AssertionError("El smoke test no pot fer COMMIT")

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_clears:
            self.tx.clear()

    def close(self):
        self.closed = True

    def get_dsn_parameters(self):
        return {"host": "copia.local", "port": "5432", "dbname": "asimov_copia", "user": "u", "password": "NO"}


class _SmokeCursor:
    def __init__(self, db):
        self.db, self.result, self.description = db, None, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        db = self.db
        db.executed.append((sql, params))
        flat = " ".join(sql.split())
        if flat.startswith("SET LOCAL"):
            self.result = None
        elif "to_regclass" in flat:
            self.result = (True,)
        elif "FROM public.entries" in flat:
            self.result = (7,) if db.entries else None
        elif flat.startswith("SELECT count(*)"):
            self.result = (db.preexisting + len(db.tx),)
        elif flat.startswith("INSERT INTO public.entry_content_fetches"):
            db.next_id += 1
            row = {"id": db.next_id, "entry_id": params[0]}
            row.update(dict(zip(store.INSERT_FETCH_COLUMNS, params[1:])))
            db.tx[db.next_id] = row
            self.result = (db.next_id,)
        elif "md5(normalized_url)" in flat:
            hits = [r["id"] for r in db.tx.values()
                    if r["normalized_url"] == params[0] and r["fetch_status"] == "ok"]
            self.result = (max(hits),) if hits else None
        elif "ANY(%s)" in flat:
            self.description = [(c,) for c in db.COLS]
            rows = []
            for rid in sorted(params[0]):
                row = dict(db.tx[rid])
                row.update(db.corrupt.get(row["fetch_status"], {}))
                rows.append(tuple(row[c] for c in db.COLS))
            self.result = rows
        else:
            raise AssertionError(f"SQL no previst: {flat[:80]}")

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result


def run_smoke(db, extra=()):
    out = io.StringIO()
    code = smoke.main(["--i-know-this-is-a-copy", *extra], environ={"SMOKE_DATABASE_DSN": "dsn"},
                      connect=lambda dsn: db, out=out)
    return code, out.getvalue()


def test_smoke_happy_path_with_a_fake_database():
    db = SmokeFakeDB()
    code, text = run_smoke(db)
    assert code == 0, text
    assert text.count("[OK   ]") == 11 and "[FALLA]" not in text
    for status in FETCH_STATUSES:
        assert status in text
    assert "RESULTAT: CORRECTE" in text
    assert "host=copia.local" in text and "password" not in text.lower()
    assert db.rollbacks >= 1 and db.closed and db.tx == {}          # revertit i tancat
    assert "despres del ROLLBACK: 3" in text                          # recompte inicial recuperat


def test_smoke_never_writes_anywhere_but_entry_content_fetches():
    db = SmokeFakeDB()
    run_smoke(db)
    for sql, _params in db.executed:
        flat = " ".join(sql.split())
        assert not re.search(r"\b(UPDATE|DELETE|TRUNCATE|DROP|ALTER)\b", flat)
        if re.search(r"INSERT INTO", flat):
            assert flat.startswith("INSERT INTO public.entry_content_fetches")
        if "public.entries" in flat:
            assert flat.startswith("SELECT id FROM public.entries")


def test_smoke_reports_a_value_mismatch_as_failure():
    db = SmokeFakeDB(corrupt={"access_denied": {"error_detail": ""}, "timeout": {"http_status": 0}})
    code, text = run_smoke(db)
    assert code == 1
    assert "[FALLA] access_denied" in text and "cadena buida" in text
    assert "[FALLA] timeout" in text
    assert db.rollbacks >= 1 and db.tx == {}


def test_smoke_detects_a_rollback_that_did_not_revert():
    db = SmokeFakeDB(rollback_clears=False)
    code, text = run_smoke(db)
    assert code == 1 and "Despres del ROLLBACK" in text


def test_smoke_fails_cleanly_without_an_entry():
    db = SmokeFakeDB(entries=False)
    code, text = run_smoke(db)
    assert code == 1 and "cap entrada existent" in text and db.tx == {} and db.rollbacks >= 1


def test_smoke_always_rolls_back_even_if_a_statement_fails():
    class Exploding(SmokeFakeDB):
        def cursor(self):
            cursor = super().cursor()
            original = cursor.execute

            def execute(sql, params=None):
                if sql.lstrip().startswith("INSERT"):
                    raise RuntimeError("violacio de CHECK simulada")
                return original(sql, params)

            cursor.execute = execute
            return cursor

    db = Exploding()
    code, text = run_smoke(db)
    assert code == 1 and "violacio de CHECK simulada" in text
    assert db.rollbacks >= 1 and db.closed


@pytest.mark.parametrize("expected_kwargs,actual_overrides,fragment", [
    ({}, {}, None),
    ({}, {"final_url": ""}, "cadena buida"),
    ({}, {"text_length": 0}, "text_length"),
    ({}, {"content_hash": "a" * 16}, "content_hash"),
    ({}, {"http_status": "200"}, "http_status"),
    ({}, {"truncated": 0}, "truncated"),
    ({}, {"fetched_at": datetime(2026, 10, 26)}, "zona horaria"),
    ({}, {"final_url": None}, "final_url"),
    ({}, {"entry_id": 99}, "entry_id"),
])
def test_smoke_compare(expected_kwargs, actual_overrides, fragment):
    record = smoke.build_records(FIXED_NOW, "x")["ok"]
    actual = {"id": 5, "entry_id": 7}
    actual.update({name: getattr(record, name) for name in smoke.COMPARED_FIELDS})
    actual.update(actual_overrides)
    problems = smoke.compare(record, actual, 7)
    if fragment is None:
        assert problems == []
    else:
        assert any(fragment in p for p in problems), problems


def test_smoke_script_source_guarantees():
    source = script_source("smoke_insert_fetches")
    assert not re.search(r"\.commit\(", source)
    assert not {"psycopg2.extras", "dotenv", "app.db"} & set(imports_of(source))
    assert "load_dotenv" not in source
    assert "rollback()" in source and "--i-know-this-is-a-copy" in source
    assert not re.search(r"(INSERT INTO|UPDATE|DELETE FROM)\s+public\.entries\b", source)
