"""
Tests de les ampliacions diagnostiques de scripts/trial_content_fetch.py
(--ids, --sample-report, boilerplate_ratio, text_source_candidates).
Sessio simulada: cap xarxa ni BD. No es modifica app/content_fetch.py.
"""

import csv
import importlib.util
import io
import os
import re

import pytest

from app import content_fetch as cf
from app.content_fetch import ContentFetcher
from tests.fakes import (
    FIXED_NOW, HTML_HEADERS, FakeClock, FakeResponse, FakeSession, page,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("trial_diag", os.path.join(ROOT, "scripts", "trial_content_fetch.py"))
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


def html_response(body, title="T", status=200):
    return FakeResponse(status, page(title, body), HTML_HEADERS)


def host_routes(host, pages):
    """pages: {'/1': '<p>..</p>'} -> robots 404 + una resposta per pagina."""
    routes = {f"https://{host}/robots.txt": FakeResponse(404)}
    for path, body in pages.items():
        routes[f"https://{host}{path}"] = body if isinstance(body, FakeResponse) else html_response(body)
    return routes


def write_sample(tmp_path, rows):
    path = tmp_path / "mostra.csv"
    fields = ["id", "grup", "source_domain", "source_title", "source_url", "canonical_url", "input_relevance", "content_len_db"]
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({**{f: "" for f in fields}, **row})
    return str(path)


def entry(entry_id, url):
    host = url.split("/")[2] if url else "sense-url.example"
    return {"id": str(entry_id), "source_domain": host, "canonical_url": url}


def run_trial(tmp_path, rows, routes, extra=(), min_text_chars=1, max_bytes=None, session=None):
    clock = FakeClock()
    session = session or FakeSession(routes, clock)
    options = {"sleep": clock.sleep, "clock": clock, "now": lambda: FIXED_NOW, "min_text_chars": min_text_chars}
    if max_bytes is not None:
        options["max_bytes"] = max_bytes
    sample = write_sample(tmp_path, rows)
    output = tmp_path / "out.csv"
    out, err = io.StringIO(), io.StringIO()
    code = trial.main([sample, "--output", str(output), *extra], session=session, fetcher_options=options,
                      out=out, err=err)
    result = list(csv.DictReader(open(output, newline="", encoding="utf-8"))) if output.exists() else None
    return code, result, session, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# --ids
# ---------------------------------------------------------------------------

def sample_rows():
    return [
        entry("o1", "https://openai.com/a"), entry("f1", "https://fra.europa.eu/1"),
        entry("f2", "https://fra.europa.eu/2"), entry("t1", "https://tc.com/1"), entry("t2", "https://tc.com/2"),
    ]


def test_parse_id_list():
    assert trial.parse_id_list("163, 1249,163,,") == ["163", "1249"]
    assert trial.parse_id_list("") == [] and trial.parse_id_list(None) == []


def test_select_by_ids_keeps_sample_order_reports_missing_and_ignores_skip_rules():
    selected, missing = trial.select_by_ids(sample_rows(), ["t2", "o1", "zz", "t2"])
    assert [r["id"] for r in selected] == ["o1", "t2"]      # ordre de la mostra, no el dels ids
    assert missing == ["zz", "t2"][:1] or missing == ["zz"]
    assert trial.select_by_ids(sample_rows(), ["o1"])[0][0]["source_domain"] == "openai.com"  # no s'omet


def test_ids_option_processes_only_those_entries(tmp_path):
    routes = {
        **host_routes("fra.europa.eu", {"/2": "<p>Dos</p>"}),
        **host_routes("openai.com", {"/a": FakeResponse(403, "", HTML_HEADERS)}),
    }
    code, rows, session, out, err = run_trial(tmp_path, sample_rows(), routes, ["--ids", "o1,f2"])
    assert code == 0
    assert [(r["id"], r["fetch_status"]) for r in rows] == [("o1", "access_denied"), ("f2", "ok")]
    assert "https://openai.com/a" in session.urls               # seleccio explicita: no s'omet
    assert not any("tc.com" in u or u.endswith("/1") for u in session.urls)


def test_ids_option_reports_unknown_ids_and_fails_if_none_match(tmp_path):
    routes = host_routes("tc.com", {"/1": "<p>Hola</p>"})
    code, rows, _, out, err = run_trial(tmp_path, sample_rows(), routes, ["--ids", "t1,999"])
    assert code == 0 and [r["id"] for r in rows] == ["t1"]
    assert "999" in err and "999" in out
    second = tmp_path / "segona"
    second.mkdir()
    code, rows, _, _, err = run_trial(second, sample_rows(), {}, ["--ids", "999"])
    assert code == 2 and rows is None and "Cap entrada" in err           # no es crea el CSV


def test_sample_options_must_come_together_and_ids_must_not_be_empty():
    for argv in (["m.csv", "--sample-report", "r.md"], ["m.csv", "--sample-ids", "1"],
                 ["m.csv", "--ids", ","], ["m.csv", "--sample-report", "r.md", "--sample-ids", ","]):
        with pytest.raises(SystemExit):
            trial.parse_args(argv)
    args = trial.parse_args(["m.csv", "--sample-report", "r.md", "--sample-ids", "1,2", "--ids", "1,2"])
    assert trial.parse_id_list(args.sample_ids) == ["1", "2"] and args.sample_report == "r.md"


# ---------------------------------------------------------------------------
# Frases i boilerplate_ratio
# ---------------------------------------------------------------------------

def test_split_sentences():
    assert trial.split_sentences("A b. C d\nE f.. G") == ["A b", "C d", "E f", "G"]
    assert trial.split_sentences("  .\n . ") == [] and trial.split_sentences(None) == []
    assert trial.split_sentences("a\r\nb") == ["a", "b"]
    assert trial.split_sentences("Pi es 3.14") == ["Pi es 3", "14"]      # limitacio documentada: el punt sempre talla
    assert trial.split_sentences("Hola, mon; adeu: be? si! Fi.") == ["Hola, mon; adeu: be? si! Fi"]  # nomes punt i salt de linia


def test_compute_boilerplate_ratios_threshold_is_at_least_half_of_the_other_pages():
    # 4 pagines -> 3 altres: calen >= 1.5 -> 2 altres paginies
    pages = [
        (0, "d", {"nav", "uno", "u0"}),
        (1, "d", {"nav", "uno", "u1"}),
        (2, "d", {"nav", "u2"}),
        (3, "d", {"u3"}),
    ]
    ratios = trial.compute_boilerplate_ratios(pages)
    # pagina 0: 'nav' (a 2 altres) si; 'uno' (a 1 altra, 1/3 < 50 %) no; 'u0' no -> 1/3
    assert ratios[0] == pytest.approx(1 / 3)
    assert ratios[1] == pytest.approx(1 / 3)
    assert ratios[2] == pytest.approx(1 / 2)          # 'nav' si, 'u2' no
    assert ratios[3] == 0.0


def test_exactly_half_counts_as_boilerplate_with_three_pages():
    pages = [(0, "d", {"x", "a"}), (1, "d", {"x", "b"}), (2, "d", {"c"})]   # 'x' en 1 de 2 altres = 50 %
    ratios = trial.compute_boilerplate_ratios(pages)
    assert ratios[0] == pytest.approx(1 / 2) and ratios[1] == pytest.approx(1 / 2) and ratios[2] == 0.0


def test_domains_with_fewer_than_three_ok_pages_have_no_ratio_and_domains_are_independent():
    pages = [(0, "a", {"x"}), (1, "a", {"x"}), (2, "b", {"x"}), (3, "b", {"x"}), (4, "b", {"x"})]
    ratios = trial.compute_boilerplate_ratios(pages)
    assert set(ratios) == {2, 3, 4} and ratios[2] == 1.0       # 'a' (2 pagines) sense proporcio


def test_pages_without_sentences_get_no_ratio():
    ratios = trial.compute_boilerplate_ratios([(0, "d", set()), (1, "d", {"x"}), (2, "d", {"x"}), (3, "d", {"x"})])
    assert 0 not in ratios and ratios[1] == ratios[2] == ratios[3] == 1.0


def test_boilerplate_ratio_column_end_to_end(tmp_path):
    pages = {
        "/1": "<p>Home link. Alpha story. Shared footer</p>",
        "/2": "<p>Home link. Beta story. Shared footer</p>",
        "/3": "<p>Home link. Gamma story. Other footer</p>",
    }
    routes = host_routes("d.com", pages)
    rows = [entry(i, f"https://d.com/{i}") for i in (1, 2, 3)]
    code, result, _, _, _ = run_trial(tmp_path, rows, routes)
    assert code == 0
    assert [r["boilerplate_ratio"] for r in result] == ["0.667", "0.667", "0.333"]
    assert list(result[0].keys()) == trial.CSV_FIELDS
    assert not (tmp_path / "out.csv.tmp").exists()


def test_boilerplate_ratio_is_empty_for_small_domains_non_ok_rows_and_duplicates(tmp_path):
    routes = {
        **host_routes("a.com", {"/1": "<p>X. Y</p>", "/2": "<p>X. Z</p>"}),                     # 2 'ok': sense proporcio
        **host_routes("b.com", {"/1": "<p>X. A</p>", "/2": "<p>X. B</p>", "/3": FakeResponse(403, "", HTML_HEADERS)}),
        **host_routes("c.com", {"/1": "<p>X. A</p>", "/2": "<p>X. B</p>"}),
    }
    rows = [entry(1, "https://a.com/1"), entry(2, "https://a.com/2"),
            entry(3, "https://b.com/1"), entry(4, "https://b.com/2"), entry(5, "https://b.com/3"),
            entry(6, "https://c.com/1"), entry(7, "https://c.com/2"), entry(8, "https://c.com/1/")]   # 8 = duplicate de 6
    code, result, _, _, _ = run_trial(tmp_path, rows, routes)
    by_id = {r["id"]: r for r in result}
    assert by_id["8"]["fetch_status"] == "duplicate" and by_id["5"]["fetch_status"] == "access_denied"
    assert all(r["boilerplate_ratio"] == "" for r in result)           # cap domini amb >= 3 'ok'


def test_boilerplate_ratio_only_counts_ok_pages_of_the_same_domain_in_this_run(tmp_path):
    pages = {f"/{i}": f"<p>Menu. Story {i}</p>" for i in (1, 2, 3)}
    routes = {**host_routes("d.com", pages), **host_routes("e.com", {"/1": "<p>Menu. Q</p>"})}
    rows = [entry(i, f"https://d.com/{i}") for i in (1, 2, 3)] + [entry(9, "https://e.com/1")]
    _, result, _, _, _ = run_trial(tmp_path, rows, routes)
    assert [r["boilerplate_ratio"] for r in result] == ["0.500", "0.500", "0.500", ""]


# ---------------------------------------------------------------------------
# text_source_candidates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("html,expected", [
    ("<article>abc</article><main>defgh</main>", "article:3;main:5;body:9"),
    ("<main>defgh</main>", "article:none;main:5;body:5"),
    ("<article>abc</article><article>de</article>", "article:3,2;main:none;body:6"),
    ("<p>solo cuerpo</p>", "article:none;main:none;body:11"),
    ("<nav>MENU</nav><p>hola</p><footer>PEU</footer>", "article:none;main:none;body:4"),
    ("<script>x()</script><style>p{}</style><p>hi</p>", "article:none;main:none;body:2"),
    ("", "article:none;main:none;body:0"),
])
def test_candidates_format(html, expected):
    assert trial.format_candidates(trial.analyze_candidates(page("T", html))) == expected


FIXTURES = [
    "<article>abc</article><main>defgh</main>",
    "<main>defgh</main>",
    "<article>abc</article><article>de</article>",
    "<div>fora</div><main>dins</main>",                      # body mes llarg que main
    "<div>fora</div><article>5chr</article>",                # com GSA: article minuscul
    "<nav>MENU MENU</nav><main>text</main><footer>PEU</footer>",
    "<article>aaaa</article><main>aaaa</main>",
    "<p>nomes body</p>", "", "<aside>x</aside><form>y</form>",
    "<main></main><p>cos llarg fora</p>",                    # main buit: continua sent el contenidor
    "<article>a</article><article>bbb</article><p>mes text fora</p>",
    "<article>Top 1</article><article></article><article>" + "g" * 50 + "</article><main>" + "m" * 10 + "</main>",
    "<main>one</main><main>second main</main>",
]


def module_rule(info):
    """Regla del modul sobre els candidats: <main> si n'hi ha; si no, l'<article> amb mes text; si no, <body>."""
    if info["main"] is not None:
        return "main", info["main"]
    if info["article"]:
        return "article", max(info["article"])
    return "body", info["body"]


@pytest.mark.parametrize("body", FIXTURES)
def test_candidates_replicate_the_choice_of_the_module(body):
    html = page("T", body)
    info = trial.analyze_candidates(html)
    container, length = module_rule(info)
    extract = cf.extract_page(html)
    assert extract.container == container
    assert len(extract.text) == length
    assert extract.body_len == info["body"]
    assert extract.method == (container if length else None)


def test_candidates_end_to_end_for_the_gsa_photos_pattern(tmp_path):
    widget, main_text = "g" * 2471, "m" * 103
    body = f"<article>Top 1</article><article></article><article>{widget}</article><main>{main_text}</main>"
    routes = host_routes("gsa.gov", {"/photos/gls-max": body})
    code, result, _, _, _ = run_trial(tmp_path, [entry(849, "https://gsa.gov/photos/gls-max")], routes,
                                      min_text_chars=400)
    row = result[0]
    body_len = 5 + 1 + 2471 + 1 + 103
    assert row["fetch_status"] == "text_too_short" and row["text_source"] == "html_main"
    assert row["text_length"] == "103"
    assert row["error_detail"] == f"container=main;len=103;body_len={body_len}"
    # la columna de candidats continua funcionant i coincideix amb el body_len que informa el modul
    assert row["text_source_candidates"] == f"article:5,0,2471;main:103;body:{body_len}"


def test_candidates_are_computed_for_ok_short_and_challenge_only(tmp_path):
    routes = {
        **host_routes("a.com", {
            "/ok": f"<main>{'palabra ' * 80}</main>",
            "/short": "<article>curt</article>",
            "/denied": FakeResponse(403, "", HTML_HEADERS),
            "/pdf": FakeResponse(200, b"%PDF", {"Content-Type": "application/pdf"}),
            "/chal": FakeResponse(200, page("Just a moment", "<p>espera</p>"), HTML_HEADERS),
        }),
        **host_routes("b.com", {"/x": "<p>x</p>"}),
    }
    rows = [entry(1, "https://a.com/ok"), entry(2, "https://a.com/short"), entry(3, "https://a.com/denied"),
            entry(4, "https://a.com/pdf"), entry(5, "https://a.com/chal"), entry(6, "https://a.com/ok/"),
            entry(7, "https://nowhere.example/x"), entry(8, "")]
    routes["https://nowhere.example/robots.txt"] = FakeResponse(503)
    _, result, _, _, _ = run_trial(tmp_path, rows, routes, min_text_chars=400)
    by_id = {r["id"]: r for r in result}
    assert by_id["1"]["fetch_status"] == "ok" and by_id["1"]["text_source_candidates"].startswith("article:none;main:")
    assert by_id["2"]["fetch_status"] == "text_too_short" and by_id["2"]["text_source_candidates"] == "article:4;main:none;body:4"
    assert by_id["5"]["fetch_status"] == "anti_bot_challenge" and by_id["5"]["text_source_candidates"] != ""
    for entry_id in ("3", "4", "6", "7", "8"):                      # 403, pdf, duplicate, robots, sense URL
        assert by_id[entry_id]["text_source_candidates"] == "", entry_id


def test_candidates_use_exactly_the_bytes_the_module_read(tmp_path):
    big = "<p>" + "x" * 5000 + "</p>"
    routes = host_routes("a.com", {"/1": big})
    _, result, _, _, _ = run_trial(tmp_path, [entry(1, "https://a.com/1")], routes, max_bytes=1000)
    row = result[0]
    assert row["truncated"] == "true"
    body_len = int(row["text_source_candidates"].rsplit("body:", 1)[1])
    assert body_len == int(row["text_length"]) and body_len < 1000


def test_recording_session_is_passive():
    routes = host_routes("a.com", {"/1": "<main>" + "palabra " * 80 + "</main>"})
    direct_session = FakeSession(routes, FakeClock())
    clock = FakeClock()
    direct = ContentFetcher(direct_session, sleep=clock.sleep, clock=clock, now=lambda: FIXED_NOW, min_text_chars=1)
    expected = direct.fetch("https://a.com/1")

    clock2 = FakeClock()
    inner = FakeSession(routes, clock2)
    recorded = ContentFetcher(trial.RecordingSession(inner), sleep=clock2.sleep, clock=clock2,
                              now=lambda: FIXED_NOW, min_text_chars=1).fetch("https://a.com/1")
    assert recorded == expected                                               # mateix registre complet
    assert [(c.url, c.headers, c.timeout, c.allow_redirects, c.stream) for c in inner.calls] == \
           [(c.url, c.headers, c.timeout, c.allow_redirects, c.stream) for c in direct_session.calls]


def test_trial_results_match_the_module_exactly(tmp_path):
    body = "<main>" + "Una frase llarga. " * 40 + "</main>"
    routes = host_routes("a.com", {"/1": body})
    _, result, _, _, _ = run_trial(tmp_path, [entry(1, "https://a.com/1")], routes)
    clock = FakeClock()
    direct = ContentFetcher(FakeSession(routes, clock), sleep=clock.sleep, clock=clock, now=lambda: FIXED_NOW,
                            min_text_chars=1).fetch("https://a.com/1")
    row = result[0]
    assert (row["content_hash"], row["text_length"], row["text_source"]) == (
        direct.content_hash, str(direct.text_length), direct.text_source)


# ---------------------------------------------------------------------------
# --sample-report
# ---------------------------------------------------------------------------

def long_text_page(text):
    return f"<p>{text}</p>"


def test_sample_report_has_id_domain_url_source_length_and_first_and_last_500(tmp_path):
    text = "START" + "x" * 1500 + "END"                       # 1508 caracters, sense espais: es conserva tal qual
    routes = host_routes("a.com", {"/1": long_text_page(text), "/2": "<p>altre</p>"})
    report = tmp_path / "r.md"
    code, result, _, out, _ = run_trial(
        tmp_path, [entry(7, "https://a.com/1"), entry(8, "https://a.com/2")], routes,
        ["--sample-report", str(report), "--sample-ids", "7"])
    assert code == 0
    md = report.read_text(encoding="utf-8")
    assert "## id 7 - a.com" in md
    assert "- URL: https://a.com/1" in md and "- text_source: html_body" in md and "- text_length: 1508" in md
    first, last = text[:500], text[-500:]
    assert f"```\n{first}\n```" in md and f"```\n{last}\n```" in md
    assert md.index(first) < md.index(last)
    assert first.startswith("START") and "END" not in first and last.endswith("END") and "START" not in last
    assert "id 8" not in md                                                 # no demanat
    assert "Informe de mostres" in out and "Nota:" not in md


def test_sample_report_text_is_exactly_what_the_module_returns(tmp_path):
    text = ("Primera frase. Segona frase amb accents: à é ï ò ü. " * 60).strip()
    routes = host_routes("a.com", {"/1": long_text_page(text)})
    report = tmp_path / "r.md"
    run_trial(tmp_path, [entry(1, "https://a.com/1")], routes, ["--sample-report", str(report), "--sample-ids", "1"])
    clock = FakeClock()
    direct = ContentFetcher(FakeSession(routes, clock), sleep=clock.sleep, clock=clock, now=lambda: FIXED_NOW,
                            min_text_chars=1).fetch("https://a.com/1")
    md = report.read_text(encoding="utf-8")
    assert direct.text_content[:500] in md and direct.text_content[-500:] in md


def test_sample_report_short_texts_note_the_overlap(tmp_path):
    routes = host_routes("a.com", {"/1": "<p>Text curt.</p>"})
    report = tmp_path / "r.md"
    run_trial(tmp_path, [entry(1, "https://a.com/1")], routes, ["--sample-report", str(report), "--sample-ids", "1"])
    md = report.read_text(encoding="utf-8")
    assert "Nota: el text te 10 caracters" in md
    assert md.count("Text curt.") == 2                                       # sencer als dos blocs


def test_sample_report_fence_is_longer_than_any_backtick_run_in_the_text(tmp_path):
    routes = host_routes("a.com", {"/1": "<p>codi ```python x``` fi</p>"})
    report = tmp_path / "r.md"
    run_trial(tmp_path, [entry(1, "https://a.com/1")], routes, ["--sample-report", str(report), "--sample-ids", "1"])
    md = report.read_text(encoding="utf-8")
    assert "````\ncodi ```python x``` fi\n````" in md
    assert trial._fence_for("sense") == "```" and trial._fence_for("a `b` c") == "```" and trial._fence_for("`" * 5) == "``````"


def test_sample_report_lists_what_is_not_included_with_the_reason(tmp_path):
    routes = {**host_routes("a.com", {"/ok": "<p>Hola</p>", "/denied": FakeResponse(403, "", HTML_HEADERS)})}
    rows = [entry(1, "https://a.com/ok"), entry(2, "https://a.com/denied"), entry(3, "https://a.com/other")]
    report = tmp_path / "r.md"
    run_trial(tmp_path, rows, routes, ["--ids", "1,2", "--sample-report", str(report), "--sample-ids", "2,1,3,99"])
    md = report.read_text(encoding="utf-8")
    assert "## id 1 - a.com" in md and "## id 2" not in md and "## id 3" not in md
    assert "## No inclosos" in md
    assert "- id 2: fetch_status=access_denied (no es 'ok')" in md
    assert "- id 3: no processada en aquesta execucio" in md and "- id 99: no processada" in md
    assert "- id 1:" not in md                                              # una 'ok' no surt a "No inclosos"


def test_sample_report_follows_the_order_of_sample_ids(tmp_path):
    routes = host_routes("a.com", {"/1": "<p>Uno</p>", "/2": "<p>Dos</p>"})
    report = tmp_path / "r.md"
    run_trial(tmp_path, [entry(1, "https://a.com/1"), entry(2, "https://a.com/2")], routes,
              ["--sample-report", str(report), "--sample-ids", "2,1"])
    md = report.read_text(encoding="utf-8")
    assert md.index("## id 2") < md.index("## id 1")


def test_sample_report_is_not_overwritten_without_the_flag(tmp_path):
    routes = host_routes("a.com", {"/1": "<p>Uno</p>"})
    report = tmp_path / "r.md"
    report.write_text("res", encoding="utf-8")
    code, result, _, _, err = run_trial(tmp_path, [entry(1, "https://a.com/1")], routes,
                                        ["--sample-report", str(report), "--sample-ids", "1"])
    assert code == 2 and result is None and report.read_text(encoding="utf-8") == "res"
    code, result, _, _, _ = run_trial(tmp_path, [entry(1, "https://a.com/1")], routes,
                                      ["--sample-report", str(report), "--sample-ids", "1", "--overwrite"])
    assert code == 0 and "## id 1" in report.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Robustesa i aillament
# ---------------------------------------------------------------------------

def test_interrupt_still_writes_the_final_csv_with_ratios(tmp_path):
    routes = {**host_routes("d.com", {"/1": "<p>Menu. A</p>", "/2": "<p>Menu. B</p>", "/3": "<p>Menu. C</p>"}),
              "https://stop.example/robots.txt": KeyboardInterrupt()}
    rows = [entry(i, f"https://d.com/{i}") for i in (1, 2, 3)] + [entry(4, "https://stop.example/x")]
    code, result, _, out, err = run_trial(tmp_path, rows, routes)
    assert code == 130 and "Interromput" in err
    assert [r["id"] for r in result] == ["1", "2", "3"]
    assert [r["boilerplate_ratio"] for r in result] == ["0.500", "0.500", "0.500"]
    assert not (tmp_path / "out.csv.tmp").exists()


def test_the_trial_does_not_change_the_module_or_add_network_behaviour():
    source = open(os.path.join(ROOT, "scripts", "trial_content_fetch.py"), encoding="utf-8").read()
    assert "headers" not in source and "retries" not in source.lower()
    assert not re.search(r"requests\.(get|post|head)\(", source)            # nomes l'embolcall de la sessio
    assert not re.search(r"^\s*(?:import|from)\s+(?:psycopg2|dotenv|app\.db)", source, re.M)
    module_source = open(cf.__file__, encoding="utf-8").read()
    assert "trial_content_fetch" not in module_source and "RecordingSession" not in module_source
